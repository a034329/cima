#!/usr/bin/env python3
"""
generar_irpf.py
===============
Genera el CSV importable en "Mi cartera de valores" (RentaWEB, AEAT)
a partir del historial de transacciones de los brokers de Angel.

FICHEROS DE ENTRADA (colocar en /app/720/irpf/):
  DeGiro_Transacciones_YYYY.csv   — DeGiro > Portfolio > Actividad de cuenta > Exportar
  IBKR_Trades_YYYY.csv            — IBKR > Reports > Activity Statement > Trades+Corporate
                                    (base currency EUR, período 01/01-31/12)
  TR_Transacciones_YYYY.csv       — Trade Republic > Cuenta > Exportar historial

FICHEROS DE SALIDA:
  cartera_valores_irpf_YYYY.csv   — Importar en RentaWEB > Mi cartera de valores
  informe_corporativas_YYYY.txt   — Informe de acciones corporativas detectadas

FORMATO DE SALIDA — Operaciones A/T (AEAT RentaWEB):
  Tipo;ISIN;Denominacion;Fecha;CantidadTitulos;ImporteTotalEUR;GastosEUR
  A = Adquisicion (compra)
  T = Transmision (venta)

FORMATO DE SALIDA — Splits/Contrasplits SP (AEAT RentaWEB):
  SP;ISIN;Denominacion;Fecha;TitulosAntiguos;TitulosNuevos;NominalAntiguo
  Fuente AEAT: https://sede.agenciatributaria.gob.es/.../4_10-sp-split-contra-split.html
  Ecuacion requerida: Titulos_ant × Nominal_ant = Titulos_nue × Nominal_nue
  ⚠ Verificar si el importador de Mi cartera de valores acepta SP via CSV,
    o si hay que introducirlos manualmente en la interfaz web de RentaWEB.

REGLAS FISCALES:
  - Splits/contrasplits: sin ganancia ni pérdida patrimonial (Art. 37.3 LIRPF)
  - Las acciones recibidas conservan la fecha de adquisición original
  - El coste total de adquisición se redistribuye entre el nuevo número de títulos
  - El script NO calcula FIFO: lo calcula RentaWEB al importar

NOTAS:
  Verificar formato actual antes de importar:
  https://sede.agenciatributaria.gob.es (ayuda IRPF YYYY)
"""

import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from collections import defaultdict

from instrument_classifier import classify_isin

try:
    from compensacion_perdidas import (
        calcular_compensacion,
        cargar_perdidas,
        guardar_perdidas,
        imprimir_resumen as imprimir_resumen_compensacion,
    )
    _COMPENSACION_DISPONIBLE = True
except ImportError:
    _COMPENSACION_DISPONIBLE = False

sys.stdout.reconfigure(encoding='utf-8')

# ── Configuracion ──────────────────────────────────────────────────────────
EJERCICIO    = "2025"
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))

DEGIRO_FILE         = os.path.join(BASE_DIR, f"DeGiro_Transacciones_{EJERCICIO}.csv")
DEGIRO_CUENTA_FILE  = os.path.join(BASE_DIR, f"DeGiro_Cuenta_{EJERCICIO}.csv")
IBKR_FILE           = os.path.join(BASE_DIR, f"IBKR_Trades_{EJERCICIO}.csv")
TR_FILE             = os.path.join(BASE_DIR, f"TR_Transacciones_{EJERCICIO}.csv")
OUTPUT_FILE         = os.path.join(BASE_DIR, f"cartera_valores_irpf_{EJERCICIO}.csv")
INFORME_FILE        = os.path.join(BASE_DIR, f"informe_corporativas_{EJERCICIO}.txt")
INFORME_DIV_FILE    = os.path.join(BASE_DIR, f"informe_dividendos_{EJERCICIO}.txt")
INFORME_OPT_FILE    = os.path.join(BASE_DIR, f"informe_opciones_{EJERCICIO}.txt")
INFORME_FX_FILE     = os.path.join(BASE_DIR, f"informe_fx_{EJERCICIO}.txt")
DERECHOS_FILE       = os.path.join(BASE_DIR, "derechos_clasificados.json")
# Fichero maestro de casillas RentaWEB por ejercicio. AEAT renumera año a año,
# por lo que NO se hardcodea en el código: se consulta el JSON.
CASILLAS_FILE       = os.path.join(os.path.dirname(BASE_DIR), "casillas_irpf.json")

# Años anteriores a buscar cuando se detecta una opción expirada/ejercida
# sin venta correspondiente en el año actual (prima cobrada en año previo).
# Bajo DGT V2172-21 la alteración patrimonial ocurre en el año de extinción.
MAX_ANIOS_BUSQUEDA = 5

# Prefijos ISIN que corresponden a opciones listadas en DeGiro/MEFF
OPTION_ISIN_PREFIXES = ('NLEX', 'ES0A0')

# Ventana temporal para agrupar filas de la misma acción corporativa (días)
WINDOW_CORPORATE_DAYS = 5

# Tipos de acciones corporativas
CA_SPLIT       = 'SPLIT'        # más títulos al mismo coste total
CA_CONTRASPLIT = 'CONTRASPLIT'  # menos títulos al mismo coste total
CA_ISIN_CHANGE = 'ISIN_CHANGE'  # mismo ratio 1:1, distinto ISIN (canje admin.)
CA_RIGHTS      = 'RIGHTS'       # derechos de suscripción (RTS)
CA_SCRIP       = 'SCRIP_DIV'    # acción liberada (dividendo en acciones)
CA_RESIDUAL    = 'RESIDUAL_BUY' # derecho residual recomprado por emisor (precio comprometido)
CA_COMPLEX     = 'COMPLEX'      # evento con precio ≠ 0 o patrón ambiguo
CA_RIGHTS_EXERCISED = 'RIGHTS_EXERCISED'  # rights issue clásico TYPE A
                                # ejercido: derechos asignados gratis +
                                # ejercicio (pago al broker) + entrega de
                                # acciones nuevas. El motor genera la fila A
                                # con coste real, NO requiere acción manual.
CA_NAME_CHANGE = 'NAME_CHANGE'  # mismo ISIN, nombres distintos (rename empresa, no fiscal)
CA_SPIN_OFF    = 'SPIN_OFF'     # escisión: acciones nuevas de una empresa escindida
                                # de la matriz, mientras la matriz sigue cotizando
                                # (Art. 37.1.a LIRPF + DGT V1766-12)
CA_MARKET_TRANSFER = 'MARKET_TRANSFER'  # mismo día, mismo ISIN, par compra+venta con
                                # Order ID propio en bolsas distintas (XGAT/XPAR,
                                # XETA/XAMS, etc.). Solo cambia el centro de custodia
                                # — Art. 33 LIRPF: NO hay alteración patrimonial.
                                # Las dos patas se excluyen del FIFO; la comisión
                                # combinada va al informe de corporativas.
CA_CORTO_FORZADO  = 'CORTO_FORZADO'  # par venta+compra mismo día con patrón
                                # similar al cambio mercado, pero con una venta
                                # del mismo ISIN cercana (±7 días) indicando
                                # que el usuario no tenía inventario disponible.
                                # Típico: call ejercida sobre activo no cubierto
                                # en el mercado correcto. Las dos patas se procesan
                                # como apertura + cobertura de corto en FIFO
                                # (Art. 33 LIRPF + Art. 35.1.b LIRPF: G/P real
                                # al cierre, gastos inherentes deducibles).

# ── Convenios de Doble Imposición (CDI) España — retención máxima en fuente ──
# Fuente: CDIs publicados por la AEAT. Tasa aplicable a inversores minoritarios.
# https://www.agenciatributaria.es/AEAT.internet/Inicio/La_Agencia_Tributaria/
#   Normativa__doctrina_y_otras_publicaciones/Convenios_de_doble_imposicion_firmados_por_Espana/
DTA_SOURCE_MAX = {
    'US': Decimal('0.15'),  # España-EEUU: 15%
    'NL': Decimal('0.15'),  # España-Países Bajos: 15%
    'DE': Decimal('0.15'),  # España-Alemania: 15%
    'FR': Decimal('0.15'),  # España-Francia: 15%
    'GB': Decimal('0.10'),  # España-RU: 10%
    'IE': Decimal('0.15'),  # España-Irlanda: 15%
    'DK': Decimal('0.15'),  # España-Dinamarca: 15%
    'LU': Decimal('0.15'),  # España-Luxemburgo: 15%
    'IT': Decimal('0.15'),  # España-Italia: 15%
    'PL': Decimal('0.15'),  # España-Polonia: 15%
    'CH': Decimal('0.15'),  # España-Suiza: 15%
    'CA': Decimal('0.15'),  # España-Canadá: 15%
    'BE': Decimal('0.15'),  # España-Bélgica: 15%
    'SE': Decimal('0.15'),  # España-Suecia: 15%
    'NO': Decimal('0.15'),  # España-Noruega: 15%
    'AT': Decimal('0.15'),  # España-Austria: 15%
    'FI': Decimal('0.10'),  # España-Finlandia: 10%
    'JP': Decimal('0.05'),  # España-Japón: 5% — NUEVO convenio 2018 (BOE-A-2021-2977,
                            # en vigor 1-5-2021), Art. 10.2: tipo GENERAL 5% del bruto.
                            # 0% para sociedades ≥10% derechos de voto 12 meses y fondos
                            # de pensiones (10.3); 10% solo dividendos deducibles (10.4).
                            # El 15% anterior era del convenio de 1974, derogado.
                            # Verificado 2026-06-11 contra BOE + Anexo III manual
                            # AEAT no residentes ("0/5/10"). Japón retiene 15,315%
                            # doméstico → exceso sobre 5% se reclama a Japón, no via 0588.
    'AU': Decimal('0.15'),  # España-Australia: 15%
    'HK': Decimal('0.10'),  # España-Hong Kong: 10%
    'SG': Decimal('0.05'),  # España-Singapur: 5%
    'KR': Decimal('0.15'),  # España-Corea: 15%
    'CN': Decimal('0.10'),  # España-China: 10%
    'IN': Decimal('0.15'),  # España-India: 15%
    'MX': Decimal('0.10'),  # España-México: 10%
    'BR': Decimal('0.15'),  # España-Brasil: 15% — CDI 1974 (BOE 31-12-1975), Art. 10.2:
                            # "no puede exceder del 15 por 100 del importe bruto".
                            # Verificado 2026-06-11 contra el texto oficial (hacienda.gob.es).
                            # Nota: Brasil no retiene dividendos domésticamente (0%),
                            # impacto práctico nulo salvo cambio normativo brasileño.
    'AR': Decimal('0.10'),  # España-Argentina: 10%
    'TW': Decimal('0.10'),  # España-Taiwán: 10% (aprox., no CDI formal — usar con cautela)
    'KY': Decimal('0.00'),  # Caimán: sin CDI → sin crédito garantizado
    'LV': Decimal('0.10'),  # España-Letonia: 10%
    'EE': Decimal('0.10'),  # España-Estonia: 10%
    'AE': Decimal('0.00'),  # EAU: sin CDI
}

# Tasa de retención en ORIGEN que Trade Republic aplica REALMENTE sobre el
# bruto, por país emisor. NO es el tope CDI (DTA_SOURCE_MAX) — es lo que el
# país de origen retiene de hecho, que puede ser MAYOR que el tope (DE 26,375%,
# CH 35% → el exceso no es deducible) o MENOR (FR 12,8% < tope 15%).
#
# Se usa para descomponer el campo `tax` de TR (origen + 19% ES sobre el neto)
# en sus dos componentes SIN inferirlo del tope CDI (que sobre-asignaba a 0588
# cuando la tasa real era menor, e infra-reportaba 0591).
#   origen = bruto × tasa_real ;  ES = tax_total − origen
# Esto funciona en ambos regímenes: pre-migración (tax = bruto×tasa_real → ES=0)
# y post-migración (tax = origen + 19%×neto → ES = 19%×neto), sin necesidad de
# conocer la fecha de migración.
#
# Tasas verificadas:
#   US 15% (W-8BEN, tipo de convenio que TR pre-aplica) y FR 12,8% (PFU
#   doméstico) confirmadas al céntimo contra extracto real de TR (J&J y Hermès).
#   DE 26,375% (25% + 5,5% Soli), NL 15%, CH 35%, IT 26%, BE 30%, DK 27%:
#   tipos estatutarios de retención a no residentes (PwC/Tax Foundation 2025).
# Países NO listados → fallback al método antiguo (min(tax, bruto×tope_CDI)).
TR_SOURCE_WHT_RATE = {
    'US': Decimal('0.15'),     # W-8BEN tratado (TR lo pre-aplica)
    'FR': Decimal('0.128'),    # PFU doméstico no residentes
    'DE': Decimal('0.26375'),  # KESt 25% + Soli 5,5%
    'NL': Decimal('0.15'),
    'CH': Decimal('0.35'),
    'IT': Decimal('0.26'),
    'BE': Decimal('0.30'),
    'DK': Decimal('0.27'),
    'GB': Decimal('0.00'),     # UK no retiene sobre dividendos
    'IE': Decimal('0.00'),     # acciones irlandesas: la mayoría UCITS (rama FUND)
}

# ISINs de ADRs cuyo país real de dividendo difiere del prefijo ISIN (US)
# Formato: ISIN → código de país real (para aplicar CDI correcto)
ADR_PAIS_REAL = {
    'US8740391003': 'TW',  # ADR de TSMC (Taiwan Semiconductor)
}

# Tipos marginales IRPF base del ahorro 2023+ (Art. 66 LIRPF). Anchuras de
# tramo (no límites acumulados): 0-6k @19%, 6k-50k @21%, 50k-200k @23%,
# 200k-300k @27%, >300k @28%. El 4º ancho es 100.000 (200k→300k), no 300.000.
IRPF_TRAMOS_AHORRO = [
    (Decimal('6000'),   Decimal('0.19')),
    (Decimal('44000'),  Decimal('0.21')),
    (Decimal('150000'), Decimal('0.23')),
    (Decimal('100000'), Decimal('0.27')),
    (None,              Decimal('0.28')),
]


# ── Casillas RentaWEB por ejercicio (cargadas de casillas_irpf.json) ──────

_CASILLAS_CACHE: dict = {}

def _load_casillas_ejercicio(ejercicio: str) -> dict:
    """Devuelve el mapping de casillas para el ejercicio indicado.

    La AEAT renumera casillas cada año. Si no existe entrada para ese
    ejercicio, devuelve la del ejercicio más reciente disponible con un
    aviso por stdout. Nunca devuelve dict vacío — si el fichero falta,
    usa un fallback mínimo con las casillas conocidas para 2025.
    """
    if ejercicio in _CASILLAS_CACHE:
        return _CASILLAS_CACHE[ejercicio]

    datos = None
    if os.path.exists(CASILLAS_FILE):
        try:
            with open(CASILLAS_FILE, encoding='utf-8') as _f:
                datos = json.load(_f)
        except Exception as _exc:
            print(f"  ⚠️  No se pudo leer {CASILLAS_FILE}: {_exc}")

    if not datos or ejercicio not in datos:
        ejercicios_disponibles = sorted(
            k for k in (datos or {}).keys() if k.isdigit()
        )
        if ejercicios_disponibles:
            fallback = ejercicios_disponibles[-1]
            print(f"  ⚠️  Casillas del ejercicio {ejercicio} no definidas — "
                  f"usando las de {fallback}. Verificar y actualizar "
                  f"casillas_irpf.json antes de presentar.")
            entrada = datos[fallback]
        else:
            # Último fallback: 2025 hardcoded (por si falta el JSON)
            print(f"  ⚠️  {CASILLAS_FILE} no disponible — usando fallback 2025 embebido.")
            entrada = {
                "campana": "2026",
                "casillas": {
                    "dividendos_rcm": {"casilla": "0029"},
                    "retencion_espanola": {"casilla": "0591"},
                    "deduccion_cdi_internacional": {"casilla": "0588"},
                    "intereses_rcm": {"casilla": "0027"},
                    "letras_tesoro_rcm": {"casilla": "0030"},
                    "otros_activos_rcm": {"casilla": "0031"},
                    "acciones_cotizadas_gp": {
                        "rango_detalle": "326-338",
                        "casilla_suma_ganancias": "339",
                        "casilla_suma_perdidas": "340",
                    },
                    "derechos_suscripcion_gp": {"rango_detalle": "341-346"},
                    "otros_elementos_patrimoniales_gp": {
                        "rango_detalle": "1624-1654",
                        "casilla_clave_tipo": "1626",
                    },
                    "saldos_negativos_arrastre_gp": {"rango_detalle": "1186+"},
                },
            }
    else:
        entrada = datos[ejercicio]

    cas = entrada.get("casillas", {})

    # Acceso simplificado: .divs, .cdi, .acciones, .derechos, .otros, etc.
    simple = {
        "ejercicio":         ejercicio,
        "campana":           entrada.get("campana", ""),
        "divs":              cas.get("dividendos_rcm", {}).get("casilla", "0029"),
        "retencion_es":      cas.get("retencion_espanola", {}).get("casilla", "0591"),
        "cdi":               cas.get("deduccion_cdi_internacional", {}).get("casilla", "0588"),
        # V1 auditoría 2026-06-11: el código buscaba la clave legacy
        # "intereses_cuentas_remuneradas" (inexistente en casillas_irpf.json,
        # que usa "intereses_rcm") y caía SIEMPRE al default inline "0023".
        # La casilla correcta es 0027 (intereses de cuentas, depósitos y
        # activos financieros en general) — verificada contra capturas
        # literales de RentaWEB (ver casillas_irpf.json, verificado_el
        # 2026-05-06). Se mantiene la clave legacy como fallback de lectura.
        "intereses":         (cas.get("intereses_rcm", {}).get("casilla")
                              or cas.get("intereses_cuentas_remuneradas", {}).get("casilla")
                              or "0027"),
        # T-Bills EXTRANJERAS (transmisión o amortización): 0031 'otros
        # activos financieros'. La 0030 es SOLO Letras del Tesoro españolas
        # (tesoro.es; corrección doctrinal 2026-06-12 — la cuota es idéntica,
        # ambas suman al mismo RCM del ahorro, pero la clasificación correcta
        # de deuda pública no española es 0031).
        "letras_tesoro":     cas.get("otros_activos_rcm", {}).get("casilla", "0031"),
        "acciones_detalle":  cas.get("acciones_cotizadas_gp", {}).get("rango_detalle", "326-338"),
        "acciones_ganancias": cas.get("acciones_cotizadas_gp", {}).get("casilla_suma_ganancias", "339"),
        "acciones_perdidas": cas.get("acciones_cotizadas_gp", {}).get("casilla_suma_perdidas", "340"),
        "derechos":          cas.get("derechos_suscripcion_gp", {}).get("rango_detalle", "341-346"),
        "otros":             cas.get("otros_elementos_patrimoniales_gp", {}).get("rango_detalle", "1624-1654"),
        "otros_clave_tipo":  cas.get("otros_elementos_patrimoniales_gp", {}).get("casilla_clave_tipo", "1626"),
        "saldos_negativos":  cas.get("saldos_negativos_arrastre_gp", {}).get("rango_detalle", "1186+"),
        "_full":             cas,
    }
    _CASILLAS_CACHE[ejercicio] = simple
    return simple


def C(concepto: str, ejercicio: str | None = None) -> str:
    """Atajo: devuelve la casilla (o rango) para un concepto en el ejercicio.

    Conceptos válidos: divs, retencion_es, cdi, intereses, letras_tesoro,
    acciones_detalle, acciones_ganancias, acciones_perdidas, derechos, otros,
    otros_clave_tipo, saldos_negativos.
    """
    mapping = _load_casillas_ejercicio(ejercicio or EJERCICIO)
    return str(mapping.get(concepto, "?"))


# ── Utilidades numéricas ────────────────────────────────────────────────────

def parse_es(s):
    """'2.584,76' o '2584,76' → Decimal. Maneja vacíos y signo."""
    if not s:
        return Decimal('0')
    s = s.strip().strip('"').replace('\xa0', '').replace(' ', '')
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    s = s.lstrip('+')
    try:
        return Decimal(s)
    except Exception:
        return Decimal('0')

_RE_MILES_ES = re.compile(r'^[+-]?\d{1,3}(\.\d{3})+$')

def parse_es_texto(s):
    """Variante de parse_es para TEXTO es-ES puro (descripciones del CSV de
    Cuenta de DeGiro: 'Compra 1.000 PRODUCTO@12,34 EUR'), donde el punto solo
    puede ser separador de miles: '1.000' → 1000.

    parse_es('1.000') devuelve 1 porque sin coma trata el punto como decimal
    — correcto para fuentes con decimales anglosajones (TR exporta
    shares='35.1700660000') pero erróneo en texto es-ES: cambios de mercado
    de ≥1000 títulos perdían 3 órdenes de magnitud → G/P fantasma.

    NO usar sobre columnas numéricas de TR/IBKR (punto decimal anglosajón).
    """
    if not s:
        return Decimal('0')
    t = s.strip().strip('"').replace('\xa0', '').replace(' ', '')
    if _RE_MILES_ES.match(t):
        try:
            return Decimal(t.lstrip('+').replace('.', ''))
        except Exception:
            return Decimal('0')
    return parse_es(s)

def fmt_es(val):
    """Decimal → string con coma decimal (formato AEAT). Ej: 2584.76 → '2584,76'

    Acepta int/float/str además de Decimal. Defensa contra call sites que
    pasen un 0 nativo cuando una `sum(...)` sobre lista vacía no especifica
    `start=Decimal('0')`. Convertir aquí evita propagar el AttributeError.
    """
    if not isinstance(val, Decimal):
        val = Decimal(str(val))
    return str(val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)).replace('.', ',')

def fmt_es_4(val):
    """4 decimales para nominales. Ej: 0.333333 → '0,3333'"""
    if not isinstance(val, Decimal):
        val = Decimal(str(val))
    return str(val.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)).replace('.', ',')


# ── Utilidades de clasificación ─────────────────────────────────────────────

def is_option_isin(isin):
    if not isin:
        return False
    return any(isin.upper().startswith(p) for p in OPTION_ISIN_PREFIXES)

def is_option_name(name):
    """'SAN C9.75 16JAN26', 'ASLM P900.00 19DEC25' → True"""
    if not name:
        return False
    return bool(re.search(r'\b[CP]\d+[\.,]\d{2}\s+\d{2}[A-Z]{3}\d{2}', name))

def is_non_tradeable(name):
    return bool(re.search(r'NON.?TRADEABLE', name.upper()))

def is_rts(name):
    """Derechos de suscripción: '-RTS', '-RIGHTS', 'DERECHOS'"""
    return bool(re.search(r'\bRTS\b|\bRIGHTS?\b|\bDERECHOS?\b|\bRCT\b', name.upper()))

def is_nil_paid(name):
    """Nil-paid rights (derechos nulos)"""
    return 'NIL' in name.upper()

def base_company_name(name):
    """Extrae el nombre base de la empresa quitando sufijos corporativos."""
    name = re.sub(r'\s*-?\s*(NON.?TRADEABLE|RTS|RIGHTS?|NIL|DERECHOS?|RCT)\s*$',
                  '', name, flags=re.IGNORECASE)
    return name.strip()


def _posicion_historica_subyacente(base_dir: str, isin: str, hasta_fecha_dt):
    """Escanea todos los DeGiro_Transacciones_*.csv en base_dir y devuelve las
    operaciones del ISIN hasta (inclusive) `hasta_fecha_dt`.

    Devuelve lista de dicts con los mismos campos que `todas_ops` (tipo, isin,
    nombre, fecha, cantidad, importe_eur, gastos_eur). Se usa para calcular
    el prorrateo de coste de acciones liberadas cuando el subyacente se compró
    en ejercicios anteriores al que se está procesando.

    Sólo considera líneas con Order ID (compra/venta reales); ignora eventos
    corporativos y asignaciones.
    """
    import glob as _glob
    ops_hist: list = []
    # Incluimos:
    #   DeGiro_Transacciones.csv           (unificado sin sufijo — multi-año completo)
    #   DeGiro_Transacciones_YYYY.csv      (ficheros por año generados o subidos)
    #   DeGiro_Transacciones_partN.csv     (multi-upload: varios exports del usuario)
    #   DeGiro_Transacciones-<cualquier>.csv (otras variantes con guión)
    candidatos = set(
        _glob.glob(os.path.join(base_dir, "DeGiro_Transacciones*.csv"))
    )
    _vistos_orderid = set()  # para deduplicar entre el unificado y sus splits
    for csv_path in sorted(candidatos):
        try:
            with open(csv_path, encoding='utf-8') as _fh:
                rd = csv.reader(_fh)
                next(rd, None)
                for row in rd:
                    if len(row) < 17:
                        continue
                    if row[3].strip() != isin:
                        continue
                    order_id = _extract_degiro_order_id(row)
                    if not order_id:
                        continue
                    fecha_dt = parse_date_dt(row[0])
                    if not fecha_dt or (hasta_fecha_dt and fecha_dt > hasta_fecha_dt):
                        continue
                    try:
                        qty       = parse_es(row[6].strip())
                        precio    = parse_es(row[7].strip())
                        valor_eur = abs(parse_es(row[11].strip()))
                        autofx    = abs(parse_es(row[13].strip())) if row[13].strip() else Decimal('0')
                        trans     = abs(parse_es(row[14].strip())) if row[14].strip() else Decimal('0')
                    except Exception:
                        continue
                    if qty == 0 or precio == 0:
                        continue
                    # Deduplicar por Order ID: si una transacción aparece en
                    # el unificado `DeGiro_Transacciones.csv` y también en el
                    # fichero por año generado por el splitter, sólo una cuenta.
                    if order_id in _vistos_orderid:
                        continue
                    _vistos_orderid.add(order_id)
                    ops_hist.append({
                        'tipo':        'A' if qty > 0 else 'T',
                        'isin':        isin,
                        'nombre':      row[2].strip()[:50],
                        'fecha':       fecha_dt.strftime('%d/%m/%Y'),
                        'cantidad':    abs(qty),
                        'importe_eur': valor_eur,
                        'gastos_eur':  autofx + trans,
                        'broker':      'DeGiro',
                    })
        except (OSError, StopIteration):
            continue
    return ops_hist

def parse_date(s, fmts=None):
    """Intenta parsear fecha. Devuelve 'DD/MM/YYYY' o None."""
    if not s:
        return None
    if fmts is None:
        fmts = ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%d.%m.%Y', '%m/%d/%Y')
    s = s.strip().split(',')[0].strip()
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime('%d/%m/%Y')
        except ValueError:
            continue
    return None

def parse_date_dt(s):
    """Devuelve datetime o None."""
    d = parse_date(s)
    if d:
        try:
            return datetime.strptime(d, '%d/%m/%Y')
        except Exception:
            pass
    return None


def _venc_to_ddmmyyyy(v):
    """Convierte vencimiento de opción '16MAY25' → '16/05/2025' (formato op['fecha'])."""
    # 'OCT' es la abreviatura inglesa que usan los vencimientos IBKR; sin
    # ella las opciones de octubre devolvían None en silencio (auditoría
    # 2026-06-11, CL8). Se conserva 'OKT' (variante alemana ya soportada).
    _M = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
          'JUL':7,'AUG':8,'SEP':9,'OCT':10,'OKT':10,'NOV':11,'DEC':12}
    if not v or len(v) < 7:
        return None
    try:
        day  = int(v[:2])
        mon  = _M.get(v[2:5].upper())
        yr   = int(v[5:])
        yr_f = 2000 + yr if yr < 100 else yr
        if not mon:
            return None
        return f"{day:02d}/{mon:02d}/{yr_f:04d}"
    except Exception:
        return None


# Hints para tickers cuyo nombre de empresa no contiene el ticker directamente
_TICKER_HINTS = {
    'ASLM': 'ASML',
    'TEF':  'TELEFON',
    'SAN':  'SANTAN',
    'CA1':  'CARREFOUR',
    'REP':  'REPSOL',
    'NESN': 'NESTLE',
    'PAG':  'PAGAYA',
}


def _subyacente_en_nombre(subyacente, nombre):
    """True si el subyacente (ticker) aparece en el nombre de la empresa."""
    nu = nombre.upper()
    # 1) ticker literal o primeros 3 caracteres
    if subyacente in nu or subyacente[:3] in nu:
        return True
    # 2) hint table para tickers con diferencia ticker/nombre
    hint = _TICKER_HINTS.get(subyacente.upper(), '')
    return bool(hint and hint in nu)


# ── Detector de acciones corporativas (DeGiro) ─────────────────────────────

def detect_corporate_actions_degiro(raw_rows):
    """
    Analiza las filas sin Order ID del CSV de DeGiro para identificar
    splits, contrasplits, cambios de ISIN y derechos de suscripción.

    MECANISMO DeGiro — cómo representa los eventos corporativos:
    ─────────────────────────────────────────────────────────────
    A) NON TRADEABLE (bloqueo temporal):
       Cuando ocurre un canje/split/cambio ISIN, DeGiro:
       1. Crea +N [EMPRESA NON TRADEABLE] [ISIN_viejo]: bloquea N acciones
       2. Crea +M [EMPRESA] [ISIN_nuevo]: acredita M acciones nuevas
       3. Cancela -N [EMPRESA NON TRADEABLE] [ISIN_viejo]: libera el bloqueo
       Si M = N → cambio de ISIN puro (no evento fiscal)
       Si M > N → split (más acciones al mismo coste total)
       Si M < N → contrasplit (menos acciones al mismo coste total)

    B) Derechos (RTS / RIGHTS):
       +N [EMPRESA-RTS] [ISIN_rts] a precio 0 → derechos asignados
       Pueden venderse (precio > 0 → T normal) o ejercerse (desaparecen al comprar)

    C) Split directo sin NON TRADEABLE:
       -N [EMPRESA] [ISIN] precio=0 → acciones antiguas salen
       +M [EMPRESA] [ISIN] precio=0 → acciones nuevas entran (mismo día)

    D) Eventos con precio ≠ 0 y NON TRADEABLE:
       Operación corporativa preciada (ej: OPA con canje mixto, return of capital).
       Se registra como COMPLEX → requiere revisión manual.

    Retorna:
        splits    : lista de dict SP (para el CSV)
        derechos  : lista de dict (para informe, gestión manual)
        complejos : lista de dict (para informe)
    """
    # ── Recopilar candidatos (sin Order ID) ───────────────────────────────
    candidates = []
    for row in raw_rows:
        order_id = _extract_degiro_order_id(row)
        if order_id:
            continue  # Trade normal; ya procesado en parse_degiro

        isin   = row[3].strip()
        nombre = row[2].strip()

        if not isin:
            continue
        if is_option_isin(isin) or is_option_name(nombre):
            continue

        try:
            precio   = float(row[7].strip().replace(',', '.'))
            cantidad = float(row[6].strip().replace(',', '.'))
        except (ValueError, IndexError):
            continue

        fecha_dt = parse_date_dt(row[0])
        if not fecha_dt or cantidad == 0:
            continue

        candidates.append({
            'fecha_dt':         fecha_dt,
            'fecha':            fecha_dt.strftime('%d/%m/%Y'),
            'isin':             isin,
            'nombre':           nombre,
            'nombre_base':      base_company_name(nombre),
            'cantidad':         cantidad,  # positivo=entrada, negativo=salida
            'precio':           precio,
            'is_non_tradeable': is_non_tradeable(nombre),
            'is_rts':           is_rts(nombre),
            'is_nil':           is_nil_paid(nombre),
        })

    # ── Estrategia A: parear NON TRADEABLE con nuevas acciones ────────────
    # Busca pares: +N NON_TRADEABLE[isin_old] + +M CLEAN[isin_new] (misma ventana)
    nt_positives = [c for c in candidates if c['is_non_tradeable'] and c['cantidad'] > 0]
    clean_positives = [c for c in candidates
                       if not c['is_non_tradeable'] and not c['is_rts'] and not c['is_nil']
                       and c['cantidad'] > 0 and c['precio'] == 0]

    eventos = []
    used_nt = set()
    used_cl = set()

    for i, nt in enumerate(nt_positives):
        if i in used_nt:
            continue
        best_cl = None
        best_delta = timedelta(days=999)
        for j, cl in enumerate(clean_positives):
            if j in used_cl:
                continue
            # Mismo nombre base y dentro de la ventana temporal
            if cl['nombre_base'].upper() != nt['nombre_base'].upper():
                continue
            delta = abs(cl['fecha_dt'] - nt['fecha_dt'])
            if delta <= timedelta(days=WINDOW_CORPORATE_DAYS) and delta < best_delta:
                best_delta = delta
                best_cl = (j, cl)

        if best_cl is None:
            # No se encontró contrapartida clara → complejo
            if nt['precio'] != 0:
                eventos.append({'tipo_ca': CA_COMPLEX, **nt,
                                 'descripcion': 'NON TRADEABLE con precio ≠ 0 sin contrapartida'})
            continue

        j, cl = best_cl
        used_nt.add(i)
        used_cl.add(j)

        qty_old = abs(nt['cantidad'])
        qty_new = cl['cantidad']

        if nt['precio'] != 0 or cl['precio'] != 0:
            # Evento preciado: OPA canje, return of capital, etc.
            eventos.append({'tipo_ca': CA_COMPLEX,
                             'fecha': nt['fecha'], 'nombre': nt['nombre_base'],
                             'isin_old': nt['isin'], 'isin_new': cl['isin'],
                             'qty_old': qty_old, 'qty_new': qty_new,
                             'precio': nt['precio'],
                             'descripcion': f'Evento preciado a {nt["precio"]} (return of capital, OPA, canje mixto)'})
        elif abs(qty_new - qty_old) < 0.01 and cl['isin'] == nt['isin']:
            # Mismo ISIN, misma cantidad → bloqueo/desbloqueo puro sin cambio
            pass  # No es un evento declarable
        elif abs(qty_new - qty_old) < 0.01:
            # Misma cantidad, distinto ISIN → cambio de ISIN administrativo
            eventos.append({'tipo_ca': CA_ISIN_CHANGE,
                             'fecha': nt['fecha'], 'nombre': cl['nombre'],
                             'isin_old': nt['isin'], 'isin_new': cl['isin'],
                             'qty_old': qty_old, 'qty_new': qty_new,
                             'descripcion': f'Canje ISIN {nt["isin"]} → {cl["isin"]} (ratio 1:1)'})
        elif qty_new > qty_old:
            eventos.append({'tipo_ca': CA_SPLIT,
                             'fecha': nt['fecha'], 'nombre': cl['nombre'],
                             'isin_old': nt['isin'], 'isin_new': cl['isin'],
                             'qty_old': qty_old, 'qty_new': qty_new,
                             'descripcion': f'Split {qty_old:.0f}:{qty_new:.0f}'})
        else:
            eventos.append({'tipo_ca': CA_CONTRASPLIT,
                             'fecha': nt['fecha'], 'nombre': cl['nombre'],
                             'isin_old': nt['isin'], 'isin_new': cl['isin'],
                             'qty_old': qty_old, 'qty_new': qty_new,
                             'descripcion': f'Contrasplit {qty_old:.0f}:{qty_new:.0f}'})

    # ── Estrategia B: split directo sin NON TRADEABLE ─────────────────────
    # Mismo ISIN, mismo día, precio=0: -N salida + +M entrada
    used_clean_ids = {id(clean_positives[j]) for j in used_cl}
    same_isin_zero = [c for c in candidates
                      if not c['is_non_tradeable'] and not c['is_rts'] and not c['is_nil']
                      and c['precio'] == 0 and id(c) not in used_clean_ids]

    by_isin_date = defaultdict(list)
    for c in same_isin_zero:
        key = (c['isin'], c['fecha'])
        by_isin_date[key].append(c)

    for (isin, fecha), rows in by_isin_date.items():
        entradas = [r for r in rows if r['cantidad'] > 0]
        salidas  = [r for r in rows if r['cantidad'] < 0]
        if not entradas or not salidas:
            continue
        qty_new = sum(r['cantidad'] for r in entradas)
        qty_old = abs(sum(r['cantidad'] for r in salidas))
        nombre  = entradas[0]['nombre']

        if abs(qty_new - qty_old) < 0.01:
            continue  # Mismo ratio → probablemente ya cubierto arriba o irrelevante

        tipo_ca = CA_SPLIT if qty_new > qty_old else CA_CONTRASPLIT
        eventos.append({'tipo_ca': tipo_ca,
                         'fecha': fecha, 'nombre': nombre,
                         'isin_old': isin, 'isin_new': isin,
                         'qty_old': qty_old, 'qty_new': qty_new,
                         'descripcion': f'{tipo_ca} directo {qty_old:.0f}:{qty_new:.0f}'})

    # ── Derechos de suscripción (RTS) ─────────────────────────────────────
    # Excluir filas NON TRADEABLE que también llevan "RTS" en el nombre:
    # son compromisos de ejercicio, no asignaciones de derechos nuevos.
    # Agrupar por (nombre_base, isin) para separar eventos con distinto ISIN
    # del mismo emisor (p.ej. scrip enero y scrip julio de ACS tienen ISINs distintos).
    rts_positives = [c for c in candidates
                     if c['is_rts'] and not c['is_non_tradeable']
                     and c['precio'] == 0 and c['cantidad'] > 0]
    rts_groups = defaultdict(list)
    for r in rts_positives:
        rts_groups[(r['nombre_base'], r['isin'])].append(r)

    derechos = []
    for (nombre_base, isin_rts), rows in rts_groups.items():
        total_rts = sum(r['cantidad'] for r in rows)
        primer_fecha = min(r['fecha_dt'] for r in rows).strftime('%d/%m/%Y')
        derechos.append({'tipo_ca': CA_RIGHTS,
                          'fecha': primer_fecha, 'nombre': nombre_base,
                          'isin': isin_rts, 'cantidad': total_rts,
                          'descripcion': f'{total_rts:.0f} derechos asignados (ISIN {isin_rts})'})

    # ── P4: Derechos residuales recomprados por el emisor ─────────────────
    # Patrón: mismo ISIN RTS, misma fecha (o ventana corta), -K RTS precio 0
    # (sin Order ID) + +K NON TRADEABLE-RTS precio 0. El emisor retira K
    # derechos residuales (que no alcanzan el ratio de canje) y los paga al
    # precio comprometido. El abono aparece en el fichero de cuenta, no aquí.
    # Fiscalmente: RCM casilla 0029 (Art. 25.1.a LIRPF).
    rts_negatives = [c for c in candidates
                     if c['is_rts'] and not c['is_non_tradeable']
                     and c['precio'] == 0 and c['cantidad'] < 0]
    nt_rts_positives = [c for c in candidates
                        if c['is_rts'] and c['is_non_tradeable']
                        and c['precio'] == 0 and c['cantidad'] > 0]
    residuales = []
    used_rts_neg = set()
    used_nt_rts  = set()
    for i, neg in enumerate(rts_negatives):
        if i in used_rts_neg:
            continue
        qty_neg = abs(neg['cantidad'])
        for j, pos in enumerate(nt_rts_positives):
            if j in used_nt_rts:
                continue
            if pos['isin'] != neg['isin']:
                continue
            delta = abs(pos['fecha_dt'] - neg['fecha_dt'])
            if delta > timedelta(days=WINDOW_CORPORATE_DAYS):
                continue
            if abs(pos['cantidad'] - qty_neg) > 0.01:
                continue
            used_rts_neg.add(i)
            used_nt_rts.add(j)
            residuales.append({
                'tipo_ca':    CA_RESIDUAL,
                'fecha':      neg['fecha'],
                'nombre':     neg['nombre_base'],
                'isin':       neg['isin'],
                'cantidad':   qty_neg,
                'descripcion': f'{qty_neg:.0f} derecho(s) residual(es) retirado(s) por el emisor (ISIN {neg["isin"]})',
            })
            break

    # ── Cambios de nombre (mismo ISIN, distintos nombres) ─────────────────
    # Empresas que se rebautizan conservando el ISIN (p. ej. Facebook → Meta,
    # Square → Block). FIFO sigue funcionando porque está indexado por ISIN,
    # pero el informe debe avisar para que reports y verificaciones manuales
    # sepan que el mismo activo aparece con nombres distintos en el CSV.
    def _norm_name(s: str) -> str:
        s = base_company_name(s).upper()
        s = re.sub(r'[^A-Z0-9 ]', ' ', s)
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    nombres_por_isin: dict = defaultdict(list)
    for row in raw_rows:
        isin_r   = row[3].strip() if len(row) > 3 else ''
        nombre_r = row[2].strip() if len(row) > 2 else ''
        if not isin_r or not nombre_r:
            continue
        if is_option_isin(isin_r) or is_option_name(nombre_r):
            continue
        fdt = parse_date_dt(row[0])
        if not fdt:
            continue
        norm = _norm_name(nombre_r)
        if not norm:
            continue
        nombres_por_isin[isin_r].append((fdt, norm, nombre_r))

    def _is_token_prefix(short_toks: list, long_toks: list) -> bool:
        return len(short_toks) <= len(long_toks) and long_toks[:len(short_toks)] == short_toks

    # Conjunto de (nombre_base_norm) → list[(isin, fdt)] para detectar spin-offs:
    # un placeholder NON TRADEABLE con ISIN_X (= ISIN del valor nuevo) y a la vez
    # otra posición preexistente de la matriz con el MISMO nombre base pero ISIN_Y
    # distinto (la matriz que sigue cotizando) → es escisión, no cambio de nombre.
    isins_por_nombre_base: dict = defaultdict(set)
    for isin_b, entries_b in nombres_por_isin.items():
        for _, _, nombre_orig in entries_b:
            base = base_company_name(nombre_orig).strip().upper()
            if base:
                isins_por_nombre_base[base].add(isin_b)

    spin_offs = []
    name_changes = []
    for isin_nc, entries in nombres_por_isin.items():
        distinct_norms = {n for _, n, _ in entries}
        if len(distinct_norms) <= 1:
            continue
        # Filtrar falsos positivos: un nombre es solo prefijo-de-tokens del otro.
        # DeGiro a veces enriquece nombres con metadata (p.ej. 'OCADO GROUP PLC'
        # → 'OCADO GROUP PLC LS -,02'). No es un verdadero rename.
        sorted_by_len = sorted(distinct_norms, key=lambda n: len(n.split()))
        canonical = sorted_by_len[0].split()
        todos_son_extensiones = all(
            _is_token_prefix(canonical, n.split()) for n in sorted_by_len
        )
        if todos_son_extensiones:
            continue

        entries_sorted = sorted(entries, key=lambda e: e[0])
        primer_norm = entries_sorted[0][1]
        primer_nombre = entries_sorted[0][2]
        primer_fecha  = entries_sorted[0][0]
        ultimo = None
        for e in reversed(entries_sorted):
            if e[1] != primer_norm and not (
                _is_token_prefix(primer_norm.split(), e[1].split())
                or _is_token_prefix(e[1].split(), primer_norm.split())
            ):
                ultimo = e
                break
        if not ultimo:
            continue
        transicion = min(
            (e[0] for e in entries_sorted if e[1] == ultimo[1]),
            default=ultimo[0],
        )

        # ── Discriminar spin-off vs name change real ───────────────────────
        # Patrón spin-off DeGiro: el placeholder "X - NON TRADEABLE" (precio 0,
        # sin Order ID) lleva el ISIN del valor NUEVO escindido (no el de la
        # matriz). Días después, "CAMBIO DE PRODUCTO" lo reemplaza con el
        # nombre real de la empresa escindida (mismo ISIN). Mientras tanto,
        # la matriz sigue cotizando con su ISIN original distinto.
        es_spinoff = False
        primer_es_nt = is_non_tradeable(primer_nombre)
        ultimo_es_nt = is_non_tradeable(ultimo[2])
        if primer_es_nt and not ultimo_es_nt:
            base_nt = base_company_name(primer_nombre).strip().upper()
            base_new = base_company_name(ultimo[2]).strip().upper()
            # Spin-off típico: el nombre base del NT coincide con la matriz
            # (3M en "3M CO - NON TRADEABLE") y existe otro ISIN distinto con
            # ese mismo nombre base (3M real, US88579Y1010, en cartera).
            isins_misma_base = isins_por_nombre_base.get(base_nt, set()) - {isin_nc}
            es_spinoff = bool(isins_misma_base) and base_nt != base_new
            if es_spinoff:
                isin_matriz = sorted(isins_misma_base)[0]
                qty_recibida = sum(c['cantidad'] for c in candidates
                                   if c['isin'] == isin_nc and c['cantidad'] > 0
                                   and c['precio'] == 0
                                   and is_non_tradeable(c['nombre']))
                spin_offs.append({
                    'tipo_ca':       CA_SPIN_OFF,
                    'fecha':         transicion.strftime('%d/%m/%Y'),
                    'fecha_efectiva': primer_fecha.strftime('%d/%m/%Y'),
                    'isin':          isin_nc,             # ISIN escindida
                    'isin_old':      isin_matriz,         # ISIN matriz
                    'isin_new':      isin_nc,
                    'nombre':        ultimo[2],           # SOLVENTUM CORP
                    'nombre_matriz': base_nt,             # 3M CO
                    'cantidad':      abs(qty_recibida),
                    'descripcion':   f'Escisión: {base_nt} ({isin_matriz}) → '
                                     f'{ultimo[2]} ({isin_nc}); '
                                     f'{abs(qty_recibida):.0f} acciones recibidas',
                })

        if es_spinoff:
            continue   # No registrar como cambio de nombre

        name_changes.append({
            'tipo_ca':     CA_NAME_CHANGE,
            'fecha':       transicion.strftime('%d/%m/%Y'),
            'isin':        isin_nc,
            'nombre':      ultimo[2],
            'nombre_old':  primer_nombre,
            'nombre_new':  ultimo[2],
            'fecha_old':   primer_fecha.strftime('%d/%m/%Y'),
            'descripcion': f'Cambio de nombre mismo ISIN: "{primer_nombre}" → "{ultimo[2]}"',
        })

    # ── Separar por tipo para retorno ─────────────────────────────────────
    splits    = [e for e in eventos if e['tipo_ca'] in (CA_SPLIT, CA_CONTRASPLIT)]
    isin_chgs = [e for e in eventos if e['tipo_ca'] == CA_ISIN_CHANGE]
    complejos = [e for e in eventos if e['tipo_ca'] == CA_COMPLEX]
    # Spin-offs detectados: añadirlos a la lista que ya devuelve la función
    # (junto con los name_changes; los consumidores los distinguen por tipo_ca).
    name_changes = spin_offs + name_changes
    derechos.extend(residuales)   # P4 se acumula con RIGHTS para el informe

    # ── Acciones liberadas potenciales (clean_positives no emparejados) ────
    # Candidatos: precio=0, sin Order ID, no RTS, no NT, no NIL, cantidad>0,
    # que no se han usado en Estrategia A ni en un split Estrategia B.
    # Son candidatos a ser acciones liberadas de un scrip dividend TYPE B.
    matched_in_strat_b = set()
    for (isin_b, fecha_b), brows in by_isin_date.items():
        entradas_b = [r for r in brows if r['cantidad'] > 0]
        salidas_b  = [r for r in brows if r['cantidad'] < 0]
        if entradas_b and salidas_b:
            for r in entradas_b + salidas_b:
                matched_in_strat_b.add(id(r))

    posibles_liberadas = [c for c in clean_positives
                          if id(c) not in used_clean_ids          # no usadas en Strat A
                          and id(c) not in matched_in_strat_b]    # no en split Strat B

    return splits, isin_chgs, derechos, complejos, posibles_liberadas, name_changes


def calcular_nominal_split(qty_old, qty_new, nominal_old=None):
    """
    Calcula el nominal nuevo para satisfacer la ecuación AEAT:
      qty_old × nominal_old = qty_new × nominal_new

    Si no se conoce el nominal antiguo, usa 1,00 EUR como referencia relativa.
    El ratio resultante es lo que importa (AEAT valida la ecuación, no el valor absoluto).
    """
    if nominal_old is None:
        nominal_old = Decimal('1.00')  # Referencia relativa
    nominal_new = (qty_old * nominal_old) / qty_new
    return nominal_old, nominal_new


def build_sp_row(evento):
    """
    Construye la fila SP para el CSV de AEAT a partir de un evento split/contrasplit.

    Formato SP (AEAT Mi cartera de valores):
      SP;ISIN;Denominacion;Fecha;TitulosAntiguos;TitulosNuevos;NominalAntiguo

    La ecuación debe cumplirse:
      TitulosAntiguos × NominalAntiguo = TitulosNuevos × NominalNuevo
      (NominalNuevo = NominalAntiguo × TitulosAntiguos / TitulosNuevos)

    Nota: si el importador CSV de AEAT no acepta SP, introducir manualmente
    en RentaWEB > Mi cartera de valores > operación > tipo SP.
    """
    qty_old = Decimal(str(evento['qty_old']))
    qty_new = Decimal(str(evento['qty_new']))
    nominal_old, nominal_new = calcular_nominal_split(qty_old, qty_new)

    return {
        'tipo':         'SP',
        'isin':         evento['isin_new'],
        'nombre':       evento['nombre'][:50],
        'fecha':        evento['fecha'],
        # Col 5: TitulosAntiguos (reusa CantidadTitulos)
        'cantidad':     qty_old,
        # Col 6: TitulosNuevos (reusa ImporteTotalEUR)
        'importe_eur':  qty_new,
        # Col 7: NominalAntiguo (reusa GastosEUR) — 1,00 EUR relativo
        'gastos_eur':   nominal_old,
        # Información adicional para el informe (no va al CSV)
        '_nominal_new': nominal_new,
        '_isin_old':    evento['isin_old'],
        '_descripcion': evento['descripcion'],
        'broker':       'DeGiro',
    }


# ── Parser DeGiro ──────────────────────────────────────────────────────────

# UUID v4 (formato del ID Orden de DeGiro). El ID puede venir en distintas
# posiciones segun la fila tenga ejecucion partida en varios mercados (que
# anade columnas vacias). Detectamos por patron en lugar de indice fijo.
_RE_DEGIRO_ORDER_ID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _extract_degiro_order_id(row):
    """Busca el ID Orden (UUID) en cualquier celda de la fila — robusto a
    columnas vacias adicionales que DeGiro inserta cuando la orden tiene
    ejecucion partida en varios mercados (MESI+XLON, etc.)."""
    for cell in reversed(row):
        s = (cell or '').strip()
        if s and _RE_DEGIRO_ORDER_ID.match(s):
            return s
    return ''


def _classify_external_fee_jurisdiction(desc_up: str) -> str:
    """Clasifica la jurisdiccion de una tasa externa segun la descripcion
    (mayusculas) que aparece en el CSV de Cuenta de DeGiro o IBKR.

    Devuelve uno de: 'es', 'uk', 'fr', 'it', 'hk', 'other'.

    Patrones que ha emitido DeGiro (verificados en datos reales):
      - "Spanish Transaction Tax"                          -> es (ITF Ley 5/2020)
      - "Tasa Tobin" / "ITF"                               -> es
      - "London/Dublin Stamp Duty"                         -> uk
      - "Hong Kong Stamp Duty"                             -> hk
      - "Impuesto de transaccion Frances"                  -> fr
      - "French Transaction Tax"                           -> fr
      - "Impuesto sobre Transacciones Financieras Italiano" -> it
    """
    # Espanol antes de "TRANSACTION TAX" generico para que SPANISH gane.
    if 'SPANISH' in desc_up or 'TASA TOBIN' in desc_up or 'ITF' in desc_up:
        return 'es'
    if 'HONG KONG' in desc_up:
        return 'hk'
    if ('FRANCÉS' in desc_up or 'FRANCES' in desc_up or 'FRENCH' in desc_up
            or 'FTT' in desc_up):
        return 'fr'
    if 'ITALIANO' in desc_up or 'ITALIAN' in desc_up:
        return 'it'
    if 'LONDON' in desc_up or 'DUBLIN' in desc_up or 'STAMP DUTY' in desc_up:
        # UK por defecto si dice Stamp Duty sin jurisdiccion mas especifica
        # (Hong Kong, Spanish, Italian ya filtrados antes).
        return 'uk'
    return 'other'


def _build_degiro_external_fees(filepath):
    """Lee el CSV de Cuenta de DeGiro y devuelve un lookup
    `id_orden -> dict` con el desglose de **tasas externas** (tributos por
    transaccion) de cada orden, para sumar al gasto del trade
    correspondiente en `parse_degiro`.

    Estructura devuelta:
        {
          id_orden: {
            'total': Decimal,
            'breakdown': {'es': Decimal, 'uk': Decimal, 'fr': Decimal,
                          'hk': Decimal, 'other': Decimal},
          },
          ...
        }

    Patrones detectados (descripcion, case-insensitive):
      - Stamp Duty (London/Dublin -> UK; Hong Kong -> HK)
      - Impuesto de transaccion (Frances -> FR; otros -> other)
      - Tasa Tobin / ITF (Espana, Ley 5/2020)
      - Transaction Tax / FTT (genericos)

    Tratamiento fiscal: tributo inherente a la adquisicion/transmision
    (Art. 35.1.b LIRPF + DGT V1989-21 para ITF espanol). Suma al valor de
    adquisicion para reducir la futura G/P al transmitir.

    Excluye explicitamente "Costes de transaccion y/o externos de DEGIRO"
    porque esa fila duplica la col 14 del CSV de Transacciones.
    """
    if not os.path.exists(filepath):
        return {}

    PATTERNS = (
        'STAMP DUTY',
        'IMPUESTO DE TRANSACCIÓN',
        'IMPUESTO DE TRANSACCION',
        'IMPUESTO SOBRE TRANSACCIONES FINANCIERAS',
        'TASA TOBIN',
        'TRANSACTION TAX',
        'FTT',
    )
    EXCLUDE = (
        'COSTES DE TRANSACCIÓN Y/O EXTERNOS DE DEGIRO',
        'COSTES DE TRANSACCION Y/O EXTERNOS DE DEGIRO',
    )

    def _empty_breakdown():
        return {'es': Decimal('0'), 'uk': Decimal('0'), 'fr': Decimal('0'),
                'it': Decimal('0'), 'hk': Decimal('0'), 'other': Decimal('0')}

    fees_by_order: dict = {}

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 12:
                continue
            desc_up = (row[5] or '').strip().upper()
            if any(ex in desc_up for ex in EXCLUDE):
                continue
            if not any(p in desc_up for p in PATTERNS):
                continue
            id_orden = (row[11] or '').strip()
            if not id_orden:
                continue  # sin ID no podemos enlazar con un trade concreto
            try:
                amt = abs(parse_es((row[8] or '').strip()))
            except Exception:
                continue
            jur = _classify_external_fee_jurisdiction(desc_up)
            entry = fees_by_order.setdefault(
                id_orden,
                {'total': Decimal('0'), 'breakdown': _empty_breakdown()},
            )
            entry['total'] += amt
            entry['breakdown'][jur] += amt

    return fees_by_order


def _build_degiro_cambios_producto(filepath: str) -> dict:
    """Lee el CSV de Cuenta de DeGiro y extrae los eventos con prefijo
    `CAMBIO DE PRODUCTO:` en el campo de descripción. Devuelve un
    lookup `(isin, fecha_dd_mm_yyyy) -> dict` con metadata del par.

    Ver §3.8 de `patrones_degiro.md` para el detalle doctrinal de las
    dos variantes (cambio de mercado puro / rename con NON TRADEABLE).
    NO incluye eventos `CAMBIO DE ISIN:` — esos se procesan aparte por
    `_build_degiro_cambios_isin` con su propia semántica (§3.4.ter).

    Estructura devuelta:
        {
          (isin, 'DD/MM/YYYY'): {
            'qty_compra', 'qty_venta', 'precio',
            'tiene_non_tradeable', 'nombres', 'descripciones',
          },
        }
    """
    if not os.path.exists(filepath):
        return {}
    eventos: dict = {}
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    for row in rows[1:]:
        if len(row) < 6:
            continue
        descripcion = (row[5] or '').strip().strip('"')
        if not descripcion.upper().startswith('CAMBIO DE PRODUCTO:'):
            continue
        fecha = (row[0] or '').strip()
        if not fecha:
            continue
        # Normalizar a DD/MM/YYYY (el CSV puede usar guion o slash).
        fecha_norm = parse_date(fecha)
        if not fecha_norm:
            continue
        isin = (row[4] or '').strip()
        nombre = (row[3] or '').strip()
        if not isin:
            continue
        # Extraer cantidad y precio del literal "Compra/Venta N Producto@Precio MONEDA (ISIN)".
        m = re.search(
            r'\b(Compra|Venta)\s+([\d.,]+)\s+.+?@([\d.,]+)\s+\w+',
            descripcion, re.IGNORECASE)
        if not m:
            continue
        tipo_op = m.group(1).lower()
        try:
            # parse_es_texto: la descripción es texto es-ES — '1.000' títulos
            # son MIL, no uno (parse_es trataría el punto como decimal).
            qty = parse_es_texto(m.group(2))
            precio = parse_es_texto(m.group(3))
        except Exception:
            continue
        if qty <= 0:
            continue
        key = (isin, fecha_norm)
        ev = eventos.setdefault(key, {
            'qty_compra':          Decimal('0'),
            'qty_venta':           Decimal('0'),
            'precio':              precio,
            'tiene_non_tradeable': False,
            'nombres':             [],
            'descripciones':       [],
        })
        if 'NON TRADEABLE' in nombre.upper() or 'NON TRADEABLE' in descripcion.upper():
            ev['tiene_non_tradeable'] = True
        if nombre not in ev['nombres']:
            ev['nombres'].append(nombre)
        ev['descripciones'].append(descripcion)
        if tipo_op == 'compra':
            ev['qty_compra'] = qty
        else:
            ev['qty_venta'] = qty
        # El precio puede variar entre las dos patas en raros casos; preservar
        # el de la primera fila vista (consistente con el patrón observado
        # donde ambas son idénticas).
        if ev['precio'] == 0:
            ev['precio'] = precio
    return eventos


def _build_degiro_cambios_isin(filepath: str) -> dict:
    """Lee el CSV de Cuenta de DeGiro y extrae los eventos con prefijo
    literal `CAMBIO DE ISIN:` (distinto de `CAMBIO DE PRODUCTO:`).
    Devuelve un lookup `fecha_dd_mm_yyyy -> [eventos]` con metadata de
    cada par (ISIN viejo → ISIN nuevo), preservando coste de adquisición.

    Ver §3.4.ter de `patrones_degiro.md`. El patrón observado en el
    histórico de cartera de prueba:
      - 27-oct-2023 Mason Graphite: CA57520W1005 → CA57532C1005.
      - 19-feb-2025 Pennon Group:   GB00BT3MB248 → GB00BNNTLN49.
      - 24-jul-2025 ACS Actividades: ES0167050287 → ES0167050915.
      - 22-dic-2025 Viscofan:        ES0184262048 → ES0184262212.

    Doctrina fiscal: no hay alteración patrimonial (canje asimilable
    Art. 37.1.e LIRPF + Capítulo VII Título VII LIS para
    reorganizaciones neutrales). El coste de adquisición del lote
    original se transfiere íntegro al ISIN nuevo.

    Estructura devuelta:
        {
          'DD/MM/YYYY': [
            {
              'isin_viejo':   str,
              'isin_nuevo':   str,
              'qty':          Decimal,
              'nombre_viejo': str (con sufijo NON TRADEABLE),
              'nombre_nuevo': str (limpio),
              'precio':       Decimal (típicamente 0, pero puede ser no-nulo),
            },
            ...
          ],
        }
    """
    if not os.path.exists(filepath):
        return {}
    eventos_raw: dict = defaultdict(list)
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    for row in rows[1:]:
        if len(row) < 6:
            continue
        descripcion = (row[5] or '').strip().strip('"')
        if not descripcion.upper().startswith('CAMBIO DE ISIN:'):
            continue
        fecha = (row[0] or '').strip()
        if not fecha:
            continue
        fecha_norm = parse_date(fecha)
        if not fecha_norm:
            continue
        isin = (row[4] or '').strip()
        nombre = (row[3] or '').strip()
        m = re.search(
            r'\b(Compra|Venta)\s+([\d.,]+)\s+.+?@([\d.,]+)\s+\w+',
            descripcion, re.IGNORECASE)
        if not m:
            continue
        tipo_op = m.group(1).lower()
        try:
            # parse_es_texto: ver _build_degiro_cambios_producto — texto es-ES.
            qty = parse_es_texto(m.group(2))
            precio = parse_es_texto(m.group(3))
        except Exception:
            continue
        eventos_raw[fecha_norm].append({
            'isin': isin, 'nombre': nombre,
            'qty': qty, 'precio': precio, 'tipo_op': tipo_op,
        })
    # Emparejar venta+compra: ISIN distinto, misma cantidad, misma fecha.
    cambios: dict = defaultdict(list)
    for fecha, items in eventos_raw.items():
        ventas  = [e for e in items if e['tipo_op'] == 'venta']
        compras = [e for e in items if e['tipo_op'] == 'compra']
        for v in ventas:
            for c in compras:
                if v['isin'] == c['isin']:
                    continue  # CAMBIO DE ISIN exige ISIN distinto entre patas
                if abs(v['qty'] - c['qty']) > Decimal('0.001'):
                    continue
                cambios[fecha].append({
                    'isin_viejo':   v['isin'],
                    'isin_nuevo':   c['isin'],
                    'qty':          v['qty'],
                    'nombre_viejo': v['nombre'],
                    'nombre_nuevo': c['nombre'],
                    'precio':       v['precio'] or c['precio'],
                })
                break
    return dict(cambios)


def _build_degiro_bond_data(filepath: str) -> dict:
    """Detecta bonos individuales en el CSV de Cuenta de DeGiro y devuelve
    información de cupones por ISIN.

    El patrón canónico para detectar un bono individual en DeGiro es la
    presencia de eventos "Cupón corrido" (al comprar/vender, cantidad
    devengada al contraparte) o "Cupón" / "Pago de cupón" (al cobrar el
    cupón anual durante la tenencia).

    El "Cupón corrido" pagado al comprar incrementa el coste de adquisición;
    el cobrado al vender reduce el importe de transmisión. Los cupones
    cobrados durante la tenencia son rendimientos del capital mobiliario
    (RCM) — casilla 0027.

    Devuelve:
        {
            'isins':    set[str],          # ISINs identificados como bono
            'cupones_corridos_por_orden':  # match cupón corrido ↔ trade
                {order_id: Decimal}        # importe cupón corrido (firmado)
            'cupones_cobrados_por_isin':   # cupón anual durante tenencia
                {isin: [{fecha, fecha_valor, nombre, importe_local,
                         divisa, descripcion}, ...]}
        }

    Los cupones cobrados se guardan en divisa LOCAL (campo `divisa`); la
    conversión a EUR se hace en main() al inyectarlos en la lista de
    intereses RCM, vía get_eur_per_unit (jerarquía BCE) — F8 auditoría
    2026-06-11: antes el importe se guardaba sin divisa (un cupón USD se
    habría tratado como EUR) y además nunca se declaraba.
    """
    bond_data = {
        'isins': set(),
        'cupones_corridos_por_orden': {},  # order_id → cupón corrido EUR
        'cupones_cobrados_por_isin': defaultdict(list),
    }
    if not filepath or not os.path.exists(filepath):
        return bond_data

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 12:
                continue
            isin = (row[4] or '').strip()
            descripcion = (row[5] or '').strip()
            desc_up = descripcion.upper()
            order_id = (row[11] or '').strip() if len(row) > 11 else ''
            # parse_es: el CSV es es-ES; el replace(',', '.') anterior
            # rompía con separador de miles ('1.234,56' → Exception → 0).
            importe = parse_es((row[8] or '0').strip())

            if not isin or len(isin) != 12:
                continue

            # Cupón corrido: cualquier ISIN con esta descripción es bono.
            if 'CUPÓN CORRIDO' in desc_up or 'CUPON CORRIDO' in desc_up:
                bond_data['isins'].add(isin)
                if order_id:
                    bond_data['cupones_corridos_por_orden'][order_id] = (
                        bond_data['cupones_corridos_por_orden'].get(order_id, Decimal('0'))
                        + importe
                    )
            # Pago de cupón anual durante la tenencia: rendimiento RCM (0027).
            elif (
                ('PAGO DE CUPÓN' in desc_up or 'PAGO DE CUPON' in desc_up
                 or desc_up.startswith('CUPÓN ') or desc_up.startswith('CUPON '))
                and 'CORRIDO' not in desc_up
            ):
                bond_data['isins'].add(isin)
                bond_data['cupones_cobrados_por_isin'][isin].append({
                    'fecha':         row[0],
                    'fecha_valor':   (row[2] or '').strip(),  # fecha económica (conversión FX)
                    'nombre':        (row[3] or '').strip()[:50],
                    'importe_local': importe,
                    'divisa':        (row[7] or '').strip() or 'EUR',
                    'descripcion':   descripcion,
                })

    return bond_data


def cupones_bonos_a_intereses(bond_data: dict, ejercicio: str) -> tuple[list, list]:
    """Convierte los cupones periódicos de `bond_data` (DeGiro) en entradas
    de la lista global de intereses RCM (mismo shape que parse_ibkr_interest
    / inyección TR), convirtiendo divisa local → EUR con la jerarquía BCE de
    la fecha valor.

    F8 auditoría 2026-06-11: estos cupones se recolectaban pero nunca se
    declaraban (RCM 0027 omitido en informe/XLSX/PDF/sidecar) y el importe
    se guardaba sin divisa.

    Devuelve (entradas, avisos): `avisos` lista los cupones NO convertibles
    (sin tipo de cambio) para que el caller los comunique al usuario.
    """
    entradas: list = []
    avisos: list = []
    cupones_por_isin = (bond_data or {}).get('cupones_cobrados_por_isin') or {}
    for isin_b in sorted(cupones_por_isin):
        for cup in cupones_por_isin[isin_b]:
            fv = cup.get('fecha_valor') or cup.get('fecha') or ''
            if fv[-4:] != str(ejercicio):
                continue  # cupón de otro ejercicio en el mismo extracto
            imp_local = cup.get('importe_local', Decimal('0'))
            if imp_local == 0:
                continue
            cur = cup.get('divisa') or 'EUR'
            rate = get_eur_per_unit(fv, cur, {})
            if rate is None:
                avisos.append(f"Cupón bono {isin_b} ({fv}): sin tipo de cambio "
                              f"{cur} — NO inyectado, declarar manualmente")
                continue
            imp_eur = (imp_local * rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
            nombre_b = cup.get('nombre') or isin_b
            entradas.append({
                'fecha':         parse_date(fv) or fv,
                'divisa':        cur,
                'importe_local': imp_local,
                'importe_eur':   imp_eur,
                'descripcion':   f"Cupón bono {nombre_b} ({isin_b})",
                'tipo':          'bond_interest',
                'casilla':       '0027',
                'broker':        'DeGiro',
                'retencion_es_eur': Decimal('0'),
            })
    return entradas, avisos


def _build_ibkr_bond_data(filepath: str) -> dict:
    """Lee las secciones `Bond Interest Paid` y `Bond Interest Received` del
    Activity Statement IBKR y devuelve el accrued interest por (ISIN, fecha)
    para aplicar la doctrina DGT V1732-10:

      - Cupón corrido **pagado** al comprar (Bond Interest Paid) → suma al
        valor de adquisición.
      - Cupón corrido **cobrado** al vender (Bond Interest Received con
        misma fecha que un Trade) → suma al valor de transmisión.

    Importante: la sección `Bond Interest Received` también contiene cupones
    periódicos reales cobrados durante la tenencia (no asociados a una venta).
    Esos NO son accrued sino RCM (casilla 0027) y los procesa
    `parse_ibkr_interest` desde la sección `Interest`. Aquí solo recuperamos
    los importes que aparecen específicamente en estas dos secciones de
    accrued, asumiendo que IBKR las separa de los cupones reales.

    Estructura de fila CSV documentada (IBKR Reporting Reference Guide):
        Bond Interest Paid,    Header, Currency, Date, Description, Amount
        Bond Interest Received, Header, Currency, Date, Description, Amount

    El parser hace match por (símbolo extraído de Description, fecha) — el
    código que procesa cada Trade busca aquí accrued con su mismo símbolo
    y fecha y lo suma al coste/precio antes de pasarlo al motor FIFO.

    Devuelve:
        {
          'accrued_pagado_por_symfecha':   # compra (suma al coste)
              {(symbol, date_iso): Decimal},  # importe positivo en EUR
          'accrued_cobrado_por_symfecha':  # venta (suma al precio)
              {(symbol, date_iso): Decimal},  # importe positivo en EUR
          'isins_con_accrued': set[str],   # symbols detectados
        }
    """
    result = {
        'accrued_pagado_por_symfecha':  {},
        'accrued_cobrado_por_symfecha': {},
        'isins_con_accrued':            set(),
    }
    if not filepath or not os.path.exists(filepath):
        return result

    SECCIONES = (
        ('Bond Interest Paid',    'accrued_pagado_por_symfecha'),
        ('Bond Interest Received', 'accrued_cobrado_por_symfecha'),
    )

    eur_per_unit_cache: dict[tuple[str, str], Decimal] = {}

    def _eur_per_unit(currency: str, date_iso: str) -> Decimal:
        key = (currency.upper(), date_iso)
        if key not in eur_per_unit_cache:
            rate = _ibkr_eur_per_unit(date_iso, currency)
            eur_per_unit_cache[key] = rate or Decimal('1')
        return eur_per_unit_cache[key]

    def _extract_symbol_from_desc(desc: str) -> str:
        """Extrae el símbolo del bono desde la Description.
        Patrón típico: 'GERMAN BUND 2.5% 15-AUG-30 (DE0001102481)' → ISIN
        o 'BUNDS 2.5 25 (BUNDS)' → símbolo. Devolvemos el primer paréntesis
        si parece ISIN (12 caracteres alfanuméricos), o el primer token
        antes del espacio."""
        match = re.search(r'\(([A-Z0-9]{12})\)', desc)
        if match:
            return match.group(1)
        # Fallback: primer token (símbolo abreviado).
        return desc.split()[0] if desc else ''

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 6:
                continue
            seccion = row[0].strip()
            target_key = None
            for nom, key in SECCIONES:
                if seccion == nom:
                    target_key = key
                    break
            if not target_key:
                continue
            row_type = row[1].strip()
            if row_type != 'Data':
                continue
            try:
                currency = (row[2] or '').strip()
                date_iso = (row[3] or '').strip()
                description = (row[4] or '').strip()
                amount_str = (row[5] or '').strip().replace(',', '')
                if not currency or not date_iso or currency.lower().startswith('total'):
                    continue
                importe_local = Decimal(amount_str or '0')
                if importe_local == 0:
                    continue
            except (InvalidOperation, ValueError):
                continue

            symbol = _extract_symbol_from_desc(description)
            if not symbol:
                continue

            importe_eur = abs(importe_local) * _eur_per_unit(currency, date_iso)
            importe_eur = importe_eur.quantize(Decimal('0.01'), ROUND_HALF_UP)

            key = (symbol, date_iso)
            existing = result[target_key].get(key, Decimal('0'))
            result[target_key][key] = existing + importe_eur
            result['isins_con_accrued'].add(symbol)

    return result


def _build_ibkr_bond_maturities(filepath: str) -> list:
    """Lee la sección `Bond Maturity` del Activity Statement IBKR y devuelve
    una lista de eventos de amortización al vencimiento.

    Estructura documentada (IBKR Reporting Reference Guide):
        Bond Maturity, Header, Account, Date, Type, Symbol, Quantity, Value, [Code]

    Cuando un bono se mantiene a vencimiento, IBKR registra aquí la
    amortización: el broker paga el `Value` (= nominal × redemption rate; lo
    típico es 100% del par) y retira la `Quantity` de la cartera. No siempre
    aparece como Trade row con `Code=RED` — depende del statement.

    Devuelve lista de dicts con `symbol`, `date_iso`, `quantity` (Decimal),
    `value_local` (Decimal — importe en divisa del bono), `currency`. La
    conversión a EUR se aplica posteriormente en `parse_ibkr` con BCE.
    """
    eventos = []
    if not filepath or not os.path.exists(filepath):
        return eventos

    SECTION = 'Bond Maturity'
    header = None
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].strip() != SECTION:
                continue
            if row[1].strip() == 'Header':
                header = row
                continue
            if row[1].strip() != 'Data' or not header:
                continue
            h = header

            def col(name, default=''):
                return row[h.index(name)].strip() if name in h else default

            try:
                date_iso = col('Date')
                symbol = col('Symbol')
                qty_str = col('Quantity').replace(',', '')
                value_str = col('Value').replace(',', '')
                # Currency: si IBKR no expone columna explícita, asumir EUR
                # (statement con Base Currency=EUR ya lo trae así).
                currency = col('Currency', 'EUR') or 'EUR'
                if not symbol or not date_iso:
                    continue
                quantity = Decimal(qty_str or '0')
                value_local = Decimal(value_str or '0')
                if quantity == 0:
                    continue
                eventos.append({
                    'symbol':      symbol,
                    'date_iso':    date_iso,
                    'quantity':    abs(quantity),
                    'value_local': abs(value_local),
                    'currency':    currency,
                })
            except (InvalidOperation, ValueError):
                continue
    return eventos


def _parse_ibkr_statement_period(filepath: str) -> tuple:
    """Lee la fila `Statement,Data,Period,"January 1, 2025 - December 31, 2025"`
    del Activity Statement IBKR y devuelve (date_inicio, date_fin) o
    (None, None) si no se puede parsear.
    """
    from datetime import datetime
    if not filepath or not os.path.exists(filepath):
        return None, None

    MES_EN = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5,
        'june': 6, 'july': 7, 'august': 8, 'september': 9, 'october': 10,
        'november': 11, 'december': 12,
    }

    def parse_en_date(s):
        s = s.strip().rstrip(',').replace(',', ' ')
        parts = s.split()
        if len(parts) < 3:
            return None
        try:
            mes = MES_EN.get(parts[0].lower())
            dia = int(parts[1])
            anio = int(parts[2])
            return datetime(anio, mes, dia).date() if mes else None
        except Exception:
            return None

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 4 or row[0] != 'Statement' or row[1] != 'Data':
                continue
            if row[2].strip() != 'Period':
                continue
            valor = row[3]
            if '-' not in valor:
                continue
            partes = valor.split(' - ')
            if len(partes) != 2:
                continue
            d_ini = parse_en_date(partes[0])
            d_fin = parse_en_date(partes[1])
            return d_ini, d_fin
    return None, None


def _aplicar_amortizaciones_bonos(operaciones: list, filepath: str,
                                   sym_isin_map: dict) -> list:
    """Cierra el FIFO de bonos al vencimiento añadiendo Trades sintéticos
    de venta. Modifica `operaciones` in-place.

    Dos capas, en este orden:

    1. **Capa principal — sección `Bond Maturity` del statement.**
       Para cada evento: emite un Trade tipo 'T' con `qty=Quantity`,
       `importe_eur=Value` (convertido a EUR vía BCE), fecha=Date.
       Estos NO se consideran "inferidos" — vienen del broker.

    2. **Capa fallback — FII.Maturity con posición abierta.**
       Para cada bono cuyo `Maturity` esté dentro del periodo del
       statement Y aún tenga posición neta > 0 tras la capa 1, emite un
       Trade sintético tipo 'T' con `qty=posición_abierta`, precio=par
       (100%, importe=qty), fecha=FII.Maturity. Marcado con
       `_amortizacion_inferida=True` para warning en PDF.

    El control de "vendido antes de Maturity" es natural: si el usuario
    vendió toda la posición antes, la suma neta será 0 y no se emite
    sintético. Si solo vendió parte, el sintético cierra el remanente.

    Devuelve lista de eventos inferidos (capa 2) para warning en el PDF.
    """
    inferidas = []

    # ── Capa 1: Bond Maturity directo ──────────────────────────────────
    bond_maturities = _build_ibkr_bond_maturities(filepath)
    isins_amortizados_por_seccion: set[str] = set()

    for ev in bond_maturities:
        symbol = ev['symbol']
        date_iso = ev['date_iso']
        # Resolver ISIN y nombre canónico desde el FII map.
        fii_entry = sym_isin_map.get(symbol) or {}
        isin_resolved = fii_entry.get('isin', symbol)
        if isin_resolved and len(isin_resolved) == 12:
            isins_amortizados_por_seccion.add(isin_resolved)
        nombre = fii_entry.get('description') or symbol
        # Conversión a EUR (Bond Maturity Value puede venir en divisa local).
        value_local = ev['value_local']
        currency = ev['currency']
        if currency != 'EUR':
            rate = _ibkr_eur_per_unit(date_iso, currency)
            if rate is None:
                # Sin tipo de cambio fiable: registrar como inferida con
                # importe 0 y dejar al usuario decidir.
                value_eur = Decimal('0')
            else:
                value_eur = (value_local * rate).quantize(
                    Decimal('0.01'), ROUND_HALF_UP)
        else:
            value_eur = value_local

        fecha = parse_date(date_iso)  # devuelve string DD/MM/YYYY
        if not fecha:
            continue

        operaciones.append({
            'tipo':        'T',
            'isin':        isin_resolved,
            'nombre':      nombre[:50],
            'fecha':       fecha,
            'cantidad':    ev['quantity'],
            'importe_eur': value_eur,
            'gastos_eur':  Decimal('0'),
            'gastos_broker':              Decimal('0'),
            'gastos_autofx':              Decimal('0'),
            'gastos_externos':            Decimal('0'),
            'gastos_externos_breakdown':  {'es': Decimal('0'), 'uk': Decimal('0'),
                                            'fr': Decimal('0'), 'hk': Decimal('0'),
                                            'other': Decimal('0')},
            'broker':      'IBKR',
            'instrument_type':         'BOND',
            'instrument_type_reason':  "IBKR Bond Maturity (amortización)",
            'instrument_type_unknown': False,
            '_ibkr_fii_type':          fii_entry.get('type', ''),
            '_amortizacion_seccion':   True,  # Trade del broker, no inferido
        })

    # ── Capa 2: inferencia FII.Maturity ────────────────────────────────
    # Calcular posición neta por ISIN solo de bonos.
    posiciones_bonos: dict = defaultdict(lambda: Decimal('0'))
    nombre_por_isin: dict = {}
    for op in operaciones:
        if op.get('instrument_type') != 'BOND':
            continue
        isin = op.get('isin', '')
        if not isin:
            continue
        sign = Decimal('1') if op['tipo'] == 'A' else Decimal('-1')
        posiciones_bonos[isin] += sign * op['cantidad']
        if isin not in nombre_por_isin:
            nombre_por_isin[isin] = op.get('nombre', isin)

    # Determinar el periodo del statement: leer Statement,Data,Period si
    # está disponible (formato típico "January 1, 2025 - December 31, 2025"),
    # con fallback al año máximo de las operaciones. La maturity debe estar
    # dentro de [inicio_periodo, fin_periodo] para considerar la inferencia.
    periodo_inicio, periodo_fin = _parse_ibkr_statement_period(filepath)
    if not periodo_fin:
        # Fallback: año máximo de las operaciones, fin de año. Las operaciones
        # llevan `fecha` como string DD/MM/YYYY (ver capa 1 más arriba) o,
        # mezcladas con flujos legados, como `date`. Aceptar ambos.
        from datetime import date as _date
        def _year_of(f):
            if hasattr(f, 'year'):
                return f.year
            if isinstance(f, str) and len(f) >= 10:
                try:
                    return int(f[-4:])
                except ValueError:
                    return None
            return None
        anios = [y for y in (_year_of(op['fecha']) for op in operaciones
                              if op.get('fecha')) if y is not None]
        anio_max = max(anios) if anios else None
        if anio_max:
            periodo_fin = _date(anio_max, 12, 31)
        else:
            return inferidas

    for isin, posicion_neta in posiciones_bonos.items():
        if posicion_neta <= 0:
            continue  # cerrada o vendida en exceso (impossible normal)
        if isin in isins_amortizados_por_seccion:
            continue  # ya cerrado por capa 1

        # Buscar entrada en FII map cuyo ISIN coincida.
        fii_entry = None
        symbol_fii = None
        for sym, entry in sym_isin_map.items():
            if entry.get('isin') == isin:
                fii_entry = entry
                symbol_fii = sym
                break
        if not fii_entry:
            continue
        maturity_str = (fii_entry.get('maturity') or '').strip()
        if not maturity_str:
            continue
        # Maturity puede venir como YYYY-MM-DD o DD-MMM-YY etc. parse_date_dt
        # devuelve datetime; normalizamos a date.
        maturity_dt = parse_date_dt(maturity_str)
        if not maturity_dt:
            continue
        maturity_date = maturity_dt.date()
        # Solo inferir si Maturity está dentro del periodo cubierto por
        # el statement (no inferir vencimientos futuros).
        if maturity_date > periodo_fin:
            continue
        if periodo_inicio and maturity_date < periodo_inicio:
            continue

        # Trade sintético inferido: qty restante × 100% del par.
        # importe = qty (ya en EUR porque qty del bono = nominal en EUR
        # cuando currency=EUR). Para divisas != EUR la cantidad nominal
        # también es en esa divisa; convertimos vía BCE en la fecha de
        # Maturity.
        ult_op_isin = next((op for op in reversed(operaciones)
                            if op.get('isin') == isin), None)
        currency_isin = 'EUR'  # asunción razonable; si IBKR no expone divisa
                               # del bono en FII no podemos inferirla.
        importe_eur = posicion_neta  # qty × 100/100 = nominal
        nombre = nombre_por_isin.get(isin, fii_entry.get('description', isin))

        # `fecha` en operaciones es string DD/MM/YYYY (mismo formato que el
        # resto del pipeline IBKR). Convertimos para mantener consistencia.
        fecha_str = maturity_date.strftime('%d/%m/%Y')
        operaciones.append({
            'tipo':        'T',
            'isin':        isin,
            'nombre':      nombre[:50],
            'fecha':       fecha_str,
            'cantidad':    posicion_neta,
            'importe_eur': importe_eur,
            'gastos_eur':  Decimal('0'),
            'gastos_broker':              Decimal('0'),
            'gastos_autofx':              Decimal('0'),
            'gastos_externos':            Decimal('0'),
            'gastos_externos_breakdown':  {'es': Decimal('0'), 'uk': Decimal('0'),
                                            'fr': Decimal('0'), 'hk': Decimal('0'),
                                            'other': Decimal('0')},
            'broker':      'IBKR',
            'instrument_type':         'BOND',
            'instrument_type_reason':  "IBKR amortización inferida desde FII.Maturity",
            'instrument_type_unknown': False,
            '_ibkr_fii_type':          fii_entry.get('type', ''),
            '_amortizacion_inferida':  True,
        })
        inferidas.append({
            'isin':           isin,
            'nombre':         nombre,
            'cantidad':       posicion_neta,
            'fecha_maturity': maturity_date,
            'importe_eur':    importe_eur,
            'symbol':         symbol_fii,
        })

    # Re-ordenar operaciones por fecha real (compras+ventas posteriores deben
    # ir después; importante para FIFO consistente con scrips).
    def _fecha_key(op):
        f = op.get('fecha')
        d = parse_date_dt(f) if isinstance(f, str) else None
        if d is None:
            # Sentinel mínimo para no crashear; estas ops aparecerán primero.
            from datetime import datetime as _dt
            d = _dt.min
        return (d, 0 if op.get('tipo') == 'A' else 1)
    operaciones.sort(key=_fecha_key)

    return inferidas


def _enrich_with_instrument_type(operaciones: list, broker: str) -> None:
    """Añade in-place los campos `instrument_type`, `instrument_type_reason` e
    `instrument_type_unknown` a cada operación que no los tenga ya.

    Usa instrument_classifier.classify_isin con (isin, nombre, broker) y, si
    está disponible, el campo `_ibkr_fii_type` (que parse_ibkr setea desde el
    Financial Instrument Information del Activity Statement).

    Centraliza la lógica para no replicarla en cada `operaciones.append({...})`.
    Ops corporativas (SP, scrip dividends, derechos sintetizados) heredan la
    clasificación del ISIN de la matriz/origen.
    """
    for op in operaciones:
        if 'instrument_type' in op:
            continue  # ya enriquecido (ej. parse_ibkr trades)
        isin = op.get('isin', '')
        nombre = op.get('nombre', '')
        ibkr_type = op.get('_ibkr_fii_type', '') if broker.upper() == 'IBKR' else None
        instr_type, instr_reason, instr_unknown = classify_isin(
            isin, nombre, broker=broker, ibkr_type=ibkr_type,
        )
        op['instrument_type'] = instr_type
        op['instrument_type_reason'] = instr_reason
        op['instrument_type_unknown'] = instr_unknown


def _detect_shorts_degiro_por_inventario(operaciones):
    """Detección automática de short selling en DeGiro por estado de
    inventario cronológico.

    Complementa al detector intra-broker de pares simétricos (caso Klepierre:
    mismo día + mercados distintos + Order IDs, sección 2080-2150). Aquel
    cubre el cierre forzoso por ejercicio de opción en mercado equivocado;
    éste cubre el caso GENERAL del daytrader que abre un short (venta de un
    valor que no tiene) y lo cubre días o semanas después con una compra
    posterior — el patrón normal de shorting voluntario.

    Algoritmo (intra-año, por (isin, broker)):
      - Iterar ops cronológicamente.
      - Tracker mini-FIFO: `inventario` (acciones disponibles) y
        `shorts_pendientes` (acciones vendidas en corto sin cubrir).
      - COMPRA con shorts_pendientes > 0 → es cobertura del short. Marcar
        `_es_corto_cobertura` (si no lo marcó ya el detector Klepierre).
      - VENTA con inventario == 0 → es apertura de short. Marcar
        `_es_corto_apertura` (si no lo marcó ya el detector Klepierre).
      - Idempotente: respeta flags ya emitidos.

    Conservador: solo marca apertura cuando inventario==0 (apertura limpia).
    Para ventas parciales (donde la venta excede inventario pero hay parte
    cubierta por lots vivos), no marca — el motor lo procesaría como venta
    normal + orphan_sale por la diferencia, comportamiento aceptable para
    un caso raro. Mejora futura: extender el motor para procesar
    venta-parcial-más-short en una sola operación.

    Limitación conocida: solo ve ops del año que pasa parse_degiro. Para
    cortos cuya apertura está en un año previo cargado vía parse_csv_irpf,
    requiere implementación en el motor (los flags tendrían que persistirse
    a través del cierre del año).

    Modifica `operaciones` in-place.
    """
    from collections import defaultdict

    # Agrupar ops DeGiro de tipo A/T por (isin, broker), conservando el
    # índice original para no romper el orden de la lista pasada al motor.
    grupos = defaultdict(list)
    for idx, op in enumerate(operaciones):
        if op.get('broker', '') != 'DeGiro':
            continue
        if op.get('tipo') not in ('A', 'T'):
            continue
        if not op.get('isin'):
            continue
        key = (op['isin'], op.get('broker', ''))
        grupos[key].append((idx, op))

    for key, lst in grupos.items():
        # GUARDIA DE ACTIVIDAD BILATERAL: el detector solo aplica a (isin,
        # broker) con tanto compras como ventas. Una venta totalmente aislada
        # (sin compra antes ni después del mismo ISIN en el año) es casi
        # siempre un evento corporativo: derechos de suscripción recibidos
        # gratis, scrip dividend TYPE B (venta de derechos en mercado, coste
        # 0), o similar. NO debe marcarse como short — el motor procesa esos
        # casos con su lógica específica (casillas 0341-0355, etc.).
        n_compras = sum(1 for _, o in lst if o['tipo'] == 'A')
        n_ventas = sum(1 for _, o in lst if o['tipo'] == 'T')
        if n_compras == 0 or n_ventas == 0:
            continue
        # Orden cronológico estable: por fecha REAL y, dentro de la misma
        # fecha, compras antes que ventas (consistente con
        # calcular_fifo_from_ops). `fecha` puede venir como string
        # 'DD/MM/YYYY' (parsers) o como date/datetime (motor): ordenar el
        # string daría orden lexicográfico (02/03/2025 < 05/01/2025) y el
        # tracker de inventario marcaría aperturas/coberturas fantasma.
        def _fecha_dt_corto(f):
            if isinstance(f, datetime):
                return f
            if isinstance(f, date):
                return datetime(f.year, f.month, f.day)
            if isinstance(f, str):
                return parse_date_dt(f) or datetime.min
            return datetime.min
        lst.sort(key=lambda x: (_fecha_dt_corto(x[1]['fecha']),
                                0 if x[1]['tipo'] == 'A' else 1))

        inventario = Decimal('0')
        shorts_pendientes = Decimal('0')

        for idx, op in lst:
            cantidad = op.get('cantidad', Decimal('0'))
            if not isinstance(cantidad, Decimal):
                cantidad = Decimal(str(cantidad))

            if op['tipo'] == 'A':
                # COMPRA: si hay shorts abiertos, cubre primero.
                if shorts_pendientes > 0:
                    cubrir = min(cantidad, shorts_pendientes)
                    shorts_pendientes -= cubrir
                    if not op.get('_es_corto_cobertura'):
                        op['_es_corto_cobertura'] = True
                        op['_corto_motivo'] = (
                            f"Compra de {cantidad} {op.get('isin', '')} "
                            f"({(op.get('nombre', '') or '')[:30]}) el "
                            f"{op['fecha']} cubre una posición corta abierta "
                            f"previamente en DeGiro (detector por estado de "
                            f"inventario cronológico)."
                        )
                    remanente = cantidad - cubrir
                    if remanente > 0:
                        inventario += remanente
                else:
                    inventario += cantidad
            else:  # 'T' — VENTA
                if inventario == 0 and cantidad > 0:
                    # Apertura LIMPIA de short (sin inventario previo).
                    shorts_pendientes += cantidad
                    if not op.get('_es_corto_apertura'):
                        op['_es_corto_apertura'] = True
                        op['_corto_motivo'] = (
                            f"Venta de {cantidad} {op.get('isin', '')} "
                            f"({(op.get('nombre', '') or '')[:30]}) el "
                            f"{op['fecha']} sin inventario disponible en "
                            f"DeGiro: candidata a apertura de venta corta. "
                            f"El motor FIFO valida con inventario real "
                            f"consolidado (incluye otros brokers e histórico)."
                        )
                elif cantidad > inventario:
                    # Venta parcial: consume inventario disponible y el resto
                    # queda como orphan_sale en el motor. No marcamos short
                    # porque el motor no maneja venta-parcial-más-short en una
                    # sola op. Decrementamos inventario a 0; el motor decidirá
                    # qué hacer con la diferencia.
                    inventario = Decimal('0')
                else:
                    inventario -= cantidad


def parse_degiro(filepath, external_fees_by_order=None, bond_data=None,
                 cambios_producto=None, cambios_isin=None):
    """
    Parsea DeGiro_Transacciones_YYYY.csv.

    Columnas:
      0  Fecha (DD-MM-YYYY)  1  Hora  2  Producto  3  ISIN
      4  Bolsa               5  Centro de ejecución
      6  Número (+ compra / - venta)
      7  Precio    8  (divisa)   9  Valor local   10 (divisa)
      11 Valor EUR            12 Tipo de cambio
      13 Comisión AutoFX      14 Costes de transacción EUR
      15 Total EUR            16 ID Orden (UUID o vacío)

    `external_fees_by_order`: lookup opcional `id_orden -> Decimal` con
    tasas externas (ITF, Stamp Duty, FTT) extraidas del CSV de Cuenta via
    `_build_degiro_external_fees`. Se suman a `gastos_eur` (Art. 35.1.b LIRPF).

    Retorna: (operaciones_AT, splits_sp, descartadas, raw_rows)
    """
    external_fees_by_order = external_fees_by_order or {}
    if not os.path.exists(filepath):
        return [], [], {'opcion': 0, 'sin_isin': 0, 'precio_cero': 0, 'corporativa': 0}, []

    raw_rows = []
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 15:
                raw_rows.append(row)

    # ── Separar por Order ID ───────────────────────────────────────────────
    grupos_id   = defaultdict(list)
    sin_id_rows = []
    for row in raw_rows:
        order_id = _extract_degiro_order_id(row)
        if order_id:
            grupos_id[order_id].append(row)
        else:
            sin_id_rows.append(row)

    operaciones = []
    descartadas = {
        'opcion': 0, 'sin_isin': 0, 'precio_cero': 0, 'corporativa': 0,
        # Derivados estructurados detectados por heurística (Factor SG,
        # Turbos, Mini, Bonus, KO, etc.) — no soportados por el motor de
        # acciones/ETFs; se declaran a mano en casillas 1624-1654 clave 4.
        'categoria_no_soportada': [],
    }

    def consolidar(rows):
        r0 = rows[0]
        isin   = r0[3].strip()
        nombre = r0[2].strip()

        if not isin:
            descartadas['sin_isin'] += 1
            return None
        if is_option_isin(isin) or is_option_name(nombre):
            descartadas['opcion'] += 1
            return None
        if parse_es(r0[7]) == 0:
            descartadas['precio_cero'] += 1
            return None  # Corporativa con precio 0 (gestionada por detector)

        fecha = parse_date(r0[0])
        if not fecha:
            return None

        cantidad = sum(abs(parse_es(row[6])) for row in rows)
        if cantidad == 0:
            descartadas['corporativa'] += 1
            return None

        # Importe EUR: mismo valor Y mismo total → duplicado del mismo centro (usar uno).
        # Si el total (col 15) difiere aunque el valor (col 11) sea igual → ejecución
        # parcial en dos centros distintos para la misma orden (ej: MESI + XLON):
        # en ese caso sumar ambos importes.
        valores_eur = [abs(parse_es(row[11])) for row in rows]
        totales_eur = [abs(parse_es(row[15])) for row in rows if len(row) > 15]
        es_duplicado_centro = (
            len(set(str(v) for v in valores_eur)) == 1
            and len(totales_eur) == len(rows)
            and len(set(str(v) for v in totales_eur)) == 1
        )
        if es_duplicado_centro:
            importe_eur = valores_eur[0]
        else:
            importe_eur = sum(valores_eur)

        # AutoFX (col 13): DeGiro lo carga POR FILL, proporcional al importe
        # de cada ejecución parcial. En una orden multi-fill hay que SUMARLOS
        # (antes se tomaba max() → se perdía gasto deducible, Art. 35.1.b).
        # Si las filas son duplicados del mismo centro (mismo valor y total),
        # el AutoFX se repite → tomamos uno, igual que con `importe_eur`.
        if es_duplicado_centro:
            gastos_autofx = abs(parse_es(rows[0][13]))
        else:
            gastos_autofx = sum(abs(parse_es(row[13])) for row in rows)
        gastos_transacc = sum(abs(parse_es(row[14])) for row in rows
                              if parse_es(row[14]) != 0)
        # Tasas externas (ITF Espana, UK/HK Stamp Duty, French FTT...) del
        # CSV de Cuenta. Tributo inherente a la adquisicion/transmision
        # (Art. 35.1.b LIRPF + DGT V1989-21).
        order_id_consol = _extract_degiro_order_id(rows[0])
        ext_entry = external_fees_by_order.get(order_id_consol)
        if ext_entry:
            gastos_externos = ext_entry['total']
            gastos_externos_breakdown = dict(ext_entry['breakdown'])
        else:
            gastos_externos = Decimal('0')
            gastos_externos_breakdown = {'es': Decimal('0'), 'uk': Decimal('0'),
                                         'fr': Decimal('0'), 'hk': Decimal('0'),
                                         'other': Decimal('0')}
        gastos_eur = gastos_autofx + gastos_transacc + gastos_externos

        tipo = 'A' if parse_es(r0[6]) > 0 else 'T'

        return {
            'tipo':        tipo,
            'isin':        isin,
            'nombre':      nombre[:50],
            'fecha':       fecha,
            'cantidad':    cantidad,
            'importe_eur': importe_eur,
            'gastos_eur':  gastos_eur,
            # Desglose para auditoria y reporting (suma = gastos_eur):
            'gastos_broker':              gastos_transacc,
            'gastos_autofx':              gastos_autofx,
            'gastos_externos':            gastos_externos,
            'gastos_externos_breakdown':  gastos_externos_breakdown,
            'broker':      'DeGiro',
            '_order_id':   order_id_consol,
        }

    for rows in grupos_id.values():
        op = consolidar(rows)
        if op:
            operaciones.append(op)

    # ══════════════════════════════════════════════════════════════════════
    # Detector de eventos "CAMBIO DE PRODUCTO" — patrones_degiro.md §3.8
    # ══════════════════════════════════════════════════════════════════════
    # Identifica pares (compra+venta) en `sin_id_rows` cuyo (isin, fecha)
    # coincide con un evento "CAMBIO DE PRODUCTO" extraído del CSV de cuenta.
    #
    # Variante 3.8.a (cambio mercado puro, sin NON TRADEABLE):
    #   → Filtra ambas filas: no entran al FIFO (Art. 33 LIRPF).
    #   → Registra evento MARKET_TRANSFER en informe corporativas.
    #
    # Variante 3.8.b (NON TRADEABLE presente → rename/spin-off):
    #   → NO filtra aquí. Deja que los detectores posteriores
    #     (NAME_CHANGE, SPIN_OFF) procesen el evento con su doctrina propia.
    market_transfer_orders: set = set()
    market_transfers_eventos: list = []
    cambio_producto_sin_id_excluidos: set = set()  # índices de sin_id_rows
    cambios_prod_map = cambios_producto or {}
    if cambios_prod_map:
        # Indexar sin_id_rows por (isin, fecha_norm) y (qty firmado, precio) para
        # localizar las dos patas de cada evento CAMBIO DE PRODUCTO.
        sin_id_por_clave: dict = defaultdict(list)
        for idx_r, row in enumerate(sin_id_rows):
            if len(row) < 8:
                continue
            r_isin = (row[3] or '').strip()
            r_fecha = parse_date((row[0] or '').strip())
            if not r_isin or not r_fecha:
                continue
            try:
                r_qty = parse_es(row[6])
                r_precio = parse_es(row[7])
            except Exception:
                continue
            sin_id_por_clave[(r_isin, r_fecha)].append({
                'idx':    idx_r,
                'qty':    r_qty,
                'precio': r_precio,
                'nombre': (row[2] or '').strip(),
            })

        for (isin_cp, fecha_cp), ev_cp in cambios_prod_map.items():
            if ev_cp.get('tiene_non_tradeable'):
                # Delegar a NAME_CHANGE / SPIN_OFF — no filtramos aquí.
                continue
            qty_target = ev_cp.get('qty_compra') or ev_cp.get('qty_venta')
            if not qty_target or qty_target == 0:
                continue
            precio_target = ev_cp.get('precio') or Decimal('0')
            candidatos = sin_id_por_clave.get((isin_cp, fecha_cp), [])
            pata_compra = None
            pata_venta  = None
            for c in candidatos:
                if c['idx'] in cambio_producto_sin_id_excluidos:
                    continue
                # Match exacto en cantidad absoluta y precio unitario
                # (el patrón observado en 7 eventos históricos es preciso
                # al céntimo, sin tolerancia necesaria).
                if abs(abs(c['qty']) - qty_target) > Decimal('0.001'):
                    continue
                if precio_target > 0 and abs(c['precio'] - precio_target) > Decimal('0.001'):
                    continue
                if c['qty'] > 0 and pata_compra is None:
                    pata_compra = c
                elif c['qty'] < 0 and pata_venta is None:
                    pata_venta = c
                if pata_compra and pata_venta:
                    break
            if pata_compra and pata_venta:
                cambio_producto_sin_id_excluidos.add(pata_compra['idx'])
                cambio_producto_sin_id_excluidos.add(pata_venta['idx'])
                market_transfers_eventos.append({
                    'tipo_ca':          CA_MARKET_TRANSFER,
                    'fecha':            fecha_cp,
                    'isin':             isin_cp,
                    'isin_old':         isin_cp,
                    'isin_new':         isin_cp,
                    'nombre':           pata_compra['nombre'] or pata_venta['nombre'],
                    'cantidad':         qty_target,
                    'mercado_origen':   '',
                    'mercado_destino':  '',
                    'descripcion': (
                        f"Cambio de mercado intra-broker (marcador "
                        f"'CAMBIO DE PRODUCTO' en CSV cuenta): "
                        f"{int(qty_target)} acciones {isin_cp}. Sin "
                        f"alteración patrimonial (Art. 33 LIRPF)."
                    ),
                })

    # ══════════════════════════════════════════════════════════════════════
    # Detector de eventos "CAMBIO DE ISIN" — patrones_degiro.md §3.4.ter
    # ══════════════════════════════════════════════════════════════════════
    # Identifica pares (venta NON TRADEABLE ISIN_viejo + compra clean
    # ISIN_nuevo) en `sin_id_rows` cuya fecha coincide con un evento
    # "CAMBIO DE ISIN" extraído del CSV de cuenta. No es alteración
    # patrimonial (Art. 37.1.e LIRPF + reorganizaciones neutrales): los
    # lotes del ISIN viejo se migran al nuevo preservando coste, fecha
    # y `lote_id`. Operativamente se emite una fila SP con `isin_old`
    # y `isin_new` distintos para que el motor FIFO ejecute el rename.
    cambio_isin_eventos: list = []  # eventos generados → se transforman en sp_ops
    cambios_isin_map = cambios_isin or {}
    if cambios_isin_map:
        # Indexar sin_id_rows por (fecha_norm, isin) para localizar las dos
        # patas (ISIN viejo y ISIN nuevo) de cada evento.
        sin_id_por_isin_fecha = defaultdict(list)
        for idx_r, row in enumerate(sin_id_rows):
            if len(row) < 8:
                continue
            r_isin = (row[3] or '').strip()
            r_fecha = parse_date((row[0] or '').strip())
            if not r_isin or not r_fecha:
                continue
            try:
                r_qty = parse_es(row[6])
            except Exception:
                continue
            sin_id_por_isin_fecha[(r_isin, r_fecha)].append({
                'idx': idx_r, 'qty': r_qty,
                'nombre': (row[2] or '').strip(),
            })

        for fecha_cp, cambios_lista in cambios_isin_map.items():
            for ev in cambios_lista:
                qty_target = ev['qty']
                pata_viejo = None  # fila con ISIN viejo (venta o entrada NON TRADEABLE)
                pata_nuevo = None  # fila con ISIN nuevo
                for c in sin_id_por_isin_fecha.get((ev['isin_viejo'], fecha_cp), []):
                    if c['idx'] in cambio_producto_sin_id_excluidos:
                        continue
                    if abs(abs(c['qty']) - qty_target) <= Decimal('0.001'):
                        pata_viejo = c
                        break
                for c in sin_id_por_isin_fecha.get((ev['isin_nuevo'], fecha_cp), []):
                    if c['idx'] in cambio_producto_sin_id_excluidos:
                        continue
                    if abs(abs(c['qty']) - qty_target) <= Decimal('0.001'):
                        pata_nuevo = c
                        break
                if pata_viejo and pata_nuevo:
                    # Solo registrar el evento ISIN_CHANGE para el log
                    # corporativo (trazabilidad). NO excluimos las filas
                    # de sin_id_rows: los detectores estructurales
                    # existentes ya las procesan correctamente (en la
                    # práctica los XLSX históricos suelen usar el ISIN
                    # nuevo retroactivamente, así que las patas se
                    # asimilan a transferencias entre mercados o cambio
                    # de nombre — ver §3.4 y §3.4.bis del documento de
                    # patrones).
                    descripcion = (
                        f"Canje ISIN {ev['isin_viejo']} → {ev['isin_nuevo']} "
                        f"(ratio 1:1, marcador 'CAMBIO DE ISIN' en CSV cuenta). "
                        f"Sin alteración patrimonial (Art. 37.1.e LIRPF + "
                        f"canje reorganización neutral). Los lotes del ISIN "
                        f"viejo se mantienen bajo su clave histórica; al "
                        f"vender, el motor consume FIFO por ISIN del trade."
                    )
                    cambio_isin_eventos.append({
                        'tipo_ca':     CA_ISIN_CHANGE,
                        'fecha':       fecha_cp,
                        'nombre':      ev['nombre_nuevo'],
                        'isin_old':    ev['isin_viejo'],
                        'isin_new':    ev['isin_nuevo'],
                        'qty_old':     qty_target,
                        'qty_new':     qty_target,
                        'descripcion': descripcion,
                    })

    # ── Detector de corto forzado intra-broker — §3.9 patrones_degiro.md ──
    # Par compra+venta del mismo día, mismo ISIN, AMBOS con Order ID propio,
    # en centros de ejecución distintos, precio cercano (<1 % diferencia).
    # NO es cambio mercado real (esos no tienen Order ID y ya los filtramos
    # vía marcador "CAMBIO DE PRODUCTO" arriba). El motor FIFO valida luego
    # con inventario disponible: si la venta tiene lots, ignora el flag y
    # procesa como trade normal. Si no tiene lots, abre short y la compra
    # subsiguiente lo cubre.
    candidatos_mt = defaultdict(list)
    for order_id, rows in grupos_id.items():
        r0 = rows[0]
        isin_mt   = r0[3].strip()
        nombre_mt = r0[2].strip()
        if not isin_mt or is_option_isin(isin_mt) or is_option_name(nombre_mt):
            continue
        try:
            qty_signed = parse_es(r0[6])
            precio_u   = parse_es(r0[7])
            centro     = r0[5].strip() if len(r0) > 5 else ''
        except Exception:
            continue
        if qty_signed == 0 or precio_u == 0 or not centro:
            continue
        fecha_str = r0[0].strip()
        candidatos_mt[(fecha_str, isin_mt)].append({
            'order_id': order_id,
            'qty':      qty_signed,
            'precio':   precio_u,
            'centro':   centro,
            'nombre':   nombre_mt,
        })

    for (fecha_str, isin_mt), items in candidatos_mt.items():
        if len(items) < 2:
            continue
        compras = [x for x in items if x['qty'] > 0]
        ventas  = [x for x in items if x['qty'] < 0]
        for c in compras:
            for v in ventas:
                if c['centro'] == v['centro']:   # mismo mercado: no es candidato
                    continue
                if abs(c['qty'] + v['qty']) > Decimal('0.01'):
                    continue
                # Precio cercano (<1% diferencia).
                ref = max(abs(c['precio']), abs(v['precio']))
                if ref == 0:
                    continue
                diff_rel = abs(c['precio'] - v['precio']) / ref
                if diff_rel > Decimal('0.01'):
                    continue

                # Par con Order ID + qty simétrica + centros distintos +
                # precio cercano. NO puede ser cambio mercado real (ese
                # patrón se detecta vía marcador "CAMBIO DE PRODUCTO" en
                # CSV cuenta, sobre filas SIN Order ID — ya filtrado arriba).
                # Marcamos como corto tentativo. El motor FIFO valida con
                # inventario: si la venta tiene lots disponibles, ignora
                # los flags y procesa como trade normal; si no, abre short
                # y la compra cubre.
                for op in operaciones:
                    oid = op.get('_order_id')
                    if oid == v['order_id']:
                        op['_es_corto_apertura'] = True
                        op['_corto_motivo'] = (
                            f"Venta {abs(int(v['qty']))} {isin_mt} en "
                            f"{v['centro']} candidata a apertura de corto "
                            f"(par simétrico con compra en {c['centro']} "
                            f"el mismo día). El motor FIFO verifica con "
                            f"inventario disponible."
                        )
                    elif oid == c['order_id']:
                        op['_es_corto_cobertura'] = True
                        op['_corto_motivo'] = (
                            f"Compra {int(c['qty'])} {isin_mt} en "
                            f"{c['centro']} candidata a cobertura del "
                            f"corto abierto en {v['centro']} el {fecha_str}."
                        )
                market_transfers_eventos.append({
                    'tipo_ca':         CA_CORTO_FORZADO,
                    'fecha':           parse_date(fecha_str) or fecha_str,
                    'isin':            isin_mt,
                    'isin_old':        isin_mt,
                    'isin_new':        isin_mt,
                    'nombre':          c['nombre'],
                    'cantidad':        abs(c['qty']),
                    'mercado_origen':  v['centro'],
                    'mercado_destino': c['centro'],
                    'descripcion': (
                        f"Par candidato a corto forzado intra-broker: "
                        f"{abs(int(c['qty']))} acciones vendidas en "
                        f"{v['centro']} y compradas en {c['centro']} el "
                        f"mismo día (sin marcador 'CAMBIO DE PRODUCTO' en "
                        f"CSV cuenta — no es cambio mercado real). FIFO "
                        f"verifica con inventario disponible: si la venta "
                        f"tiene lots, procesa como trade normal; si no, "
                        f"abre short y la compra lo cubre (Art. 33 + "
                        f"35.1.b LIRPF, G/P al cierre del corto)."
                    ),
                })
                break  # un único par por (fecha, isin) para esta compra

    # ── Sin Order ID: pre-detectar splits/transferencias con precio ≠ 0 ────
    # Patrón: mismo ISIN, misma fecha, par entrada+salida con mismo importe EUR
    # absoluto y sin OrderID → split (qty distinta) o transferencia entre
    # mercados/cambio de nombre (qty igual, e.g. QuantumScape NSY→NDQ).
    # Debe ejecutarse ANTES del bucle de ejercicios para excluir estas filas.
    # Las filas marcadas como cambio mercado puro vía "CAMBIO DE PRODUCTO"
    # ya están excluidas del procesado de trades aquí (no entran al FIFO).
    sin_id_excluidos = set(cambio_producto_sin_id_excluidos)
    extra_splits = []          # splits/contrasplits detectados con precio ≠ 0

    sin_id_por_isin_fecha = defaultdict(list)
    for idx_r, row in enumerate(sin_id_rows):
        if len(row) < 15:
            continue
        isin_r   = row[3].strip()
        nombre_r = row[2].strip()
        if not isin_r or is_option_isin(isin_r) or is_option_name(nombre_r):
            continue
        try:
            precio_r    = parse_es(row[7].strip())
            total_eur_r = abs(parse_es(row[11].strip()))
            qty_r       = parse_es(row[6].strip())
            fecha_r     = parse_date_dt(row[0])
        except Exception:
            continue
        if precio_r == 0 or qty_r == 0 or not fecha_r:
            continue
        sin_id_por_isin_fecha[(isin_r, fecha_r)].append(
            (idx_r, row, float(qty_r), float(total_eur_r))
        )

    for (isin_r, fecha_r), items in sin_id_por_isin_fecha.items():
        entradas = [(i, r, q, t) for i, r, q, t in items if q > 0]
        salidas  = [(i, r, q, t) for i, r, q, t in items if q < 0]
        if not entradas or not salidas:
            continue
        total_eur_e = sum(t for _, _, _, t in entradas)
        total_eur_s = sum(t for _, _, _, t in salidas)
        if abs(total_eur_e - total_eur_s) > 0.05:   # tolerancia 0,05 EUR por redondeo FX
            continue
        # Par cuadrado (mismos EUR): excluir de procesamiento de ejercicios
        for i, _, _, _ in entradas + salidas:
            sin_id_excluidos.add(i)
        qty_new = sum(q for _, _, q, _ in entradas)
        qty_old = abs(sum(q for _, _, q, _ in salidas))
        nombre  = entradas[0][1][2].strip()
        fecha_str = fecha_r.strftime('%d/%m/%Y')
        if abs(qty_new - qty_old) < 0.01:
            # Misma cantidad → transferencia entre mercados / cambio de nombre.
            # Sin evento fiscal; excluir silenciosamente.
            pass
        else:
            tipo_ca = CA_SPLIT if qty_new > qty_old else CA_CONTRASPLIT
            extra_splits.append({
                'tipo_ca':    tipo_ca,
                'fecha':      fecha_str,
                'nombre':     nombre,
                'isin_old':   isin_r,
                'isin_new':   isin_r,
                'qty_old':    qty_old,
                'qty_new':    qty_new,
                'descripcion': f'{tipo_ca} directo (precio≠0) {qty_old:.0f}:{qty_new:.0f}',
            })

    # Lista local de rights issues ejercidos detectados — la usamos abajo para:
    #   (a) emitir un evento informativo CA_RIGHTS_EXERCISED por cada uno;
    #   (b) filtrar los falsos positivos CA_COMPLEX que detect_corporate_*
    #       crearía para el mismo NON-TRADEABLE.
    rights_exercised_detectados: list[dict] = []

    # ── Sin Order ID: detectar patrón NON TRADEABLE → ejercicio de derechos ─
    # En un rights issue DeGiro registra el ejercicio sin Order ID como:
    #   Día 1: +N [EMPRESA NON TRADEABLE] [ISIN_nt] precio>0 → pago suscripción
    #   Día 2: -N [EMPRESA NON TRADEABLE] [ISIN_nt] precio>0 → cancelación
    #          +N [EMPRESA ORDINARIA]     [ISIN_ord] precio>0 → entrega acciones
    # Tratamiento fiscal (Art. 37.1.a LIRPF): el ejercicio NO genera G/P.
    # → Excluir las 3 filas; generar A;ISIN_ord con importe del pago del Día 1.

    # Fase A — localizar suscripciones NON TRADEABLE (A;NT, precio>0, qty>0)
    nt_candidatos = {}   # isin_nt → {idx, qty, imp_eur, fecha_dt}
    for idx_r, row in enumerate(sin_id_rows):
        if idx_r in sin_id_excluidos or len(row) < 12:
            continue
        nombre_r = row[2].strip()
        isin_r   = row[3].strip()
        if not isin_r or not is_non_tradeable(nombre_r):
            continue
        try:
            precio_r  = parse_es(row[7].strip())
            qty_r     = parse_es(row[6].strip())
            imp_eur_r = abs(parse_es(row[11].strip()))
            fecha_r   = parse_date_dt(row[0])
        except Exception:
            continue
        if precio_r <= 0 or qty_r <= 0 or not fecha_r:
            continue
        # Si hay varios (raro), guardar el más tardío
        if isin_r not in nt_candidatos or fecha_r > nt_candidatos[isin_r]['fecha_dt']:
            nt_candidatos[isin_r] = {
                'idx': idx_r, 'qty': float(qty_r),
                'imp_eur': float(imp_eur_r), 'fecha_dt': fecha_r,
            }

    # Fase B — para cada candidato, buscar cancelación + entrega en fecha posterior
    if nt_candidatos:
        # Agrupar sin_id por fecha para búsqueda eficiente
        _sin_id_by_date = defaultdict(list)
        for idx_r, row in enumerate(sin_id_rows):
            if idx_r in sin_id_excluidos or len(row) < 12:
                continue
            fd = parse_date_dt(row[0])
            if fd:
                _sin_id_by_date[fd].append((idx_r, row))

        for isin_nt, sub in nt_candidatos.items():
            for days_fwd in range(1, 31):   # buscar en los 30 días siguientes
                target = sub['fecha_dt'] + timedelta(days=days_fwd)
                if target not in _sin_id_by_date:
                    continue
                cancelacion_idx = None
                entrega_info    = None
                for idx_r, row in _sin_id_by_date[target]:
                    isin_r   = row[3].strip()
                    nombre_r = row[2].strip()
                    try:
                        qty_r    = float(parse_es(row[6].strip()))
                        precio_r = parse_es(row[7].strip())
                    except Exception:
                        continue
                    if precio_r <= 0:
                        continue
                    if isin_r == isin_nt and qty_r < 0 and abs(abs(qty_r) - sub['qty']) < 0.01:
                        cancelacion_idx = idx_r
                    elif (isin_r != isin_nt
                          and not is_non_tradeable(nombre_r)
                          and qty_r > 0
                          and abs(qty_r - sub['qty']) < 0.01):
                        entrega_info = {'idx': idx_r, 'row': row, 'isin': isin_r}
                if cancelacion_idx is not None and entrega_info is not None:
                    # Patrón confirmado: excluir las 3 filas
                    sin_id_excluidos.add(sub['idx'])
                    sin_id_excluidos.add(cancelacion_idx)
                    sin_id_excluidos.add(entrega_info['idx'])
                    # Generar A;ISIN_ord con coste = pago original (Día 1)
                    nombre_ord = entrega_info['row'][2].strip()
                    operaciones.append({
                        'tipo':        'A',
                        'isin':        entrega_info['isin'],
                        'nombre':      nombre_ord[:50],
                        'fecha':       target.strftime('%d/%m/%Y'),
                        'cantidad':    Decimal(str(sub['qty'])),
                        'importe_eur': Decimal(str(round(sub['imp_eur'], 2))),
                        'gastos_eur':  Decimal('0'),
                        'broker':      'DeGiro',
                    })
                    # Registrar el evento informativo (sin acción manual)
                    rights_exercised_detectados.append({
                        'tipo_ca':     CA_RIGHTS_EXERCISED,
                        'isin':        isin_nt,           # ISIN del NIL/NON-TRADEABLE (a filtrar de COMPLEX)
                        'isin_ord':    entrega_info['isin'],
                        'nombre':      base_company_name(nombre_ord)[:50],
                        'fecha':       sub['fecha_dt'].strftime('%d/%m/%Y'),
                        'fecha_entrega': target.strftime('%d/%m/%Y'),
                        'qty':         sub['qty'],
                        'coste_eur':   round(sub['imp_eur'], 2),
                        'descripcion': (
                            f'Rights issue ejercido: {sub["qty"]:.0f} derechos × '
                            f'precio suscripción → {sub["qty"]:.0f} acciones nuevas '
                            f'(coste real {sub["imp_eur"]:.2f} EUR). Procesado '
                            f'automáticamente como compra A en {entrega_info["isin"]}; '
                            f'no requiere acción manual.'
                        ),
                    })
                    break  # encontrado; pasar al siguiente candidato

    # ── Sin Order ID: precio>0 y no-opción → ejercicio de opción ──────────
    # Las transmisiones de acciones por ejercicio/asignación de opciones no
    # llevan Order ID en DeGiro. Se procesan antes que el detector corporativo.
    n_ejercicio_sin_id = 0
    for idx_r, row in enumerate(sin_id_rows):
        if idx_r in sin_id_excluidos:
            continue
        if len(row) < 15:
            continue
        isin_r   = row[3].strip()
        nombre_r = row[2].strip()
        if is_option_isin(isin_r) or is_option_name(nombre_r):
            continue
        try:
            precio_r = parse_es(row[7].strip())
        except Exception:
            precio_r = Decimal('0')
        if precio_r > 0:
            op = consolidar([row])
            if op:
                op['_sin_orden'] = True   # posible ejercicio — marcado para el CSV
                operaciones.append(op)
                n_ejercicio_sin_id += 1

    descartadas['corporativa'] += (len(sin_id_rows) - len(sin_id_excluidos)) - n_ejercicio_sin_id

    # ── Detectar acciones corporativas (filas sin Order ID) ───────────────
    (splits, isin_chgs, derechos, complejos,
     posibles_liberadas, name_changes) = detect_corporate_actions_degiro(raw_rows)
    # Los cambios de ISIN detectados vía marcador "CAMBIO DE ISIN" del CSV
    # cuenta se añaden al log corporativas para trazabilidad, pero NO se
    # convierten en filas SP automáticas. La migración real de lotes
    # ISIN viejo → nuevo queda como follow-up: requiere coordinación con
    # los XLSX históricos (que en la práctica suelen exportarse ya con
    # el ISIN nuevo retroactivamente desde el broker), evitando crear
    # huérfanos o duplicar lotes.
    existentes_isin_chg = {(e.get('fecha'), e.get('isin_old'), e.get('isin_new'))
                            for e in (splits + isin_chgs)}
    for ev in cambio_isin_eventos:
        key = (ev.get('fecha'), ev.get('isin_old'), ev.get('isin_new'))
        if key not in existentes_isin_chg:
            isin_chgs.append(ev)
            existentes_isin_chg.add(key)
    todos_splits = splits + extra_splits
    sp_ops = [build_sp_row(ev) for ev in todos_splits]

    # Filtrar falsos positivos COMPLEX que en realidad son rights issues
    # ejercidos. detect_corporate_actions_degiro marca el "+N XXX-NIL
    # NON-TRADEABLE precio>0" como COMPLEX porque no encuentra contrapartida
    # limpia. Aquí ya hemos detectado el patrón completo (cancelación + entrega
    # de acciones ordinarias) y generado la fila A correcta — el aviso COMPLEX
    # sobra y solo confunde al usuario.
    rights_isins = {ev['isin'] for ev in rights_exercised_detectados}
    rights_dates = {ev['fecha'] for ev in rights_exercised_detectados}
    if rights_isins:
        complejos = [
            c for c in complejos
            if not (c.get('isin') in rights_isins
                    and c.get('fecha') in rights_dates
                    and 'NON TRADEABLE' in (c.get('descripcion','')).upper())
        ]

    _enrich_with_instrument_type(operaciones, broker='DEGIRO')

    # Las ops DERIVATIVE (Factor SG, Turbos, Mini, KO, etc.) SÍ se procesan
    # con el motor FIFO igual que acciones/ETFs (FIFO por ISIN). El emisor
    # del informe las separa visualmente en su propia sección con casillas
    # 1624-1654 clave 4 (NO en G/P de acciones).

    # Bonos individuales: si el ISIN aparece con eventos "Cupón corrido"
    # en el CSV de Cuenta, lo marcamos como BOND y sumamos el cupón
    # corrido al coste (compra) o lo restamos del importe (venta) — Enfoque
    # Art. 25.2 LIRPF mayoritario en práctica retail. Los cupones cobrados
    # durante la tenencia son RCM (casilla 0027) y se gestionan aparte.
    if bond_data and bond_data.get('isins'):
        bond_isins = bond_data['isins']
        cupones_corridos = bond_data.get('cupones_corridos_por_orden', {})
        for op in operaciones:
            if op.get('isin') in bond_isins:
                op['instrument_type'] = 'BOND'
                op['instrument_type_reason'] = (
                    "ISIN con eventos 'Cupón corrido' en DeGiro_Cuenta "
                    "— bono individual, casillas 0027/0031"
                )
                op['instrument_type_unknown'] = False
                # Aplicar cupón corrido al coste/importe del trade — DGT V1732-10:
                # - cupón corrido pagado al COMPRAR  → SUMA al valor de adquisición.
                # - cupón corrido cobrado al VENDER  → SUMA al valor de transmisión.
                # Resultado: la diferencia neta de cupón corrido (cobrado − pagado)
                # se queda dentro del rendimiento de transmisión casilla 0031.
                # Match por order_id (la columna 16 del CSV de Transacciones).
                order_id = op.get('_order_id', '') or op.get('order_id', '')
                cupon_corrido = cupones_corridos.get(order_id, Decimal('0'))
                if cupon_corrido != 0:
                    op['importe_eur'] = (op.get('importe_eur', Decimal('0'))
                                         + abs(cupon_corrido))
                    op['_cupon_corrido_eur'] = abs(cupon_corrido)

    # Detector general de shorts DeGiro por estado de inventario cronológico
    # (complementa al detector intra-broker de Klepierre que solo cubre el
    # caso de pares simétricos mismo día/mercados distintos). Cubre el
    # daytrader que abre short hoy y cubre días/semanas después.
    # Idempotente: respeta los flags ya emitidos arriba.
    _detect_shorts_degiro_por_inventario(operaciones)

    return operaciones, sp_ops, descartadas, {
        'splits':             todos_splits,
        'isin_chgs':          isin_chgs,
        'derechos':           derechos,
        'complejos':          complejos,
        'posibles_liberadas': posibles_liberadas,
        'name_changes':       name_changes,
        'rights_exercised':   rights_exercised_detectados,
        'market_transfers':   market_transfers_eventos,
    }


# ── Parser IBKR ────────────────────────────────────────────────────────────


def _build_ibkr_symbol_isin_map(filepath: str) -> dict:
    """Construye mapping symbol → {isin, description, type, asset_category}
    desde la sección 'Financial Instrument Information' del Activity Statement
    IBKR.

    La sección Trades NO incluye ISIN, descripción ni tipo como columnas. Sin
    este mapping los lotes IBKR aparecerían sin ISIN (no se consolidarían con
    DeGiro en el FIFO multi-broker), con el ticker como nombre (ej. "VISe"
    en vez de "VISCOFAN SA") y sin información para clasificar acción/ETF.

    El campo Type del FII es la fuente más fiable para distinguir COMMON/ETF/
    PREFERRED/RIGHT — usado por instrument_classifier para decidir si una
    transmisión va al bloque 0326-0340 (acciones) o 2224-2236 (ETFs UCITS,
    Renta 2025+).

    El campo Asset Category permite filtrar instrumentos NO soportados
    (Cryptocurrency, Bonds, Futures, Warrants, CFDs, Structured Products,
    Mutual Funds…) que en otro caso caerían en G/P de acciones cuando deberían
    ir a otras casillas (1800-1806 cripto, 1624-1654 derivados, etc.).

    El campo Symbol puede listar varios alias separados por coma (ej. 'NOV, NOVd').
    Devuelve un dict {symbol_alias: {'isin': isin, 'description': description,
    'type': type, 'asset_category': asset_category}}. Incluye TODAS las
    categorías; el filtrado se hace en parse_ibkr.
    """
    if not os.path.exists(filepath):
        return {}

    SECTION = 'Financial Instrument Information'
    out = {}
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        current_header = None
        for row in reader:
            if not row or row[0] != SECTION:
                continue
            if row[1] == 'Header':
                current_header = row
                continue
            if row[1] != 'Data' or not current_header:
                continue
            h = current_header
            def col(name):
                return row[h.index(name)].strip() if name in h else ''
            asset = col('Asset Category')
            symbol_field = col('Symbol')
            isin = col('Security ID')
            description = col('Description')
            type_field = col('Type')
            # Para bonos: Maturity (fecha de vencimiento) e Issuer permiten
            # inferir la amortización al vencimiento cuando IBKR no incluye
            # la sección Bond Maturity en el statement exportado.
            maturity = col('Maturity')
            issuer = col('Issuer')
            if not symbol_field:
                continue
            # Cryptocurrency: IBKR no asigna ISIN a las cripto (BTC, ETH,
            # LTC, BCH, SOL, ADA, XRP, DOGE, AVAX, LINK, SUI vía Paxos /
            # Zerohash). Generamos un ISIN sintético `CRYPTO:<SYMBOL>` para
            # que el motor FIFO funcione consolidando por símbolo. El campo
            # Type del FII también suele venir vacío para cripto.
            if asset == 'Cryptocurrency':
                if not isin:
                    sym_clean = symbol_field.split(',')[0].strip().upper()
                    isin = f"CRYPTO:{sym_clean}"
                if not type_field:
                    type_field = 'CRYPTOCURRENCY'
            elif not isin or len(isin) != 12:
                # Sin ISIN de 12 chars. Típico en small-caps/ADRs US que
                # reportan un CUSIP (9 chars) en Security ID, o tras un cambio
                # de CUSIP por reverse split. ANTES se descartaban como "no
                # soportados", pero eso dejaba el trade con ISIN vacío y el FIFO
                # (A) lo marcaba como venta huérfana y (B) — peor — colapsaba
                # TODOS los instrumentos sin ISIN a la misma clave "", cruzando
                # lotes de valores distintos entre sí. Caso real NCNA/BNKK 2025:
                # cash-in-lieu de la fracción tras un contrasplit con cambio de
                # CUSIP. Generamos una clave sintética por SÍMBOLO (estable
                # frente al cambio de CUSIP — el ticker se mantiene), igual que
                # con cripto. Consolida la actividad del instrumento dentro de
                # IBKR; no cruza con otros brokers (requeriría ISIN real), pero
                # estos small-caps rara vez están en dos brokers.
                primary_alias = symbol_field.split(',')[0].strip().upper()
                if not primary_alias:
                    continue
                isin = f"SYM:{primary_alias}"
            for alias in (s.strip() for s in symbol_field.split(',')):
                if alias:
                    out.setdefault(alias, {
                        'isin': isin,
                        'description': description,
                        'type': type_field,
                        'asset_category': asset,
                        'maturity': maturity,
                        'issuer': issuer,
                    })
    return out


# ── Asset Categories IBKR soportadas vs no soportadas ────────────────────────

# Categorías que el motor procesa con FIFO (transmisiones simples).
# Cryptocurrency se incluye porque IBKR lo trata como un trade más con
# Symbol/Quantity/Price y se puede aplicar FIFO por ISIN como cualquier
# otro instrumento. La separación visual en el PDF se hace por
# instrument_type ('STOCK' / 'ETF' / 'CRYPTO').
_IBKR_ACCEPTED_CATEGORIES = ('Stocks', 'ETF', 'Cryptocurrency', 'Bonds')

# Pipelines especializados que ya manejan estas categorías por separado
# (no se consideran "no soportadas", solo no van por parse_ibkr Trades).
_IBKR_HANDLED_ELSEWHERE = (
    'Equity and Index Options',  # parse_ibkr_opciones
    'Forex',                     # parse_ibkr_fx_pl
)

# Categorías NO soportadas — generan warning + descartadas con detalle.
# Cada categoría apunta a la casilla AEAT correcta para que el usuario
# las declare a mano.
_IBKR_UNSUPPORTED_CATEGORY_HINTS = {
    'Futures': {
        'casilla': '1624-1654 clave 4',
        'apartado': 'F2 — Otros elementos patrimoniales',
        'nota': 'Mismo bloque que opciones cerradas/expiradas (Art. 33 LIRPF).',
    },
    'Warrants': {
        'casilla': '1624-1654 clave 4',
        'apartado': 'F2 — Otros elementos patrimoniales',
        'nota': 'Derivados cotizados; DGT consulta 1038/2001.',
    },
    'CFDs': {
        'casilla': '1624-1654 clave 4',
        'apartado': 'F2 — Otros elementos patrimoniales',
        'nota': 'Contratos por diferencia.',
    },
    'Structured Products': {
        'casilla': '1624-1654 clave 4',
        'apartado': 'F2 — Otros elementos patrimoniales',
        'nota': 'ETNs, certificados, notas estructuradas — productos de deuda con payoff vinculado.',
    },
    'Mutual Funds': {
        'casilla': 'Bloque IIC con retención (distinto de 2224-2236)',
        'apartado': 'F2 — Transmisión de IIC (sociedades y fondos)',
        'nota': 'Fondos de inversión NO cotizados; la gestora suele retener al 19%.',
    },
}


def _ibkr_classify_asset_category(asset_cat: str) -> str:
    """Devuelve 'accepted' (procesar normal), 'elsewhere' (otro pipeline) o
    'unsupported' (descartar con warning) para una Asset Category de IBKR."""
    if not asset_cat or asset_cat in _IBKR_ACCEPTED_CATEGORIES:
        return 'accepted'
    if asset_cat in _IBKR_HANDLED_ELSEWHERE:
        return 'elsewhere'
    return 'unsupported'


# Peg fijo del Dirham UAE al USD desde 1997 (Banco Central de los EAU).
# El BCE no publica AED, pero el cross-rate vía USD es estable y oficial.
_AED_PER_USD = Decimal('3.6725')


def _ibkr_eur_per_unit(date_iso: str, currency: str) -> Decimal | None:
    """Devuelve EUR por unidad de la divisa para la fecha dada.

    Para divisas en BCE (USD, GBP, DKK, CHF, HKD, PLN…) consulta `_ECB_CACHE`
    con fallback de hasta 7 días anteriores (fines de semana/festivos).
    Para AED, no publicada por el BCE, usa el peg fijo al USD (3.6725 AED/USD)
    y el USD→EUR del BCE.

    Args:
        date_iso: fecha en formato YYYY-MM-DD.
        currency: código ISO de la divisa.

    Returns:
        Decimal con EUR/unidad o None si no se puede resolver.
    """
    if currency == 'EUR':
        return Decimal('1')

    # Asegurar que el cache de disco está cargado (idempotente).
    _ecb_cache_load_disk()

    def _ecb_lookup(cur: str) -> Decimal | None:
        # Tipo exacto del día
        rate = _ECB_CACHE.get((date_iso, cur))
        if rate:
            return rate
        # Fallback: hasta 7 días anteriores
        try:
            base = datetime.strptime(date_iso, '%Y-%m-%d')
            for delta in range(1, 8):
                prev = (base - timedelta(days=delta)).strftime('%Y-%m-%d')
                rate = _ECB_CACHE.get((prev, cur))
                if rate:
                    return rate
        except Exception:
            pass
        return None

    if currency == 'AED':
        usd_per_eur_inv = _ecb_lookup('USD')   # EUR por 1 USD
        if usd_per_eur_inv is None:
            return None
        # 1 AED = (1/3.6725) USD → EUR/AED = (1/3.6725) × EUR/USD
        return usd_per_eur_inv / _AED_PER_USD

    return _ecb_lookup(currency)


def _build_ibkr_transaction_fees(filepath: str) -> dict:
    """Lee la sección 'Transaction Fees' del Activity Statement IBKR y devuelve
    un lookup `(symbol, date_iso, qty_abs) → fee_eur` para sumar al `gastos_eur`
    del trade correspondiente.

    Caso típico: **Tasa Tobin española (ITF)** sobre compra de acciones
    españolas con capitalización > 1.000M EUR. Aparece como una fila separada
    en lugar de incluirse en `Comm/Fee` del trade. Fiscalmente forma parte del
    coste de adquisición (Art. 35 LIRPF), por lo que tiene que sumarse.

    Otros casos posibles: SEC fee, FINRA TAF, Stamp Duty (UK), French FTT, etc.

    Multi-fee por trade se acumula (sum por key).

    Estructura devuelta (paralela a `_build_degiro_external_fees`):
        {(symbol, date_iso, qty_abs): {
            'total': Decimal,
            'breakdown': {'es': X, 'uk': Y, 'fr': Z, 'hk': W, 'other': V},
        }, ...}
    """
    def _empty_breakdown():
        return {'es': Decimal('0'), 'uk': Decimal('0'), 'fr': Decimal('0'),
                'it': Decimal('0'), 'hk': Decimal('0'), 'other': Decimal('0')}

    lookup: dict = {}
    if not os.path.exists(filepath):
        return lookup

    fees_header = None
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 4 or row[0] != 'Transaction Fees':
                continue
            row_type = row[1].strip()
            if row_type == 'Header':
                fees_header = row
                continue
            if row_type != 'Data' or not fees_header:
                continue
            h = fees_header

            def col_f(name):
                return row[h.index(name)].strip() if name in h else ''

            try:
                asset = col_f('Asset Category')
                if asset not in ('Stocks', 'ETF'):
                    continue
                symbol = col_f('Symbol')
                if not symbol or symbol == 'Total':
                    continue
                date_str = col_f('Date/Time')
                qty_str = col_f('Quantity')
                amount_str = col_f('Amount')
                currency = col_f('Currency')
                desc_raw = col_f('Description')
                if amount_str in ('Amount', 'Total') or not amount_str:
                    continue
                amount = abs(Decimal(amount_str.replace(',', '') or '0'))
                if amount == 0:
                    continue
                qty_abs = (abs(Decimal(qty_str.replace(',', '')))
                           if qty_str else Decimal('0'))
                date_iso = (date_str.split(',')[0].strip()
                            if date_str else '')
                # Convertir a EUR si la fee viene en divisa local.
                if currency and currency != 'EUR':
                    rate = _ibkr_eur_per_unit(date_iso, currency)
                    if rate is None:
                        continue  # sin tipo BCE → ignorar (no inventar)
                    amount = (amount * rate).quantize(
                        Decimal('0.01'), ROUND_HALF_UP)
                jur = _classify_external_fee_jurisdiction(desc_raw.upper())
                key = (symbol, date_iso, qty_abs)
                entry = lookup.setdefault(
                    key,
                    {'total': Decimal('0'), 'breakdown': _empty_breakdown()},
                )
                entry['total'] += amount
                entry['breakdown'][jur] += amount
            except (ValueError, IndexError, KeyError):
                pass

    return lookup


def _synth_ibkr_scrip_chains(corp_events: list, operaciones: list) -> list:
    """Detecta cadenas de scrip dividend IBKR y sintetiza una fila A en la matriz.

    Patrón Viscofan-style (caso real 2025):
        1. `VIS(ES_MATRIZ) Dividend Rights Issue 1 for 1 (VIS.D, ..., ES_DERECHO)`
           → recibe N derechos gratis del derecho-ISIN (qty>0)
        2. `VIS.D(ES_DERECHO) Expire Dividend Right (VIS.D, ..., ES_DERECHO)`
           → expiran -N derechos (qty<0)
        3. `VIS.D(ES_DERECHO) Expire Dividend Right (VIS.I, ..., ES_INTERIM)`
           → recibe M acciones interim por canje (qty>0)
        4. `VIS.I(ES_INTERIM) Merged(Acquisition) WITH ES_MATRIZ ... (VIS, ..., ES_MATRIZ)`
           → recibe M acciones matriz por la fusión (qty>0)
        5. `VIS.I(ES_INTERIM) Merged(Acquisition) WITH ES_MATRIZ ... (VIS.I, ..., ES_INTERIM)`
           → desaparecen -M acciones interim (qty<0)

    Cuando se detecta la cadena completa: elimina de `operaciones` las filas A
    con isin=ES_DERECHO (derechos comprados; los gratis no estaban) y añade
    una fila A nueva en ES_MATRIZ con qty=M y coste = sum(importes derechos
    comprados + gastos). El motor FIFO la trata como compra normal.

    Args:
        corp_events: lista de eventos Corporate Actions (con `descripcion_full`).
        operaciones: lista de A/T parseadas (modificada in-place).

    Returns:
        Lista de cadenas detectadas (info para reporting); cada item:
            {matriz_isin, derecho_isin, interim_isin, qty_acciones, coste_eur,
             nombre_matriz, fecha}
    """
    RE_ISSUE = re.compile(
        r'\(([A-Z0-9]{12})\)\s+DIVIDEND RIGHTS ISSUE.*?'
        r'\(([\w\.\-]+),\s*([^,]+?),\s*([A-Z0-9]{12})\)',
        re.IGNORECASE,
    )
    RE_EXPIRE = re.compile(
        r'\(([A-Z0-9]{12})\)\s+EXPIRE DIVIDEND RIGHT\s*'
        r'\(([\w\.\-]+),\s*([^,]+?),\s*([A-Z0-9]{12})\)',
        re.IGNORECASE,
    )
    RE_MERGED = re.compile(
        r'\(([A-Z0-9]{12})\)\s+MERGED\(ACQUISITION\)\s+WITH\s+([A-Z0-9]{12}).*?'
        r'\(([\w\.\-]+),\s*([^,]+?),\s*([A-Z0-9]{12})\)',
        re.IGNORECASE,
    )

    # Indexar eventos por tipo para lookup rápido. Conservamos referencia al
    # `ev` original para poder eliminarlos de `corp_events` cuando la cadena
    # se sintetiza correctamente — así NO aparecen como CA_COMPLEX en el
    # informe_corporativas (revisión manual).
    issues = []            # [(matriz_isin, derecho_isin, fecha, ev)]
    expire_canjes = []     # [(derecho_isin, interim_isin, qty, fecha, ev)]
    expire_consumes = []   # [(derecho_isin, qty, fecha, ev)] — Expire qty<0
    merges_in_matriz = []  # [(interim, matriz_isin, nombre_matriz, qty, fecha, ev)]
    merges_out = []        # [(interim_isin, qty, fecha, ev)] — Merged qty<0
    for ev in corp_events:
        desc = ev.get('descripcion_full', '') or ev.get('descripcion', '')
        qty = ev.get('cantidad', 0) or 0
        fecha = ev.get('fecha', '')
        m_iss = RE_ISSUE.search(desc)
        if m_iss and qty > 0:
            issues.append((m_iss.group(1), m_iss.group(4), fecha, ev))
            continue
        m_exp = RE_EXPIRE.search(desc)
        if m_exp:
            derecho = m_exp.group(1)
            target_isin = m_exp.group(4)
            if qty > 0 and target_isin != derecho:
                expire_canjes.append(
                    (derecho, target_isin, qty, fecha, ev))
            elif qty < 0 and target_isin == derecho:
                expire_consumes.append((derecho, qty, fecha, ev))
            continue
        m_mer = RE_MERGED.search(desc)
        if m_mer:
            interim = m_mer.group(1)
            destino_isin = m_mer.group(2)
            destino_paren = m_mer.group(5)
            if qty > 0:
                nombre_destino = m_mer.group(4).strip()
                if destino_paren == destino_isin:
                    merges_in_matriz.append(
                        (interim, destino_isin, nombre_destino, qty, fecha, ev))
            elif qty < 0 and destino_paren == interim:
                merges_out.append((interim, qty, fecha, ev))
            continue

    cadenas = []
    consumed_derecho_isins = set()
    consumed_event_ids: set = set()  # id() de los eventos a quitar de corp_events

    for matriz_isin, derecho_isin, fecha_issue, ev_issue in issues:
        if derecho_isin in consumed_derecho_isins:
            continue
        canjes = [c for c in expire_canjes if c[0] == derecho_isin]
        if not canjes:
            continue
        _, interim_isin, _qty_interim, fecha_canje, ev_canje = canjes[0]
        merges = [m for m in merges_in_matriz
                  if m[0] == interim_isin and m[1] == matriz_isin]
        if not merges:
            continue
        _, _, nombre_matriz, qty_acciones, fecha_merge, ev_merge = merges[0]

        # Calcular coste de los derechos comprados (los gratis tienen coste 0
        # y no afectan a la suma).
        ops_derechos = [
            op for op in operaciones
            if op.get('isin') == derecho_isin and op.get('tipo') == 'A'
        ]
        coste_total = sum(
            (op['importe_eur'] + op['gastos_eur'] for op in ops_derechos),
            Decimal('0'),
        )

        # Eliminar las filas A de derechos consumidos.
        operaciones[:] = [
            op for op in operaciones
            if not (op.get('isin') == derecho_isin and op.get('tipo') == 'A')
        ]

        # Sintetizar fila A en la matriz con marca scrip MIXTO.
        nombre = f"{nombre_matriz} [🎁 LIBERADA scrip MIXTO IBKR]"
        operaciones.append({
            'tipo':        'A',
            'isin':        matriz_isin,
            'nombre':      nombre[:50],
            'fecha':       fecha_merge or fecha_canje or fecha_issue,
            'cantidad':    Decimal(str(qty_acciones)),
            'importe_eur': coste_total,
            'gastos_eur':  Decimal('0'),
            'broker':      'IBKR',
        })

        # Marcar los 5 eventos de la cadena como consumidos para que el
        # informe_corporativas NO los pinte como CA_COMPLEX:
        #   1. Issue
        #   2. Expire qty>0 (canje a interim)
        #   3. Expire qty<0 (consume los derechos del mismo ISIN)
        #   4. Merged qty>0 (recibe acciones matriz)
        #   5. Merged qty<0 (desaparecen acciones interim)
        consumed_event_ids.add(id(ev_issue))
        consumed_event_ids.add(id(ev_canje))
        consumed_event_ids.add(id(ev_merge))
        for _d, _q, _f, ev in expire_consumes:
            if _d == derecho_isin:
                consumed_event_ids.add(id(ev))
        for _i, _q, _f, ev in merges_out:
            if _i == interim_isin:
                consumed_event_ids.add(id(ev))

        consumed_derecho_isins.add(derecho_isin)
        cadenas.append({
            'matriz_isin':   matriz_isin,
            'derecho_isin':  derecho_isin,
            'interim_isin':  interim_isin,
            'nombre_matriz': nombre_matriz,
            'qty_acciones':  qty_acciones,
            'coste_eur':     coste_total,
            'fecha':         fecha_merge,
        })

    # Filtrar corp_events in-place — los eventos de cadenas resueltas dejan
    # de existir como "complex pendiente de revisión".
    if consumed_event_ids:
        corp_events[:] = [
            ev for ev in corp_events
            if id(ev) not in consumed_event_ids
        ]

    return cadenas


def parse_ibkr(filepath, bond_data: dict | None = None):
    """
    Parsea Activity Statement de IBKR.
    Secciones leídas: Trades (A/T) y Corporate Actions (SP).

    Exportar desde IBKR:
      Reports > Statements > Activity Statement
      Secciones: Trades + Corporate Actions (ambas)
      Base Currency: EUR  ← importante
      Período: 01/01/YYYY - 31/12/YYYY
      Formato: CSV

    Args:
        filepath: ruta al CSV del Activity Statement.
        bond_data: opcional, dict producido por `_build_ibkr_bond_data` con
            el accrued interest pagado/cobrado por (símbolo, fecha) — se
            aplica al coste/precio según DGT V1732-10. Si es None se llama
            internamente.
    """
    if not os.path.exists(filepath):
        return [], [], {'opcion': 0, 'divisa_no_eur': 0, 'otros': 0}, {}
    if bond_data is None:
        bond_data = _build_ibkr_bond_data(filepath)

    operaciones = []
    sp_ops      = []
    descartadas = {
        'opcion': 0, 'divisa_no_eur': 0, 'otros': 0,
        # Lista de trades de Asset Category no soportada (cripto, bonds,
        # futures, warrants, CFDs, structured, mutual funds). Cada entrada:
        # {symbol, isin, nombre, fecha, cantidad, importe_eur, asset_category, broker}
        'categoria_no_soportada': [],
    }
    corp_events = []
    trades_header = None
    corp_header   = None

    # Mapping symbol → ISIN desde Financial Instrument Information.
    # Necesario porque la sección Trades NO incluye ISIN como columna.
    sym_isin_map = _build_ibkr_symbol_isin_map(filepath)

    # Lookup de Transaction Fees adicionales (Tasa Tobin, Stamp Duty, FTT…)
    # indexado por (symbol, date_iso, qty_abs). Se suman a gastos_eur del trade.
    fees_lookup = _build_ibkr_transaction_fees(filepath)

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 5:
                continue

            # ── Trades ────────────────────────────────────────────────────
            if row[0] == 'Trades' and row[1] == 'Header':
                trades_header = row
                continue

            if row[0] == 'Trades' and row[1] == 'Data' and trades_header:
                h = trades_header
                def col_t(name):
                    return row[h.index(name)] if name in h else ''
                # IBKR añade filas ClosedLot/SubTotal/Total a la sección Trades
                # cuando el usuario exporta con "lot detail". Solo las filas
                # 'Order' son operaciones reales; las ClosedLot duplicarían los
                # lotes de apertura como compras fantasma a coste 0 → plusvalías
                # ficticias del 100%. Aceptamos únicamente DataDiscriminator='Order'.
                _disc = col_t('DataDiscriminator')
                if _disc and _disc != 'Order':
                    continue
                try:
                    asset_cat = col_t('Asset Category')
                    # 'Option' (incluido 'Equity and Index Options' y 'Futures
                    # Options') → parse_ibkr_opciones.
                    # 'Futures' (futuros lineales) → parse_ibkr_futures.
                    # 'Forex' → parse_ibkr_fx_pl.
                    # Las tres categorías van por pipelines especializados, no
                    # por el bloque de trades normales (acciones/ETF/etc.).
                    if ('Option' in asset_cat
                            or asset_cat in ('Forex', 'Futures')):
                        descartadas['opcion'] += 1
                        continue
                    # Asset Categories aceptadas en el motor: Stocks, ETF,
                    # Cryptocurrency (Paxos/Zerohash) y Bonds (con cupón
                    # corrido vía secciones Bond Interest Paid/Received).
                    # Las demás (Warrants, CFDs, Structured Products, Mutual
                    # Funds) caen al filtro defensivo `_ibkr_classify_asset_category`.
                    if asset_cat not in ('Stocks', 'ETF', 'Cryptocurrency', 'Bonds', ''):
                        descartadas['otros'] += 1
                        continue

                    qty_str = col_t('Quantity')
                    if not qty_str or qty_str in ('Quantity', 'Total'):
                        continue

                    currency = col_t('Currency')
                    symbol   = col_t('Symbol')
                    date_str = col_t('Date/Time')
                    proceeds = col_t('Proceeds')
                    comm_fee = col_t('Comm/Fee')
                    isin_col = col_t('ISIN')

                    quantity = Decimal(qty_str.replace(',', ''))
                    importe  = abs(Decimal(proceeds.replace(',', '') or '0'))
                    gastos   = abs(Decimal(comm_fee.replace(',', '') or '0'))

                    fecha = parse_date(date_str)
                    if not fecha:
                        continue

                    # Conversión a EUR para trades en divisa local. IBKR
                    # multidivisa NO convierte cuando hay saldo en la divisa
                    # de la operación, así que las cifras vienen en local.
                    # Usamos BCE del día (y peg AED→USD para AED).
                    if currency != 'EUR':
                        # Date/Time IBKR: "2025-12-22, 05:08:13" → ISO YYYY-MM-DD
                        date_iso = (date_str.split(',')[0].strip()
                                    if date_str else '')
                        rate = _ibkr_eur_per_unit(date_iso, currency)
                        if rate is None:
                            descartadas['divisa_no_eur'] += 1
                            print(f"  [AVISO IBKR] {symbol} en {currency} "
                                  f"({date_iso}) — sin tipo de cambio BCE, "
                                  f"fila descartada")
                            continue
                        importe = (importe * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)
                        gastos = (gastos * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)

                    # Si la fila Trades no trae ISIN (lo habitual en IBKR),
                    # resolverlo desde el mapping de Financial Instrument Information.
                    fii_entry = sym_isin_map.get(symbol) or {}
                    isin_col_clean = (isin_col or '').strip()
                    if len(isin_col_clean) == 12:
                        isin_resolved = isin_col_clean
                    else:
                        isin_resolved = fii_entry.get('isin', '')
                    # Último recurso: ni la fila ni el FII dan un identificador
                    # (símbolo ausente del FII, o este sin ISIN/CUSIP). Clave
                    # sintética por símbolo para no orfanar la operación NI
                    # colapsarla con otros instrumentos sin ISIN bajo clave "".
                    if not isin_resolved and symbol:
                        isin_resolved = f"SYM:{symbol.strip().upper()}"
                    # Nombre canónico desde FII Description ("VISCOFAN SA"),
                    # con fallback al símbolo IBKR ("VISe") si no hay descripción.
                    nombre_canon = fii_entry.get('description') or symbol
                    fii_type = fii_entry.get('type') or ''
                    fii_asset_cat = fii_entry.get('asset_category') or ''

                    # Filtro defensivo por Asset Category. Si IBKR clasifica el
                    # instrumento como Cryptocurrency / Bonds / Futures / Warrants
                    # / CFDs / Structured Products / Mutual Funds, NO va al motor
                    # de acciones/ETFs (caería en casillas 0326-0340 cuando
                    # debería ir a 1800-1806, 1624-1654, etc.). Se acumula en
                    # `descartadas['categoria_no_soportada']` para banner del PDF.
                    cat_status = _ibkr_classify_asset_category(fii_asset_cat)
                    if cat_status == 'unsupported':
                        descartadas['categoria_no_soportada'].append({
                            'symbol':         symbol,
                            'isin':           isin_resolved,
                            'nombre':         nombre_canon[:60],
                            'fecha':          fecha,
                            'cantidad':       float(quantity),
                            'importe_eur':    float(importe),
                            'asset_category': fii_asset_cat,
                            'broker':         'IBKR',
                        })
                        continue
                    if cat_status == 'elsewhere':
                        # Categorías procesadas por otro pipeline
                        # (parse_ibkr_opciones, parse_ibkr_fx_pl). Saltar para
                        # evitar duplicar la operación en el motor de acciones.
                        continue

                    # Clasificación STOCK/ETF/CRYPTO para Renta 2025+ — IBKR FII
                    # Type es la fuente más fiable. Si el classifier devuelve
                    # un tipo neutro pero el Asset Category es Cryptocurrency,
                    # forzamos a CRYPTO (el FII Type para cripto a veces viene
                    # vacío en algunos statements antiguos).
                    instr_type, instr_reason, instr_unknown = classify_isin(
                        isin_resolved, nombre_canon, broker='IBKR',
                        ibkr_type=fii_type,
                    )
                    if fii_asset_cat == 'Cryptocurrency' and instr_type != 'CRYPTO':
                        instr_type = 'CRYPTO'
                        instr_reason = "IBKR Asset Category='Cryptocurrency'"
                        instr_unknown = False

                    # Asset Category=Bonds → instrument_type=BOND. El classifier
                    # no detecta bonos por nombre (los nombres son demasiado
                    # variables: "GERMAN BUND 2.5% 30", "T 4 1/4 02/15/35", etc.).
                    # Marcamos por la categoría IBKR explícita.
                    if asset_cat == 'Bonds':
                        instr_type = 'BOND'
                        instr_reason = "IBKR Asset Category='Bonds'"
                        instr_unknown = False

                    # Cupón corrido (DGT V1732-10): el accrued interest pagado
                    # al comprar suma al coste de adquisición; el cobrado al
                    # vender suma al valor de transmisión. IBKR lo reporta en
                    # secciones separadas (Bond Interest Paid / Received), no
                    # en la fila Trade. _build_ibkr_bond_data hace el join por
                    # (symbol, fecha_iso). Importe ya en EUR.
                    if instr_type == 'BOND' and bond_data:
                        date_iso_acc = (date_str.split(',')[0].strip()
                                        if date_str else '')
                        if quantity > 0:
                            accrued_pagado = bond_data['accrued_pagado_por_symfecha'].get(
                                (symbol, date_iso_acc), Decimal('0'))
                            if accrued_pagado > 0:
                                importe = importe + accrued_pagado
                        else:
                            accrued_cobrado = bond_data['accrued_cobrado_por_symfecha'].get(
                                (symbol, date_iso_acc), Decimal('0'))
                            if accrued_cobrado > 0:
                                importe = importe + accrued_cobrado

                    # Sumar Transaction Fees externas (Tasa Tobin española en
                    # compras de acciones IBEX > 1.000M EUR, etc.). Match por
                    # (symbol, fecha_iso, qty). Forma parte del coste de
                    # adquisición — Art. 35 LIRPF.
                    date_iso_match = (date_str.split(',')[0].strip()
                                      if date_str else '')
                    ext_entry = fees_lookup.get(
                        (symbol, date_iso_match, abs(quantity)),
                    )
                    if ext_entry:
                        extra_fee = ext_entry['total']
                        ext_breakdown = dict(ext_entry['breakdown'])
                    else:
                        extra_fee = Decimal('0')
                        ext_breakdown = {'es': Decimal('0'), 'uk': Decimal('0'),
                                         'fr': Decimal('0'), 'hk': Decimal('0'),
                                         'other': Decimal('0')}
                    gastos_broker_ibkr = gastos
                    if extra_fee > 0:
                        gastos = gastos + extra_fee

                    # ── Detección de short (IBKR) ────────────────────────────
                    # IBKR emite en el campo `Code` el indicador Opening/Closing
                    # del trade respecto a tu posición. Combinado con el signo
                    # de Quantity identifica shorts de forma inequívoca:
                    #   qty<0 + 'O' en Code → apertura de short (vendes sin tener)
                    #   qty>0 + 'C' en Code → cobertura de short (compras para cubrir)
                    #   qty>0 + 'B' en Code → AUTOMATIC BUY-IN (cobertura forzosa
                    #     ordenada por IBKR cuando el short se queda sin borrow
                    #     o sin margen suficiente). Semánticamente es cobertura
                    #     idéntica; se distingue con `_buy_in_forzoso=True` para
                    #     que el informe pueda etiquetarlo aparte.
                    # Combinaciones soportadas: 'O', 'O;P' (partial), 'A;O'
                    # (assignment de una opción que abrió short), análogo para C
                    # y combos con 'B' (p.ej. 'B;C').
                    # El motor FIFO valida contra inventario (motor_fiscal.py:966)
                    # — si hay inventario disponible los flags son ignorados,
                    # así que es seguro emitirlos como hint. Solo aplicamos a
                    # Asset Category=Stocks/ETF (no bonos, cripto ni opciones).
                    code_raw = col_t('Code') if 'Code' in h else ''
                    code_tokens = {t.strip() for t in code_raw.split(';') if t.strip()}
                    is_short_open = (
                        quantity < 0 and 'O' in code_tokens
                        and asset_cat in ('Stocks', 'ETF')
                    )
                    is_buy_in = (
                        quantity > 0 and 'B' in code_tokens
                        and asset_cat in ('Stocks', 'ETF')
                    )
                    is_short_cover_voluntario = (
                        quantity > 0 and 'C' in code_tokens
                        and 'B' not in code_tokens
                        and asset_cat in ('Stocks', 'ETF')
                    )
                    # Buy-in cubre short también — semánticamente es Closing
                    # forzoso. La presencia conjunta 'B' + 'C' sigue siendo
                    # cobertura (no doblar).
                    is_short_cover = is_short_cover_voluntario or is_buy_in

                    op_dict = {
                        'tipo':        'A' if quantity > 0 else 'T',
                        'isin':        isin_resolved,
                        'nombre':      nombre_canon[:50],
                        'fecha':       fecha,
                        'cantidad':    abs(quantity),
                        'importe_eur': importe,
                        'gastos_eur':  gastos,
                        # Desglose paralelo a DeGiro (suma = gastos_eur):
                        'gastos_broker':              gastos_broker_ibkr,
                        'gastos_autofx':              Decimal('0'),
                        'gastos_externos':            extra_fee,
                        'gastos_externos_breakdown':  ext_breakdown,
                        'broker':      'IBKR',
                        # Clasificación acción vs ETF (Renta 2025+ split). El
                        # FII Type de IBKR es la fuente más fiable cuando lo
                        # tenemos; el classifier hace fallback a whitelist+
                        # heurística para casos sin Type.
                        'instrument_type':         instr_type,
                        'instrument_type_reason':  instr_reason,
                        'instrument_type_unknown': instr_unknown,
                        '_ibkr_fii_type':          fii_type,
                        '_ibkr_code':              code_raw,
                    }
                    if is_short_open:
                        op_dict['_es_corto_apertura'] = True
                        op_dict['_corto_motivo'] = (
                            f"IBKR marcó la venta de {abs(quantity)} {symbol} "
                            f"({nombre_canon[:40]}) el {fecha} con Code='{code_raw}' "
                            f"(Opening). Combinado con cantidad negativa = apertura "
                            f"de venta corta. El motor FIFO valida con inventario."
                        )
                    if is_short_cover:
                        op_dict['_es_corto_cobertura'] = True
                        if is_buy_in:
                            op_dict['_buy_in_forzoso'] = True
                            op_dict['_corto_motivo'] = (
                                f"IBKR ejecutó AUTOMATIC BUY-IN sobre {quantity} {symbol} "
                                f"({nombre_canon[:40]}) el {fecha} con Code='{code_raw}'. "
                                f"Cobertura FORZOSA por el broker de una posición corta "
                                f"que quedó sin garantía o sin stock prestable. G/P se "
                                f"calcula igual que una cobertura normal (precio apertura "
                                f"− precio buy-in)."
                            )
                        else:
                            op_dict['_corto_motivo'] = (
                                f"IBKR marcó la compra de {quantity} {symbol} "
                                f"({nombre_canon[:40]}) el {fecha} con Code='{code_raw}' "
                                f"(Closing). Combinado con cantidad positiva = cobertura "
                                f"de una posición corta abierta previamente."
                            )
                    operaciones.append(op_dict)
                except (ValueError, IndexError, KeyError):
                    descartadas['otros'] += 1

            # ── Corporate Actions ─────────────────────────────────────────
            # IBKR CSV: Corporate Actions,Header,...
            #           Corporate Actions,Data,Stocks,USD,SYM,ISIN,Date,Description,Qty,Value,...
            if row[0] == 'Corporate Actions' and row[1] == 'Header':
                corp_header = row
                continue

            if row[0] == 'Corporate Actions' and row[1] == 'Data' and corp_header:
                h = corp_header
                def col_c(name):
                    return row[h.index(name)] if name in h else ''
                try:
                    asset_cat = col_c('Asset Category')
                    if asset_cat not in ('Stocks', 'ETF', ''):
                        continue

                    desc     = col_c('Description').upper()
                    symbol   = col_c('Symbol')
                    isin_col = col_c('ISIN')
                    date_str = col_c('Report Date') or col_c('Date')
                    qty_str  = col_c('Quantity')

                    if not qty_str or qty_str in ('Quantity',):
                        continue

                    qty   = Decimal(qty_str.replace(',', ''))
                    fecha = parse_date(date_str)
                    if not fecha:
                        continue

                    # La sección Corporate Actions de IBKR NO trae columnas
                    # Symbol/ISIN: el ticker y el ISIN van EMBEBIDOS en la
                    # descripción, "TICKER(ISIN) <evento> ...". Extraerlos de ahí
                    # (verificado en datos reales). `symbol`/`isin_col` se mantienen
                    # como fuente preferente por si una variante del export sí los
                    # trae como columna.
                    _head = re.match(r'\s*(\S+?)\(([A-Z]{2}[A-Z0-9]{10})\)', desc)
                    if not symbol and _head:
                        symbol = _head.group(1)
                    isin_desc = _head.group(2) if _head else ''

                    # Nombre canónico (FII Description) con fallback al símbolo.
                    fii_entry = sym_isin_map.get(symbol) or {}
                    nombre_canon = fii_entry.get('description') or symbol

                    # Clave del instrumento resuelta EXACTAMENTE como en Trades
                    # (columna ISIN 12 chars → mapa FII → SYM:<symbol>), para que
                    # la fila del split/contrasplit case con los lotes. El ISIN de
                    # la descripción solo se usa como último recurso (puede ser el
                    # ISIN nuevo tras un cambio de CUSIP, que NO casaría con la
                    # clave SYM: que reciben los trades). Antes se usaba la columna
                    # ISIN —vacía en CA reales— → el split se generaba sin clave y
                    # NO se aplicaba a ningún lote (caso NCNA/BNKK 2025). Para los
                    # splits que ya funcionaban (ISIN válido) es idéntico.
                    isin_col_clean = (isin_col or '').strip()
                    if len(isin_col_clean) == 12:
                        isin_ca = isin_col_clean
                    elif fii_entry.get('isin'):
                        isin_ca = fii_entry['isin']
                    elif symbol:
                        isin_ca = f"SYM:{symbol.strip().upper()}"
                    else:
                        isin_ca = isin_desc

                    # Detectar splits/contrasplits en la descripción IBKR
                    # IBKR los describe como: "SPLIT 1 FOR 3" o "REVERSE SPLIT 3 FOR 1"
                    split_match = re.search(
                        r'(REVERSE\s+)?SPLIT\s+(\d+(?:\.\d+)?)\s+FOR\s+(\d+(?:\.\d+)?)',
                        desc)
                    if split_match:
                        is_reverse = bool(split_match.group(1))
                        n1 = Decimal(split_match.group(2))
                        n2 = Decimal(split_match.group(3))
                        # IBKR: "SPLIT X FOR Y" → por cada Y acción vieja se reciben X nuevas
                        qty_old = n2 if not is_reverse else n1
                        qty_new = n1 if not is_reverse else n2
                        tipo_ca = CA_SPLIT if qty_new > qty_old else CA_CONTRASPLIT

                        corp_events.append({'tipo_ca': tipo_ca,
                                             'fecha': fecha, 'nombre': nombre_canon[:50],
                                             'isin_old': isin_ca, 'isin_new': isin_ca,
                                             'qty_old': float(qty_old),
                                             'qty_new': float(qty_new),
                                             'descripcion': desc[:80]})
                        sp_ops.append(build_sp_row(corp_events[-1]))
                    else:
                        corp_events.append({'tipo_ca': CA_COMPLEX,
                                             'fecha': fecha, 'nombre': nombre_canon[:50],
                                             'isin': isin_ca, 'cantidad': float(qty),
                                             'descripcion': desc[:80],
                                             'descripcion_full': desc})  # full para post-procesado scrip
                except (ValueError, IndexError, KeyError):
                    pass

    # Post-procesado: detectar cadenas scrip dividend (Issue → Expire → Merged)
    # y sintetizar la fila A final sobre la matriz, eliminando las compras de
    # derechos consumidas. Modifica `operaciones` in-place.
    scrip_chains_synth = _synth_ibkr_scrip_chains(corp_events, operaciones)

    # Amortización de bonos al vencimiento. Dos capas:
    #   1. Sección `Bond Maturity` (preferida) — IBKR registra la
    #      amortización con qty y value exactos.
    #   2. Inferencia desde FII.Maturity para bonos cuya posición neta
    #      sigue abierta tras procesar todos los Trades reales y cuyo
    #      vencimiento cae dentro del periodo del statement, con warning.
    amortizaciones_inferidas = _aplicar_amortizaciones_bonos(
        operaciones, filepath, sym_isin_map,
    )

    _enrich_with_instrument_type(operaciones, broker='IBKR')

    return operaciones, sp_ops, descartadas, {
        'complejos': corp_events,
        'scrip_chains': scrip_chains_synth,  # info para reporting
        'amortizaciones_bonos_inferidas': amortizaciones_inferidas,
    }


# ── Parser Trade Republic ──────────────────────────────────────────────────
#
# Formato CSV oficial de Trade Republic Transaction Export (23 columnas):
#   datetime, date, account_type, category, type, asset_class, name, symbol,
#   shares, price, amount, fee, tax, currency, original_amount,
#   original_currency, fx_rate, description, transaction_id,
#   counterparty_name, counterparty_iban, payment_reference, mcc_code
#
# Categorías y tipos observados (verificado 2026-05-18 contra CSV real):
#   CASH / CUSTOMER_INBOUND       → depósito propio (ignorar)
#   CASH / TRANSFER_INBOUND       → transferencia recibida (ignorar)
#   CASH / INTEREST_PAYMENT       → intereses cuenta remunerada → RCM 0027
#                                    [parse_tr_intereses]
#   CASH / DIVIDEND               → dividendo ETF/Fondo → RCM 0029
#                                    [parse_tr_dividendos]
#   CASH / TAX_OPTIMIZATION       → ajuste fiscal automático ALEMÁN — no
#                                    aplica España (ignorar con warning)
#   TRADING / BUY                 → compra (A)
#   TRADING / SELL                → venta (T)
#   DELIVERY / MIGRATION          → cambio custodio interno TR (sin alteración)
#   DELIVERY / FREE_RECEIPT       → staking rewards (CRYPTO) → RCM staking
#                                    Art. 25.2 LIRPF + DGT V1766-22
#                                    [parse_tr_staking]
#
# ISIN — convención TR:
#   - Fondos/ETFs (asset_class=FUND): ISIN ISO en columna `symbol`
#   - Cripto (asset_class=CRYPTO): `symbol` es ticker (BTC, SOL); el ISIN
#     ficticio (XF\d{3}[A-Z]+\d{4}) viene incrustado en `description`,
#     coincidente con el ISIN ficticio que IBKR asigna a la misma cripto.

# Regex compartido entre los parsers TR
_RE_TR_ISIN_CRYPTO = re.compile(r'\b(XF\d{3}[A-Z]+\d{4})\b')
_RE_TR_ISIN_ISO    = re.compile(r'^[A-Z]{2}[A-Z0-9]{10}$')


def _tr_detect_sep(filepath):
    with open(filepath, encoding='utf-8') as f:
        first_line = f.readline()
    return ';' if first_line.count(';') > first_line.count(',') else ','


def _tr_open_reader(filepath):
    sep = _tr_detect_sep(filepath)
    f = open(filepath, encoding='utf-8')
    return f, csv.DictReader(f, delimiter=sep)


def parse_tr(filepath):
    """
    Parsea operaciones de trading (BUY/SELL) de Trade Republic.

    El resto de tipos del CSV (DIVIDEND, INTEREST_PAYMENT, MIGRATION,
    FREE_RECEIPT, etc.) se procesan por funciones específicas y se IGNORAN
    aquí sin contarlos como error.

    Devuelve: operaciones, sp_ops, descartadas (dict desglosado).
    """
    if not os.path.exists(filepath):
        return [], [], {
            'tipo_no_operable': 0, 'sin_isin': 0,
            'migration': 0, 'cash_movement': 0, 'tax_optimization': 0,
        }

    operaciones = []
    descartadas = {
        'tipo_no_operable':  0,   # tipos manejados por otras funciones (DIVIDEND, etc.)
        'sin_isin':          0,   # BUY/SELL sin ISIN identificable
        'migration':         0,   # cambio de custodio interno
        'cash_movement':     0,   # depósitos / transferencias propias
        'tax_optimization':  0,   # mecanismo alemán, no aplica España
        'corporate_action':  0,   # scrip dividends, splits, etc. → parse_tr_corporate_actions
    }

    f, reader = _tr_open_reader(filepath)
    try:
        for row in reader:
            type_raw    = (row.get('type') or '').strip().upper()
            category    = (row.get('category') or '').strip().upper()
            asset_class = (row.get('asset_class') or '').strip().upper()

            # Saltar transferencias propias y depósitos
            if type_raw in ('CUSTOMER_INBOUND', 'TRANSFER_INBOUND',
                            'CUSTOMER_OUTBOUND', 'TRANSFER_OUTBOUND'):
                descartadas['cash_movement'] += 1
                continue

            # Migración: pares -N/+N por ISIN, sin alteración patrimonial
            if type_raw == 'MIGRATION':
                descartadas['migration'] += 1
                continue

            # TAX_OPTIMIZATION: compensación fiscal automática alemana — NO aplica España
            if type_raw == 'TAX_OPTIMIZATION':
                descartadas['tax_optimization'] += 1
                continue

            # Scrip dividends y otras corporativas: procesados por
            # parse_tr_corporate_actions. Aquí solo se cuentan; los CASH
            # LIQUIDATION_PROCEEDS comparten cuenta porque también forman
            # parte del ciclo.
            if (type_raw in _TR_CORP_ACTION_TYPES
                    or (category == 'CASH' and type_raw == 'LIQUIDATION_PROCEEDS')):
                descartadas['corporate_action'] += 1
                continue

            # Tipos manejados por otras funciones específicas
            if type_raw in ('DIVIDEND', 'INTEREST_PAYMENT', 'FREE_RECEIPT'):
                descartadas['tipo_no_operable'] += 1
                continue

            # Sólo BUY/SELL en esta función
            if type_raw == 'BUY':
                tipo = 'A'
            elif type_raw == 'SELL':
                tipo = 'T'
            else:
                descartadas['tipo_no_operable'] += 1
                continue

            # ISIN
            symbol      = (row.get('symbol') or '').strip()
            description = (row.get('description') or '').strip()

            isin = ''
            if _RE_TR_ISIN_ISO.match(symbol):
                isin = symbol
            elif asset_class == 'CRYPTO':
                m = _RE_TR_ISIN_CRYPTO.search(description)
                if m:
                    isin = m.group(1)

            if not isin or is_option_isin(isin):
                descartadas['sin_isin'] += 1
                continue

            nombre  = (row.get('name') or '').strip()[:50]
            fecha_s = (row.get('date') or '').strip()
            fecha   = parse_date(fecha_s)

            try:
                cantidad = abs(parse_es(row.get('shares', '') or '0'))
                importe  = abs(parse_es(row.get('amount', '') or '0'))
                gastos   = abs(parse_es(row.get('fee', '') or '0'))
            except (ValueError, InvalidOperation):
                continue

            if not fecha or cantidad == 0:
                continue

            # asset_class de TR es autoritativo: el broker sabe si lo que
            # vendió/compró es STOCK, FUND (UCITS), CRYPTO o SYNTHETIC.
            # Lo mapeamos al esquema interno (instrument_type) para que el
            # XLSX maestro lo escriba en la columna correcta y motor_fiscal
            # envíe los ETFs UCITS a casillas 2224-2236 en vez de 0326-0340.
            # Para asset_class='' (raro) caemos al classifier vía
            # _enrich_with_instrument_type.
            _ASSET_CLASS_MAP = {
                'STOCK':     'STOCK',
                'FUND':      'ETF',       # UCITS / fondo cotizado
                'CRYPTO':    'CRYPTO',
                'BOND':      'BOND',
                'SYNTHETIC': 'STOCK',     # derechos (RTS) — se tratan como
                                          # acciones para la columna de tipo;
                                          # el motor los marca por separado
                                          # cuando detecta corporativas
            }
            instr_type = _ASSET_CLASS_MAP.get(asset_class)
            op_entry = {
                'tipo': tipo, 'isin': isin, 'nombre': nombre,
                'fecha': fecha, 'cantidad': cantidad,
                'importe_eur': importe, 'gastos_eur': gastos,
                'broker': 'TR',
                'es_savings_plan': description.startswith('Savings plan'),
                'asset_class': asset_class,
                'transaction_id': (row.get('transaction_id') or '').strip(),
            }
            if instr_type:
                op_entry['instrument_type'] = instr_type
                op_entry['instrument_type_reason'] = (
                    f"TR asset_class={asset_class}"
                )
                op_entry['instrument_type_unknown'] = False
            operaciones.append(op_entry)
    finally:
        f.close()

    return operaciones, [], descartadas


def parse_tr_dividendos(filepath):
    """
    Parsea dividendos de Trade Republic (CASH/DIVIDEND).

    Reparto del campo `tax` (CRÍTICO):
    El campo `tax` que TR emite en cada fila de dividendo es la **suma de las
    retenciones aplicadas, no solo la española**. En carteras con acciones
    extranjeras (J&J, Hermès, Nestlé ADR…) hemos observado dos regímenes:

      - **Pre-migración a IBAN ES** (TR Bank GmbH custodio): TR aplica
        únicamente la retención del país emisor (15 % USA con W-8BEN, ~12-13 %
        Francia, 0 % UK, 0 % UCITS Irlanda…). `tax` ≈ source_rate × bruto.

      - **Post-migración a IBAN ES** (TR Sucursal ES retenedor IRPF):
        TR aplica primero la retención del país emisor sobre el bruto, y
        después el 19 % de IRPF español sobre el neto. `tax` resultante:
        source_ret + (bruto − source_ret) × 19 %. Para J&J esto da
        15 % + 16,1 % ≈ 31,1 % del bruto, no el 19 % nacional aislado.

    Si atribuimos todo `tax` a retención nacional (como hacía la versión
    anterior), inflamos la casilla 0591 y dejamos vacía la 0588 (CDI),
    perdiendo dinero por dividendos extranjeros que sí tienen crédito CDI.

    Solución: dividir el `tax` en source vs español usando como techo el
    CDI bilateral del país emisor (DTA_SOURCE_MAX). Es una aproximación —
    el rate real puede ser inferior al CDI cap (Francia individual:
    12,8 %; UCITS Irlanda: 0 % aunque CDI sea 15 %) — pero el efecto
    neto sobre la cuota es marginal (lo que perdemos en 0591 lo
    recuperamos en 0588, ambos contra cuota). Para los casos con `pais=ES`
    (ej. ACS pagador directo) el reparto es trivial: todo es nacional.

    Devuelve filas de tipo 'DIV' (bruto) y 'RET' (retención por país).
    """
    if not os.path.exists(filepath):
        return []

    resultados = []
    # TAX_OPTIMIZATION negativos (posible retención española retroactiva) y
    # dividendos cobrados en bruto (candidatos a esa retención) — ver bloque
    # de emparejamiento tras el bucle.
    tax_opts = []
    gross_divs = []
    f, reader = _tr_open_reader(filepath)
    try:
        for row in reader:
            tipo_row = (row.get('type') or '').strip().upper()
            if tipo_row == 'TAX_OPTIMIZATION':
                try:
                    _to_tax = parse_es(row.get('tax', '') or '0')
                except (ValueError, InvalidOperation):
                    _to_tax = Decimal('0')
                # Solo los NEGATIVOS (retención practicada). Los positivos son la
                # devolución del régimen alemán (Steueroptimierung) → se ignoran.
                if _to_tax < 0:
                    tax_opts.append((parse_date((row.get('date') or '').strip()), abs(_to_tax)))
                continue
            if tipo_row != 'DIVIDEND':
                continue

            symbol = (row.get('symbol') or '').strip()
            if not _RE_TR_ISIN_ISO.match(symbol):
                continue
            isin = symbol

            nombre = (row.get('name') or '').strip()[:50]
            fecha  = parse_date((row.get('date') or '').strip())
            if not fecha:
                continue

            try:
                # NO usar abs(): TR emite DIVIDEND con `amount` negativo
                # cuando es una reversa de una emisión anterior (mismo ISIN,
                # mismo bruto en negativo). El abs() previo inflaba el
                # bruto al duplicar emisión+reversa. Caso real verificado
                # 2026-05-19: Hermes 2025-02-19 +2,53 € / 2025-02-20 −2,53 €
                # debían netear a 0 y el parser sumaba 5,06 €.
                amount = parse_es(row.get('amount', '') or '0')
                tax    = parse_es(row.get('tax', '') or '0')
            except (ValueError, InvalidOperation):
                continue

            currency = (row.get('currency') or 'EUR').strip().upper()
            orig_amt = (row.get('original_amount') or '').strip()
            orig_cur = (row.get('original_currency') or '').strip().upper()
            fx_rate  = (row.get('fx_rate') or '').strip()

            # Determinar país emisor desde el ISIN (no del pagador). El motor
            # usa este campo para decidir si la retención va a 0591 (nacional)
            # o si se cómputa CDI 0588 (extranjera).
            pais = _pais_de_isin(isin, nombre)

            resultados.append({
                'fecha': fecha, 'isin': isin, 'nombre': nombre,
                'tipo': 'DIV',
                'pais': pais,
                'importe_eur': amount,
                'divisa': currency,
                'broker': 'TR',
                'bruto_original': f"{orig_amt} {orig_cur}" if orig_amt else '',
                'fx_rate': fx_rate,
            })

            # Retención reportada por TR: split source vs ES si el ISIN es
            # extranjero. Para emisores españoles (ES0…), todo es nacional.
            # Solo procesamos retenciones con `tax < 0` Y `amount > 0` —
            # las reversas (amount < 0) no llevan tax asociado en la
            # observación real.
            #
            # Caso especial UCITS: los fondos UCITS irlandeses (IE…) NO
            # retienen en origen a no residentes (Section 739D TCA 1997).
            # Si el broker marca `asset_class=FUND`, asumimos source rate 0
            # con independencia de lo que diga el CDI bilateral — toda la
            # retención reportada va a casilla 0591 (nacional). Esto evita
            # atribuir falsamente a IE/LU/FR un 15 % que no se retiene en
            # productos UCITS.
            asset_class_div = (row.get('asset_class') or '').strip().upper()
            if tax < 0 and amount > 0:
                tax_total = abs(tax)
                if pais == 'ES':
                    source_ret = Decimal('0')
                    es_ret     = tax_total
                elif asset_class_div == 'FUND':
                    source_ret = Decimal('0')
                    es_ret     = tax_total
                elif pais in TR_SOURCE_WHT_RATE:
                    # Descomponer por la tasa de origen REAL (no por el tope
                    # CDI). origen = bruto × tasa_real; ES = resto. El downstream
                    # (calcular_resumen_dividendos) topa el crédito 0588 al CDI
                    # y marca el exceso como no recuperable, así que aquí se
                    # emite el origen completo (aunque supere el tope, p. ej. DE).
                    source_rate = TR_SOURCE_WHT_RATE[pais]
                    source_ret = (amount * source_rate).quantize(Decimal('0.01'),
                                                                 ROUND_HALF_UP)
                    # Salvaguarda: el origen no puede exceder la retención total
                    # (cubre tasas de tabla ligeramente altas vs lo aplicado).
                    source_ret = min(source_ret, tax_total)
                    es_ret     = tax_total - source_ret
                else:
                    # País sin tasa real conocida → método antiguo (techo CDI).
                    cdi_rate = DTA_SOURCE_MAX.get(pais, Decimal('0'))
                    source_max = (amount * cdi_rate).quantize(Decimal('0.01'),
                                                              ROUND_HALF_UP)
                    source_ret = min(tax_total, source_max)
                    es_ret     = tax_total - source_ret

                if source_ret > 0:
                    resultados.append({
                        'fecha': fecha, 'isin': isin, 'nombre': nombre,
                        'tipo': 'RET',
                        'pais': pais,
                        'importe_eur': source_ret,
                        'divisa': 'EUR',
                        'broker': 'TR',
                    })
                if es_ret > 0:
                    resultados.append({
                        'fecha': fecha, 'isin': isin, 'nombre': nombre,
                        'tipo': 'RET',
                        'pais': 'ES',
                        'importe_eur': es_ret,
                        'divisa': 'EUR',
                        'broker': 'TR',
                    })
            elif tax == 0 and amount > 0:
                # Dividendo cobrado en bruto (sin retención al pago). Candidato a
                # recibir una retención española retroactiva vía TAX_OPTIMIZATION.
                gross_divs.append({'isin': isin, 'nombre': nombre,
                                   'fecha': fecha, 'bruto': amount})
    finally:
        f.close()

    # ── Retención española retroactiva camuflada como TAX_OPTIMIZATION ────────
    # TR puede aplicar el 19% de IRPF semanas después sobre un dividendo cobrado
    # en bruto (típico en el dividendo del mes de transición a Sucursal ES) y lo
    # registra como un asiento TAX_OPTIMIZATION de importe NEGATIVO, sin ISIN.
    # No es la devolución alemana (Steueroptimierung, que es positiva): es
    # retención española acreditable (casilla 0591/0597). Se empareja por
    # importe ≈ 19% del bruto de un dividendo previo cobrado en bruto y se emite
    # como fila RET país='ES' atada a ese ISIN. Caso real verificado 2026-05-24:
    # JEPQ 09/07/2025 (bruto 205,09) → TAX_OPTIMIZATION −38,97 el 05/08/2025
    # (= 205,09 × 19%), reportado por TR en el Modelo 198 como retención REA.
    usados = set()
    for fecha_to, ret in tax_opts:
        candidatos = [
            (i, gd) for i, gd in enumerate(gross_divs)
            if i not in usados
            and abs((gd['bruto'] * Decimal('0.19')).quantize(Decimal('0.01'), ROUND_HALF_UP) - ret) <= Decimal('0.03')
        ]
        # Preferir el dividendo en bruto más reciente anterior al ajuste.
        if fecha_to:
            previos = [c for c in candidatos if c[1]['fecha'] and c[1]['fecha'] <= fecha_to]
            if previos:
                candidatos = previos
        if candidatos:
            con_fecha = [c for c in candidatos if c[1]['fecha']]
            i, gd = (max(con_fecha, key=lambda c: c[1]['fecha'])
                     if con_fecha else candidatos[0])
            usados.add(i)
            resultados.append({
                'fecha': fecha_to or gd['fecha'], 'isin': gd['isin'],
                'nombre': gd['nombre'], 'tipo': 'RET', 'pais': 'ES',
                'importe_eur': ret, 'divisa': 'EUR', 'broker': 'TR',
                'origen': 'retencion_es_retroactiva_tax_optimization',
            })

    return resultados


def parse_tr_intereses(filepath):
    """
    Parsea intereses de la cuenta remunerada de Trade Republic
    (CASH/INTEREST_PAYMENT).

    Tributan como rendimiento del capital mobiliario, Art. 25.2 LIRPF →
    casilla 0027 (intereses de cuentas).

    Si TR retiene IRPF español (`tax` < 0), se reporta como retención del
    pagador español, compatible con el flujo de retenciones IRPF (no CDI).
    """
    if not os.path.exists(filepath):
        return []

    resultados = []
    f, reader = _tr_open_reader(filepath)
    try:
        for row in reader:
            if (row.get('type') or '').strip().upper() != 'INTEREST_PAYMENT':
                continue

            fecha = parse_date((row.get('date') or '').strip())
            if not fecha:
                continue

            try:
                amount = abs(parse_es(row.get('amount', '') or '0'))
                tax    = parse_es(row.get('tax', '') or '0')
            except (ValueError, InvalidOperation):
                continue

            if amount <= 0:
                continue

            resultados.append({
                'fecha': fecha,
                'tipo': 'INTEREST',
                'importe_eur': amount,
                'retencion_es_eur': abs(tax) if tax < 0 else Decimal('0'),
                'broker': 'TR',
                'fuente': 'Cuenta remunerada Trade Republic',
            })
    finally:
        f.close()

    return resultados


def staking_a_lotes(staking_entries):
    """Convierte staking rewards en operaciones de ADQUISICIÓN ('A') para el
    FIFO, con coste = valor EUR por el que tributaron como RCM en especie al
    recibirse (DGT V1766-22 + Art. 43.1 LIRPF).

    CL7 auditoría 2026-06-11: sin este lote, la venta posterior del cripto
    recibido por staking salía como venta huérfana (coste 0) → el valor de
    recepción tributaba DOS veces (como RCM al recibir y como ganancia
    íntegra al vender). Con el lote, la G/P de la venta es
    precio_venta − valor_recepción, que es lo correcto.

    Devuelve (ops, avisos): los rewards sin ISIN identificable no generan
    lote (el FIFO está indexado por ISIN) y se reportan en `avisos` para
    revisión manual — su ingreso RCM se declara igualmente por la vía
    parse_tr_staking.
    """
    ops: list = []
    avisos: list = []
    for s in staking_entries or []:
        if s.get('tipo') != 'STAKING_REWARD':
            continue
        if not s.get('isin'):
            avisos.append(
                f"Staking {s.get('asset', '?')} {s.get('fecha', '?')}: sin ISIN "
                f"identificable — lote de adquisición NO creado (coste "
                f"{fmt_es(s.get('importe_eur', Decimal('0')))} EUR a registrar "
                f"manualmente al vender)")
            continue
        ops.append({
            'tipo':         'A',
            'isin':         s['isin'],
            'nombre':       (s.get('asset') or s['isin'])[:50],
            'fecha':        s['fecha'],
            'cantidad':     s['cantidad'],
            'importe_eur':  s['importe_eur'],   # valor RCM de recepción
            'gastos_eur':   Decimal('0'),
            'broker':       s.get('broker', 'TR'),
            'instrument_type':        'CRYPTO',
            'instrument_type_reason': 'staking reward (lote V1766-22)',
            'instrument_type_unknown': False,
            '_es_staking_reward': True,
        })
    return ops, avisos


def parse_tr_staking(filepath):
    """
    Parsea recepciones gratuitas de cripto (staking rewards) de Trade Republic
    (DELIVERY/FREE_RECEIPT, asset_class=CRYPTO).

    Doctrina fiscal — DGT V1766-22 (26-7-2022): los rendimientos del staking se
    califican como rendimientos del capital mobiliario por la cesión a terceros
    de capitales propios SATISFECHO EN ESPECIE (Art. 25.2 LIRPF). Valoración:
    precio de mercado en EUR en el momento de cada recepción (Art. 43.1 LIRPF
    para operaciones en especie).

    Casilla RentaWEB 2025: 0027 (intereses de cuentas, depósitos y activos
    financieros en general), por analogía con un rendimiento periódico por
    cesión de capital. La V1766-22 NO fija casilla concreta, así que existe
    alternativa doctrinal en 0031 (transmisión / amortización de otros activos
    financieros) por ser el rendimiento satisfecho en especie. Implicación
    práctica: ambas tributan en base del ahorro al mismo tipo → la cuota es
    idéntica; la elección solo afecta a coherencia formal y trazabilidad ante
    un requerimiento AEAT.

    Devuelve lista de dicts con tipo 'STAKING_REWARD'.
    """
    if not os.path.exists(filepath):
        return []

    # Pre-pasada: mapa símbolo → ISIN cripto. Las filas FREE_RECEIPT de TR
    # NO llevan el ISIN en la descripción ("FREE_RECEIPT SOL"), pero los
    # trades BUY/SELL del mismo activo sí ("Buy trade XF000SOL0012 …").
    # Sin este backfill, los rewards quedaban sin ISIN y staking_a_lotes
    # no podía crear el lote de adquisición (CL7) — caso real detectado en
    # la verificación e2e con datos de Angel (6 rewards de SOL).
    isin_por_symbol: dict = {}
    f0, reader0 = _tr_open_reader(filepath)
    try:
        for row in reader0:
            if (row.get('asset_class') or '').strip().upper() != 'CRYPTO':
                continue
            sym = (row.get('symbol') or '').strip()
            if not sym or sym in isin_por_symbol:
                continue
            m0 = _RE_TR_ISIN_CRYPTO.search((row.get('description') or ''))
            if m0:
                isin_por_symbol[sym] = m0.group(1)
    finally:
        f0.close()

    resultados = []
    f, reader = _tr_open_reader(filepath)
    try:
        for row in reader:
            type_raw    = (row.get('type') or '').strip().upper()
            category    = (row.get('category') or '').strip().upper()
            asset_class = (row.get('asset_class') or '').strip().upper()

            if not (category == 'DELIVERY' and type_raw == 'FREE_RECEIPT'
                    and asset_class == 'CRYPTO'):
                continue

            fecha = parse_date((row.get('date') or '').strip())
            if not fecha:
                continue

            try:
                shares = parse_es(row.get('shares', '') or '0')
                price  = parse_es(row.get('price', '') or '0')
            except (ValueError, InvalidOperation):
                continue

            if shares <= 0 or price <= 0:
                continue

            valor_eur = (shares * price).quantize(Decimal('0.01'), ROUND_HALF_UP)

            symbol      = (row.get('symbol') or '').strip()
            description = (row.get('description') or '').strip()
            isin = ''
            m = _RE_TR_ISIN_CRYPTO.search(description)
            if m:
                isin = m.group(1)
            elif symbol in isin_por_symbol:
                # Backfill desde los trades BUY/SELL del mismo activo (las
                # filas FREE_RECEIPT no llevan ISIN en la descripción).
                isin = isin_por_symbol[symbol]

            resultados.append({
                'fecha': fecha,
                'tipo': 'STAKING_REWARD',
                'asset': symbol,
                'isin': isin,
                'cantidad': shares,
                'precio_unit_eur': price,
                'importe_eur': valor_eur,
                'broker': 'TR',
                'fuente': f'Staking {symbol} en Trade Republic',
            })
    finally:
        f.close()

    return resultados


_RE_TR_EMISOR_PREFIX = re.compile(r'^["\']?([A-Za-z0-9&]+)')
_RE_TR_ISIN_IN_DESC  = re.compile(r'\bISIN\s+([A-Z]{2}[A-Z0-9]{10})\b')

# Tipos de evento que componen el ciclo de scrip dividend / corporate action.
# Se procesan EXCLUSIVAMENTE por parse_tr_corporate_actions (no por parse_tr).
_TR_CORP_ACTION_TYPES = frozenset({
    'INTERMEDIATE_SECURITIES_DISTRIBUTION',
    'DIVIDEND_REINVESTMENT',
    'DIVIDEND_REINVESTMENT_CANCELLED',
    'EXCHANGE',
    'PARI_PASSU',
    'LIQUIDATION_DIVIDEND',
    'LIQUIDATION_PROCEEDS',
})


def _tr_emisor_prefix(name):
    """Extrae el prefix de emisor de un nombre TR.

    'ACS,ACT.CO.SER.INH.-ANR-'    -> 'ACS'
    'ACS Cons y Serv'             -> 'ACS'
    'ACS,ACT.CO.SER.INH.EO-,50'   -> 'ACS'
    """
    if not name:
        return ''
    m = _RE_TR_EMISOR_PREFIX.match(name.strip())
    return m.group(1).upper() if m else ''


_SPINOFFS_CONOCIDOS_PATH = os.path.join(BASE_DIR, 'spinoffs_conocidos.json')


def _cargar_spinoffs_conocidos():
    """Devuelve {isin_escindida: dict_entrada} con las entradas activas del
    catalogo. Una entrada esta activa si:
        - `_activo` no es False
        - `ratio_coste_escindida` y `ratio_coste_matriz_residual` son numericos validos
        - su suma esta dentro de tolerancia (±0.0001) de 1.0

    Si el fichero falta, no parsea o esta vacio → dict vacio. El motor cae al
    flujo manual (coste 0 + comentario amarillo), mismo comportamiento de hoy.
    """
    if not os.path.exists(_SPINOFFS_CONOCIDOS_PATH):
        return {}
    try:
        with open(_SPINOFFS_CONOCIDOS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    out = {}
    for entry in data.get('spinoffs', []):
        if entry.get('_activo') is False:
            continue
        isin_e = (entry.get('isin_escindida') or '').strip().upper()
        isin_m = (entry.get('isin_matriz') or '').strip().upper()
        if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin_e):
            continue
        if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin_m):
            continue
        try:
            r_e = Decimal(str(entry['ratio_coste_escindida']))
            r_m = Decimal(str(entry['ratio_coste_matriz_residual']))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        # Invariante critico: r_e + r_m debe ser ~1.0 (sin esto el coste se
        # duplica o se evapora al sumar matriz_post + escindida).
        if abs((r_e + r_m) - Decimal('1')) > Decimal('0.0001'):
            continue
        if r_e <= 0 or r_e >= 1 or r_m <= 0 or r_m >= 1:
            continue
        fecha_str = entry.get('fecha_efectiva', '')
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha_str):
            continue
        try:
            fecha_eff = date(int(fecha_str[0:4]), int(fecha_str[5:7]), int(fecha_str[8:10]))
        except ValueError:
            continue
        out[isin_e] = {
            'isin_matriz':                 isin_m,
            'ticker_matriz':               entry.get('ticker_matriz', ''),
            'nombre_matriz':               entry.get('nombre_matriz', ''),
            'isin_escindida':              isin_e,
            'ticker_escindida':            entry.get('ticker_escindida', ''),
            'nombre_escindida':            entry.get('nombre_escindida', ''),
            'fecha_efectiva':              fecha_eff,
            'ratio_coste_escindida':       r_e,
            'ratio_coste_matriz_residual': r_m,
            'fuente':                      entry.get('fuente', ''),
            'ratio_canje':                 entry.get('ratio_canje', ''),
        }
    return out


def _coste_matriz_a_fecha(todas_ops, isin_matriz, fecha_efectiva):
    """Suma del coste FIFO restante del ISIN matriz a fecha_efectiva.

    Algoritmo: simula el inventario FIFO de la matriz consumido por sus ventas
    hasta el dia ANTERIOR a fecha_efectiva (las ops del mismo dia tambien
    cuentan como previas — el spin-off se aplica al cierre de la jornada).
    Devuelve Decimal('0') si no hay posicion viva.

    No modifica todas_ops; solo recorre y agrega. Asume que el coste de cada
    lote esta en `importe_eur` y la cantidad en `cantidad`. Splits previos NO
    se aplican aqui porque el motor multi-anyo los aplica via filas SP cuando
    se ejecuta calcular_fifo — pero `todas_ops` se le pasa a este helper
    ANTES del motor, asi que la matriz vive con sus cantidades originales.
    Para spin-offs en US (donde no hay splits anidados en el mismo evento)
    es suficiente.
    """
    if not isin_matriz or fecha_efectiva is None:
        return Decimal('0')

    from collections import deque
    eventos = []
    for op in todas_ops:
        if op.get('isin') != isin_matriz:
            continue
        tipo = op.get('tipo', '')
        if tipo not in ('A', 'AD', 'AL', 'T', 'TR', 'VD'):
            continue
        f = op.get('fecha')
        if f is None:
            continue
        # Normalizar a date — algunas ops llegan como datetime.
        if isinstance(f, str):
            try:
                d, mo, y = f.split('/')
                f = date(int(y), int(mo), int(d))
            except (ValueError, AttributeError):
                continue
        elif hasattr(f, 'date') and not isinstance(f, date):
            f = f.date()
        # Solo eventos estrictamente antes de fecha_efectiva. Si la matriz
        # tiene una compra el MISMO dia que el spin-off, se considera previa
        # (el ex-date del spin-off es el corte natural — los lotes adquiridos
        # ese mismo dia ya estan en cartera al cierre).
        if f > fecha_efectiva:
            continue
        eventos.append((f, op))

    # Orden cronologico, compras antes que ventas el mismo dia (para FIFO).
    eventos.sort(key=lambda t: (t[0], 0 if t[1]['tipo'] in ('A', 'AD', 'AL') else 1))

    lotes = deque()  # cada lote: {'cant': Decimal, 'coste_unit': Decimal}
    for _, op in eventos:
        tipo = op['tipo']
        try:
            cant = Decimal(str(op.get('cantidad', 0)))
            imp  = Decimal(str(op.get('importe_eur', 0)))
        except (InvalidOperation, ValueError):
            continue
        if cant <= 0:
            continue
        if tipo in ('A', 'AD', 'AL'):
            coste_unit = imp / cant if cant > 0 else Decimal('0')
            lotes.append({'cant': cant, 'coste_unit': coste_unit})
        else:  # T, TR, VD — venta consume FIFO
            restante = cant
            while restante > 0 and lotes:
                lote = lotes[0]
                consumir = min(restante, lote['cant'])
                lote['cant'] -= consumir
                restante -= consumir
                if lote['cant'] <= 0:
                    lotes.popleft()
            # Venta huerfana: no podemos restar mas — se ignora.

    coste_vivo = sum((l['cant'] * l['coste_unit'] for l in lotes), Decimal('0'))
    return coste_vivo.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _reducir_lotes_matriz_proporcional(todas_ops, isin_matriz, fecha_efectiva,
                                       ratio_residual):
    """Multiplica el `importe_eur` de cada operacion AD/A/AL del ISIN matriz
    con fecha <= fecha_efectiva por ratio_residual. NO modifica la cantidad.

    Preserva la dispersion de costes entre lotes (lotes caros siguen siendo
    proporcionalmente caros), exigido por Art. 37.1.a §4 LIRPF. Si se restara
    un importe fijo por accion (reparto lineal), el lote mas barato absorberia
    un peso desproporcionado y la dispersion historica se distorsionaria.

    Modifica `todas_ops` in-place. Anota `_matriz_ajustada_por_spinoff=True`
    en cada op tocada para que el comentario del XLSX lo refleje.
    """
    if not isin_matriz or fecha_efectiva is None:
        return
    ratio_residual = Decimal(str(ratio_residual))
    for op in todas_ops:
        if op.get('isin') != isin_matriz:
            continue
        if op.get('tipo') not in ('A', 'AD', 'AL'):
            continue
        f = op.get('fecha')
        if f is None:
            continue
        # Normalizar a `date`. Las ops de los parsers vienen con fecha
        # como str 'DD/MM/YYYY' (formato ES); otras pueden venir como
        # datetime o date. Si no parsea, saltamos la op para no romper.
        if isinstance(f, str):
            try:
                d, mo, y = f.split('/')
                f = date(int(y), int(mo), int(d))
            except (ValueError, AttributeError):
                continue
        elif hasattr(f, 'date') and not isinstance(f, date):
            f = f.date()
        if f > fecha_efectiva:
            continue
        try:
            importe_pre = Decimal(str(op.get('importe_eur', 0)))
        except (InvalidOperation, ValueError):
            continue
        op['importe_eur'] = (importe_pre * ratio_residual).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )
        op['_matriz_ajustada_por_spinoff'] = True


_SPLITS_CONOCIDOS_PATH = os.path.join(BASE_DIR, 'splits_conocidos.json')


def _load_splits_conocidos():
    """Carga el catalogo estatico de splits para emisores que TR no emite
    como linea CSV. Devuelve solo las entradas activas (sin _activo=false
    y con fecha valida YYYY-MM-DD)."""
    if not os.path.exists(_SPLITS_CONOCIDOS_PATH):
        return []
    try:
        with open(_SPLITS_CONOCIDOS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for entry in data.get('splits', []):
        if entry.get('_activo') is False:
            continue
        fecha = entry.get('fecha', '')
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha):
            continue
        isin = (entry.get('isin') or '').strip().upper()
        if not re.match(r'^[A-Z]{2}[A-Z0-9]{10}$', isin):
            continue
        try:
            qty_old = float(entry['titulos_antiguos'])
            qty_new = float(entry['titulos_nuevos'])
        except (KeyError, TypeError, ValueError):
            continue
        if qty_old <= 0 or qty_new <= 0 or qty_old == qty_new:
            continue
        out.append({
            'isin':    isin,
            'ticker':  entry.get('ticker', ''),
            'nombre':  entry.get('nombre', '') or entry.get('ticker', ''),
            'fecha':   fecha,
            'qty_old': qty_old,
            'qty_new': qty_new,
            'fuente':  entry.get('fuente', ''),
        })
    return out


# Ratios de split admitidos por la heuristica TR. Solo enteros que se
# corresponden con splits historicos reales sobre acciones (NVDA 10, GOOGL
# 20, AMZN 20, TSLA 3/5, AAPL 4/7, WMT 3, BRK.B 50). Excluimos 6 (cascada
# 2x3), 8/9/11 (no observados) y >50 (rarisimo) para reducir falsos
# positivos. Fuente — verificacion 2026-06: NVIDIA Q1 FY25 (22-may-2024,
# 10-for-1 ex-10-jun-2024), Alphabet Q4 2021 earnings (1-feb-2022, 20-for-1
# ex-18-jul-2022), Amazon 9-mar-2022 (20-for-1 ex-6-jun-2022), Tesla 2020
# y 2022, Apple 4-for-1 ex-31-ago-2020, Walmart 3-for-1 ex-26-feb-2024.
_TR_SPLITS_RATIOS_ADMITIDOS = (2, 3, 4, 5, 7, 10, 20)

# Tolerancia para considerar factor como ratio entero (0,5%).
# Lo suficientemente estrecha para que un "factor = 2,05" se rechace
# (evitamos confundir scrip dividends pequenos con splits) y un
# "factor = 10,00" se acepte limpiamente.
_TR_SPLITS_TOL = Decimal("0.005")


def parse_tr_splits(filepath):
    """
    Heuristica conservadora para detectar splits que Trade Republic aplica
    silenciosamente sobre la posicion sin emitir linea CSV.

    Premisa (verificada via brokerchooser / eupersonalfinance / curvo.eu
    2026-06): TR NO permite venta corta sobre acciones reales. Cualquier
    operacion corta solo es posible via derivados/Knock-Outs HSBC-SG-UBS-
    Vontobel, que viajan en el CSV como SYNTHETIC y se filtran fuera.
    Por tanto, si la suma de SELL_qty supera la suma de BUY_qty para una
    accion (STOCK), el unico mecanismo posible es una inflacion silenciosa
    de cantidades — es decir, un split forward.

    Reglas (todas obligatorias):
      1. asset_class = 'STOCK' (excluye FUND, CRYPTO, BOND, SYNTHETIC —
         derivados no aplican y los UCITS pueden tener mecanicas distintas).
      2. ISIN ISO valido con al menos una operacion BUY y una SELL en el CSV.
      3. SUM(SELL_qty) / SUM(BUY_qty) cae dentro de {2, 3, 4, 5, 7, 10, 20}
         con tolerancia 0,5%.
      4. No emitir SP si la primera operacion del ISIN ya es post-split
         conocido (caso "compre todo despues del split"): se cubre solo si
         hay catalogo y la fecha de la primera op > fecha catalogo.

    Limitaciones:
      - SOLO funciona si el usuario vendio TODA la posicion post-split. Si
         conserva acciones, el factor SELL/BUY queda fraccionario y no
         cuadra con ningun ratio. Esos casos siguen disparando el warning
         de orfana — son irresolubles sin conocer el balance final.
      - SOLO funciona si el CSV contiene tanto la BUY como la SELL. En
         multi-anio, el CSV TR es unificado y cubre toda la historia, asi
         que esto suele cumplirse. Si el usuario tuvo la posicion en otro
         broker y la traspaso, la BUY no esta y no podremos inferir.

    Refinamiento (no critico): si el ISIN aparece en `splits_conocidos.json`
    con factor coincidente y fecha entre las operaciones del CSV, se usa
    la fecha exacta del catalogo en lugar del dia posterior a la ultima BUY.

    CONTRASPLITS (reverse splits): la heuristica de exhaustion no puede
    detectarlos (tras un 10:1 inverso, "compre 100 / vendi 10" es identico
    a "conservo 90 titulos"). Solo se emiten cuando `splits_conocidos.json`
    documenta un contrasplit (titulos_nuevos < titulos_antiguos) para un
    ISIN con alguna BUY anterior a la fecha ex-split. Un contrasplit no
    catalogado sigue siendo indetectable — anadir la entrada al catalogo
    es la unica via.

    Devuelve: list[dict] con filas SP listas para extend a `todas_sp`.
    """
    if not os.path.exists(filepath):
        return []

    from collections import defaultdict
    from datetime import date as _date, timedelta as _td

    # Por ISIN: suma de BUY/SELL, ultima fecha BUY, primera fecha SELL,
    # primera fecha cualquiera (para descartar splits anteriores al CSV).
    info = defaultdict(lambda: {
        'buy': Decimal('0'), 'sell': Decimal('0'),
        'nombre': '',
        'max_buy_iso': '', 'first_sell_iso': '', 'first_any_iso': '',
        'first_buy_iso': '',
    })

    f, reader = _tr_open_reader(filepath)
    try:
        for row in reader:
            type_raw = (row.get('type') or '').strip().upper()
            if type_raw not in ('BUY', 'SELL'):
                continue
            asset_class = (row.get('asset_class') or '').strip().upper()
            if asset_class != 'STOCK':
                continue
            symbol = (row.get('symbol') or '').strip().upper()
            if not _RE_TR_ISIN_ISO.match(symbol):
                continue
            fecha_iso = (row.get('date') or '').strip()
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha_iso):
                continue
            try:
                qty = abs(parse_es(row.get('shares', '') or '0'))
            except (ValueError, InvalidOperation):
                continue
            if qty <= 0:
                continue
            nombre = (row.get('name') or '').strip()[:50]
            bucket = info[symbol]
            bucket['nombre'] = bucket['nombre'] or nombre
            if not bucket['first_any_iso'] or fecha_iso < bucket['first_any_iso']:
                bucket['first_any_iso'] = fecha_iso
            if type_raw == 'BUY':
                bucket['buy'] += qty
                if fecha_iso > bucket['max_buy_iso']:
                    bucket['max_buy_iso'] = fecha_iso
                if not bucket['first_buy_iso'] or fecha_iso < bucket['first_buy_iso']:
                    bucket['first_buy_iso'] = fecha_iso
            else:  # SELL
                bucket['sell'] += qty
                if not bucket['first_sell_iso'] or fecha_iso < bucket['first_sell_iso']:
                    bucket['first_sell_iso'] = fecha_iso
    finally:
        f.close()

    catalogo = _load_splits_conocidos()
    catalogo_by_isin = defaultdict(list)
    for entry in catalogo:
        catalogo_by_isin[entry['isin']].append(entry)

    sp_ops = []
    for isin, d in info.items():
        if d['buy'] <= 0 or d['sell'] <= 0:
            continue
        if d['sell'] <= d['buy']:
            continue
        # La ultima BUY tiene que ser anterior a la primera SELL — si hubo
        # SELL antes que cualquier BUY, no es un caso de split sino de
        # huerfana real (transferencia de otro broker, export truncado).
        if not d['max_buy_iso'] or not d['first_sell_iso']:
            continue
        if d['max_buy_iso'] >= d['first_sell_iso']:
            # Posible cascada (BUY/SELL intercalados): no aplicamos la
            # heuristica de exhaustion total porque la inferencia se
            # ensucia. Caso conservador: dejar warning.
            continue

        factor = d['sell'] / d['buy']
        ratio = None
        for r in _TR_SPLITS_RATIOS_ADMITIDOS:
            if abs(factor - Decimal(r)) / Decimal(r) < _TR_SPLITS_TOL:
                ratio = r
                break
        if ratio is None:
            continue

        # Buscar coincidencia exacta en catalogo: mismo ISIN, mismo ratio,
        # fecha entre max_buy_iso y first_sell_iso. Si la hay, usar la
        # fecha del catalogo (mas precisa fiscalmente).
        split_iso = None
        fuente_catalogo = ''
        for entry in catalogo_by_isin.get(isin, []):
            if entry['qty_old'] == 1 and entry['qty_new'] == ratio:
                if d['max_buy_iso'] < entry['fecha'] <= d['first_sell_iso']:
                    split_iso = entry['fecha']
                    fuente_catalogo = entry['fuente'][:80]
                    break
        if split_iso is None:
            # Fallback: dia siguiente a la ultima BUY.
            y, m, dd = d['max_buy_iso'].split('-')
            split_iso = (_date(int(y), int(m), int(dd)) + _td(days=1)).isoformat()

        fecha_es = parse_date(split_iso)
        descripcion = (
            f"{d['nombre']} 1:{ratio} inferido (TR no emite linea; sells "
            f"{d['sell']} = buys {d['buy']} x {ratio})"
        )
        if fuente_catalogo:
            descripcion += f" [catalogo: {fuente_catalogo}]"
        evento = {
            'tipo_ca':     'SPLIT',
            'fecha':       fecha_es,
            'nombre':      d['nombre'][:50],
            'isin_old':    isin,
            'isin_new':    isin,
            'qty_old':     1,
            'qty_new':     ratio,
            'descripcion': descripcion[:150],
        }
        row_sp = build_sp_row(evento)
        row_sp['broker'] = 'TR'
        row_sp['_heuristica_tr'] = True
        sp_ops.append(row_sp)

    # ── Contrasplits (reverse splits) — SOLO via catalogo ────────────────
    # La heuristica de exhaustion NO puede detectar un contrasplit: tras un
    # 10:1 inverso, "compre 100 / vendi 10" es indistinguible de "conservo
    # 90 titulos" mirando solo el CSV. Sin ajuste, el FIFO casaria los 10
    # titulos vendidos contra las primeras compras a 1/10 del coste real —
    # cifra erronea sin warning (auditoria 2026-06-11, CL6). Por eso el
    # contrasplit solo se emite cuando el catalogo verificado lo documenta
    # y el CSV muestra al menos una BUY anterior a la fecha ex-split (la
    # posicion pudo existir al aplicarse). Si en esa fecha no quedaban
    # lotes vivos, _apply_split del motor es un no-op — sin riesgo de
    # ajuste fantasma.
    for isin, d in info.items():
        if d['buy'] <= 0:
            continue
        for entry in catalogo_by_isin.get(isin, []):
            if entry['qty_new'] >= entry['qty_old']:
                continue  # forward split → lo cubre la heuristica de arriba
            if not d['first_buy_iso'] or d['first_buy_iso'] >= entry['fecha']:
                continue  # toda la posicion es post-contrasplit: nada que ajustar
            nombre = d['nombre'] or entry['nombre']
            descripcion = (
                f"{nombre} contrasplit {entry['qty_old']:g}:{entry['qty_new']:g} "
                f"de catalogo (TR no emite linea) [{entry['fuente'][:60]}]"
            )
            evento = {
                'tipo_ca':     'SPLIT',
                'fecha':       parse_date(entry['fecha']),
                'nombre':      nombre[:50],
                'isin_old':    isin,
                'isin_new':    isin,
                'qty_old':     entry['qty_old'],
                'qty_new':     entry['qty_new'],
                'descripcion': descripcion[:150],
            }
            row_sp = build_sp_row(evento)
            row_sp['broker'] = 'TR'
            row_sp['_heuristica_tr'] = True
            sp_ops.append(row_sp)

    return sp_ops


def parse_tr_corporate_actions(filepath, prev_csv_paths=None):
    """
    Procesa scrip dividends de Trade Republic (Art. 37.1.a §4 LIRPF + reforma
    Ley 26/2014). Detecta clusters de eventos CORPORATE_ACTION / CASH alrededor
    del ISIN sintético de los derechos (SYNTHETIC, suele acabar en -ANR-) y los
    clasifica en una de las 3 opciones del scrip:

      - Opción A (canje por acciones): INTERMEDIATE_SECURITIES_DISTRIBUTION +
        DIVIDEND_REINVESTMENT / EXCHANGE entrega acciones nuevas (a veces con
        ISIN temporal EM.01/YY que PARI_PASSU fusiona con la ordinaria).
        Doctrina: emisión gratuita → coste 0 y prorrateo del coste medio
        sobre la posición ordinaria (Art. 37.1.a §4 LIRPF). Emite fila A
        con importe=0 y es_scrip=True para que motor_fiscal aplique el
        prorrateo automáticamente al vender.

      - Opción B (venta de derechos en mercado): INTERMEDIATE + SELL del ISIN
        SYNTHETIC en BME durante el periodo de cotización. Doctrina post-Ley
        26/2014: ganancia patrimonial pura (Art. 33.1 LIRPF), no reduce el
        coste de la matriz, casillas 0341-0355 (derechos) o 0326-0340 según
        ejercicio. Emite fila A virtual con coste 0 para que el SELL del CSV
        (ya procesado por parse_tr) cierre el FIFO con base 100% imputable.

      - Opción C (recompra por el emisor): INTERMEDIATE + LIQUIDATION_DIVIDEND
        sobre los derechos + CASH LIQUIDATION_PROCEEDS con el importe en
        efectivo. Doctrina post-Ley 26/2014: rendimiento del capital
        mobiliario (Art. 25.1.a LIRPF), casilla 0029. Emite fila DIV con
        el importe neto + RET con la retención si TR la aplicó (lo hace
        post-migración IBAN ES desde jul-2025, BOE-A-2025-5909).

    Eventos DIVIDEND_REINVESTMENT_CANCELLED + EXCHANGE forman pares de
    corrección retroactiva interna de TR (TR re-codifica DIVIDEND_REINVESTMENT
    como EXCHANGE post-hoc); el flujo neto del cluster es idéntico.

    El ISIN matriz se identifica por prefix de emisor (primera palabra del
    nombre, antes de coma/espacio). Si TR exporta el cluster sin BUY/SELL
    previo de la ordinaria en el mismo CSV, el cluster queda como
    'ciclos_sin_match_matriz' y se documenta en el informe de corporativas
    como caso a revisar manualmente.

    Devuelve: (operaciones_scrip, dividendos_scrip, descartadas)
      - operaciones_scrip: list de dicts tipo='A' con es_scrip=True
        (compatibles con parse_tr output).
      - dividendos_scrip: list de dicts tipo='DIV'/'RET' (compatibles con
        parse_tr_dividendos output).
      - descartadas: dict {ciclos_a, ciclos_b, ciclos_c, ciclos_sin_match,
        ciclos_indeterminados, eventos_huerfanos}.
    """
    descartadas = {
        'ciclos_a': 0, 'ciclos_b': 0, 'ciclos_c': 0,
        'ciclos_sin_match': 0, 'ciclos_indeterminados': 0,
        'eventos_huerfanos': 0,
    }
    if not os.path.exists(filepath):
        return [], [], descartadas

    rows = []
    f, reader = _tr_open_reader(filepath)
    try:
        for r in reader:
            rows.append(r)
    finally:
        f.close()

    # Auto-discovery de CSVs históricos del mismo broker para resolver el
    # ISIN matriz cuando la posición se compró en años anteriores. Sin esto
    # un scrip Opción A sobre una posición vieja queda como 'ciclos_sin_match'.
    if prev_csv_paths is None:
        prev_csv_paths = []
        base_dir = os.path.dirname(filepath)
        m_year = re.search(r'_(\d{4})\.csv$', filepath)
        if m_year and base_dir:
            year = int(m_year.group(1))
            for y in range(year - 10, year):
                candidate = os.path.join(
                    base_dir, f"TR_Transacciones_{y}.csv"
                )
                if os.path.exists(candidate):
                    prev_csv_paths.append(candidate)

    # 1. Índice de ISIN ordinario por prefix de emisor. Solo cuentan BUY/SELL
    # reales (no DIVIDEND_REINVESTMENT que entrega EM.01/YY transitorios).
    # Construido a partir del CSV actual + todos los CSVs históricos para
    # cubrir posiciones abiertas en años anteriores.
    emisor_to_isin = {}
    emisor_to_name = {}

    def _index_rows(rows_iter):
        for r in rows_iter:
            cat   = (r.get('category') or '').strip().upper()
            type_ = (r.get('type')     or '').strip().upper()
            asset = (r.get('asset_class') or '').strip().upper()
            isin  = (r.get('symbol')   or '').strip()
            name  = (r.get('name')     or '').strip()
            if (cat == 'TRADING' and type_ in ('BUY', 'SELL')
                    and asset in ('STOCK', 'FUND', 'BOND')
                    and _RE_TR_ISIN_ISO.match(isin)):
                e = _tr_emisor_prefix(name)
                if e and e not in emisor_to_isin:
                    emisor_to_isin[e] = isin
                    emisor_to_name[e] = name[:50]

    _index_rows(rows)
    for prev_path in prev_csv_paths:
        try:
            f_prev, reader_prev = _tr_open_reader(prev_path)
            try:
                _index_rows(reader_prev)
            finally:
                f_prev.close()
        except (IOError, OSError):
            continue

    # 2. Recolectar clusters por ISIN de derecho. Cada cluster acumula:
    #   - INTERMEDIATE: derechos asignados
    #   - DIVIDEND_REINVESTMENT / EXCHANGE STOCK: acciones nuevas (Opción A)
    #   - DIVIDEND_REINVESTMENT_CANCELLED: par de cancelación (se compensa)
    #   - PARI_PASSU STOCK: fusión EM con ordinaria
    #   - LIQUIDATION_DIVIDEND SYNTHETIC: recompra emisor (Opción C)
    #   - SELL SYNTHETIC: venta en mercado (Opción B)
    #   - CASH LIQUIDATION_PROCEEDS: cobro Opción C (matched por ISIN en
    #     description "for ISIN <X>")
    clusters = {}  # right_isin -> dict
    cash_proceeds_by_right_isin = {}  # right_isin -> list of (fecha, amount)
    cash_tax_by_right_isin      = {}  # right_isin -> list of (fecha, amount, tax)

    def _get_cluster(right_isin, right_name, date):
        if right_isin not in clusters:
            clusters[right_isin] = {
                'right_isin':      right_isin,
                'right_name':      right_name,
                'emisor':          _tr_emisor_prefix(right_name),
                'shares_assigned': Decimal('0'),
                'date_assigned':   date,
                'shares_new_in':   Decimal('0'),   # Opción A net (post-cancel)
                'date_new':        None,
                'isin_new_stock':  '',             # ISIN EM.01/YY o destino PARI_PASSU
                'shares_liquid':   Decimal('0'),   # Opción C net (synthetic out)
                'date_liquid':     None,
                'has_market_sell': False,          # Opción B detector
            }
        return clusters[right_isin]

    for r in rows:
        cat   = (r.get('category') or '').strip().upper()
        type_ = (r.get('type')     or '').strip().upper()
        asset = (r.get('asset_class') or '').strip().upper()
        isin  = (r.get('symbol')   or '').strip()
        name  = (r.get('name')     or '').strip()
        date  = parse_date_dt((r.get('date') or '').strip())
        desc  = (r.get('description') or '').strip()
        if not date:
            continue

        # CASH LIQUIDATION_PROCEEDS: cobro Opción C. Symbol viene vacío en TR;
        # extraer ISIN del derecho desde "Liquidation … for ISIN <X>".
        if cat == 'CASH' and type_ == 'LIQUIDATION_PROCEEDS':
            try:
                amount = parse_es(r.get('amount', '') or '0')
            except (ValueError, InvalidOperation):
                continue
            m = _RE_TR_ISIN_IN_DESC.search(desc)
            if not m or amount <= 0:
                continue
            ri = m.group(1)
            cash_proceeds_by_right_isin.setdefault(ri, []).append((date, amount))
            continue

        # CASH DIVIDEND adyacente al ciclo de recompra: la retención sobre el
        # LIQUIDATION (jul-2025 post-migración tuvo split DE+ES → ratio
        # observado ~16 %, no 19 % nacional puro). El amount aquí es la
        # retención (negativa); el shares trae los derechos como referencia
        # cantidad.
        if cat == 'CASH' and type_ == 'DIVIDEND' and not _RE_TR_ISIN_ISO.match(isin):
            try:
                amount = parse_es(r.get('amount', '') or '0')
            except (ValueError, InvalidOperation):
                continue
            m = _RE_TR_ISIN_IN_DESC.search(desc)
            if not m:
                continue
            ri = m.group(1)
            # La retención en este formato anómalo viene como amount<0; el
            # bruto/neto del cash va en el LIQUIDATION_PROCEEDS de arriba.
            if amount < 0:
                cash_tax_by_right_isin.setdefault(ri, []).append((date, abs(amount)))
            continue

        if type_ not in _TR_CORP_ACTION_TYPES:
            continue

        try:
            shares = parse_es(r.get('shares', '') or '0')
        except (ValueError, InvalidOperation):
            shares = Decimal('0')

        # INTERMEDIATE: marca inicio de cluster (asignación derechos)
        if type_ == 'INTERMEDIATE_SECURITIES_DISTRIBUTION' and asset == 'SYNTHETIC':
            if not _RE_TR_ISIN_ISO.match(isin) or shares <= 0:
                continue
            c = _get_cluster(isin, name, date)
            c['shares_assigned'] = shares
            c['date_assigned']   = date
            continue

        # DIVIDEND_REINVESTMENT entrega STOCK (Opción A — acciones nuevas)
        # DIVIDEND_REINVESTMENT consume SYNTHETIC (mismo cluster)
        # EXCHANGE: re-emisión retroactiva del mismo movimiento
        # DIVIDEND_REINVESTMENT_CANCELLED: cancelación (se compensa con su
        # pareja del mismo type_=DIVIDEND_REINVESTMENT)
        if type_ in ('DIVIDEND_REINVESTMENT', 'DIVIDEND_REINVESTMENT_CANCELLED',
                     'EXCHANGE'):
            if not _RE_TR_ISIN_ISO.match(isin):
                continue
            if asset == 'SYNTHETIC':
                # consume derechos del cluster — buscar por right_isin
                c = _get_cluster(isin, name, date)
                # cantidad negativa = consumo (canje); positiva = devolución
                # (DIVIDEND_REINVESTMENT_CANCELLED suele devolver +). El neto
                # tras cancelaciones se calcula sobre shares_new_in del STOCK.
                continue
            elif asset == 'STOCK':
                # acciones recibidas (canje). El ISIN puede ser temporal
                # (EM.01/YY) y luego PARI_PASSU lo fusiona con la ordinaria.
                # Necesitamos vincular al cluster — el nombre comparte emisor
                # con el derecho del INTERMEDIATE.
                emisor = _tr_emisor_prefix(name)
                # Buscar cluster abierto del mismo emisor (el más reciente
                # con date_assigned <= date).
                candidatos = [c for c in clusters.values()
                              if c['emisor'] == emisor
                              and c['date_assigned'] <= date]
                if not candidatos:
                    descartadas['eventos_huerfanos'] += 1
                    continue
                c = max(candidatos, key=lambda x: x['date_assigned'])
                c['shares_new_in'] += shares  # suma con cancelaciones (signo)
                if c['date_new'] is None or date > c['date_new']:
                    c['date_new'] = date
                # Anotar el ISIN si es el último entregado (post-PARI_PASSU
                # lo sobreescribiremos con la ordinaria).
                if shares > 0 and not c['isin_new_stock']:
                    c['isin_new_stock'] = isin
                continue

        # PARI_PASSU: fusión del ISIN EM.01/YY con la ordinaria del emisor.
        # Emite -X en EM.01/YY y +X en la ordinaria; el efecto es cambiar
        # el isin_new_stock del cluster.
        if type_ == 'PARI_PASSU' and asset == 'STOCK':
            if not _RE_TR_ISIN_ISO.match(isin):
                continue
            emisor = _tr_emisor_prefix(name)
            candidatos = [c for c in clusters.values()
                          if c['emisor'] == emisor
                          and c['date_assigned'] <= date]
            if not candidatos:
                continue
            c = max(candidatos, key=lambda x: x['date_assigned'])
            # +X significa "este ISIN es ahora el destino final".
            if shares > 0:
                c['isin_new_stock'] = isin
            continue

        # LIQUIDATION_DIVIDEND: recompra emisor (Opción C). Synthetic out.
        if type_ == 'LIQUIDATION_DIVIDEND' and asset == 'SYNTHETIC':
            if not _RE_TR_ISIN_ISO.match(isin):
                continue
            c = _get_cluster(isin, name, date)
            # shares negativos = retiro de derechos
            c['shares_liquid'] += abs(shares) if shares < 0 else shares
            if c['date_liquid'] is None or date > c['date_liquid']:
                c['date_liquid'] = date
            continue

    # 3. Detector Opción B: SELL del ISIN sintético en mercado. parse_tr ya
    # lo procesa como T normal; nosotros solo marcamos el cluster para emitir
    # la entrada virtual de coste 0 (sin la cual el FIFO no encontraría lote).
    for r in rows:
        cat   = (r.get('category') or '').strip().upper()
        type_ = (r.get('type')     or '').strip().upper()
        asset = (r.get('asset_class') or '').strip().upper()
        isin  = (r.get('symbol')   or '').strip()
        if (cat == 'TRADING' and type_ == 'SELL' and asset == 'SYNTHETIC'
                and isin in clusters):
            clusters[isin]['has_market_sell'] = True

    # 4. Clasificar cada cluster y emitir filas.
    operaciones_scrip = []
    dividendos_scrip  = []

    for right_isin, c in clusters.items():
        if c['shares_assigned'] <= 0:
            descartadas['eventos_huerfanos'] += 1
            continue
        emisor = c['emisor']
        isin_matriz = emisor_to_isin.get(emisor, '')
        nombre_matriz = emisor_to_name.get(emisor, c['right_name'][:50])

        is_a = c['shares_new_in'] > 0
        is_c = c['shares_liquid'] > 0
        is_b = c['has_market_sell']

        # Si concurren A y C (raro — caso mixto histórico), priorizar C
        # cuando la cantidad neta acciones_in es 0 (todos los derechos se
        # acabaron recomprando). Si shares_new_in > 0 strict, A prevalece.
        if is_a and is_c and c['shares_new_in'] <= 0:
            is_a = False
        if is_a and is_b:
            # mixto: parte canje + parte venta. No común; documentar como
            # indeterminado para revisión manual.
            descartadas['ciclos_indeterminados'] += 1
            continue

        # Sin match de matriz por emisor: no podemos atribuir el coste 0 a
        # ninguna posición. Documentar.
        if (is_a or is_b) and not isin_matriz:
            descartadas['ciclos_sin_match'] += 1
            continue

        if is_a:
            # Emisión gratuita: añadir acciones a la posición matriz con
            # coste 0. El destino es PARI_PASSU (ordinaria) si lo hubo, o
            # el ISIN entregado por DIVIDEND_REINVESTMENT/EXCHANGE.
            isin_destino = c['isin_new_stock'] or isin_matriz
            # Si el destino es un ISIN temporal (EM.01/YY) que NUNCA tuvo
            # un PARI_PASSU dentro del CSV, lo redirigimos a la matriz por
            # consistencia FIFO — el contribuyente acaba con acciones
            # ordinarias.
            if isin_destino != isin_matriz and isin_destino not in emisor_to_isin.values():
                isin_destino = isin_matriz
            fecha_dt = c['date_new'] or c['date_assigned']
            operaciones_scrip.append({
                'tipo': 'A', 'isin': isin_destino, 'nombre': nombre_matriz,
                'fecha': fecha_dt.strftime('%d/%m/%Y'),
                'cantidad': c['shares_new_in'],
                'importe_eur': Decimal('0'),
                'gastos_eur': Decimal('0'),
                'es_scrip': True,
                'instrument_type': 'STOCK',
                'broker': 'TR',
                'asset_class': 'STOCK',
                'origen': 'scrip_canje',
                'scrip_right_isin': right_isin,
            })
            descartadas['ciclos_a'] += 1
            continue

        if is_b:
            # Venta de derechos en mercado: emitir entrada virtual con coste 0
            # bajo el MISMO ISIN del derecho (que es el que parse_tr usa para
            # el SELL). Así el FIFO de ese ISIN se cierra contra esta entrada
            # y la ganancia equivale al importe íntegro de la venta.
            operaciones_scrip.append({
                'tipo': 'A', 'isin': right_isin, 'nombre': c['right_name'][:50],
                'fecha': c['date_assigned'].strftime('%d/%m/%Y'),
                'cantidad': c['shares_assigned'],
                'importe_eur': Decimal('0'),
                'gastos_eur': Decimal('0'),
                'es_scrip': True,
                'instrument_type': 'STOCK',  # TODO: 'DERECHO' cuando motor lo soporte
                'broker': 'TR',
                'asset_class': 'SYNTHETIC',
                'origen': 'scrip_venta_mercado',
                'scrip_right_isin': right_isin,
            })
            descartadas['ciclos_b'] += 1
            continue

        if is_c:
            # Recompra por emisor: RCM Art. 25.1.a LIRPF. Match con
            # CASH LIQUIDATION_PROCEEDS por right_isin.
            proceeds = cash_proceeds_by_right_isin.get(right_isin, [])
            taxes    = cash_tax_by_right_isin.get(right_isin, [])
            if not proceeds:
                # LIQUIDATION_DIVIDEND sin proceeds cash → posiblemente
                # ciclo a caballo entre años en CSV multi-año; documentar.
                descartadas['ciclos_indeterminados'] += 1
                continue
            neto = sum(p[1] for p in proceeds)
            retencion = sum(t[1] for t in taxes)
            bruto = neto + retencion
            fecha_cobro_dt = max(p[0] for p in proceeds)
            fecha_cobro = fecha_cobro_dt.strftime('%d/%m/%Y')
            pais = _pais_de_isin(isin_matriz or right_isin, nombre_matriz)
            dividendos_scrip.append({
                'fecha': fecha_cobro,
                'isin': isin_matriz or right_isin,
                'nombre': nombre_matriz,
                'tipo': 'DIV',
                'pais': pais,
                'importe_eur': bruto,
                'divisa': 'EUR',
                'broker': 'TR',
                'bruto_original': '',
                'fx_rate': '',
                'origen': 'scrip_recompra_emisor',
                'scrip_right_isin': right_isin,
            })
            if retencion > 0:
                # Split origen/ES igual que parse_tr_dividendos: pais=ES
                # → todo nacional. Para clusters extranjeros (rare en scrip
                # — la mayoría son ES emisor ACS/IBE/etc.) aplicaría el CDI.
                if pais == 'ES':
                    dividendos_scrip.append({
                        'fecha': fecha_cobro, 'isin': isin_matriz or right_isin,
                        'nombre': nombre_matriz, 'tipo': 'RET',
                        'pais': 'ES',
                        'importe_eur': retencion, 'divisa': 'EUR',
                        'broker': 'TR',
                    })
                else:
                    cdi_rate = DTA_SOURCE_MAX.get(pais, Decimal('0'))
                    source_max = (bruto * cdi_rate).quantize(
                        Decimal('0.01'), ROUND_HALF_UP)
                    source_ret = min(retencion, source_max)
                    es_ret     = retencion - source_ret
                    if source_ret > 0:
                        dividendos_scrip.append({
                            'fecha': fecha_cobro, 'isin': isin_matriz or right_isin,
                            'nombre': nombre_matriz, 'tipo': 'RET',
                            'pais': pais,
                            'importe_eur': source_ret, 'divisa': 'EUR',
                            'broker': 'TR',
                        })
                    if es_ret > 0:
                        dividendos_scrip.append({
                            'fecha': fecha_cobro, 'isin': isin_matriz or right_isin,
                            'nombre': nombre_matriz, 'tipo': 'RET',
                            'pais': 'ES',
                            'importe_eur': es_ret, 'divisa': 'EUR',
                            'broker': 'TR',
                        })
            descartadas['ciclos_c'] += 1
            continue

        # Cluster con INTERMEDIATE pero sin desenlace (A/B/C) — ciclo abierto
        # entre años o evento huérfano.
        descartadas['ciclos_indeterminados'] += 1

    return operaciones_scrip, dividendos_scrip, descartadas


# ── Informe de acciones corporativas ───────────────────────────────────────

def write_informe_corporativas(todos_eventos, filepath,
                               derechos_ventas_b_mercado=None,
                               derechos_ventas_warn=None,
                               derechos_clasificados=None,
                               liberadas_scrip=None):
    """
    Genera un informe legible con todos los eventos corporativos detectados.
    Este informe sirve para que el usuario verifique y complete en RentaWEB
    los eventos que no se pueden incluir automáticamente (derechos, complejos).

    derechos_ventas_b_mercado: list de ops TYPE B vendidas en mercado → G/P derechos
                               (están en el CSV como T; se documentan en el informe)
    derechos_ventas_warn     : list de ops con ISIN de derecho SIN CLASIFICAR
    derechos_clasificados    : dict ISIN → {tipo, emisor, descripcion, ...}
    liberadas_scrip          : list de ops A (acciones liberadas coste 0) generadas

    DOCTRINA FISCAL:
      TYPE B vendido en mercado (Order ID) → G/P patrimonial → Art. 37.1.a LIRPF
                                             Casillas 341-346 (derechos suscripción)
      TYPE B precio comprometido empresa   → RCM             → Art. 25.1.a LIRPF → casilla 0029
      TYPE B acciones liberadas (canje)    → sin renta este año. Coste prorrateado
                                             sobre la posición previa (Art. 37.1.a §4 LIRPF).
                                             En ejercicios mixtos (con compra de derechos)
                                             el coste asignado = precio de los derechos comprados.
    """
    if derechos_ventas_b_mercado is None: derechos_ventas_b_mercado = []
    if derechos_ventas_warn      is None: derechos_ventas_warn      = []
    if derechos_clasificados     is None: derechos_clasificados      = {}
    if liberadas_scrip           is None: liberadas_scrip            = []
    # Casillas aplicables al ejercicio (AEAT renumera año a año)
    _CAS = _load_casillas_ejercicio(EJERCICIO)
    c_div      = _CAS['divs']
    c_cdi      = _CAS['cdi']
    c_ret_es   = _CAS['retencion_es']
    c_acc      = _CAS['acciones_detalle']
    c_der      = _CAS['derechos']
    c_otros    = _CAS['otros']
    lines = []
    lines.append(f"INFORME DE ACCIONES CORPORATIVAS — Ejercicio {EJERCICIO}")
    lines.append("=" * 65)
    lines.append(f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    lines.append(f"Casillas RentaWEB: ejercicio {EJERCICIO} (campaña {_CAS['campana']}). "
                 f"AEAT renumera año a año — verificar si cambió en la campaña actual.")
    lines.append("")
    lines.append("NOTA LEGAL (Art. 37.3 LIRPF):")
    lines.append("  Splits y contrasplits NO generan ganancia ni pérdida patrimonial.")
    lines.append("  Las acciones recibidas conservan la fecha de adquisición original.")
    lines.append("  El coste total de adquisición se redistribuye entre el nuevo nº de títulos.")
    lines.append("")

    def seccion(titulo, eventos, detalle_fn):
        lines.append(f"{'─'*65}")
        lines.append(f"{titulo} ({len(eventos)} eventos)")
        lines.append(f"{'─'*65}")
        if not eventos:
            lines.append("  Ninguno detectado.")
        for ev in eventos:
            lines.append(detalle_fn(ev))
        lines.append("")

    sp_events     = [e for e in todos_eventos if e.get('tipo_ca') in (CA_SPLIT, CA_CONTRASPLIT)]
    isin_events   = [e for e in todos_eventos if e.get('tipo_ca') == CA_ISIN_CHANGE]
    rts_events    = [e for e in todos_eventos if e.get('tipo_ca') == CA_RIGHTS]
    resid_events  = [e for e in todos_eventos if e.get('tipo_ca') == CA_RESIDUAL]
    cplx_events   = [e for e in todos_eventos if e.get('tipo_ca') == CA_COMPLEX]
    name_events   = [e for e in todos_eventos if e.get('tipo_ca') == CA_NAME_CHANGE]
    spin_events   = [e for e in todos_eventos if e.get('tipo_ca') == CA_SPIN_OFF]
    rxe_events    = [e for e in todos_eventos if e.get('tipo_ca') == CA_RIGHTS_EXERCISED]
    mt_events     = [e for e in todos_eventos if e.get('tipo_ca') == CA_MARKET_TRANSFER]
    corto_events  = [e for e in todos_eventos if e.get('tipo_ca') == CA_CORTO_FORZADO]

    def fmt_sp(ev):
        qo = ev.get('qty_old', 0)
        qn = ev.get('qty_new', 0)
        nominal_old = Decimal('1.00')
        nominal_new = (Decimal(str(qo)) * nominal_old / Decimal(str(qn))) if qn else Decimal('0')
        return (
            f"\n  [{ev['tipo_ca']}] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN antiguo : {ev.get('isin_old','')}\n"
            f"  ISIN nuevo   : {ev.get('isin_new','')}\n"
            f"  Títulos ant. : {qo:.0f}  →  Títulos nue.: {qn:.0f}  "
            f"(ratio 1:{qn/qo:.4f} por acción antigua)\n"
            f"  Nominal ref. : ant. {fmt_es_4(nominal_old)} EUR → "
            f"nue. {fmt_es_4(nominal_new)} EUR\n"
            f"  Descripción  : {ev.get('descripcion','')}\n"
            f"  → INCLUIDO en cartera_valores_irpf_{EJERCICIO}.csv como fila SP\n"
            f"  → VERIFICAR en RentaWEB que los nominales son correctos"
        )

    def fmt_isin(ev):
        return (
            f"\n  [ISIN_CHANGE] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN antiguo : {ev.get('isin_old','')}\n"
            f"  ISIN nuevo   : {ev.get('isin_new','')}\n"
            f"  Ratio        : 1:1 (mismo nº de títulos)\n"
            f"  Descripción  : {ev.get('descripcion','')}\n"
            f"  → NO requiere entrada en Mi cartera de valores (cambio administrativo)\n"
            f"    Si RentaWEB muestra el ISIN antiguo, actualizar manualmente a {ev.get('isin_new','')}"
        )

    # Mapas rápidos por ISIN para ventas en mercado y liberadas detectadas
    _ventas_b_mercado_por_isin = {op['isin']: op for op in derechos_ventas_b_mercado}
    _ventas_warn_por_isin      = {op['isin']: op for op in derechos_ventas_warn}
    # liberadas indexadas por ISIN de derechos (campo _isin_derechos)
    _liberadas_por_isin_d = defaultdict(list)
    for lib in liberadas_scrip:
        _liberadas_por_isin_d[lib.get('_isin_derechos', '')].append(lib)
    # residuales recomprados por emisor, indexados por ISIN RTS
    _residuales_por_isin_d = {e.get('isin', ''): e
                              for e in todos_eventos
                              if e.get('tipo_ca') == CA_RESIDUAL}

    def fmt_rts(ev):
        isin_d    = ev.get('isin', '')
        info      = derechos_clasificados.get(isin_d)
        tipo      = info.get('tipo') if info else None
        venta_b   = _ventas_b_mercado_por_isin.get(isin_d)   # venta en mercado (G/P)
        venta_w   = _ventas_warn_por_isin.get(isin_d)
        liberadas = _liberadas_por_isin_d.get(isin_d, [])
        residual  = _residuales_por_isin_d.get(isin_d)

        if tipo == 'B':
            n_lib = sum(float(lib['cantidad']) for lib in liberadas)
            n_vnd = float(venta_b['cantidad']) if venta_b else 0.0
            n_tot = float(ev.get('cantidad', 0))

            lineas_lib = []
            if liberadas:
                for lib in liberadas:
                    if lib.get('_ejercicio_mixto'):
                        n_comprados = lib.get('_derechos_comprados', 0)
                        coste_lib   = lib['importe_eur']
                        lineas_lib.append(
                            f"     🎁 Liberada (MIXTA): {lib['cantidad']:.0f} acc. {lib['nombre'][:30]} "
                            f"({lib['fecha']}) → coste {fmt_es(coste_lib)} EUR\n"
                            f"                          Incluye {n_comprados} derechos comprados en mercado\n"
                            f"                          → CSV como A con marca LIBERADA (coste = comprados)"
                        )
                    elif lib.get('_coste_unit_prorrateado'):
                        prr = lib['_coste_unit_prorrateado']
                        qpv = lib.get('_qty_previa_subyacente', 0)
                        fo  = lib.get('_fecha_origen_fifo', '')
                        lineas_lib.append(
                            f"     🎁 Liberada PURA: {lib['cantidad']:.0f} acc. {lib['nombre'][:30]} ({lib['fecha']})\n"
                            f"                        Coste prorrateado (Art. 37.1.a §4 LIRPF):\n"
                            f"                        · Posición previa : {qpv} acc.\n"
                            f"                        · Coste unit. recalculado : {fmt_es(prr)} EUR/acc\n"
                            f"                        · Fecha adquisición FIFO  : {fo or 'N/D'}\n"
                            f"                        → CSV: A importe 0, marca LIBERADA con coste prorrateado.\n"
                            f"                          Al vender en el futuro, usa {fmt_es(prr)} EUR/acc (NO 0 EUR)."
                        )
                    else:
                        lineas_lib.append(
                            f"     🎁 Liberada: {lib['cantidad']:.0f} acc. {lib['nombre'][:30]} "
                            f"({lib['fecha']}) → coste 0 en CSV\n"
                            f"                          ⚠️  Sin posición previa en los ficheros cargados:\n"
                            f"                          el prorrateo debe calcularse manualmente al vender\n"
                            f"                          (Art. 37.1.a §4 LIRPF)."
                        )
            lib_bloque = ('\n' + '\n'.join(lineas_lib)) if lineas_lib else ''

            if venta_b and liberadas:
                # Ejercicio mixto: parte vendidos en mercado, parte ejercidos
                importe_vnd = venta_b['importe_eur']
                n_ejercidos = max(n_tot - n_vnd, 0.0)
                nota_irrisorio = ''
                if importe_vnd < Decimal('1'):
                    nota_irrisorio = (
                        f"\n     ⚠️  Importe irrisorio ({fmt_es(importe_vnd)} EUR): derecho residual\n"
                        f"        sobrante del ejercicio (ratio de canje no exacto).\n"
                        f"        Si el bruto real es 0,00 EUR y las comisiones lo absorben\n"
                        f"        → G/P = 0 EUR, sin impacto fiscal práctico."
                    )
                estado = (
                    f"  ✅ TIPO B — Scrip dividend (ejercicio MIXTO: liberadas + venta residual)\n"
                    f"     Derechos asignados : {n_tot:.0f}\n"
                    f"     Ejercidos          : {n_ejercidos:.0f} → {n_lib:.0f} acción(es) liberada(s){lib_bloque}\n"
                    f"     Vendidos en mercado: {n_vnd:.0f} → {fmt_es(importe_vnd)} EUR{nota_irrisorio}\n"
                    f"     → Venta INCLUIDA en CSV como T → GANANCIA PATRIMONIAL → casillas {c_der}\n"
                    f"        (Art. 37.1.a LIRPF — DGT V2312-18, V0078-21)\n"
                    f"        Coste asignado al derecho: 0 EUR (recibido gratuitamente)\n"
                    f"        G/P = importe venta − 0 = {fmt_es(importe_vnd)} EUR bruto\n"
                    f"  ℹ️  Si optó por «precio comprometido» (empresa recompra): esa parte es\n"
                    f"     RCM → casilla {c_div}. Verificar extracto y añadir manualmente."
                )
            elif venta_b:
                # Solo venta en mercado (todos los derechos vendidos)
                importe_vnd = venta_b['importe_eur']
                nota_irrisorio = ''
                if importe_vnd < Decimal('1'):
                    nota_irrisorio = (
                        f"\n     ⚠️  Importe irrisorio ({fmt_es(importe_vnd)} EUR).\n"
                        f"        Si el bruto real es 0,00 EUR → G/P = 0, sin impacto fiscal."
                    )
                estado = (
                    f"  ✅ TIPO B — Scrip dividend (venta total en mercado)\n"
                    f"     Derechos asignados : {n_tot:.0f}\n"
                    f"     Vendidos en mercado: {n_vnd:.0f} → {fmt_es(importe_vnd)} EUR{nota_irrisorio}\n"
                    f"     → INCLUIDO en CSV como T → GANANCIA PATRIMONIAL → casillas {c_der}\n"
                    f"        (Art. 37.1.a LIRPF — venta a tercero en mercado secundario)\n"
                    f"        Coste asignado al derecho: 0 EUR (recibido gratuitamente)\n"
                    f"        G/P = importe venta − 0 = {fmt_es(importe_vnd)} EUR bruto\n"
                    f"        ✔ Sí compensa con minusvalías de acciones/fondos (mismo bloque G/P)\n"
                    f"  ℹ️  Si optó por «precio comprometido» (empresa recompra): esa parte es\n"
                    f"     RCM → Art. 25.1.a LIRPF → casilla {c_div}. Verificar en extracto."
                )
            elif liberadas:
                # Solo ejercicio (todos los derechos ejercidos → acciones liberadas)
                n_mixto = sum(1 for lib in liberadas if lib.get('_ejercicio_mixto'))
                linea_extra = ''
                if n_mixto:
                    linea_extra = (
                        f"\n     ℹ️  {n_mixto} liberada(s) MIXTA(s): usaste derechos asignados +\n"
                        f"        derechos comprados en mercado. El coste de los comprados\n"
                        f"        se ha transferido a la acción liberada."
                    )
                n_resid = int(residual['cantidad']) if residual else 0
                n_ejer  = int(n_tot) - n_resid
                linea_ejer_resid = ''
                if residual:
                    linea_ejer_resid = (
                        f"\n     Residual recomprado por emisor: {n_resid:.0f} derecho(s)\n"
                        f"        → RCM casilla {c_div} (ver sección 4 del informe)"
                    )
                estado = (
                    f"  ✅ TIPO B — Scrip dividend (ejercicio TOTAL → acciones liberadas)\n"
                    f"     Derechos asignados : {n_tot:.0f}\n"
                    f"     Ejercidos          : {n_ejer:.0f} → {n_lib:.0f} acción(es) liberada(s){lib_bloque}{linea_ejer_resid}\n"
                    f"     No hay venta de derechos que declarar.{linea_extra}\n"
                    f"     Base: Art. 37.1.a §4 LIRPF — coste prorrateado sobre la posición previa.\n"
                    f"           La fecha de adquisición legal es la de las originales (FIFO)."
                )
            else:
                estado = (
                    f"  ✅ TIPO B — Scrip dividend\n"
                    f"     No se detectó venta ni ejercicio de derechos en el extracto.\n"
                    f"     Opciones posibles:\n"
                    f"     · Si vendió en mercado → G/P patrimonial → casillas {c_der} (Art. 37.1.a)\n"
                    f"     · Si empresa recompró al precio comprometido → RCM → casilla {c_div}\n"
                    f"     · Si recibió acciones liberadas → coste 0; declarar al vender\n"
                    f"     Verificar extracto broker para determinar qué opción se ejecutó."
                )
        elif tipo == 'A':
            estado = (
                f"  ✅ TIPO A — Ampliación de capital con desembolso\n"
                f"     Venta de derechos → GANANCIA PATRIMONIAL → incluida en CSV como T (casillas {c_der})\n"
                f"        (Art. 37.1.a LIRPF → casillas {c_der})\n"
                f"     Ejercicio → coste nuevas acciones = precio suscripción + coste asignado.\n"
                f"     Base: Art. 37.1.a LIRPF"
            )
        elif venta_w:
            estado = (
                f"  ⚠️  SIN CLASIFICAR — venta de {venta_w['cantidad']:.0f} derechos detectada\n"
                f"     Importe: {fmt_es(venta_w['importe_eur'])} EUR  (incluido en CSV como T)\n"
                f"     VERIFICAR naturaleza:\n"
                f"     · TIPO A (ampliación real) → G/P casillas {c_der} ← ya en CSV, correcto\n"
                f"     · TIPO B scrip + venta en mercado → G/P casillas {c_der} ← ya en CSV, correcto\n"
                f"     · TIPO B + precio comprometido empresa → RCM casilla {c_div} ← mover manualmente\n"
                f"     → Ejecutar /irpf classify para reclasificar y ver instrucción precisa."
            )
        else:
            estado = (
                f"  ❓ SIN CLASIFICAR — no se detectó venta de derechos.\n"
                f"     Si se vendieron/ejercieron, verificar extracto broker.\n"
                f"     Consultar: Art. 37.1.a LIRPF (TIPO A o TIPO B venta mercado → G/P)\n"
                f"                Art. 25.1.a LIRPF (TIPO B precio comprometido → RCM)"
            )

        return (
            f"\n  [RIGHTS] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN derechos: {isin_d}\n"
            f"  Derechos     : {ev.get('cantidad',0):.0f}\n"
            f"  Descripción  : {ev.get('descripcion','')}\n"
            f"{estado}"
        )

    def fmt_cplx(ev):
        isin_old  = ev.get('isin_old', ev.get('isin', ''))
        isin_new  = ev.get('isin_new', '')
        qty_old   = ev.get('qty_old', 0)
        qty_new   = ev.get('qty_new', 0)
        desc      = ev.get('descripcion', '')
        nombre    = ev.get('nombre', '')
        fecha_ev  = ev.get('fecha', '')

        cabecera = (
            f"\n  [COMPLEX] {nombre}{' | ' + fecha_ev if fecha_ev else ''}\n"
            f"  ISIN origen  : {isin_old}\n"
        )
        if isin_new and isin_new != isin_old:
            cabecera += f"  ISIN nuevo   : {isin_new}\n"
        cabecera += f"  Descripción  : {desc}\n"

        # ── Detección: posible escisión / spin-off ─────────────────────────
        es_posible_escision = (
            isin_new and isin_new != isin_old
            and qty_old > 0 and qty_new > 0
        )
        if es_posible_escision:
            return cabecera + (
                f"  ⚠️  POSIBLE ESCISIÓN / SPIN-OFF — Revisión manual requerida\n"
                f"\n"
                f"  OPCIONES FISCALES (Art. 37.1.a LIRPF + doctrina DGT):\n"
                f"\n"
                f"  OPCIÓN A — Reparto proporcional del coste (RECOMENDADA si tienes el coste original)\n"
                f"    · El coste de adquisición de {nombre} (ISIN {isin_old}) se REDISTRIBUYE\n"
                f"      entre las acciones de la empresa original y las de la nueva empresa\n"
                f"      en proporción a sus valores de mercado en la fecha de la escisión.\n"
                f"    · Las {qty_new:.0f} acciones de la empresa escindida (ISIN {isin_new})\n"
                f"      reciben la parte proporcional del coste de las {qty_old:.0f} acciones origen.\n"
                f"    · El coste de las {qty_old:.0f} acciones origen SE REDUCE en ese importe.\n"
                f"    · Fórmula: Coste_escindida = Coste_total × (ValorMdo_escindida / ValorMdo_total)\n"
                f"    · Fuente: DGT V1766-12, V0419-13 (escisiones no proporcionales).\n"
                f"\n"
                f"  OPCIÓN B — Historial completo propio (ÓPTIMA pero requiere todas las compras)\n"
                f"    · Reconstruir todas las compras originales desde el inicio.\n"
                f"    · Aplicar el reparto proporcional a cada lote por precio medio.\n"
                f"    · Necesita todos los extractos desde la primera compra.\n"
                f"\n"
                f"  ❌ QUÉ NO HACER — Errores comunes:\n"
                f"    · No registrar las acciones escindidas como compra a precio 0,01 EUR:\n"
                f"      esto tributa el 100% de la venta futura como plusvalía sin coste.\n"
                f"    · No eliminar acciones de la empresa original para 'cuadrar' los números:\n"
                f"      la empresa original NO pierde acciones en una escisión.\n"
                f"    · No declarar la recepción de acciones como dividendo en especie\n"
                f"      (salvo instrucción específica de la AEAT para la operación concreta).\n"
                f"\n"
                f"  CASH RECIBIDO (si aplica): si se recibió efectivo como 'fractional cash'\n"
                f"    o equivalente, se declara como ganancia patrimonial en el bloque de otros elementos patrimoniales (casillas 1624-1654).\n"
                f"    Importe = efectivo recibido − 0 (coste de fracciones = 0).\n"
                f"\n"
                f"  ACCIÓN REQUERIDA:\n"
                f"    1. Buscar en extractos del broker los valores de mercado en fecha escisión.\n"
                f"    2. Calcular el % proporcional: ValorMdo_escindida / (ValorMdo_orig + ValorMdo_escind).\n"
                f"    3. Ajustar el precio medio de compra de ambas posiciones en RentaWEB.\n"
                f"    4. Si no tienes el coste original completo → consultar con asesor fiscal.\n"
                f"    5. Declarar el efectivo recibido como G/P (casillas 326-338 si viene de acciones cotizadas, o 1624-1654 si es otro tipo) si procede."
            )

        # ── Evento complejo genérico ──────────────────────────────────────
        return cabecera + (
            f"  → REVISIÓN MANUAL REQUERIDA\n"
            f"    Puede ser: OPA con canje mixto, return of capital, fusión, escisión, etc.\n"
            f"    Opciones según tipo:\n"
            f"    · Escisión / spin-off: reparto PROPORCIONAL del coste original entre la empresa\n"
            f"      matriz y la escindida (según valores de mercado en fecha escisión).\n"
            f"      Las nuevas acciones heredan parte del coste — NO se registran a precio 0,01 EUR.\n"
            f"      Base: DGT V1766-12, V0419-13. Cash recibido → G/P (casillas 326-338 si procede de acciones cotizadas, 1624-1654 otros).\n"
            f"    · OPA con canje de acciones: Art. 37.1.e LIRPF — diferimiento posible.\n"
            f"    · Return of capital: reduce coste de adquisición; si coste = 0, G/P en casillas 326-338 (acciones) o 1624-1654 (otros).\n"
            f"    · Fusión / absorción: Art. 37.1.e LIRPF — se canjean acciones sin tributar.\n"
            f"    Consultar con asesor fiscal o revisar ficha del valor en AEAT."
        )

    def fmt_residual(ev):
        isin_d   = ev.get('isin', '')
        qty      = float(ev.get('cantidad', 0))
        info     = derechos_clasificados.get(isin_d, {}) or {}
        emisor   = info.get('emisor', ev.get('nombre', ''))
        pc       = info.get('precio_comprometido_eur')
        try:
            pc_d = Decimal(str(pc)) if pc is not None else None
        except Exception:
            pc_d = None
        importe_teorico = (pc_d * Decimal(str(qty))) if pc_d is not None else None

        nota_importe = ''
        if importe_teorico is not None:
            nota_importe = f"     Importe teórico   : {fmt_es(importe_teorico)} EUR (precio comprometido × {qty:.0f} derechos)\n"
            if importe_teorico < Decimal('2'):
                nota_importe += (
                    f"     ⚠️  Importe < 2 EUR: probable que DeGiro lo haya omitido por ser\n"
                    f"        absorbido por la comisión. Verificar extracto de cuenta —\n"
                    f"        si el ingreso neto real es 0 EUR → no hay RCM que declarar.\n"
                )
        else:
            nota_importe = (
                f"     ⚠️  precio_comprometido_eur no definido en derechos_clasificados.json —\n"
                f"        verificar en el extracto de cuenta el importe real del abono.\n"
            )

        return (
            f"\n  [RESIDUAL] {emisor} | {ev.get('fecha','')}\n"
            f"  ISIN derechos: {isin_d}\n"
            f"  Derechos retirados por el emisor : {qty:.0f}\n"
            f"  Descripción  : {ev.get('descripcion','')}\n"
            f"  ✅ TIPO B — Scrip dividend (residual recomprado al precio comprometido)\n"
            f"{nota_importe}"
            f"     → FISCALIDAD: Rendimiento del capital mobiliario (RCM)\n"
            f"        Art. 25.1.a LIRPF → casilla 0029 (como dividendo)\n"
            f"        Se declara MANUALMENTE en RentaWEB; no está en el CSV.\n"
            f"        Compensa solo con RCM negativos o hasta 25% de G/P."
        )

    def fmt_name_change(ev):
        return (
            f"\n  [NAME_CHANGE] {ev.get('nombre_new','')} | {ev.get('fecha','')}\n"
            f"  ISIN         : {ev.get('isin','')}\n"
            f"  Nombre ant.  : {ev.get('nombre_old','')}  ({ev.get('fecha_old','')})\n"
            f"  Nombre nue.  : {ev.get('nombre_new','')}\n"
            f"  Descripción  : {ev.get('descripcion','')}\n"
            f"  → Sin evento fiscal: FIFO y precio medio se mantienen (mismo ISIN).\n"
            f"  → Al declarar ventas en RentaWEB, usar el nombre actual del valor.\n"
            f"  → Verificar que no sea un cambio de ISIN encubierto (comprobar ISIN en cuenta)."
        )

    def fmt_spin_off(ev):
        nombre        = ev.get('nombre', '')
        nombre_matriz = ev.get('nombre_matriz', '')
        isin_matriz   = ev.get('isin_old', '')
        isin_nueva    = ev.get('isin', '')
        cantidad      = ev.get('cantidad', 0)
        fecha_eff     = ev.get('fecha_efectiva', ev.get('fecha', ''))
        fecha_set     = ev.get('fecha', '')
        # Flag de resolucion automatica via catalogo spinoffs_conocidos.json.
        # Si no existe, valor neutro 'no' — el bloque sigue ofreciendo la
        # formula manual para que el usuario edite la celda amarilla.
        auto = 'si' if ev.get('_spinoff_resuelto_auto') else 'no'
        fuente_auto = ev.get('_spinoff_fuente', '—') if ev.get('_spinoff_resuelto_auto') else '—'
        coste_aplicado = ev.get('_spinoff_coste_aplicado', '')
        coste_aplicado_str = (
            f"{coste_aplicado:.2f} EUR" if isinstance(coste_aplicado, Decimal)
            and coste_aplicado > 0 else '—'
        )
        return (
            f"\n  [SPIN_OFF] {nombre_matriz} → {nombre}\n"
            f"  Fecha efectiva    : {fecha_eff}\n"
            f"  Fecha settlement  : {fecha_set}\n"
            f"  Empresa matriz    : {nombre_matriz}  (ISIN {isin_matriz}) — sigue cotizando\n"
            f"  Empresa escindida : {nombre}  (ISIN {isin_nueva})\n"
            f"  Acciones recibidas: {cantidad:.0f}\n"
            f"  Descripción       : {ev.get('descripcion','')}\n"
            f"  Resuelto auto     : {auto}\n"
            f"  Coste aplicado    : {coste_aplicado_str}\n"
            f"  Fuente            : {fuente_auto}\n"
            f"\n"
            f"  TRATAMIENTO FISCAL (Art. 37.1.a LIRPF + DGT V1766-12, V0419-13):\n"
            f"    El coste de adquisición de las acciones de {nombre_matriz} se REDISTRIBUYE\n"
            f"    proporcionalmente entre la matriz y la escindida según los valores de mercado\n"
            f"    en la fecha efectiva de la escisión.\n"
            f"\n"
            f"    Fórmula: Coste_escindida = Coste_matriz_total × (ValorMdo_escindida / ValorMdo_total)\n"
            f"             Coste_matriz_nuevo = Coste_matriz_total − Coste_escindida\n"
            f"\n"
            f"  → INCLUIDO en cartera_valores_irpf_{EJERCICIO}.csv como fila AD con coste 0\n"
            f"    para el ISIN {isin_nueva}. EDITA esa fila con el coste prorrateado real\n"
            f"    antes de presentar; reduce también el coste de la matriz en RentaWEB.\n"
            f"\n"
            f"  ⚠️  CASH-IN-LIEU por fracción residual:\n"
            f"    Si recibiste efectivo el día settlement (línea 'Coste de la Acción' o\n"
            f"    'Fractional cash' en el extracto de cuenta de la MATRIZ), declara ese importe\n"
            f"    como G/P patrimonial en casillas 326-338 (acciones cotizadas) o 1624-1654\n"
            f"    (otros elementos patrimoniales) según corresponda.\n"
            f"\n"
            f"  ❌ ERRORES COMUNES — qué NO hacer:\n"
            f"    · NO registrar las acciones escindidas a 0,01 EUR ni a precio de mercado:\n"
            f"      tributarías el 100% de la venta futura como plusvalía sin coste real.\n"
            f"    · NO eliminar acciones de la matriz para 'cuadrar': la matriz NO pierde\n"
            f"      acciones en una escisión (solo reduce su valor por la parte segregada).\n"
            f"    · NO declarar la recepción como dividendo en especie."
        )

    seccion(f"1. SPLITS Y CONTRASPLITS — Incluidos en el CSV como 'SP'", sp_events, fmt_sp)
    seccion(f"2. CAMBIOS DE ISIN (ratio 1:1) — No requieren entrada", isin_events, fmt_isin)
    seccion(f"3. DERECHOS DE SUSCRIPCIÓN — Acción manual requerida", rts_events, fmt_rts)
    seccion(f"4. RESIDUALES RECOMPRADOS POR EMISOR — RCM casilla 0029 (manual)", resid_events, fmt_residual)
    seccion(f"5. EVENTOS COMPLEJOS — Revisión manual requerida", cplx_events, fmt_cplx)
    def fmt_rights_exercised(ev):
        # Formato español (coma decimal) para consistencia con el resto del
        # informe y para que pdf_generator._extract_amount() pueda parsear
        # los importes correctamente al renderizar el PDF.
        return (
            f"\n  [RIGHTS_EXERCISED] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN derecho (NIL)   : {ev.get('isin','')}\n"
            f"  ISIN acción ordinaria: {ev.get('isin_ord','')}\n"
            f"  Acciones recibidas   : {ev.get('qty', 0):.0f}\n"
            f"  Coste real pagado    : {fmt_es(Decimal(str(ev.get('coste_eur', 0))))} EUR\n"
            f"  Fecha entrega        : {ev.get('fecha_entrega','')}\n"
            f"  → ✅ SIN ACCIÓN REQUERIDA. {ev.get('descripcion','')}\n"
            f"  → Las acciones se han añadido al CSV de cartera con su coste real.\n"
            f"  → El FIFO consume estas acciones al venderlas, generando G/P correcta.\n"
            f"  → Tratamiento fiscal: Art. 37.1.a LIRPF — el ejercicio del derecho NO\n"
            f"    genera ganancia/pérdida; el coste pagado se incorpora al valor de\n"
            f"    adquisición de las acciones nuevas."
        )

    seccion(f"6. CAMBIOS DE NOMBRE DE EMPRESA — Mismo ISIN, informativo", name_events, fmt_name_change)
    seccion(f"7. ESCISIONES (SPIN-OFFS) — Prorratear coste contra empresa matriz", spin_events, fmt_spin_off)
    seccion(f"8. RIGHTS ISSUES EJERCIDOS — Procesados automáticamente, sin acción requerida", rxe_events, fmt_rights_exercised)

    def fmt_market_transfer(ev):
        qty = ev.get('cantidad', 0)
        try:
            qty_str = f"{int(qty)}"
        except Exception:
            qty_str = str(qty)
        return (
            f"\n  [MARKET_TRANSFER] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN              : {ev.get('isin','')}\n"
            f"  Cantidad          : {qty_str} acciones\n"
            f"  Mercado origen    : {ev.get('mercado_origen','')}\n"
            f"  Mercado destino   : {ev.get('mercado_destino','')}\n"
            f"  Descripción       : {ev.get('descripcion','')}\n"
            f"  → EXCLUIDO del FIFO. El coste de adquisición original se preserva.\n"
            f"  → NO requiere entrada en RentaWEB (Art. 33 LIRPF: no hay alteración patrimonial)."
        )

    seccion(f"9. CAMBIOS DE MERCADO INTRA-BROKER — Excluidos del FIFO (Art. 33 LIRPF)",
            mt_events, fmt_market_transfer)

    def fmt_corto_forzado(ev):
        qty = ev.get('cantidad', 0)
        try:
            qty_str = f"{int(qty)}"
        except Exception:
            qty_str = str(qty)
        return (
            f"\n  [CORTO_FORZADO] {ev.get('nombre','')} | {ev.get('fecha','')}\n"
            f"  ISIN              : {ev.get('isin','')}\n"
            f"  Cantidad           : {qty_str} acciones\n"
            f"  Mercado de venta   : {ev.get('mercado_origen','')} (apertura corto)\n"
            f"  Mercado de compra  : {ev.get('mercado_destino','')} (cobertura corto)\n"
            f"  Descripción        : {ev.get('descripcion','')}\n"
            f"  → FIFO genera DOS matches:\n"
            f"      1. Apertura: importe transmisión = importe venta; gastos = comisión venta.\n"
            f"      2. Cobertura: coste adquisición = importe compra + comisiones + tasas.\n"
            f"      G/P realizada = importe apertura − coste cobertura − gastos (Art. 33 + 35.1.b LIRPF).\n"
            f"  → Si la G/P es PÉRDIDA, va a casillas 0326-0340 como pérdida computable.\n"
            f"  → La regla 2M del Art. 33.5.f NO se considera aplicable al cierre del propio corto\n"
            f"    (zona gris doctrinal — pero la posición post-cobertura es cero, no hay exposición\n"
            f"    mantenida; revisar con asesor si la AEAT regulariza)."
        )

    seccion(f"10. CORTOS FORZADOS — Procesados en FIFO (Art. 33 + 35.1.b LIRPF)",
            corto_events, fmt_corto_forzado)

    lines.append("FIN DEL INFORME")

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ── Tipos de cambio históricos BCE ─────────────────────────────────────────

# Caché global: (fecha_ISO 'YYYY-MM-DD', currency) → Decimal (EUR por unidad)
_ECB_CACHE: dict = {}

# Caché persistente en disco. Los tipos BCE de años cerrados son inmutables:
# con este fichero el script evita petir a la red y acelera notablemente la
# generación del informe, además de no romper si el BCE está inaccesible.
ECB_CACHE_FILE = os.path.join(BASE_DIR, "ecb_fx_cache.json")
_ECB_CACHE_LOADED = False          # flag: disco ya cargado en esta ejecución
_ECB_CACHE_DIRTY  = False          # flag: hay entradas nuevas sin volcar


def _ecb_cache_load_disk() -> None:
    """Carga el caché persistente en `_ECB_CACHE`. Idempotente.

    Formato en disco: {"USD": {"2017-01-02": "0.9511", ...}, "GBP": {...}}.
    Silencioso ante errores: un caché corrupto no debe tumbar el script; se
    regenerará en la siguiente petición que tenga éxito.
    """
    global _ECB_CACHE_LOADED
    if _ECB_CACHE_LOADED:
        return
    _ECB_CACHE_LOADED = True
    if not os.path.exists(ECB_CACHE_FILE):
        return
    try:
        with open(ECB_CACHE_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except Exception as exc:
        print(f"  ⚠️  Cache BCE corrupto ({ECB_CACHE_FILE}): {exc}. Se reconstruirá.")
        return
    loaded = 0
    for cur, rates in (raw or {}).items():
        if not isinstance(rates, dict):
            continue
        for date_str, val in rates.items():
            try:
                _ECB_CACHE[(date_str, cur)] = Decimal(str(val))
                loaded += 1
            except Exception:
                pass
    if loaded:
        print(f"  💾 Cache BCE: {loaded} tipos cargados de disco ({os.path.basename(ECB_CACHE_FILE)})")


def _ecb_cache_refresh_from_disk() -> None:
    """Recarga entradas nuevas del disco al `_ECB_CACHE` en memoria.

    No elimina ni sobrescribe entradas ya presentes (los tipos históricos son
    inmutables). Solo añade lo que haya en disco y falte en memoria. Seguro
    de llamar varias veces; silencioso ante errores de I/O.
    """
    if not os.path.exists(ECB_CACHE_FILE):
        return
    try:
        with open(ECB_CACHE_FILE, encoding='utf-8') as fh:
            raw = json.load(fh)
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    for cur, rates in raw.items():
        if not isinstance(rates, dict):
            continue
        for date_str, val in rates.items():
            key = (date_str, cur)
            if key in _ECB_CACHE:
                continue
            try:
                _ECB_CACHE[key] = Decimal(str(val))
            except Exception:
                pass


def _ecb_cache_save_disk() -> None:
    """Vuelca `_ECB_CACHE` a disco si hay cambios pendientes.

    Diseño concurrente (entorno web con varios procesos/hilos):
    - Lock NO bloqueante (`fcntl.flock` + `LOCK_NB`): si otro proceso está
      escribiendo, se salta el save sin esperar — los cambios se reintentarán
      en la siguiente ejecución. Nunca bloqueamos a un hilo peticionario.
    - Merge antes de escribir: releemos el disco (que puede contener entradas
      de OTROS procesos que descargaron en paralelo) y fusionamos con lo
      nuestro. Así ningún hilo pisa datos ajenos.
    - Fichero temporal único por PID para evitar colisión entre procesos.
    - `os.replace` final es atómico en POSIX: los lectores nunca ven un
      fichero a medias.
    """
    global _ECB_CACHE_DIRTY
    if not _ECB_CACHE_DIRTY:
        return

    # Reagrupar nuestras entradas por divisa
    local_by_cur: dict = {}
    for (date_str, cur), val in _ECB_CACHE.items():
        local_by_cur.setdefault(cur, {})[date_str] = str(val)

    try:
        import fcntl  # POSIX only; no disponible en Windows
    except ImportError:
        fcntl = None  # type: ignore

    lock_path = ECB_CACHE_FILE + ".lock"
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Otro proceso está escribiendo; nuestras entradas se vuelcan
                # en otra ocasión. No bloqueamos al hilo del servidor web.
                return

        # Merge: leer lo que haya ahora en disco (puede incluir descargas
        # que otro proceso hizo en paralelo) y superponer lo nuestro.
        merged: dict = {}
        try:
            with open(ECB_CACHE_FILE, encoding='utf-8') as fh:
                remote = json.load(fh) or {}
            if isinstance(remote, dict):
                for cur, rates in remote.items():
                    if isinstance(rates, dict):
                        merged[cur] = dict(rates)
        except FileNotFoundError:
            pass
        except Exception:
            # Fichero corrupto: lo reconstruimos desde cero con lo nuestro
            merged = {}

        for cur, rates in local_by_cur.items():
            bucket = merged.setdefault(cur, {})
            for d, v in rates.items():
                bucket.setdefault(d, v)  # respeta lo que ya haya (inmutable)

        # Orden estable para diffs legibles
        ordered = {
            cur: dict(sorted(merged[cur].items()))
            for cur in sorted(merged)
        }

        tmp = f"{ECB_CACHE_FILE}.tmp.{os.getpid()}"
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(ordered, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, ECB_CACHE_FILE)
            _ECB_CACHE_DIRTY = False
        except Exception as exc:
            print(f"  ⚠️  No se pudo guardar cache BCE en disco: {exc}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    finally:
        if lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(lock_fd)
            except Exception:
                pass


def fetch_ecb_rates(currencies: set, year: str,
                    min_fecha: str | None = None) -> None:
    """
    Asegura que _ECB_CACHE contiene los tipos diarios del BCE para las divisas
    y el año solicitados. Primero intenta disco; solo acude a la red cuando
    faltan datos para ese par (año, divisa).

    Parámetros:
      currencies — set de códigos ISO de divisa (USD, GBP, …).
      year       — año del ejercicio, como string ('2025').
      min_fecha  — opcional, ISO 'YYYY-MM-DD'. Si se pasa y la última fecha
                  cacheada para (año, divisa) es ANTERIOR a min_fecha, se
                  fuerza una nueva descarga. Evita el bug de cache parcial:
                  si la caché tiene hasta marzo pero el extracto llega hasta
                  junio, sin min_fecha la función saltaría la red dejando
                  meses recientes sin tipo BCE.

    Fuente oficial: ECB SDMX API (gratuita, sin API key).
    URL: https://data-api.ecb.europa.eu/service/data/EXR/D.{CUR}.EUR.SP00.A
         ?startPeriod={year}-01-01&endPeriod={year}-12-31&format=csvdata

    El valor devuelto por el BCE es "unidades de divisa extranjera por 1 EUR".
    Convertimos a "EUR por unidad" = 1 / valor_BCE.

    Robustez: cualquier fallo de red (timeout SSL, DNS, 5xx, etc.) se degrada
    a warning y el script continúa. Los tipos del CSV del broker actúan como
    fallback en get_eur_per_unit.
    """
    global _ECB_CACHE_DIRTY

    # Divisas que el BCE publica directamente frente al EUR
    supported = {'USD', 'GBP', 'DKK', 'HKD', 'CHF', 'PLN', 'AED', 'SEK',
                 'NOK', 'JPY', 'CAD', 'AUD', 'CNY', 'SGD', 'KRW'}
    to_fetch = currencies & supported

    _ecb_cache_load_disk()

    def _cache_complete(currency: str) -> bool:
        """¿La caché cubre (año, divisa) hasta min_fecha (si se pidió)?"""
        year_prefix = f"{year}-"
        fechas = [k[0] for k in _ECB_CACHE
                  if k[1] == currency and k[0].startswith(year_prefix)]
        if not fechas:
            return False
        if min_fecha is None:
            return True
        # Hay datos; comprobar que la última fecha cacheada cubre min_fecha.
        return max(fechas) >= min_fecha

    for cur in to_fetch:
        if _cache_complete(cur):
            continue

        # Antes de tirar de red, releemos el disco: otro proceso del servidor
        # web puede haber descargado justo este par (año, divisa) mientras el
        # hilo actual seguía otra ruta. Evita descargas duplicadas.
        _ecb_cache_refresh_from_disk()
        if _cache_complete(cur):
            continue

        url = (f"https://data-api.ecb.europa.eu/service/data/EXR/"
               f"D.{cur}.EUR.SP00.A"
               f"?startPeriod={year}-01-01&endPeriod={year}-12-31"
               f"&format=csvdata")
        try:
            req = urllib.request.Request(url, headers={'Accept': 'text/csv'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8')
        except Exception as e:
            # Captura amplia: URLError, TimeoutError, ssl.SSLError, OSError, etc.
            # Un fallo del BCE no debe tumbar el script; se usará fallback del CSV.
            print(f"  ⚠️  BCE API no disponible para {cur} ({year}): {type(e).__name__}: {e}")
            continue

        reader = csv.DictReader(raw.splitlines())
        count = 0
        for row in reader:
            date_str = row.get('TIME_PERIOD', '').strip()
            val_str  = row.get('OBS_VALUE', '').strip()
            if not date_str or not val_str:
                continue
            try:
                # BCE: unidades de moneda extranjera por 1 EUR
                rate_foreign_per_eur = Decimal(val_str)
                eur_per_unit = Decimal('1') / rate_foreign_per_eur
                _ECB_CACHE[(date_str, cur)] = eur_per_unit
                count += 1
            except Exception:
                pass
        if count:
            _ECB_CACHE_DIRTY = True
            print(f"  📡 BCE: {count} tipos de cambio {cur}/EUR descargados para {year}")
        else:
            print(f"  ⚠️  BCE: sin datos para {cur} en {year}")

    # Persistir a disco las descargas nuevas para futuras ejecuciones
    _ecb_cache_save_disk()


def get_eur_per_unit(fecha_val_ddmmyyyy: str, currency: str,
                     fx_local: dict) -> Decimal | None:
    """
    Resuelve el tipo de cambio EUR/unidad con esta jerarquía:
      1. Tipo real de la fila Cambio de Divisa del propio extracto DeGiro
      2. BCE: tipo exacto de la fecha
      3. BCE: último tipo disponible en los 7 días anteriores (fin de semana/festivo)
      4. BCE: media del mes en el fichero local
      5. None (imposible convertir)
    """
    if currency == 'EUR':
        return Decimal('1')

    # Convertir DD-MM-YYYY → YYYY-MM-DD para el caché BCE
    try:
        dt = datetime.strptime(fecha_val_ddmmyyyy, '%d-%m-%Y')
        fecha_iso = dt.strftime('%Y-%m-%d')
    except ValueError:
        fecha_iso = ''

    # 1. Tipo local del extracto
    rate = fx_local.get((fecha_val_ddmmyyyy, currency))
    if rate:
        return rate

    # 2. BCE: tipo exacto
    if fecha_iso:
        rate = _ECB_CACHE.get((fecha_iso, currency))
        if rate:
            return rate

    # 3. BCE: último tipo en los 7 días anteriores
    if fecha_iso:
        try:
            dt_base = datetime.strptime(fecha_iso, '%Y-%m-%d')
            for delta in range(1, 8):
                prev = (dt_base - timedelta(days=delta)).strftime('%Y-%m-%d')
                rate = _ECB_CACHE.get((prev, currency))
                if rate:
                    return rate
        except Exception:
            pass

    # 4. Media del mes en fx_local
    mes = fecha_val_ddmmyyyy[3:10] if len(fecha_val_ddmmyyyy) >= 10 else ''
    rates_mes = [r for (fv, cur), r in fx_local.items()
                 if cur == currency and fv[3:10] == mes]
    if rates_mes:
        return sum(rates_mes) / len(rates_mes)

    return None


# ── Dividendos: parsers ────────────────────────────────────────────────────

def _pais_de_isin(isin, nombre=''):
    """Devuelve el código de país a efectos de CDI."""
    if isin in ADR_PAIS_REAL:
        return ADR_PAIS_REAL[isin]
    return isin[:2].upper() if isin and len(isin) >= 2 else 'XX'


# Umbral por divisa para detectar formato subunidad en el FX del CSV.
# Si el rate supera el umbral, asumimos que está expresado en centésimas
# (GBX en lugar de GBP, USX en lugar de USD, etc.) y dividimos por 100
# para normalizar a "unidad principal por EUR". Las divisas que
# normalmente NO se subdividen en mercado (HKD, DKK, JPY, NOK, SEK)
# llevan umbral alto suficiente para que el rate normal pase intacto.
#
# Bug origen (Pennon 04/04/2025): DeGiro registra FX 0,8591 (GBP/EUR)
# para el cobro del dividendo y 83,7991 (GBX/EUR) para una venta de
# acción del mismo mes. Ambos llevan ISO "GBP" en el extracto.
# Mezclar los dos en fx_lookup → media mensual de 0,5879 → dividendo
# de 44,07 GBP convertido como 25,91 EUR (correcto: 51,30 EUR).
_FX_SUBUNIT_THRESHOLDS = {
    'GBP': 10,    # GBP/EUR ≈ 0,85 · GBX/EUR ≈ 85
    'USD': 10,    # USD/EUR ≈ 1,05 · USX/EUR ≈ 105
    'CHF': 10,    # CHF/EUR ≈ 1,00 · CHX/EUR ≈ 100
    'AUD': 10,
    'CAD': 10,
    'NZD': 10,
    'SGD': 10,
    'HKD': 100,   # HKD/EUR ≈ 8,5
    'DKK': 100,   # DKK/EUR ≈ 7,5
    'NOK': 100,
    'SEK': 100,
}


def _normalizar_fx_subunidad(currency: str, rate: Decimal) -> Decimal:
    """Si el FX viene en subunidad (peniques, centavos), divide por 100.

    Ver _FX_SUBUNIT_THRESHOLDS para el umbral por divisa. Devuelve el
    rate normalizado a "unidad principal por EUR".
    """
    threshold = _FX_SUBUNIT_THRESHOLDS.get(currency.upper())
    if threshold is not None and rate > threshold:
        return rate / Decimal('100')
    return rate


def parse_degiro_cuenta(filepath):
    """
    Parsea DeGiro_Cuenta_YYYY.csv (extracto de cuenta / account statement).

    Cómo obtenerlo: DeGiro → Cuenta → Descargar extracto de cuenta (período: todo el año)

    Formato real del CSV de DeGiro (2025):
      Fecha, Hora, Fecha valor, Producto, ISIN, Descripción,
      Tipo (FX rate),
      Variación (divisa), Variación (importe),
      Saldo (divisa), Saldo (importe),
      ID Orden

    Los dividendos vienen en divisa local (USD, GBP, etc.).
    La conversión a EUR se deduce de las filas "Retirada Cambio de Divisa"
    adyacentes, que contienen el tipo de cambio exacto utilizado por DeGiro.

    Retorna tupla (resultados, ejercidas_isin, gastos_plataforma) — los
    early-returns deben mantener la MISMA aridad (auditoría 2026-06-11
    [BAJO]: devolvían [] o [], set() y rompían el unpacking del caller).
    """
    if not os.path.exists(filepath):
        return [], set(), []

    # Auto-cargar el caché BCE de disco. Si el caller no llamó a
    # fetch_ecb_rates() antes, los días que caen en festivo BCE
    # (Inmaculada, Viernes Santo, etc.) caerían al fallback "media del
    # mes" de fx_local en lugar del BCE -1d, dando rates incorrectas.
    # _ecb_cache_load_disk es idempotente.
    _ecb_cache_load_disk()

    # ── Primera pasada: leer todas las filas ──────────────────────────────
    all_rows = []
    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            all_rows.append(row)

    if not header:
        return [], set(), []

    # Columnas fijas del formato DeGiro cuenta 2025:
    # 0:Fecha  1:Hora  2:Fecha valor  3:Producto  4:ISIN  5:Descripción
    # 6:Tipo(FX)  7:Var.divisa  8:Var.importe  9:Saldo.divisa  10:Saldo.importe  11:ID Orden
    IDX_FECHA_VAL = 2
    IDX_PROD      = 3
    IDX_ISIN      = 4
    IDX_DESC      = 5
    IDX_FX        = 6   # tipo de cambio (unidades moneda extranjera por 1 EUR)
    IDX_CUR       = 7   # divisa de la variación
    IDX_AMT       = 8   # importe de la variación

    # ── Segunda pasada: tipos de cambio + ISINs de opciones ejercidas ────
    # "Retirada Cambio de Divisa" → convierte divisa extranjera a EUR
    # Col 6 (Tipo) = unidades de moneda extranjera por 1 EUR  → EUR/unidad = 1/Tipo
    # "OPCIÓN EJERCIDA" con importe=0 → ISIN de la opción que fue ejercida (no el subyacente)
    fx_lookup = {}       # (fecha_valor, currency) → Decimal (EUR por 1 unidad)
    ejercidas_isin = set()  # ISINs de opciones ejercidas (para marcarlas en el informe)
    for row in all_rows:
        if len(row) <= IDX_AMT:
            continue
        desc_up = row[IDX_DESC].strip().upper() if len(row) > IDX_DESC else ''
        isin_row = row[IDX_ISIN].strip() if len(row) > IDX_ISIN else ''

        # Detectar ISINs de opciones ejercidas (el cierre de la opción a 0)
        if 'OPCIÓN EJERCIDA' in desc_up and isin_row:
            try:
                amt_opt = parse_es(row[IDX_AMT].strip())
            except Exception:
                amt_opt = Decimal('1')
            if amt_opt == 0 and isin_row:   # fila del cierre de la OPCIÓN (no la del subyacente)
                ejercidas_isin.add(isin_row)

        if 'RETIRADA CAMBIO DE DIVISA' not in desc_up:
            continue
        fecha_val = row[IDX_FECHA_VAL].strip()
        currency  = row[IDX_CUR].strip()
        fx_str    = row[IDX_FX].strip()
        if not fx_str or currency == 'EUR':
            continue
        try:
            fx_rate = parse_es(fx_str)   # moneda extranjera por 1 EUR
            # Normalizar formato del FX: DeGiro usa indistintamente la
            # unidad principal (GBP, USD, …) o la subunidad cotizada en
            # mercado (GBX = peniques británicos, etc.) según el tipo de
            # operación que originó el cambio de divisa. Mezclar ambos
            # formatos en fx_lookup rompe el fallback "media del mes" y
            # produce conversiones absurdas (caso real Pennon abr-2025:
            # FX dividendo 0,8591 GBP/EUR + FX venta acción 83,7991 GBX/EUR
            # → media 0,5879 → dividendo 44,07 GBP × 0,5879 = 25,91 EUR
            # cuando lo correcto era 51,30 EUR).
            fx_rate = _normalizar_fx_subunidad(currency, fx_rate)
            if fx_rate > 0:
                eur_per_unit = Decimal('1') / fx_rate
                key = (fecha_val, currency)
                if key not in fx_lookup:
                    fx_lookup[key] = eur_per_unit
        except Exception:
            pass

    # ── Tercera pasada: extraer dividendos y retenciones del año fiscal ────
    resultados = []
    for row in all_rows:
        if len(row) <= IDX_AMT:
            continue
        desc_raw = row[IDX_DESC].strip() if len(row) > IDX_DESC else ''
        desc_up  = desc_raw.upper()

        if 'CAMBIO DE DIVISA' in desc_up or 'CASH SWEEP' in desc_up:
            continue

        if any(k in desc_up for k in ('DIVIDENDO', 'DIVIDEND')) \
                and 'RETENCI' not in desc_up and 'TAX' not in desc_up:
            tipo = 'DIV'
        elif any(k in desc_up for k in ('RETENCIÓN DEL DIVIDENDO', 'RETENCION DEL DIVIDENDO',
                                         'DIVIDEND TAX', 'IMPUESTO DE DIVIDENDO',
                                         'RETENCIÓN', 'WITHHOLD')):
            tipo = 'RET'
        else:
            continue

        fecha_val = row[IDX_FECHA_VAL].strip()  # DD-MM-YYYY — fecha económica real
        isin      = row[IDX_ISIN].strip() if len(row) > IDX_ISIN else ''
        nombre    = row[IDX_PROD].strip() if len(row) > IDX_PROD else ''
        fecha     = parse_date(fecha_val)        # usar fecha valor para el informe
        currency  = row[IDX_CUR].strip() if len(row) > IDX_CUR else 'EUR'
        amt_str   = row[IDX_AMT].strip() if len(row) > IDX_AMT else '0'
        amount_signed = parse_es(amt_str)

        if amount_signed == 0:
            continue

        # Filtrar al ejercicio fiscal por Fecha valor (fecha económica del dividendo)
        anyo_val = fecha_val[-4:] if len(fecha_val) >= 10 else ''
        if anyo_val != EJERCICIO:
            continue

        # Conservar signo correcto:
        #   DIV: positivo = cobrado, negativo = corrección/reversión (reduce bruto)
        #   RET: en cuenta aparece negativo (impuesto retenido) → lo convertimos a positivo;
        #        si es positivo (devolución de retención) → lo convertimos a negativo (reduce total)
        if tipo == 'RET':
            amount = -amount_signed   # negativo en cuenta → positivo (retención); refund → negativo
        else:  # DIV
            amount = amount_signed    # mantener signo original

        # Convertir a EUR usando jerarquía: extracto → BCE exacto → BCE -7d → media mes
        if currency == 'EUR':
            amount_eur = amount
        else:
            eur_per_unit = get_eur_per_unit(fecha_val, currency, fx_lookup)
            if eur_per_unit is None:
                print(f"  ⚠️  Sin tipo de cambio {currency} para {fecha_val} — {nombre[:30]}")
                continue
            amount_eur = (amount * eur_per_unit).quantize(Decimal('0.01'), ROUND_HALF_UP)

        resultados.append({
            'fecha':       fecha or '',
            'isin':        isin,
            'nombre':      nombre[:50],
            'tipo':        tipo,
            'importe_eur': amount_eur,
            'divisa':      currency,
            'pais':        _pais_de_isin(isin, nombre),
            'broker':      'DeGiro',
        })

    # ── Cuarta pasada: comisiones de plataforma (conectividad) ────────────
    # DeGiro cobra ~2,50 EUR/mercado/mes. Art. 26.1.a LIRPF: deducibles como
    # "gastos de administración y depósito de valores negociables" de los
    # rendimientos del capital mobiliario. Filtrar al ejercicio fiscal por el
    # año que aparece en la descripción (ej. "...mercado 2025").
    gastos_plataforma = []
    for row in all_rows:
        if len(row) <= IDX_AMT:
            continue
        desc_raw = row[IDX_DESC].strip() if len(row) > IDX_DESC else ''
        desc_up  = desc_raw.upper()
        if 'CONECTIVIDAD' not in desc_up:
            continue
        # Filtrar al ejercicio fiscal por el año en la descripción
        if EJERCICIO not in desc_raw:
            continue
        amt_str = row[IDX_AMT].strip() if len(row) > IDX_AMT else '0'
        try:
            importe = abs(parse_es(amt_str))
        except Exception:
            continue
        if importe <= 0:
            continue
        fecha = parse_date(row[IDX_FECHA_VAL].strip()) if len(row) > IDX_FECHA_VAL else ''
        gastos_plataforma.append({
            'fecha':       fecha or '',
            'descripcion': desc_raw,
            'importe_eur': importe,
        })

    return resultados, ejercidas_isin, gastos_plataforma


def parse_ibkr_dividendos(filepath):
    """
    Parsea secciones 'Dividends' y 'Withholding Tax' del Activity Statement de IBKR.
    Retorna lista de dicts con los mismos campos que parse_degiro_cuenta.

    Filas del CSV IBKR en estas secciones (formato real verificado 2026-05-06):

        Dividends,Header,Currency,Date,Description,Amount
        Dividends,Data,DKK,2025-08-19,NOV(DK0062498333) Cash Dividend...,112.5
        Dividends,Data,Total,,,112.5                             ← descartar
        Dividends,Data,Total in EUR,,,15.0705                    ← descartar
        Dividends,Data,USD,2025-11-24,OWL(US09581B1035) ...,27.45
        Dividends,Data,Total,,,27.45                             ← descartar
        Dividends,Data,Total in EUR,,,23.8263255                 ← descartar
        Dividends,Data,Total Dividends in EUR,,,41.8668255       ← descartar

    Bug previo (2026-05-06): el parser solo descartaba filas cuando el campo
    Amount era literal 'Amount' o 'Total', pero las filas Total tienen
    Amount numérico → se contaban como dividendos extra con descripción
    vacía → ISIN no extraido → caian en cubo [XX]. Triple contabilidad.

    Fix: descartar cuando la columna Currency NO es un código ISO 4217
    (3 letras mayúsculas estrictas tipo USD/EUR/DKK/HKD/AED). Cualquier
    otra cosa ('Total', 'Total in EUR', 'Total Dividends in EUR', vacío)
    es una fila agregada que NO debe procesarse.

    Conversión de divisa: si Currency != EUR, convertir Amount a EUR via
    BCE del día de la fecha del dividendo (`_ibkr_eur_per_unit`). Antes el
    parser fijaba 'divisa': 'EUR' sin convertir → importes en moneda local
    se inflaban al sumar agregados.
    """
    if not os.path.exists(filepath):
        return []

    resultados = []
    div_header  = None
    wht_header  = None

    # Regex para extraer ISIN de la descripción IBKR:
    # "KRAFT HEINZ CO(US5007541064) Cash Dividend USD 0.40 per Share"
    re_isin = re.compile(r'\(([A-Z]{2}[A-Z0-9]{10})\)')
    re_name = re.compile(r'^([^(]+)')
    # Currency válido: exactamente 3 mayúsculas (ISO 4217). Filtra
    # 'Total', 'Total in EUR', 'Total Dividends in EUR', vacíos, etc.
    re_currency_iso = re.compile(r'^[A-Z]{3}$')

    def _to_eur(amount_local: Decimal, currency: str, date_iso: str) -> Decimal | None:
        """Convierte un importe en divisa local a EUR usando BCE del día.
        Devuelve None si no hay TC disponible (la fila se omite).
        """
        if currency == 'EUR':
            return amount_local
        rate = _ibkr_eur_per_unit(date_iso, currency)
        if rate is None:
            return None
        return (amount_local * rate).quantize(Decimal('0.01'), ROUND_HALF_UP)

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 4:
                continue

            section = row[0].strip()

            # ── Dividends ─────────────────────────────────────────────────
            if section == 'Dividends' and row[1].strip() == 'Header':
                div_header = row
                continue
            if section == 'Dividends' and row[1].strip() == 'Data' and div_header:
                h = div_header
                def col_d(name):
                    return row[h.index(name)].strip() if name in h else ''
                currency = col_d('Currency')
                if not re_currency_iso.match(currency):
                    continue   # fila agregada (Total, Total in EUR, ...)
                desc    = col_d('Description')
                date_s  = col_d('Date')
                amount  = col_d('Amount')
                if not amount or amount in ('Amount', 'Total'):
                    continue
                val_local = parse_es(amount.replace(',', ''))
                if val_local == 0:
                    continue
                # Conservar el signo: IBKR emite un Amount NEGATIVO cuando es
                # una reversa/corrección de un dividendo anterior. Descartar
                # los negativos (abs/<=0 continue) inflaba el bruto al contar
                # la emisión y no la reversa. Igual que el parser DeGiro/TR.
                date_iso = (date_s or '').strip()
                val = _to_eur(abs(val_local), currency, date_iso)
                if val is None:
                    print(f"  [AVISO IBKR Div] {currency} {date_iso} sin BCE — fila omitida")
                    continue
                if val_local < 0:
                    val = -val
                m_isin = re_isin.search(desc)
                m_name = re_name.match(desc)
                isin   = m_isin.group(1) if m_isin else ''
                nombre = m_name.group(1).strip()[:50] if m_name else desc[:50]
                resultados.append({
                    'fecha':       parse_date(date_s) or date_s,
                    'isin':        isin,
                    'nombre':      nombre,
                    'tipo':        'DIV',
                    'importe_eur': val,
                    'divisa':      currency,
                    'pais':        _pais_de_isin(isin),
                    'broker':      'IBKR',
                })
                continue

            # ── Withholding Tax ───────────────────────────────────────────
            if section == 'Withholding Tax' and row[1].strip() == 'Header':
                wht_header = row
                continue
            if section == 'Withholding Tax' and row[1].strip() == 'Data' and wht_header:
                h = wht_header
                def col_w(name):
                    return row[h.index(name)].strip() if name in h else ''
                currency = col_w('Currency')
                if not re_currency_iso.match(currency):
                    continue   # fila agregada
                desc   = col_w('Description')
                date_s = col_w('Date')
                amount = col_w('Amount')
                if not amount or amount in ('Amount', 'Total'):
                    continue
                val_signed = parse_es(amount.replace(',', ''))
                if val_signed == 0:
                    continue
                # IBKR registra la retención como Amount NEGATIVO; una
                # DEVOLUCIÓN de retención llega como Amount POSITIVO. Antes
                # `abs()` convertía la devolución en retención adicional
                # (inflaba 0588/0591). Convención de salida (como DeGiro):
                # importe RET positivo = retenido; negativo = devuelto.
                date_iso = (date_s or '').strip()
                val = _to_eur(abs(val_signed), currency, date_iso)
                if val is None:
                    print(f"  [AVISO IBKR Ret] {currency} {date_iso} sin BCE — fila omitida")
                    continue
                # Amount negativo (retención) → val positivo; positivo
                # (devolución) → val negativo.
                if val_signed > 0:
                    val = -val
                m_isin = re_isin.search(desc)
                m_name = re_name.match(desc)
                isin   = m_isin.group(1) if m_isin else ''
                nombre = m_name.group(1).strip()[:50] if m_name else desc[:50]
                resultados.append({
                    'fecha':       parse_date(date_s) or date_s,
                    'isin':        isin,
                    'nombre':      nombre,
                    'tipo':        'RET',
                    'importe_eur': val,
                    'divisa':      currency,
                    'pais':        _pais_de_isin(isin),
                    'broker':      'IBKR',
                })

    return resultados


def parse_ibkr_fx_pl(filepath):
    """
    Lee la sección 'Realized & Unrealized Performance Summary' del Activity
    Statement IBKR y devuelve:
      {
        'fx': [{'divisa': 'USD', 'realized': Decimal, 'unrealized': Decimal}, ...],
        'tbills': [{'symbol': '912797LW5 ...', 'realized': Decimal}, ...],
      }

    Las cifras vienen ya en EUR (base currency del statement). Solo se incluyen
    filas con valores no cero. Las filas Total (sumatorios) se descartan.

    Esta sección es exclusiva de IBKR — DeGiro no la proporciona.
    """
    resultado = {'fx': [], 'tbills': []}
    if not os.path.exists(filepath):
        return resultado

    SECTION = 'Realized & Unrealized Performance Summary'
    header = None

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 4 or row[0].strip() != SECTION:
                continue
            row_type = row[1].strip()
            if row_type == 'Header':
                header = row
                continue
            if row_type != 'Data' or not header:
                continue

            def col(name):
                return row[header.index(name)].strip() if name in header else ''

            asset = col('Asset Category')
            symbol = col('Symbol')

            # Filas Total agregadas (sin Symbol o con Symbol vacío) — descartar
            if not symbol or asset == 'Total':
                continue

            try:
                realized   = Decimal(col('Realized Total').replace(',', '') or '0')
                unrealized = Decimal(col('Unrealized Total').replace(',', '') or '0')
            except Exception:
                continue

            if realized == 0 and unrealized == 0:
                continue

            if asset == 'Forex':
                resultado['fx'].append({
                    'divisa':     symbol,            # AED, USD, DKK
                    'realized':   realized,
                    'unrealized': unrealized,
                })
            elif asset == 'Treasury Bills':
                resultado['tbills'].append({
                    'symbol':     symbol,
                    'realized':   realized,
                })

    return resultado


def parse_ibkr_interest(filepath):
    """Lee la seccion `Interest` del Activity Statement IBKR y devuelve la
    lista de pagos/cobros de interes con conversion a EUR.

    Tipos detectados:
      - **Credit Interest** (positivo): interes cobrado por el broker al
        cliente (cash sweep, USD credit balance, etc.). Es **RCM** — casilla
        0027 (intereses de cuentas, depósitos y activos financieros en general).
      - **Debit Interest** (negativo): interes pagado por el cliente al
        broker (margen, saldo deudor). **NO es deducible automaticamente**
        para particulares — solo seria gasto deducible si esta vinculado a
        la obtencion de RCM (Art. 26.1.b LIRPF), interpretacion conservadora
        de la AEAT. Se reporta como informativo.
      - **Bond Interest**: cupones de bonos (categorizado como tal en CSV
        IBKR). Es **RCM** — casilla 0027 (intereses obligaciones / deuda).

    Estructura de fila CSV: `Interest,Data,<Currency>,<Date>,<Description>,<Amount>`

    Devuelve: lista de dicts con `fecha`, `divisa`, `importe_local`,
    `importe_eur`, `descripcion`, `tipo` ('credit' / 'debit' / 'bond_interest'),
    `casilla` (0027 / None).

    Conversion EUR via BCE del dia (mismo mecanismo que `_ibkr_eur_per_unit`).
    """
    resultados = []
    if not os.path.exists(filepath):
        return resultados

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 6 or row[0].strip() != 'Interest':
                continue
            row_type = row[1].strip()
            if row_type != 'Data':
                continue
            currency = (row[2] or '').strip()
            date_iso = (row[3] or '').strip()
            description = (row[4] or '').strip()
            amount_str = (row[5] or '').strip()

            # Saltar filas Total/Total in EUR (subtotales agregados sin Date)
            if not currency or currency.lower().startswith('total') or not date_iso:
                continue
            try:
                importe_local = Decimal(amount_str.replace(',', '') or '0')
            except Exception:
                continue
            if importe_local == 0:
                continue

            desc_up = description.upper()
            if 'BOND INTEREST' in desc_up or 'COUPON' in desc_up:
                tipo = 'bond_interest'
                casilla = '0027'
            elif importe_local < 0 or 'DEBIT INTEREST' in desc_up:
                tipo = 'debit'
                casilla = None  # No deducible automaticamente para particulares
            else:
                tipo = 'credit'
                casilla = '0027'

            # Conversion a EUR. Si la divisa ya es EUR, sin conversion.
            if currency == 'EUR':
                importe_eur = importe_local
            else:
                rate = _ibkr_eur_per_unit(date_iso, currency)
                if rate is None:
                    print(f"  [AVISO IBKR Interest] {currency} {date_iso} sin BCE — saltado")
                    continue
                importe_eur = (importe_local * rate).quantize(Decimal('0.01'),
                                                              ROUND_HALF_UP)

            resultados.append({
                'fecha':         date_iso,
                'divisa':        currency,
                'importe_local': importe_local,
                'importe_eur':   importe_eur,
                'descripcion':   description,
                'tipo':          tipo,
                'casilla':       casilla,
            })

    return resultados


def calcular_resumen_dividendos(registros):
    """
    Agrupa registros de dividendos/retenciones por ISIN y calcula:
      - bruto: dividendo bruto recibido
      - retencion_origen: lo que retuvo el país extranjero
      - limite_cdi: máximo que España acuerda acreditar según CDI
      - recuperable: min(retencion_origen, limite_cdi) → deducción real en IRPF
      - exceso_no_recuperable: retención origen que supera el CDI (coste definitivo)

    Retorna lista de dicts ordenada por nombre.
    """
    by_isin = defaultdict(lambda: {
        'nombre': '', 'pais': 'XX', 'bruto': Decimal('0'),
        'retencion_origen': Decimal('0'), 'retencion_es': Decimal('0'),
        'brokers': set(), 'eventos': [],
    })

    for r in registros:
        key = r['isin'] or r['nombre'][:30]
        d   = by_isin[key]
        if not d['nombre']:
            d['nombre'] = r['nombre']
            d['pais']   = r['pais']
        # El país emisor lo fija la fila DIV, no una RET. Una RET con pais='ES'
        # sobre un valor extranjero solo indica el 19% que retuvo TR Sucursal ES
        # (post-migración); no convierte el emisor en español.
        if r['tipo'] == 'DIV':
            d['pais'] = r['pais']
        d['brokers'].add(r['broker'])
        # Acumulado por ISIN (fuente de verdad para los totales) + lista de
        # eventos individuales para vista expandible (auditoría por usuario).
        if r['tipo'] == 'DIV':
            d['bruto'] += r['importe_eur']
        elif r['tipo'] == 'RET':
            # Separar retención de ORIGEN (extranjera → crédito CDI, casilla
            # 0588) de la retención ESPAÑOLA (pagador ES o TR Sucursal ES
            # post-migración → casilla 0591, 100% acreditable). El parser ya
            # etiqueta cada fila RET con su país: pais='ES' es el 19% nacional.
            # Sumarlas juntas (bug previo) metía el 19% español en el cómputo
            # CDI y lo daba por "exceso no recuperable", perdiendo el crédito.
            if (r.get('pais') or '') == 'ES':
                d['retencion_es'] += r['importe_eur']
            else:
                d['retencion_origen'] += r['importe_eur']
        d['eventos'].append({
            'fecha':       r.get('fecha', ''),
            'tipo':        r['tipo'],        # 'DIV' o 'RET'
            'importe_eur': r['importe_eur'],
            'broker':      r.get('broker', ''),
            # País de la fila RET: 'ES' = retención española 19% (0591), resto =
            # retención de origen extranjera (0588). Necesario para que la vista
            # de detalle separe ambas a nivel de evento, no solo de ISIN.
            'pais':        r.get('pais', ''),
        })

    resumen = []
    for isin, d in by_isin.items():
        bruto     = d['bruto']
        ret_orig  = d['retencion_origen']   # retención de ORIGEN (extranjera, → 0588)
        ret_es    = d['retencion_es']       # retención ESPAÑOLA 19% (→ 0591)
        pais      = d['pais']
        tasa_cdi  = DTA_SOURCE_MAX.get(pais)

        # España (ES): no hay CDI consigo misma — la retención es retención nacional
        # del pagador (19%), totalmente acreditable contra cuota IRPF (casilla 0591 por pagador).
        # NO es CDI ni va a casilla 0588. NO es "exceso no recuperable" — es una retención directa.
        es_nacional = (pais == 'ES')

        # El crédito CDI se calcula SIEMPRE sobre la retención de ORIGEN
        # (extranjera). La retención española (ret_es) va por separado a 0591 y
        # nunca entra en el tope CDI — aunque el emisor sea extranjero (caso TR
        # Sucursal ES, que retiene el 19% sobre el neto de un dividendo USA/FR…).
        if es_nacional:
            limite_cdi   = Decimal('0')
            recuperable  = Decimal('0')   # un emisor ES no genera crédito CDI
            exceso       = Decimal('0')
            sin_cdi      = True           # sin CDI (no aplica)
        elif tasa_cdi is not None:
            limite_cdi   = (bruto * tasa_cdi).quantize(Decimal('0.01'), ROUND_HALF_UP)
            recuperable  = min(ret_orig, limite_cdi)
            exceso       = max(ret_orig - limite_cdi, Decimal('0'))
            sin_cdi      = False
        else:
            limite_cdi   = Decimal('0')
            recuperable  = Decimal('0')
            exceso       = ret_orig
            sin_cdi      = True

        # Si empresa española muestra ret=0 a través de broker extranjero → posible bruto sin retener
        sin_retencion_es = es_nacional and ret_es == 0 and bruto > 0

        # Consolidar eventos por (fecha, broker): el motor emite DIV y RET
        # como filas separadas (porque vienen de líneas distintas del CSV/PDF
        # del broker), pero son el mismo pago real. Agrupar permite mostrar
        # bruto y retención en columnas adyacentes de una sola fila — más
        # legible para auditoría manual.
        eventos_consolidados: dict = {}
        for ev in d['eventos']:
            k = (ev['fecha'], ev['broker'])
            if k not in eventos_consolidados:
                eventos_consolidados[k] = {
                    'fecha':            ev['fecha'],
                    'broker':           ev['broker'],
                    'bruto':            Decimal('0'),
                    'retencion':        Decimal('0'),   # total (origen + ES) — compat
                    'retencion_origen': Decimal('0'),   # extranjera → 0588
                    'retencion_es':     Decimal('0'),   # española 19% → 0591
                }
            if ev['tipo'] == 'DIV':
                eventos_consolidados[k]['bruto']     += ev['importe_eur']
            elif ev['tipo'] == 'RET':
                eventos_consolidados[k]['retencion'] += ev['importe_eur']
                # Misma separación por país que a nivel de ISIN (líneas arriba):
                # ES → retención española 0591; resto → retención de origen 0588.
                if (ev.get('pais') or '') == 'ES':
                    eventos_consolidados[k]['retencion_es']     += ev['importe_eur']
                else:
                    eventos_consolidados[k]['retencion_origen'] += ev['importe_eur']

        # Orden cronológico por fecha DD/MM/YYYY (no alfabético). Si el
        # parse falla — fechas no conformes al formato esperado — caemos
        # al orden por string para no romper la generación.
        def _key_fecha(e):
            f = e.get('fecha', '')
            try:
                d, m, y = f.split('/')
                return (int(y), int(m), int(d), e.get('broker', ''))
            except Exception:
                return (9999, 99, 99, f, e.get('broker', ''))
        eventos_out = sorted(eventos_consolidados.values(), key=_key_fecha)

        resumen.append({
            'isin':               isin,
            'nombre':             d['nombre'],
            'pais':               pais,
            'bruto':              bruto,
            'ret_origen':         ret_orig,
            'retencion_es':       ret_es,
            'tasa_cdi':           tasa_cdi,
            'limite_cdi':         limite_cdi,
            'recuperable':        recuperable,
            'exceso':             exceso,
            'sin_cdi':            sin_cdi,
            'es_nacional':        es_nacional,
            'sin_retencion_es':   sin_retencion_es,
            'brokers':            ', '.join(sorted(d['brokers'])),
            'eventos':            eventos_out,
        })

    resumen.sort(key=lambda x: x['nombre'])
    return resumen


def compute_external_fees_summary(todas_ops):
    """Calcula el resumen de tasas externas (tributos por transaccion) para
    su volcado en la hoja 'Tasas externas' del Excel maestro.

    Estas tasas YA estan sumadas al coste de adquisicion de cada operacion
    (Art. 35.1.b LIRPF + DGT V1989-21).

    Devuelve dict:
        {
          'jur_order':     ['es', 'uk', 'fr', 'hk', 'other'],
          'labels':        {jur: label_str, ...},
          'por_jur_total': {jur: Decimal, ...},
          'por_jur_ops':   {jur: int, ...},
          'total_global':  Decimal,
          'detalles':      [
              {'fecha', 'isin', 'nombre', 'broker', 'jur', 'importe',
               'op_tipo', 'cantidad'}, ...
          ],
        }
    """
    LABELS = {
        'es':    "🇪🇸 ITF / Tasa Tobin (Espana, Ley 5/2020)",
        'uk':    "🇬🇧 UK / Dublin Stamp Duty",
        'fr':    "🇫🇷 Impuesto de transaccion Frances (FTT)",
        'it':    "🇮🇹 Impuesto sobre Transacciones Financieras Italiano",
        'hk':    "🇭🇰 Hong Kong Stamp Duty",
        'other': "🌐 Otros (SEC, FINRA, sin clasificar)",
    }
    JUR_ORDER = ('es', 'uk', 'fr', 'it', 'hk', 'other')

    por_jur_total = {j: Decimal('0') for j in JUR_ORDER}
    por_jur_ops   = {j: 0 for j in JUR_ORDER}
    detalles = []

    for op in todas_ops:
        ext = op.get('gastos_externos', Decimal('0'))
        if not ext or ext == 0:
            continue
        breakdown = op.get('gastos_externos_breakdown', {})
        for jur in JUR_ORDER:
            amt = breakdown.get(jur, Decimal('0'))
            if amt and amt > 0:
                por_jur_total[jur] += amt
                por_jur_ops[jur]   += 1
                detalles.append({
                    'fecha':     op.get('fecha', ''),
                    'isin':      op.get('isin', ''),
                    'nombre':    op.get('nombre', '')[:50],
                    'broker':    op.get('broker', ''),
                    'jur':       jur,
                    'importe':   amt,
                    'op_tipo':   op.get('tipo', ''),
                    'cantidad':  op.get('cantidad', 0),
                })

    # Ordenar detalles por fecha (asc) y por importe (desc) dentro de fecha
    def _sort_key(d):
        try:
            f = datetime.strptime(d['fecha'], '%d/%m/%Y')
        except Exception:
            f = datetime.min
        return (f, -float(d['importe']))
    detalles.sort(key=_sort_key)

    return {
        'jur_order':     list(JUR_ORDER),
        'labels':        LABELS,
        'por_jur_total': por_jur_total,
        'por_jur_ops':   por_jur_ops,
        'total_global':  sum(por_jur_total.values()),
        'detalles':      detalles,
    }


def write_informe_dividendos(resumen, filepath, registros=None, derechos_scrip=None, gastos_plataforma=None, tbills=None, ibkr_interest=None, staking=None):
    """Escribe informe_dividendos_YYYY.txt."""
    lines = []
    SEP = '─' * 65

    lines += [
        f"INFORME DE DIVIDENDOS — Ejercicio {EJERCICIO}",
        "=" * 65,
        f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
        "NOTA FISCAL:",
        "  · Dividendos tributan en la base del ahorro (casilla 0029).",
        "  · Retención de pagador ESPAÑOL (19%) → acreditar en casilla 0591 por pagador.",
        "    Es retención directa, 100% recuperable. NO es CDI ni va a casilla 0588.",
        "  · Retención extranjera → deducción por doble imposición → casilla 0588.",
        "    Límite CDI según el convenio con el país de la empresa.",
        "  · Fuente legal: Art. 80 LIRPF, Art. 67 y 80 LIRPF para CDI.",
        "",
        "RETENCIONES POR TIPO DE BROKER:",
        "  · DeGiro (NL), IBKR (US/UK), Trade Republic (DE) — brokers extranjeros:",
        "    - Acciones extranjeras: el país de origen retiene en la fuente.",
        "      El broker lo traslada. Puede haber diferencias por AutoFX o tarifas.",
        "    - Acciones españolas (ES): la empresa pagadora debería retener 19%.",
        "      En DeGiro/IBKR/TR esto se procesa a través de Euroclear/Clearstream.",
        "      Si el campo 'retención' es 0 para una empresa española → VERIFICAR",
        "      con extracto real si se cobró el bruto; en ese caso declarar el bruto",
        "      y pagar la retención del 19% en la autoliquidación IRPF.",
        "  · Trade Republic cuenta remunerada (IBAN ES / depósito bancario):",
        "    - Los intereses SON intereses bancarios, NO dividendos.",
        "    - TR actúa como banco español → retiene 19% en origen.",
        "    - Declarar en casilla 0027 (intereses), con retención en casilla 0591.",
        "    - No aparecen en este informe (requieren exportación separada de TR).",
        "",
    ]

    total_bruto      = Decimal('0')
    total_ret_origen = Decimal('0')
    total_cdi        = Decimal('0')   # CDI recuperable (casilla 0588)
    total_ret_nac    = Decimal('0')   # Retención nacional ES (casilla 0591 per pagador)
    total_exceso     = Decimal('0')
    # Bruto de los dividendos EXTRANJEROS con retención > 0 — campo
    # "Rendimientos netos reducidos del capital mobiliario obtenidos en el
    # extranjero incluidos en la base del ahorro" del segundo popup de la
    # casilla 0588. Excluye nacionales (van por 0591) y extranjeros con
    # retención 0 % en origen (p. ej. UK), que no generan crédito CDI.
    total_bruto_ext_con_ret = Decimal('0')

    for d in resumen:
        es_nac = d.get('es_nacional', False)

        if es_nac:
            linea_retencion = (
                f"  Retención nacional (19%) : {fmt_es(d['retencion_es'])} EUR"
                + (f"  ({fmt_es((d['retencion_es']/d['bruto']*100).quantize(Decimal('0.1'), ROUND_HALF_UP))}%)"
                   if d['bruto'] > 0 else "")
            )
            linea_recuperable = f"  ✅ Acreditable casilla 0591: {fmt_es(d['retencion_es'])} EUR  (retención directa, no CDI)"
            lineas_extra = []
            if d.get('sin_retencion_es'):
                lineas_extra.append(
                    f"  ⚠️  ATENCIÓN: sin retención declarada para empresa española."
                    f"\n     Verificar si {d['brokers']} retuvo el 19% en origen."
                    f"\n     Si no retuvo → declarar bruto y pagar 19% en IRPF."
                )
        else:
            tasa_str  = f"{d['tasa_cdi']*100:.0f}%" if d['tasa_cdi'] is not None else "sin CDI"
            aviso_cdi = " ⚠️  SIN CDI — sin crédito garantizado" if d['sin_cdi'] else ""
            linea_retencion = (
                f"  Retención en origen     : {fmt_es(d['ret_origen'])} EUR"
                + (f"  ({fmt_es((d['ret_origen']/d['bruto']*100).quantize(Decimal('0.1'), ROUND_HALF_UP))}%)"
                   if d['bruto'] > 0 else "")
            )
            linea_recuperable = f"  ✅ Recuperable CDI (0588) : {fmt_es(d['recuperable'])} EUR  [{tasa_str} CDI]"
            lineas_extra = []
            if d['exceso'] > 0:
                lineas_extra.append(f"  Exceso no recuperable   : {fmt_es(d['exceso'])} EUR{aviso_cdi}")
            # Retención española adicional sobre dividendo extranjero: TR Sucursal
            # ES retiene el 19% sobre el neto post-migración. Va a casilla 0591
            # (acreditable 100%), independiente del crédito CDI de la 0588.
            if d.get('retencion_es', 0) > 0:
                lineas_extra.append(
                    f"  Retención española (19%): {fmt_es(d['retencion_es'])} EUR  → casilla 0591 (TR Sucursal ES, acreditable 100%)"
                )

        lines += [
            "",
            SEP,
            f"  {d['nombre'][:45]}  [{d['pais']}]  {d['isin']}",
            SEP,
            f"  Broker(s)               : {d['brokers']}",
            f"  Dividendo bruto         : {fmt_es(d['bruto'])} EUR",
            linea_retencion,
            f"  Límite CDI              : {'n/a (retención nacional)' if es_nac else fmt_es(d['limite_cdi'])+' EUR'}",
            linea_recuperable,
        ] + lineas_extra

        total_bruto       += d['bruto']
        # "Retención total pagada" = origen (extranjera) + española, ambas.
        total_ret_origen  += d['ret_origen'] + d.get('retencion_es', Decimal('0'))
        # Retención española (0591): SIEMPRE el campo retencion_es, tanto de
        # emisores ES (ACS) como de extranjeros con 19% de TR Sucursal ES (J&J).
        total_ret_nac     += d.get('retencion_es', Decimal('0'))
        if not es_nac:
            total_cdi     += d['recuperable']
            total_exceso  += d['exceso']
            if d['ret_origen'] > 0:
                total_bruto_ext_con_ret += d['bruto']

    # Desglose por broker (si hay datos raw)
    broker_lines = []
    if registros:
        pb_bruto = defaultdict(Decimal)
        pb_ret   = defaultdict(Decimal)
        pb_cdi   = defaultdict(Decimal)
        pb_nac   = defaultdict(Decimal)
        # Para CDI/nacional, necesitamos saber el país de cada registro
        isin_pais = {d['isin']: (d['pais'], d.get('es_nacional', False),
                                  d['recuperable'], d['bruto'])
                     for d in resumen}
        for r in registros:
            b = r['broker']
            if r['tipo'] == 'DIV':
                pb_bruto[b] += r['importe_eur']
            elif r['tipo'] == 'RET':
                pb_ret[b] += r['importe_eur']
        all_brokers = sorted(set(list(pb_bruto) + list(pb_ret)))
        if all_brokers:
            broker_lines += ["", "  DESGLOSE POR BROKER:"]
            for b in all_brokers:
                broker_lines.append(
                    f"  · {b:<12}: bruto {fmt_es(pb_bruto[b]):>9} EUR  "
                    f"retención {fmt_es(pb_ret[b]):>8} EUR"
                )

    # ── Sección: precio comprometido empresa (RCM real) ───────────────────
    # Solo aparece si el usuario eligió la opción «precio comprometido» (empresa
    # recompra los derechos al precio fijo comprometido). Esa parte es RCM.
    # Las ventas en mercado abierto (a terceros) son G/P → ya están en el CSV.
    scrip_lines = []
    if derechos_scrip:
        total_scrip = sum(op['importe_eur'] for op in derechos_scrip)
        scrip_lines += [
            "",
            "=" * 65,
            "  RECOMPRA AL PRECIO COMPROMETIDO — Scrip Dividend TIPO B",
            "=" * 65,
            "  Importe recibido de la empresa al «precio comprometido» (compromiso irrevocable).",
            "  Tratamiento: RENDIMIENTO DEL CAPITAL MOBILIARIO — Art. 25.1.a LIRPF",
            "  → Declarar en casilla 0029 como dividendo del mismo pagador.",
            "  IMPORTANTE: este tratamiento SOLO aplica si fue la empresa quien recompró",
            "  los derechos al precio comprometido. Si los vendió en el mercado secundario,",
            "  el tratamiento es GANANCIA PATRIMONIAL (casillas 341-346 para derechos) — ver informe_corporativas.",
            "",
        ]
        for op in derechos_scrip:
            emisor = op.get('clasificacion', {}).get('emisor', op['nombre'])
            desc   = op.get('clasificacion', {}).get('descripcion', '')
            scrip_lines += [
                f"  {'─'*63}",
                f"  {emisor}  [{op['isin']}]",
                f"  Fecha        : {op['fecha']}",
                f"  Derechos     : {op['cantidad']:.0f}  recomprados por la empresa",
                f"  Importe bruto: {fmt_es(op['importe_eur'])} EUR",
                f"  → Casilla 0029 — pagador: {emisor}",
            ]
            if desc:
                scrip_lines.append(f"  Ref.         : {desc}")
        scrip_lines += [
            "",
            f"  TOTAL precio comprometido (casilla 0029): {fmt_es(total_scrip)} EUR",
        ]

    # ── Sección: comisiones de plataforma ─────────────────────────────────
    plataforma_lines = []
    if gastos_plataforma:
        total_plat = sum(g['importe_eur'] for g in gastos_plataforma)
        plataforma_lines += [
            "",
            "=" * 65,
            "  GASTOS DE PLATAFORMA — Comisiones de conectividad (DeGiro)",
            "=" * 65,
            "  Deducibles como gastos de administración y depósito de valores",
            "  negociables (Art. 26.1.a LIRPF). Reducen el rendimiento neto del",
            "  capital mobiliario. No pueden generar rendimiento negativo.",
            "  → Introducir en RentaWEB como 'Gastos deducibles' en la sección",
            "    de rendimientos del capital mobiliario (junto a casilla 0029).",
            "",
        ]
        for g in gastos_plataforma:
            plataforma_lines.append(
                f"  {g['fecha']:<12}  {g['descripcion'][:45]:<45}  {fmt_es(g['importe_eur']):>7} EUR"
            )
        plataforma_lines += [
            "",
            f"  TOTAL gastos plataforma deducibles: {fmt_es(total_plat)} EUR",
        ]

    # Sección de intereses (T-Bills IBKR) — solo se imprime si hay datos.
    # Se reporta para visibilidad pero NO suma al sidecar totals.json.
    tbills_lines = []
    if tbills:
        from decimal import Decimal as _D
        total_tbills = sum((t['realized'] for t in tbills), _D('0'))
        total_tbills = total_tbills.quantize(_D('0.01'), ROUND_HALF_UP)
        tbills_lines = [
            "",
            "=" * 65,
            "  INTERESES — Treasury Bills IBKR (RCM, casilla 0031)",
            "=" * 65,
            "  Detectados desde la sección 'Realized & Unrealized Performance",
            "  Summary' del Activity Statement IBKR. Tributan como rendimientos",
            "  de capital mobiliario (intereses), NO como dividendos.",
            "",
        ]
        for t in tbills:
            r = t['realized'].quantize(_D('0.01'), ROUND_HALF_UP)
            tbills_lines.append(f"  {t['symbol'][:55]}: {fmt_es(r):>10} EUR")
        tbills_lines += [
            "",
            f"  TOTAL intereses T-Bills: {fmt_es(total_tbills)} EUR",
            f"  → Declarar en casilla 0031 (otros activos financieros — la 0030 es solo Letras del Tesoro españolas).",
            "  → Verificar Withholding Tax IBKR para retenciones aplicables → 0588.",
            "  → Detalle ampliado en informe_fx_YYYY.txt.",
        ]

    # Sección de Intereses IBKR (Credit / Debit / Bond Interest)
    interest_lines = []
    if ibkr_interest:
        from decimal import Decimal as _D
        credit_total = sum((r['importe_eur'] for r in ibkr_interest
                            if r['tipo'] in ('credit', 'bond_interest')),
                           _D('0')).quantize(_D('0.01'), ROUND_HALF_UP)
        debit_total  = sum((r['importe_eur'] for r in ibkr_interest
                            if r['tipo'] == 'debit'),
                           _D('0')).quantize(_D('0.01'), ROUND_HALF_UP)
        # Retención IRPF española 19% sobre intereses (TR Sucursal ES, cuenta
        # remunerada post-migración) → campo "Retenciones" del popup de 0027.
        ret_es_int = sum((r.get('retencion_es_eur', _D('0')) for r in ibkr_interest
                          if r['tipo'] in ('credit', 'bond_interest')),
                         _D('0')).quantize(_D('0.01'), ROUND_HALF_UP)
        interest_lines = [
            "",
            "=" * 65,
            "  INTERESES IBKR — sección 'Interest' del Activity Statement",
            "=" * 65,
            "  Credit / Bond Interest → RCM, casilla 0027 (intereses cuentas /",
            "  obligaciones).",
            "  Debit Interest (margen, saldo deudor) → INFORMATIVO. Solo es",
            "  deducible si se vincula a obtención de RCM concretos (Art. 26.1.b",
            "  LIRPF) — interpretación conservadora AEAT, no se suma automático.",
            "",
        ]
        for r in sorted(ibkr_interest, key=lambda x: x.get('fecha', '')):
            tipo = r['tipo']
            tag = ('credit' if tipo == 'credit'
                   else 'bond  ' if tipo == 'bond_interest'
                   else 'debit ')
            importe = r['importe_eur'].quantize(_D('0.01'), ROUND_HALF_UP)
            desc = (r.get('descripcion', '') or '')[:50]
            interest_lines.append(
                f"  {r.get('fecha', ''):<11} {tag} {fmt_es(importe):>10} EUR  {desc}"
            )
        interest_lines += [
            "",
            f"  TOTAL Credit + Bond (declarable casilla 0027): {fmt_es(credit_total)} EUR",
        ]
        if ret_es_int > 0:
            interest_lines.append(
                f"  ✅ Retención IRPF ES (intereses): {fmt_es(ret_es_int)} EUR  "
                f"→ campo 'Retenciones' del popup individual de 0027 (TR Sucursal ES, 100% acreditable)"
            )
        interest_lines.append(
            f"  TOTAL Debit (informativo, NO deducible automático): {fmt_es(debit_total)} EUR"
        )

    staking_lines = []
    if staking:
        total_stk = sum((s['importe_eur'] for s in staking),
                        Decimal('0')).quantize(Decimal('0.01'), ROUND_HALF_UP)
        activos_stk = sorted({s.get('asset', '') for s in staking if s.get('asset')})
        staking_lines = [
            "",
            "=" * 65,
            "  STAKING DE CRIPTOMONEDAS — RCM casilla 0027 (DGT V1766-22)",
            "=" * 65,
            "  RCM Art. 25.2 LIRPF satisfecho en especie, valorado en EUR al",
            "  precio de mercado en el momento de cada recepción (Art. 43.1).",
            "  Alternativa doctrinal: casilla 0031 (cuota idéntica, base ahorro).",
            "",
        ]
        for s_ev in sorted(staking, key=lambda x: x.get('fecha', '')):
            staking_lines.append(
                f"  {s_ev.get('fecha', ''):<11} {s_ev.get('asset', ''):<6} "
                f"{fmt_es(s_ev['importe_eur']):>10} EUR  "
                f"(x{s_ev.get('cantidad', '')})"
            )
        staking_lines += [
            "",
            f"  TOTAL staking (declarable casilla 0027): {fmt_es(total_stk)} EUR  "
            f"({len(staking)} recepciones de {', '.join(activos_stk)})",
            "  → Sin retención. Al vender el cripto recibido, su coste de",
            "    adquisición es este valor de recepción (lote 🪙 en Operaciones)",
            "    — la venta tributa solo por la plusvalía desde ese valor.",
        ]

    lines += [
        "",
        "=" * 65,
        "  TOTALES",
        "=" * 65,
        f"  Dividendo bruto total   : {fmt_es(total_bruto)} EUR",
        f"  Retención total pagada  : {fmt_es(total_ret_origen)} EUR",
        f"",
        f"  Bruto extranjero con retención (campo 0588 cap. mobiliario): {fmt_es(total_bruto_ext_con_ret)} EUR",
        f"    → segundo popup de casilla 0588 → 'Rendimientos netos reducidos del capital",
        f"      mobiliario obtenidos en el extranjero incluidos en la base del ahorro'.",
        f"      Excluye dividendos españoles y extranjeros con retención 0 % en origen.",
        f"  ✅ CDI recuperable (0588): {fmt_es(total_cdi)} EUR  → campo 'Impuesto satisfecho en el extranjero' del mismo popup",
        f"  ✅ Retención ES           : {fmt_es(total_ret_nac)} EUR  → campo 'Retenciones' del popup individual de 0029 (RentaWEB suma automáticamente)",
        f"  Total exceso (perdido)  : {fmt_es(total_exceso)} EUR",
    ] + broker_lines + scrip_lines + plataforma_lines + tbills_lines + interest_lines + staking_lines + [
        "",
        "  DÓNDE DECLARARLO EN RentaWEB:",
        "  · Todos los dividendos brutos → casilla 0029 (rendimientos capital mobiliario)",
        "  · Retención española (19%) → casilla 0591 junto a cada pagador",
        "  · Deducción doble imposición CDI → casilla 0588 (solo dividendos extranjeros)",
        "  · Intereses TR cuenta remunerada → casilla 0027 + retención en su popup (0591)",
        "  · Intereses T-Bills IBKR → casilla 0031 (ver sección INTERESES T-Bills)",
        "  · Intereses IBKR Credit/Bond → casilla 0027 (ver sección INTERESES IBKR)",
        "  · Staking de criptomonedas → casilla 0027 (ver sección STAKING)",
        "  · Scrip dividend / venta derechos TIPO B → casilla 0029 (ver sección anterior)",
        "  · Gastos plataforma (conectividad) → gastos deducibles RCM (ver sección anterior)",
        "",
        "FIN DEL INFORME",
    ]

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ── Opciones y derivados: parsers ──────────────────────────────────────────

def parse_opciones_degiro(raw_rows, ejercidas_isin=None):
    """
    Extrae operaciones de opciones y productos derivados del CSV de transacciones
    de DeGiro. Identifica por ISIN (NLEX*, ES0A*) o por nombre (patrón C/P + precio).

    ejercidas_isin: conjunto de ISINs de opciones que fueron ejercidas (obtenido
                    de parse_degiro_cuenta). Si None, se trata todo como expirada.

    Retorna lista de dicts:
      fecha, simbolo, isin, tipo_op ('C'|'P'|'?'), subyacente,
      strike, vencimiento, accion ('compra'|'venta'), cantidad,
      prima_unitaria, importe_eur, gastos_eur, expirada (bool), ejercida (bool)
    """
    if ejercidas_isin is None:
        ejercidas_isin = set()
    # Regex para nombre de opción: "ASLM C900.00 19DEC25" o "SAN C9.75 16JAN26"
    RE_OPT_NAME = re.compile(
        r'^(?P<sub>[A-Z0-9&. ]+?)\s+'
        r'(?P<tipo>[CP])\s*'
        r'(?P<strike>\d+[.,]?\d*)\s+'
        r'(?P<venc>\d{2}[A-Z]{3}\d{2})',
        re.IGNORECASE
    )

    grupos_id   = defaultdict(list)
    sin_id_rows = []
    for row in raw_rows:
        order_id = _extract_degiro_order_id(row)
        if order_id:
            grupos_id[order_id].append(row)
        else:
            sin_id_rows.append(row)

    opciones = []

    def procesar_rows(rows, expirada=False, ejercida=False):
        r0     = rows[0]
        isin   = r0[3].strip()
        nombre = r0[2].strip()

        es_opcion = is_option_isin(isin) or is_option_name(nombre)
        if not es_opcion:
            return

        fecha  = parse_date(r0[0]) or ''
        m      = RE_OPT_NAME.match(nombre)
        if m:
            subyacente  = m.group('sub').strip()
            tipo_op     = m.group('tipo').upper()
            strike      = m.group('strike').replace(',', '.')
            vencimiento = m.group('venc').upper()
        else:
            subyacente  = nombre
            tipo_op     = '?'
            strike      = ''
            vencimiento = ''

        cantidad = sum(abs(parse_es(row[6])) for row in rows)
        if cantidad == 0:
            return

        # Importe EUR
        valores_eur = [abs(parse_es(row[11])) for row in rows]
        _opc_duplicado = len(set(str(v) for v in valores_eur)) == 1
        if _opc_duplicado:
            importe_eur = valores_eur[0]
        else:
            importe_eur = sum(valores_eur)

        # AutoFX por fill: sumar en multi-fill (antes max() perdía gasto
        # deducible). Si las filas son duplicados (mismo valor) → uno solo.
        if _opc_duplicado:
            gastos_autofx = abs(parse_es(rows[0][13]))
        else:
            gastos_autofx = sum(abs(parse_es(row[13])) for row in rows)
        gastos_transacc = sum(abs(parse_es(row[14])) for row in rows
                              if parse_es(row[14]) != 0)
        gastos_eur = gastos_autofx + gastos_transacc

        accion = 'venta' if parse_es(r0[6]) < 0 else 'compra'
        prima_unit = (importe_eur / cantidad).quantize(
            Decimal('0.0001'), ROUND_HALF_UP) if cantidad > 0 else Decimal('0')

        opciones.append({
            'fecha':         fecha,
            'simbolo':       nombre[:40],
            'isin':          isin,
            'tipo_op':       tipo_op,
            'subyacente':    subyacente,
            'strike':        strike,
            'vencimiento':   vencimiento,
            'accion':        accion,
            'cantidad':      cantidad,
            'prima_unitaria':prima_unit,
            'importe_eur':   importe_eur,
            'gastos_eur':    gastos_eur,
            'expirada':      expirada,
            'ejercida':      ejercida,
            'broker':        'DeGiro',
        })

    for rows in grupos_id.values():
        procesar_rows(rows, expirada=False)

    # Filas sin order_id con precio=0 → opciones expiradas o ejercidas
    # La distinción se hace por el ISIN: si está en ejercidas_isin → ejercida, si no → expirada
    for row in sin_id_rows:
        isin   = row[3].strip()
        nombre = row[2].strip()
        if is_option_isin(isin) or is_option_name(nombre):
            es_precio_cero = (parse_es(row[7]) == 0)
            es_ejercida    = isin in ejercidas_isin
            es_expirada    = es_precio_cero and not es_ejercida
            procesar_rows([row], expirada=es_expirada, ejercida=es_ejercida)

    return opciones


# ── Búsqueda de primas en años anteriores (DGT V2172-21) ───────────────────

def _detectar_formato_degiro(filepath):
    """Detecta si el CSV es formato 'transacciones' o 'cuenta' según la cabecera."""
    try:
        with open(filepath, encoding='utf-8') as f:
            header = f.readline()
        # Cuenta: "Fecha,Hora,Fecha valor,Producto,ISIN,Descripción,..."
        # Transacciones: "Fecha,Hora,Producto,ISIN,Bolsa de referencia,..."
        if 'Fecha valor' in header or 'Descripción' in header:
            return 'cuenta'
        return 'transacciones'
    except Exception:
        return 'transacciones'


def _buscar_en_transacciones(filepath, target_isins):
    """
    Busca ventas de opciones en un CSV de formato 'transacciones' (DeGiro portfolio activity).
    Retorna dict {isin → {isin, simbolo, fecha, importe_eur, gastos_eur,
                           subyacente, tipo_op, strike, vencimiento, anio_csv}}.
    """
    anio_csv = os.path.basename(filepath).replace('.csv','').split('_')[-1]
    RE_OPT_NAME = re.compile(
        r'^(?P<sub>[A-Z0-9&. ]+?)\s+(?P<tipo>[CP])\s*'
        r'(?P<strike>\d+[.,]?\d*)\s+(?P<venc>\d{2}[A-Z]{3}\d{2})',
        re.IGNORECASE)

    grupos = defaultdict(list)
    try:
        with open(filepath, encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if len(row) < 17:
                    continue
                order_id = _extract_degiro_order_id(row)
                isin     = row[3].strip()
                if order_id and isin in target_isins:
                    grupos[order_id].append(row)
    except Exception:
        return {}

    resultados = {}
    for order_id, rows in grupos.items():
        r0     = rows[0]
        isin   = r0[3].strip()
        nombre = r0[2].strip()
        try:
            cantidad = float(r0[6].strip().replace(',', '.'))
        except Exception:
            continue
        if cantidad >= 0:
            continue  # solo ventas (cantidad negativa)

        fecha  = parse_date(r0[0]) or ''
        importe_eur = sum(abs(parse_es(r[11])) for r in rows)
        gastos_eur  = sum(abs(parse_es(r[14])) for r in rows if parse_es(r[14]) != 0)

        m = RE_OPT_NAME.match(nombre)
        if m:
            sub   = m.group('sub').strip()
            tipo  = m.group('tipo').upper()
            strk  = m.group('strike').replace(',', '.')
            venc  = m.group('venc').upper()
        else:
            sub, tipo, strk, venc = nombre, '?', '', ''

        resultados[isin] = {
            'isin': isin, 'simbolo': nombre, 'fecha': fecha,
            'importe_eur': importe_eur, 'gastos_eur': gastos_eur,
            'subyacente': sub, 'tipo_op': tipo, 'strike': strk,
            'vencimiento': venc, 'anio_csv': anio_csv,
        }
    return resultados


def _buscar_en_cuenta(filepath, target_isins):
    """
    Busca ventas de opciones en un CSV de formato 'cuenta' (DeGiro account statement).
    Retorna dict {isin → {isin, simbolo, fecha, importe_eur, gastos_eur,
                           subyacente, tipo_op, strike, vencimiento, anio_csv}}.
    """
    anio_csv = os.path.basename(filepath).replace('.csv','').split('_')[-1]
    RE_OPT_NAME = re.compile(
        r'^(?P<sub>[A-Z0-9&. ]+?)\s+(?P<tipo>[CP])\s*'
        r'(?P<strike>\d+[.,]?\d*)\s+(?P<venc>\d{2}[A-Z]{3}\d{2})',
        re.IGNORECASE)
    # Columnas cuenta: 0:Fecha 1:Hora 2:FechaValor 3:Producto 4:ISIN 5:Descripción
    #                  6:FX   7:Var.divisa  8:Var.importe  9:Saldo.div 10:Saldo.imp  11:IDOrden
    by_order = defaultdict(list)
    try:
        with open(filepath, encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 9:
                    continue
                order_id = row[11].strip() if len(row) > 11 else ''
                isin     = row[4].strip()  if len(row) > 4  else ''
                if order_id and isin in target_isins:
                    by_order[order_id].append(row)
    except Exception:
        return {}

    resultados = {}
    for order_id, rows in by_order.items():
        isin    = rows[0][4].strip()
        nombre  = rows[0][3].strip()
        fecha   = parse_date(rows[0][0]) or ''
        importe = Decimal('0')
        gastos  = Decimal('0')

        for row in rows:
            if len(row) < 9:
                continue
            desc = row[5].strip().upper() if len(row) > 5 else ''
            amt  = parse_es(row[8]) if len(row) > 8 else Decimal('0')
            if 'VENTA' in desc and amt > 0:
                importe = amt
            elif ('COSTE' in desc or 'COSTES' in desc) and amt < 0:
                gastos = abs(amt)

        if importe == 0:
            continue

        m = RE_OPT_NAME.match(nombre)
        if m:
            sub   = m.group('sub').strip()
            tipo  = m.group('tipo').upper()
            strk  = m.group('strike').replace(',', '.')
            venc  = m.group('venc').upper()
        else:
            sub, tipo, strk, venc = nombre, '?', '', ''

        resultados[isin] = {
            'isin': isin, 'simbolo': nombre, 'fecha': fecha,
            'importe_eur': importe, 'gastos_eur': gastos,
            'subyacente': sub, 'tipo_op': tipo, 'strike': strk,
            'vencimiento': venc, 'anio_csv': anio_csv,
        }
    return resultados


def buscar_primas_anios_anteriores(orphan_info, base_dir, ejercicio,
                                   max_anios=MAX_ANIOS_BUSQUEDA):
    """
    Para cada opción huérfana (expirada/ejercida en el año actual sin venta
    correspondiente en el CSV del año actual), busca la venta original en los
    CSVs de años anteriores (DeGiro_Transacciones_XXXX.csv o DeGiro_Cuenta_XXXX.csv).

    Bajo DGT V2172-21 la alteración patrimonial de una short se produce en el año
    de extinción (cierre / expiración / ejercicio), no en el año de cobro de la prima.
    Por tanto la prima cobrada en un año anterior debe declararse en el año actual.

    Retorna:
        recuperadas   : dict {isin → datos de la venta encontrada}
        no_encontradas: list de dicts de opciones cuya venta no pudo localizarse
    """
    if not orphan_info:
        return {}, []

    pendientes   = set(orphan_info.keys())
    recuperadas  = {}

    for delta in range(1, max_anios + 1):
        if not pendientes:
            break
        anio = str(int(ejercicio) - delta)

        # Intentar ambos nombres posibles (transacciones y cuenta)
        for patron in [f'DeGiro_Transacciones_{anio}.csv',
                       f'DeGiro_Cuenta_{anio}.csv']:
            if not pendientes:
                break
            csv_path = os.path.join(base_dir, patron)
            if not os.path.exists(csv_path):
                continue

            formato = _detectar_formato_degiro(csv_path)
            if formato == 'cuenta':
                encontradas = _buscar_en_cuenta(csv_path, pendientes)
            else:
                encontradas = _buscar_en_transacciones(csv_path, pendientes)

            for isin, data in encontradas.items():
                recuperadas[isin] = {**orphan_info[isin], **data}
                pendientes.discard(isin)

    no_encontradas = [orphan_info[isin] for isin in pendientes]
    return recuperadas, no_encontradas


def parse_ibkr_opciones(filepath):
    """
    Parsea la sección 'Trades' con Asset Category = Options del Activity Statement IBKR.
    Retorna tupla (opciones, descartadas) — alineado con parse_ibkr,
    parse_degiro, parse_tr. `descartadas` es un dict con detalle de filas
    no incorporadas (útil en uso embebido/backend donde print() se pierde):
        descartadas = {
            'sin_fx': [{'symbol', 'currency', 'fecha'} ...],
        }
    El CLI sigue emitiendo el aviso por stdout además de poblar el dict.
    """
    descartadas: dict = {'sin_fx': []}
    if not os.path.exists(filepath):
        return [], descartadas

    opciones      = []
    trades_header = None
    RE_IBKR_OPT   = re.compile(
        r'(?P<sub>\w+)\s+(?P<venc>\d{2}[A-Z]{3}\d{2})\s+(?P<strike>[\d.]+)\s+(?P<tipo>[CP])',
        re.IGNORECASE
    )

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 5:
                continue

            if row[0] == 'Trades' and row[1] == 'Header':
                trades_header = row
                continue

            if row[0] == 'Trades' and row[1] == 'Data' and trades_header:
                h = trades_header
                def col_t(name):
                    return row[h.index(name)].strip() if name in h else ''
                # Solo filas 'Order' (ver nota en parse_ibkr): las ClosedLot del
                # export con lot detail duplicarían las posiciones.
                _disc = col_t('DataDiscriminator')
                if _disc and _disc != 'Order':
                    continue
                try:
                    asset_cat = col_t('Asset Category')
                    if 'Option' not in asset_cat:
                        continue

                    symbol   = col_t('Symbol')
                    date_str = col_t('Date/Time')
                    qty_str  = col_t('Quantity')
                    proceeds = col_t('Proceeds')
                    comm_fee = col_t('Comm/Fee')
                    isin_col = col_t('ISIN')
                    currency = col_t('Currency')

                    if not qty_str or qty_str in ('Quantity', 'Total'):
                        continue

                    quantity    = Decimal(qty_str.replace(',', ''))
                    importe_eur = abs(Decimal(proceeds.replace(',', '') or '0'))
                    gastos_eur  = abs(Decimal(comm_fee.replace(',', '') or '0'))
                    fecha       = parse_date(date_str) or ''

                    # Conversión a EUR para opciones en divisa local (USD…).
                    if currency and currency != 'EUR':
                        date_iso = (date_str.split(',')[0].strip()
                                    if date_str else '')
                        rate = _ibkr_eur_per_unit(date_iso, currency)
                        if rate is None:
                            descartadas['sin_fx'].append({
                                'symbol': symbol,
                                'currency': currency,
                                'fecha': date_iso,
                            })
                            print(f"  [AVISO IBKR opc] {symbol} en {currency} "
                                  f"({date_iso}) — sin tipo BCE, descartada")
                            continue
                        importe_eur = (importe_eur * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)
                        gastos_eur = (gastos_eur * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)

                    m = RE_IBKR_OPT.match(symbol)
                    if m:
                        subyacente  = m.group('sub')
                        vencimiento = m.group('venc').upper()
                        strike      = m.group('strike')
                        tipo_op     = m.group('tipo').upper()
                    else:
                        subyacente  = symbol
                        vencimiento = ''
                        strike      = ''
                        tipo_op     = '?'

                    cantidad   = abs(quantity)
                    prima_unit = (importe_eur / cantidad).quantize(
                        Decimal('0.0001'), ROUND_HALF_UP) if cantidad > 0 else Decimal('0')
                    accion     = 'venta' if quantity < 0 else 'compra'

                    # Code IBKR indica el resultado del trade:
                    #   O    = Open (apertura)
                    #   C    = Close (cierre en mercado)
                    #   Ep   = Expired worthless
                    #   Ex   = Exercised (ejercida — call corta o put larga)
                    #   A    = Assigned (asignada — put corta o call larga)
                    # Ep → expirada=True (prima va a 1624-1654, Art. 37.1.m).
                    # Ex/A → ejercida=True (prima integra coste/precio del subyacente).
                    code_str = col_t('Code') if 'Code' in h else ''
                    code_tokens = {t.strip() for t in code_str.split(';')}
                    es_expirada = 'Ep' in code_tokens
                    es_ejercida = bool({'Ex', 'A'} & code_tokens)

                    opciones.append({
                        'fecha':          fecha,
                        'simbolo':        symbol[:40],
                        'isin':           isin_col[:12] if isin_col else '',
                        'tipo_op':        tipo_op,
                        'subyacente':     subyacente,
                        'strike':         strike,
                        'vencimiento':    vencimiento,
                        'accion':         accion,
                        'cantidad':       cantidad,
                        'prima_unitaria': prima_unit,
                        'importe_eur':    importe_eur,
                        'gastos_eur':     gastos_eur,
                        'expirada':       es_expirada,
                        'ejercida':       es_ejercida,
                        'broker':         'IBKR',
                    })
                except (ValueError, IndexError, KeyError):
                    pass

    return opciones, descartadas


def parse_ibkr_futures(filepath):
    """Parsea la sección 'Trades' con Asset Category = Futures del Activity
    Statement de IBKR. Devuelve lista de trades con su Realized P/L ya
    calculado por IBKR (incluye multiplier y conversión).

    Doctrina aplicable: Manual práctico AEAT cap. 11, sección 14
    (Operaciones realizadas en los mercados de futuros y opciones).
    Las G/P de futuros especulativos van a la casilla 1626 con clave 4
    (Otros elementos patrimoniales no afectos a actividades económicas)
    de la base imponible del ahorro (Art. 33 LIRPF). Imputación al
    ejercicio en que se liquida la posición o se extingue el contrato
    (no día a día por MTM).

    El Realized P/L que IBKR expone en la sección Trades para el trade
    de CIERRE de cada contrato (Code='C') consolida apertura + cierre
    incluyendo el multiplier del contrato (ES=50, MES=5, NQ=20, etc.)
    y la conversión a la moneda base de la cuenta. Confiamos en él.

    Diferencia operativa clave vs opciones:
      - Opciones: el motor empareja apertura+cierre manualmente porque
        necesitamos distinguir cierre en mercado (→1626) vs ejercicio
        con entrega (→prima al subyacente). El Realized P/L de IBKR
        para opciones existe pero no lo usamos por esa razón.
      - Futuros: NO hay ejercicio con entrega típico para retail (los
        contratos se cierran en mercado o se compensan al rollover).
        Usar el Realized P/L de IBKR directamente es correcto y
        más simple.

    Cross-año automático: si el contrato se abrió en el año N-1 y se
    cierra en el año N, IBKR ya consolida el Realized P/L en el
    statement del año N (año del cierre). El motor lo lee y lo imputa
    al año del cierre, alineado con la doctrina AEAT.

    Retorna tupla (futures, descartadas) — alineado con parse_ibkr,
    parse_degiro, parse_tr. Cada elemento de `futures` es un dict con
    los campos:
        fecha (date del trade de cierre)
        symbol (ticker IBKR, ej. 'ESH26', 'MES Z25')
        descripcion (nombre completo del contrato)
        multiplier (si está disponible en FII, decimal o None)
        cantidad (número de contratos cerrados, valor absoluto)
        accion ('compra' o 'venta' — dirección del trade de cierre)
        realized_pl_eur (Decimal, ya en EUR y con multiplier aplicado)
        gastos_eur (comisión + tasas, Decimal en EUR)
        currency_origen (moneda original del contrato, p.ej. 'USD')
        code (string con tokens del campo Code de IBKR)
        broker ('IBKR')
        instrument_type ('FUTURE')

    `descartadas` es un dict con detalle de líneas no incorporadas
    (útil en uso embebido/backend donde print() se perdería):
        descartadas = {
            'sin_fx': [{'symbol', 'currency', 'fecha'} ...],
        }
    """
    descartadas: dict = {'sin_fx': []}
    if not os.path.exists(filepath):
        return [], descartadas

    futures      = []
    trades_header = None

    # Mapeo Symbol → {multiplier, description} desde FII (informativo).
    fii_map: dict = {}
    fii_header = None

    with open(filepath, encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 5:
                continue

            # ── Financial Instrument Information (multiplier + description) ──
            if row[0] == 'Financial Instrument Information' and row[1] == 'Header':
                fii_header = row
                continue
            if (row[0] == 'Financial Instrument Information'
                    and row[1] == 'Data' and fii_header):
                h = fii_header
                def col_f(name):
                    return row[h.index(name)] if name in h else ''
                if 'Futures' not in col_f('Asset Category'):
                    continue
                sym = col_f('Symbol').strip()
                if not sym:
                    continue
                try:
                    mult_raw = col_f('Multiplier')
                    mult = Decimal(mult_raw.replace(',', '')) if mult_raw else None
                except (InvalidOperation, ValueError):
                    mult = None
                fii_map[sym] = {
                    'multiplier': mult,
                    'description': col_f('Description').strip(),
                }
                continue

            # ── Trades section ───────────────────────────────────────────
            if row[0] == 'Trades' and row[1] == 'Header':
                trades_header = row
                continue
            if row[0] == 'Trades' and row[1] == 'Data' and trades_header:
                h = trades_header
                def col_t(name):
                    return row[h.index(name)].strip() if name in h else ''
                # Solo filas 'Order' (ver nota en parse_ibkr): las ClosedLot del
                # export con lot detail duplicarían las posiciones.
                _disc = col_t('DataDiscriminator')
                if _disc and _disc != 'Order':
                    continue
                try:
                    asset_cat = col_t('Asset Category')
                    # SOLO Futures puros — opciones sobre futuros van por
                    # parse_ibkr_opciones (que matchea 'Option' in asset_cat
                    # incluyendo 'Futures Options').
                    if asset_cat != 'Futures':
                        continue

                    symbol   = col_t('Symbol')
                    date_str = col_t('Date/Time')
                    qty_str  = col_t('Quantity')
                    proceeds = col_t('Proceeds')
                    comm_fee = col_t('Comm/Fee')
                    realized = col_t('Realized P/L')
                    currency = col_t('Currency')

                    if not qty_str or qty_str in ('Quantity', 'Total'):
                        continue

                    quantity     = Decimal(qty_str.replace(',', ''))
                    realized_pl  = Decimal(realized.replace(',', '') or '0')
                    gastos_local = abs(Decimal(comm_fee.replace(',', '') or '0'))

                    fecha = parse_date(date_str)
                    if not fecha:
                        continue

                    # Conversión a EUR. Si la cuenta IBKR ya está en EUR,
                    # realized_pl y gastos_local YA vienen en EUR (es la
                    # base currency de la cuenta). Si no, IBKR ya hizo la
                    # conversión a la base de la cuenta (no es necesario
                    # FX adicional). En cualquier caso, aplicamos BCE
                    # cuando currency != 'EUR' (principio del tipo de
                    # cambio oficial publicado por el BCE).
                    if currency and currency != 'EUR':
                        date_iso = (date_str.split(',')[0].strip()
                                    if date_str else '')
                        rate = _ibkr_eur_per_unit(date_iso, currency)
                        if rate is None:
                            descartadas['sin_fx'].append({
                                'symbol': symbol,
                                'currency': currency,
                                'fecha': date_iso,
                            })
                            print(f"  [AVISO IBKR fut] {symbol} en "
                                  f"{currency} ({date_iso}) — sin tipo BCE, "
                                  f"descartado")
                            continue
                        realized_pl = (realized_pl * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)
                        gastos_local = (gastos_local * rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP)

                    # Solo el trade de cierre tiene Realized P/L != 0.
                    # Las aperturas tienen Realized P/L = 0 (no se ha
                    # cerrado nada). Para evitar duplicar el cómputo, sólo
                    # registramos las líneas con Realized P/L ≠ 0.
                    if realized_pl == 0:
                        continue

                    code_str = col_t('Code') if 'Code' in h else ''
                    accion   = 'venta' if quantity < 0 else 'compra'

                    fii = fii_map.get(symbol, {})
                    futures.append({
                        'fecha':              fecha,
                        'symbol':             symbol[:40],
                        'descripcion':        fii.get('description', symbol)[:60],
                        'multiplier':         fii.get('multiplier'),
                        'cantidad':           abs(quantity),
                        'accion':             accion,
                        'realized_pl_eur':    realized_pl,
                        'gastos_eur':         gastos_local,
                        'currency_origen':    currency,
                        'code':               code_str,
                        'broker':             'IBKR',
                        'instrument_type':    'FUTURE',
                    })
                except (ValueError, IndexError, KeyError, InvalidOperation):
                    pass

    return futures, descartadas


def calcular_resumen_futuros(futuros):
    """Agrega los cierres de futuros devueltos por parse_ibkr_futures por
    Symbol (= contrato) y devuelve (por_contrato, totales).

    Cada cierre ya trae el Realized P/L calculado por IBKR (que incluye el
    multiplier del contrato y la conversión a la base currency). Esta
    función sólo agrupa y suma.

    Doctrina: Manual práctico AEAT cap. 11 §14. Los futuros especulativos
    se imputan al ejercicio en que se liquida la posición o se extingue
    el contrato → casilla 1626 con clave 4 (Otros elementos patrimoniales)
    de la base imponible del ahorro.

    IMPORTANTE (V3 auditoría 2026-06-11): el Realized P/L de la sección
    Trades de IBKR YA incorpora las comisiones en la base de coste — cita
    literal de la guía oficial de informes de IBKR (ibkrguides.com, Trades):
    "For the purpose of cost basis and realized profit or loss, commissions
    are netted. For MTM profit or loss, commissions are not netted".
    Por tanto pl_neto_eur = realized_pl_eur SIN restar gastos otra vez —
    restarlos duplicaba la deducción (infra-declaraba ganancias / inflaba
    pérdidas). `gastos_eur` se conserva como columna INFORMATIVA.

    Retorna:
      por_contrato: lista de dicts con:
        symbol, descripcion, multiplier, n_cierres, currency_origen,
        realized_pl_eur (suma), gastos_eur (suma, informativo — ya neteado
        en el Realized P/L), pl_neto_eur (= realized_pl_eur)
      totales: dict con:
        n_contratos_distintos, n_cierres_total,
        realized_pl_total_eur, gastos_total_eur, pl_neto_eur
    """
    by_symbol = defaultdict(lambda: {
        'symbol': '',
        'descripcion': '',
        'multiplier': None,
        'n_cierres': 0,
        'currency_origen': '',
        'realized_pl_eur': Decimal('0'),
        'gastos_eur': Decimal('0'),
        'fechas': [],
    })

    for f in futuros:
        sym = f['symbol']
        d = by_symbol[sym]
        d['symbol'] = sym
        if not d['descripcion'] and f.get('descripcion'):
            d['descripcion'] = f['descripcion']
        if d['multiplier'] is None and f.get('multiplier') is not None:
            d['multiplier'] = f['multiplier']
        if not d['currency_origen']:
            d['currency_origen'] = f.get('currency_origen', '')
        d['n_cierres']       += 1
        d['realized_pl_eur'] += f.get('realized_pl_eur', Decimal('0'))
        d['gastos_eur']      += f.get('gastos_eur', Decimal('0'))
        d['fechas'].append(f['fecha'])

    # Lista ordenada por |pl_neto| descendente (más relevantes arriba).
    # pl_neto = realized_pl: las comisiones YA están neteadas dentro del
    # Realized P/L de IBKR (ver docstring) — restarlas otra vez las
    # deduciría dos veces.
    por_contrato = []
    for sym, d in by_symbol.items():
        d['pl_neto_eur'] = d['realized_pl_eur']
        por_contrato.append(d)
    por_contrato.sort(key=lambda x: abs(x['pl_neto_eur']), reverse=True)

    totales = {
        'n_contratos_distintos':  len(by_symbol),
        'n_cierres_total':        sum(d['n_cierres'] for d in por_contrato),
        'realized_pl_total_eur':  sum((d['realized_pl_eur'] for d in por_contrato),
                                       Decimal('0')),
        'gastos_total_eur':       sum((d['gastos_eur'] for d in por_contrato),
                                       Decimal('0')),
        'pl_neto_eur':            sum((d['pl_neto_eur'] for d in por_contrato),
                                       Decimal('0')),
    }
    return por_contrato, totales


def calcular_resumen_opciones(opciones):
    """
    Agrupa operaciones de opciones por subyacente + tipo + strike + vencimiento.
    Calcula primas cobradas (ventas), primas pagadas (compras) y P&L neto.

    Las opciones expiradas al vencimiento aportan 0 a primas cobradas/pagadas
    (ya se reflejaron en la compra/venta original).

    Retorna (por_contrato, totales).
    """
    by_contrato = defaultdict(lambda: {
        'nombre': '', 'tipo_op': '?', 'subyacente': '',
        'strike': '', 'vencimiento': '', 'brokers': set(),
        'primas_cobradas': Decimal('0'), 'gastos_cobradas': Decimal('0'),
        'primas_pagadas': Decimal('0'),  'gastos_pagadas': Decimal('0'),
        'contratos_vendidos': Decimal('0'), 'contratos_comprados': Decimal('0'),
        'expiradas': 0, 'n_ejercidas': 0,
        '_ventas': [],   # lista de (fecha, importe_eur, gastos_eur, cantidad) — para FIFO
        '_compras': [],  # lista de (fecha, importe_eur, gastos_eur, cantidad) — para FIFO
        '_ejercidas_fechas': [],  # fechas DD/MM/YYYY de los trades de cierre (Ex/A IBKR; OPCIÓN EJERCIDA DeGiro)
        'nota_anio_anterior': None,  # año CSV de origen si la prima viene de un año anterior
    })

    for op in opciones:
        clave = f"{op['subyacente']}_{op['tipo_op']}_{op['strike']}_{op['vencimiento']}_{op['broker']}"
        d     = by_contrato[clave]
        if not d['nombre']:
            d['nombre']      = op['simbolo']
            d['tipo_op']     = op['tipo_op']
            d['subyacente']  = op['subyacente']
            d['strike']      = op['strike']
            d['vencimiento'] = op['vencimiento']
        d['brokers'].add(op['broker'])

        if op.get('ejercida'):
            d['n_ejercidas'] += 1
            # Guardar la fecha del trade que cerró la opción por ejercicio/asignación.
            # En IBKR las asignaciones ocurren con frecuencia ANTES del vencimiento,
            # así que la fecha real de transferencia del subyacente puede ser distinta.
            if op.get('fecha'):
                d['_ejercidas_fechas'].append(op['fecha'])
            continue  # el cierre a 0 no aporta prima; la prima ya está en la venta original

        if op['expirada']:
            d['expiradas'] += 1
            continue

        if op['accion'] == 'venta':
            d['primas_cobradas']    += op['importe_eur']
            d['gastos_cobradas']    += op['gastos_eur']
            d['contratos_vendidos'] += op['cantidad']
            d['_ventas'].append((op['fecha'], op['importe_eur'], op['gastos_eur'], op['cantidad']))
            if op.get('nota_anio_anterior') and not d['nota_anio_anterior']:
                d['nota_anio_anterior'] = op['nota_anio_anterior']
        else:
            d['primas_pagadas']      += op['importe_eur']
            d['gastos_pagadas']      += op['gastos_eur']
            d['contratos_comprados'] += op['cantidad']
            d['_compras'].append((op['fecha'], op['importe_eur'], op['gastos_eur'], op['cantidad']))

    def _parse_ddmmyyyy(s):
        if not s: return datetime.min
        for fmt in ('%d/%m/%Y', '%d-%m-%Y'):
            try: return datetime.strptime(s, fmt)
            except: pass
        return datetime.min

    # Fecha de corte para inferir expiración: 31/12 del ejercicio, acotado por
    # la fecha de ejecución (no inferir vencimientos futuros respecto a hoy
    # si el ejercicio aún está abierto, p.ej. al procesar el año en curso).
    try:
        _corte_dt = min(datetime(int(EJERCICIO), 12, 31), datetime.now())
    except Exception:
        _corte_dt = datetime.now()

    def _parse_opra_venc(venc_str):
        """'16JAN26' -> datetime(2026, 1, 16). DeGiro usa 'OKT' por octubre."""
        s = _venc_to_ddmmyyyy(venc_str)
        if not s:
            return None
        try:
            return datetime.strptime(s, '%d/%m/%Y')
        except ValueError:
            return None

    def _split_filas_fifo(filas_sorted, n_contratos):
        """Divide filas (fecha, importe, gastos, cantidad) ya ordenadas por
        fecha en (porción que cubre n_contratos, resto), ponderando por
        CONTRATOS y no por filas (F9 auditoría 2026-06-11: una fila de
        venta de 5 contratos contaba como 1 al cortar con [:n_comp]).
        Si el corte parte una fila, importe y gastos se prorratean por la
        fracción de contratos; el redondeo se asigna a la porción cubierta
        y el resto conserva la diferencia exacta (sin perder céntimos).
        """
        cubiertas, resto = [], []
        pendiente = (n_contratos if isinstance(n_contratos, Decimal)
                     else Decimal(str(n_contratos)))
        for fila in filas_sorted:
            fecha_f, imp_f, gas_f, qty_f = fila
            if pendiente <= 0:
                resto.append(fila)
                continue
            if qty_f <= pendiente:
                cubiertas.append(fila)
                pendiente -= qty_f
            else:
                frac  = pendiente / qty_f
                imp_c = (imp_f * frac).quantize(Decimal('0.01'), ROUND_HALF_UP)
                gas_c = (gas_f * frac).quantize(Decimal('0.01'), ROUND_HALF_UP)
                cubiertas.append((fecha_f, imp_c, gas_c, pendiente))
                resto.append((fecha_f, imp_f - imp_c, gas_f - gas_c,
                              qty_f - pendiente))
                pendiente = Decimal('0')
        return cubiertas, resto

    resultados = []
    for clave, d in by_contrato.items():
        # ── Inferencia de expiración sin valor (Art. 14.1.c LIRPF) ──────────
        # DeGiro no emite fila de cierre cuando una opción LARGA expira sin
        # valor: el contrato simplemente desaparece de cartera. Sin esto el
        # motor lo clasifica como `es_long_abierta=True` indefinidamente y
        # nunca imputa la prima como pérdida en casilla 1626. Las cortas
        # worthless sí se detectan (DeGiro sí emite fila precio-0 de
        # asignación cero); IBKR codifica la expiración con Code=Ep en
        # ambas direcciones, ya cubierto por `expirada=True` en parse_ibkr.
        #
        # Reglas (estrictas para no romper otros casos):
        # - Solo si el vencimiento ya pasó respecto a la fecha de corte
        #   (31/12 del ejercicio acotado por hoy).
        # - Solo si el neto del contrato sigue abierto (compradas != vendidas
        #   y sin cancelaciones por expiración/ejercicio).
        # - NO si hubo ejercicio registrado (la prima va al subyacente, no
        #   a 1626, doctrina V2172-21).
        venc_dt = _parse_opra_venc(d.get('vencimiento') or '')
        d.setdefault('_inferred_expiration', False)
        d.setdefault('_inferred_exp_date', '')
        if (venc_dt and venc_dt <= _corte_dt
                and d['n_ejercidas'] == 0
                and d['expiradas'] == 0):
            n_long_neto  = int(d['contratos_comprados'] - d['contratos_vendidos'])
            n_short_neto = int(d['contratos_vendidos'] - d['contratos_comprados'])
            n_to_expire = 0
            if n_long_neto > 0 and d['contratos_vendidos'] == 0:
                # Largo puro vencido sin cierre — caso típico DeGiro
                n_to_expire = n_long_neto
            elif n_short_neto > 0 and d['contratos_comprados'] == 0:
                # Corto puro vencido sin cierre — robustez por si DeGiro
                # también omite la fila precio-0 en algún edge
                n_to_expire = n_short_neto
            if n_to_expire > 0:
                d['expiradas'] += n_to_expire
                d['_inferred_expiration'] = True
                d['_inferred_exp_date']   = venc_dt.strftime('%d/%m/%Y')

        pl_bruto = d['primas_cobradas'] - d['primas_pagadas']
        gastos   = d['gastos_cobradas'] + d['gastos_pagadas']
        pl_neto  = pl_bruto - gastos

        # Clasificación fiscal (Art. 14.1.c + 37.1.m LIRPF, DGT V2172-21):
        # - mixta: short ejercida Y con buy-to-close (ventas Y compras) → FIFO:
        #          parte cerrada a 1626, parte ejercida integra en acciones
        # - ejercida pura (short): ejercida sin buy-to-close → prima cobrada
        #          íntegra a acciones
        # - ejercida_larga: LONG ejercida (solo compras + ejercicio) → la prima
        #          PAGADA se integra en el subyacente (CALL: +coste adquisición;
        #          PUT: −valor transmisión). NO va a 1626. Antes caía en "mixta"
        #          con _ventas vacío → pérdida fantasma en 1626 (F9 auditoría).
        # - long_abierta: solo compras sin cierre/expiración → coste diferido al año de cierre
        # - short_abierta: vendida pero NO cerrada/expirada/ejercida al 31/12 → prima
        #   diferida al año de cierre (la alteración patrimonial aún no se ha producido)
        # - roll_abierta: roll dentro del año (sell→buy-close→re-sell) con re-sell abierta
        #   al 31/12 → porción cerrada a 1626, porción abierta diferida al año de extinción
        # - normal: cerrada/expirada en el ejercicio → otros elementos patrimoniales
        es_mixta = (d['n_ejercidas'] > 0 and d['contratos_comprados'] > 0
                    and d['contratos_vendidos'] > 0)
        es_ejercida_larga = (d['n_ejercidas'] > 0
                             and d['contratos_comprados'] > 0
                             and d['contratos_vendidos'] == 0)
        es_ejercida = (d['n_ejercidas'] > 0 and not es_mixta
                       and not es_ejercida_larga)
        es_long_abierta = (d['contratos_comprados'] > 0
                           and d['contratos_vendidos'] == 0
                           and d['expiradas'] == 0
                           and d['n_ejercidas'] == 0)
        n_net_abiertos = max(0, int(d['contratos_vendidos'])
                             - int(d['contratos_comprados'])
                             - d['expiradas'] - d['n_ejercidas'])
        es_short_abierta = (d['contratos_vendidos'] > 0
                            and d['contratos_comprados'] == 0
                            and d['expiradas'] == 0
                            and d['n_ejercidas'] == 0
                            and not es_mixta)
        # Roll con re-venta abierta: hay compras (cierre) Y posición neta abierta al 31/12
        es_roll_abierta = (n_net_abiertos > 0
                           and d['contratos_comprados'] > 0
                           and d['n_ejercidas'] == 0
                           and not es_mixta)

        # Fecha del último trade de cierre por ejercicio/asignación. Útil cuando
        # difiere del vencimiento (asignaciones tempranas IBKR). Para contratos
        # cuya expiración hemos INFERIDO (DeGiro no emite fila de cierre cuando
        # una larga expira sin valor), usar la fecha del vencimiento para que
        # el flujo imputador asigne la pérdida/ganancia al año del vencimiento
        # — no al de la compra/venta original.
        fecha_cierre_ej = ''
        if d['_ejercidas_fechas']:
            fechas_validas = [(f, _parse_ddmmyyyy(f)) for f in d['_ejercidas_fechas']]
            fechas_validas = [(f, dt) for f, dt in fechas_validas if dt != datetime.min]
            if fechas_validas:
                fecha_cierre_ej = max(fechas_validas, key=lambda x: x[1])[0]
        elif d.get('_inferred_expiration') and d.get('_inferred_exp_date'):
            fecha_cierre_ej = d['_inferred_exp_date']

        base = {**d,
            'pl_bruto':          pl_bruto,
            'gastos':            gastos,
            'pl_neto':           pl_neto,
            'brokers':           ', '.join(sorted(d['brokers'])),
            'n_net_abiertos':    n_net_abiertos,
            'es_ejercida':       es_ejercida,
            'es_ejercida_larga': es_ejercida_larga,
            'es_long_abierta':   es_long_abierta,
            'es_mixta':          es_mixta,
            'es_short_abierta':  es_short_abierta,
            'es_roll_abierta':   es_roll_abierta,
            'fecha_cierre':      fecha_cierre_ej,  # '' si no aplica
            'inferred_expiration': d.get('_inferred_expiration', False),
            'inferred_exp_date':   d.get('_inferred_exp_date', ''),
        }

        if es_mixta:
            # FIFO ponderado por CONTRATOS: las ventas más antiguas quedan
            # cerradas por el buy-to-close (tantos contratos como se
            # compraron); el resto son las que se ejercieron. NO cortar por
            # filas — una fila puede agrupar varios contratos.
            ventas_sorted = sorted(d['_ventas'], key=lambda x: _parse_ddmmyyyy(x[0]))
            ventas_cerradas, ventas_ejercidas = _split_filas_fifo(
                ventas_sorted, d['contratos_comprados'])

            # `start=Decimal('0')` es obligatorio: si la lista está vacía
            # (caso edge cuando los extractos solapan y la heurística de
            # mixta queda al límite), `sum(...)` sin start devuelve `int(0)`
            # y el fmt_es posterior intentaría llamar `.quantize` sobre int.
            prima_cerrada   = sum((v[1] for v in ventas_cerradas),  Decimal('0'))
            prima_ejercida  = sum((v[1] for v in ventas_ejercidas), Decimal('0'))
            gastos_cerrado  = (sum((v[2] for v in ventas_cerradas), Decimal('0'))
                               + d['gastos_pagadas'])
            gastos_ejercida = sum((v[2] for v in ventas_ejercidas), Decimal('0'))
            pl_cerrado      = prima_cerrada - d['primas_pagadas'] - gastos_cerrado

            base.update({
                '_prima_cerrada':   prima_cerrada,
                '_prima_ejercida':  prima_ejercida,
                '_gastos_cerrado':  gastos_cerrado,
                '_gastos_ejercida': gastos_ejercida,
                '_pl_cerrado':      pl_cerrado,
            })

        if es_roll_abierta:
            # FIFO ponderado por CONTRATOS (ver _split_filas_fifo): las ventas
            # más antiguas quedan cerradas por el buy-to-close; las más
            # recientes quedan abiertas al 31/12 → prima diferida.
            ventas_sorted_r   = sorted(d['_ventas'], key=lambda x: _parse_ddmmyyyy(x[0]))
            ventas_cerradas_r, ventas_abiertas_r = _split_filas_fifo(
                ventas_sorted_r, d['contratos_comprados'])

            prima_cerrada_r  = sum((v[1] for v in ventas_cerradas_r), Decimal('0'))
            prima_abierta_r  = sum((v[1] for v in ventas_abiertas_r), Decimal('0'))
            gastos_cerrado_r = (sum((v[2] for v in ventas_cerradas_r), Decimal('0'))
                                + d['gastos_pagadas'])
            gastos_abierta_r = sum((v[2] for v in ventas_abiertas_r), Decimal('0'))
            pl_cerrado_r     = prima_cerrada_r - d['primas_pagadas'] - gastos_cerrado_r

            base.update({
                '_prima_cerrada_r':  prima_cerrada_r,
                '_prima_abierta_r':  prima_abierta_r,
                '_gastos_cerrado_r': gastos_cerrado_r,
                '_gastos_abierta_r': gastos_abierta_r,
                '_pl_cerrado_r':     pl_cerrado_r,
            })

        resultados.append(base)

    resultados.sort(key=lambda x: (x['subyacente'], x['vencimiento'], x['strike']))

    # Clasificar en listas
    normales       = [r for r in resultados
                      if not r['es_ejercida'] and not r['es_long_abierta']
                      and not r['es_mixta'] and not r['es_short_abierta']
                      and not r['es_roll_abierta']
                      and not r['es_ejercida_larga']]
    ejercidas      = [r for r in resultados if r['es_ejercida']]   # short ejercidas puras
    ejercidas_largas = [r for r in resultados if r['es_ejercida_larga']]
    mixtas         = [r for r in resultados if r['es_mixta']]
    long_abiertas  = [r for r in resultados if r['es_long_abierta']]
    short_abiertas = [r for r in resultados if r['es_short_abierta']]
    roll_abiertas  = [r for r in resultados if r['es_roll_abierta']]

    # Casilla 1626: normales + porción cerrada de mixtas + porción cerrada de rolls
    # SHORT ABIERTAS AL 31/12 → diferidas al año de cierre (V2172-21 / Art. 14.1.c LIRPF)
    totales = {
        'primas_cobradas': (sum(d['primas_cobradas']   for d in normales)
                            + sum(d['_prima_cerrada']   for d in mixtas)
                            + sum(d['_prima_cerrada_r'] for d in roll_abiertas)),
        'primas_pagadas':  (sum(d['primas_pagadas']    for d in normales)
                            + sum(d['primas_pagadas']   for d in mixtas)
                            + sum(d['primas_pagadas']   for d in roll_abiertas)),
        'gastos':          (sum(d['gastos']             for d in normales)
                            + sum(d['_gastos_cerrado']  for d in mixtas)
                            + sum(d['_gastos_cerrado_r']for d in roll_abiertas)),
        'pl_bruto':        (sum(d['pl_bruto']           for d in normales)
                            + sum(d['_prima_cerrada'] - d['primas_pagadas'] for d in mixtas)
                            + sum(d['_prima_cerrada_r'] - d['primas_pagadas'] for d in roll_abiertas)),
        'pl_neto':         (sum(d['pl_neto']            for d in normales)
                            + sum(d['_pl_cerrado']      for d in mixtas)
                            + sum(d['_pl_cerrado_r']    for d in roll_abiertas)),
        'n_contratos_vend': (sum(d['contratos_vendidos']   for d in normales)
                             + sum(d['contratos_comprados'] for d in mixtas)
                             + sum(d['contratos_comprados'] for d in roll_abiertas)),
        'n_contratos_comp':  sum(d['contratos_comprados'] for d in normales),
        'n_expiradas':       sum(d['expiradas']            for d in normales),
        # Ejercidas: puras + porción ejercida de mixtas (nota informativa, no van a 1626)
        'ejercidas_prima_integrar': (sum(d['primas_cobradas'] for d in ejercidas)
                                     + sum(d['_prima_ejercida'] for d in mixtas)),
        'ejercidas_gastos':         (sum(d['gastos_cobradas']  for d in ejercidas)
                                     + sum(d['_gastos_ejercida'] for d in mixtas)),
        # Long ejercidas: prima PAGADA a integrar en el subyacente (V2172-21:
        # CALL → suma al coste de adquisición; PUT → resta del valor de
        # transmisión). Nota informativa, no va a 1626.
        'ejercidas_larga_coste_integrar': sum((d['primas_pagadas']
                                               for d in ejercidas_largas),
                                              Decimal('0')),
        'ejercidas_larga_gastos':         sum((d['gastos_pagadas']
                                               for d in ejercidas_largas),
                                              Decimal('0')),
        # Long abiertas (coste diferido, no deducible en este ejercicio)
        'long_abiertas_coste':  sum(d['primas_pagadas'] for d in long_abiertas),
        'long_abiertas_gastos': sum(d['gastos_pagadas'] for d in long_abiertas),
        # Short abiertas al 31/12 (puras + porción abierta de rolls): prima diferida
        'short_abiertas_prima':  (sum(d['primas_cobradas']   for d in short_abiertas)
                                   + sum(d['_prima_abierta_r'] for d in roll_abiertas)),
        'short_abiertas_gastos': (sum(d['gastos_cobradas']    for d in short_abiertas)
                                   + sum(d['_gastos_abierta_r'] for d in roll_abiertas)),
        # Listas clasificadas
        '_normales':       normales,
        '_ejercidas':      ejercidas,
        '_ejercidas_largas': ejercidas_largas,
        '_mixtas':         mixtas,
        '_long_abiertas':  long_abiertas,
        '_short_abiertas': short_abiertas,
        '_roll_abiertas':  roll_abiertas,
    }

    return resultados, totales


def write_informe_fx(fx_pl_data, filepath):
    """Escribe informe_fx_YYYY.txt con G/P de divisa de IBKR.

    Solo se invoca cuando hay datos FX (extracto IBKR procesado).
    DeGiro no proporciona este desglose — su FX se imputa dentro de cada
    operación al TC del día.

    NO modifica el sidecar totals.json: la decisión de declarar o aplicar
    minimis (DGT V0563-09, ~1.000 EUR) queda en manos del usuario.
    """
    lines = []
    SEP  = '─' * 65
    SEP2 = '═' * 65

    fx_rows     = fx_pl_data.get('fx', [])
    tbills_rows = fx_pl_data.get('tbills', [])

    lines += [
        f"INFORME DE GANANCIAS Y PÉRDIDAS DE DIVISA — Ejercicio {EJERCICIO}",
        SEP2,
        f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"Casillas RentaWEB: ejercicio {EJERCICIO} (campaña {_load_casillas_ejercicio(EJERCICIO)['campana']})",
        "",
        "FUENTE:",
        "  · Sección 'Realized & Unrealized Performance Summary' del Activity",
        "    Statement de IBKR. DeGiro NO proporciona este desglose; su FX",
        "    se imputa dentro de cada operación al TC del día.",
        "",
        "NOTA FISCAL:",
        "  · G/P por diferencia de cambio en saldos en divisa extranjera.",
        "  · Hecho imponible: cada conversión / consumo del saldo (no las",
        "    posiciones latentes).",
        "  · Tributa como ganancia o pérdida patrimonial — Art. 33 LIRPF.",
        f"  · Casillas RentaWEB: {C('otros')} (otros elementos patrimoniales).",
        f"  · Tipo/clave en {C('otros_clave_tipo')}.",
        "  · Regla práctica DGT V0563-09: la AEAT suele tolerar no declarar",
        "    diferencias de cambio menores en particulares (criterio orientativo,",
        "    no normativo). Umbral interpretativo ~1.000 EUR.",
        "",
    ]

    total_realized   = Decimal('0')
    total_unrealized = Decimal('0')

    if fx_rows:
        lines.append(f"REALIZED — pérdidas/ganancias materializadas en {EJERCICIO}")
        lines.append(SEP)
        for r in sorted(fx_rows, key=lambda x: x['divisa']):
            realized = r['realized'].quantize(Decimal('0.01'), ROUND_HALF_UP)
            total_realized += realized
            signo = '' if realized >= 0 else ''
            lines.append(f"  {r['divisa']:<6} {signo}{fmt_es(realized):>12} EUR")
        lines.append(SEP)
        lines.append(f"  {'TOTAL':<6} {fmt_es(total_realized.quantize(Decimal('0.01'), ROUND_HALF_UP)):>12} EUR")
        lines.append("")

        unr_no_cero = [r for r in fx_rows if r['unrealized'] != 0]
        if unr_no_cero:
            lines.append("UNREALIZED al 31/12 (informativo, NO declarable este año)")
            lines.append(SEP)
            for r in sorted(unr_no_cero, key=lambda x: x['divisa']):
                u = r['unrealized'].quantize(Decimal('0.01'), ROUND_HALF_UP)
                total_unrealized += u
                lines.append(f"  {r['divisa']:<6} {fmt_es(u):>12} EUR")
            lines.append("")

        lines.append("INSTRUCCIONES")
        lines.append(SEP)
        if total_realized < 0:
            lines.append(f"  Pérdida realizada agregada {EJERCICIO}: {fmt_es(total_realized)} EUR")
            if abs(total_realized) < 1000:
                lines.append("  · Importe < 1.000 EUR → la AEAT suele tolerar no declarar")
                lines.append("    (criterio DGT V0563-09, orientativo).")
                lines.append("  · Si optas por declararla para compensar otras G/P:")
            else:
                lines.append("  · Importe > 1.000 EUR → declarar es la práctica estándar.")
            lines.append(f"    RentaWEB → F2 → casillas {C('otros')} → 'Otros elementos patrimoniales'")
            lines.append(f"    Tipo (casilla {C('otros_clave_tipo')}): 'Resto'")
            lines.append(f"    Importe: {fmt_es(total_realized)} EUR (signo negativo: pérdida)")
            lines.append("")
            lines.append("  ⚠️  REGLA DEL AÑO — Art. 33.5.e LIRPF (no implementada, verificar):")
            lines.append("    Si se RECOMPRÓ la misma divisa dentro del año siguiente a la")
            lines.append("    transmisión con pérdida (lo habitual en cuentas con operativa")
            lines.append("    activa en esa divisa), la pérdida se DIFIERE — no se anula —")
            lines.append("    hasta la transmisión posterior de lo recomprado. A las divisas")
            lines.append("    les aplica la ventana de 1 AÑO del 33.5.e (elementos no")
            lines.append("    admitidos a negociación en mercado regulado), no la de 2 meses.")
            lines.append("    IBKR solo reporta el agregado anual por divisa, sin lotes —")
            lines.append("    Cuádrate NO puede comprobar la recompra automáticamente.")
            lines.append("    Criterio conservador: declara la pérdida solo si no recompraste")
            lines.append("    esa divisa en la ventana; en caso de duda, difiérela.")
        else:
            lines.append(f"  Ganancia realizada agregada {EJERCICIO}: {fmt_es(total_realized)} EUR")
            lines.append("  · Las ganancias por diferencia de cambio son SIEMPRE declarables")
            lines.append("    (la regla de minimis solo aplica a pérdidas tolerables).")
            lines.append(f"    RentaWEB → F2 → casillas {C('otros')} → 'Otros elementos patrimoniales'")
            lines.append(f"    Tipo (casilla {C('otros_clave_tipo')}): 'Resto'")
            lines.append(f"    Importe: {fmt_es(total_realized)} EUR")
        lines.append("")

    if tbills_rows:
        total_tbill = sum((t['realized'] for t in tbills_rows), Decimal('0'))
        total_tbill = total_tbill.quantize(Decimal('0.01'), ROUND_HALF_UP)
        lines.append("INFORMACIÓN ADICIONAL — Treasury Bills detectados")
        lines.append(SEP)
        for t in tbills_rows:
            r = t['realized'].quantize(Decimal('0.01'), ROUND_HALF_UP)
            lines.append(f"  {t['symbol'][:55]}: {fmt_es(r):>10} EUR")
        lines.append(SEP)
        lines.append(f"  TOTAL T-Bills: {fmt_es(total_tbill)} EUR")
        lines.append("")
        lines.append(f"  → Estos importes son RCM (Art. 25.2 LIRPF, base del ahorro).")
        lines.append(f"  → Casilla RentaWEB: {C('letras_tesoro')} (otros activos financieros — deuda pública extranjera al descuento).")
        lines.append(f"    Alternativa doctrinal para T-Bills extranjeros: 0031 (otros activos")
        lines.append(f"    financieros) — cuota IDÉNTICA en base del ahorro; el manual AEAT no")
        lines.append(f"    distingue deuda pública extranjera de forma expresa.")
        lines.append("  → Verificar en sección Withholding Tax si hubo retención sobre")
        lines.append(f"    estos T-Bills (en ese caso → casilla {C('cdi')} = recuperable CDI).")
        lines.append("  → También listados en informe_dividendos para visibilidad.")
        lines.append("")

    lines.append(SEP2)
    lines.append("ALCANCE")
    lines.append(SEP)
    lines.append("  · Este informe SOLO se genera con datos de IBKR.")
    lines.append("  · NO suma al sidecar totals.json — la decisión de declarar")
    lines.append("    queda explícitamente en manos del usuario.")
    lines.append("")

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def write_informe_opciones(por_contrato, totales, filepath, no_encontradas=None):
    """Escribe informe_opciones_YYYY.txt con secciones fiscales."""
    lines = []
    SEP  = '─' * 65
    SEP2 = '═' * 65

    normales       = totales['_normales']
    ejercidas      = totales['_ejercidas']
    ejercidas_largas = totales.get('_ejercidas_largas', [])
    mixtas         = totales['_mixtas']
    long_abiertas  = totales['_long_abiertas']
    short_abiertas = totales['_short_abiertas']
    roll_abiertas  = totales['_roll_abiertas']

    def bloque_opcion(d, etiqueta_extra=''):
        tipo_str = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
        pl_str   = f"+{fmt_es(d['pl_neto'])}" if d['pl_neto'] >= 0 else fmt_es(d['pl_neto'])
        signo    = "✅" if d['pl_neto'] >= 0 else "❌"
        blk = [
            "",
            SEP,
            f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}{etiqueta_extra}",
            SEP,
            f"  Broker                  : {d['brokers']}",
            f"  Contratos vendidos      : {d['contratos_vendidos']:.0f}   Comprados: {d['contratos_comprados']:.0f}",
        ]
        if d.get('nota_anio_anterior'):
            blk.append(
                f"  ⚠️  Prima cobrada en {d['nota_anio_anterior']} — declarada en {EJERCICIO} "
                f"(DGT V2172-21: tributa en año de extinción)"
            )
        if d['expiradas']:
            blk.append(f"  Expiradas al vencimiento: {d['expiradas']}")
        if d['n_ejercidas']:
            blk.append(f"  Ejercidas               : {d['n_ejercidas']}")
        blk += [
            f"  Primas cobradas (ventas): {fmt_es(d['primas_cobradas'])} EUR",
            f"  Primas pagadas (compras): {fmt_es(d['primas_pagadas'])} EUR",
            f"  Gastos (comisiones)     : {fmt_es(d['gastos'])} EUR",
            f"  {signo} P&L neto             : {pl_str} EUR",
        ]
        return blk

    lines += [
        f"INFORME DE OPCIONES Y DERIVADOS — Ejercicio {EJERCICIO}",
        SEP2,
        f"Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
        "NOTA FISCAL (Art. 14.1.c + 37.1.m LIRPF · DGT V2172-21):",
        "  · Short cerrada/expirada en el ejercicio: prima neta → casillas 1624-1654 (otros elementos patrimoniales).",
        "  · Short abierta al 31/12: prima DIFERIDA al año en que se cierre/expire/ejerza.",
        "    La alteración patrimonial no se produce hasta la extinción del contrato.",
        "  · Short ejercida: prima cobrada ÍNTEGRA modifica el precio de acciones.",
        "    NO va al bloque 1624-1654; ajusta precio transmisión/adquisición del subyacente.",
        "  · Long EJERCIDA: prima pagada se integra en el subyacente (CALL: suma al",
        "    coste de adquisición; PUT: resta del valor de transmisión). NO va a 1624-1654.",
        "  · Long abierta al 31/12: coste DIFERIDO hasta cierre/vencimiento (año siguiente).",
        "  · Long cerrada con ganancia/pérdida: va a casillas 1624-1654 (otros elementos patrimoniales).",
        "",
    ]

    # ── SECCIÓN 1: Otros elementos patrimoniales (posiciones cerradas/expiradas) ──
    lines += [
        SEP2,
        "  SECCIÓN 1 — OTROS ELEMENTOS PATRIMONIALES  (cerradas / expiradas)",
        "  Casillas RentaWEB: 1624-1654 (apartado F2)",
        "  Short cerradas (P&L) + short expiradas + long cerradas con P&L",
        SEP2,
    ]
    if normales or mixtas or roll_abiertas:
        for d in normales:
            lines += bloque_opcion(d)
        for d in mixtas:
            # Mostrar solo la porción CERRADA de la posición mixta
            tipo_str = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
            pl_c     = d['_pl_cerrado']
            pl_str   = f"+{fmt_es(pl_c)}" if pl_c >= 0 else fmt_es(pl_c)
            signo    = "✅" if pl_c >= 0 else "❌"
            n_cerradas = int(d['contratos_comprados'])
            lines += [
                "",
                SEP,
                f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}  🔀 MIXTA (porción cerrada)",
                SEP,
                f"  Broker                  : {d['brokers']}",
                f"  Contratos cerrados      : {n_cerradas}   (buy-to-close, resto ejercido)",
                f"  Primas cobradas (ventas): {fmt_es(d['_prima_cerrada'])} EUR",
                f"  Primas pagadas (compras): {fmt_es(d['primas_pagadas'])} EUR",
                f"  Gastos (comisiones)     : {fmt_es(d['_gastos_cerrado'])} EUR",
                f"  {signo} P&L neto             : {pl_str} EUR",
            ]
        for d in roll_abiertas:
            # Porción CERRADA del roll: sell→buy-close→re-sell (solo el ciclo cerrado)
            tipo_str   = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
            pl_c       = d['_pl_cerrado_r']
            pl_str     = f"+{fmt_es(pl_c)}" if pl_c >= 0 else fmt_es(pl_c)
            signo      = "✅" if pl_c >= 0 else "❌"
            n_cerradas = int(d['contratos_comprados'])
            n_abiertos = d['n_net_abiertos']
            lines += [
                "",
                SEP,
                f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}  🔄 ROLL (porción cerrada)",
                SEP,
                f"  Broker                  : {d['brokers']}",
                f"  Contratos cerrados      : {n_cerradas}   (buy-to-close; {n_abiertos} re-vendido(s) diferido(s) → Sección 4)",
                f"  Primas cobradas (ventas): {fmt_es(d['_prima_cerrada_r'])} EUR",
                f"  Primas pagadas (compras): {fmt_es(d['primas_pagadas'])} EUR",
                f"  Gastos (comisiones)     : {fmt_es(d['_gastos_cerrado_r'])} EUR",
                f"  {signo} P&L neto             : {pl_str} EUR",
            ]
    else:
        lines += ["", "  (ninguna posición en esta categoría)"]

    pl_neto_1626   = totales['pl_neto']
    pl_str_1626    = f"+{fmt_es(pl_neto_1626)}" if pl_neto_1626 >= 0 else fmt_es(pl_neto_1626)
    prima_ejercida = totales['ejercidas_prima_integrar']
    prima_diferida = totales['short_abiertas_prima']
    prima_total    = (totales['primas_cobradas'] + prima_ejercida
                      + prima_diferida)   # todas las ventas del año
    sep_lin = "  " + "─" * 43

    # Fechas extremas para agrupar todo el bloque 1624-1654 en una sola
    # entrada de RentaWEB (AEAT acepta agrupación con fecha de la primera
    # apertura → fecha del último cierre).
    def _parse_dd(s):
        if not s:
            return datetime.min
        for fmt in ('%d/%m/%Y', '%d-%m-%Y'):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        return datetime.min

    fechas_apertura: list = []
    fechas_cierre: list = []
    for _d in normales + mixtas + roll_abiertas:
        for _v in _d.get('_ventas', []):
            if _v and _v[0]:
                fechas_apertura.append(_v[0])
        for _c in _d.get('_compras', []):
            if _c and _c[0]:
                fechas_cierre.append(_c[0])
        if _d.get('expiradas', 0) > 0:
            _venc_dd = _venc_to_ddmmyyyy(_d.get('vencimiento', ''))
            if _venc_dd:
                fechas_cierre.append(_venc_dd)
    fecha_primera_apertura = (min(fechas_apertura, key=_parse_dd)
                              if fechas_apertura else '')
    fecha_ultimo_cierre = (max(fechas_cierre, key=_parse_dd)
                           if fechas_cierre else '')

    # Desglose por broker para el bloque de otros elementos patrimoniales (1624-1654)
    pb_1626 = defaultdict(lambda: {'pl_neto': Decimal('0'), 'n': 0})
    for d in normales:
        pb_1626[d['brokers']]['pl_neto'] += d['pl_neto']
        pb_1626[d['brokers']]['n'] += 1
    for d in mixtas:
        pb_1626[d['brokers']]['pl_neto'] += d['_pl_cerrado']
        pb_1626[d['brokers']]['n'] += 1
    for d in roll_abiertas:
        pb_1626[d['brokers']]['pl_neto'] += d['_pl_cerrado_r']
        pb_1626[d['brokers']]['n'] += 1

    broker_opt_lines = []
    if pb_1626:
        broker_opt_lines += ["", "  DESGLOSE POR BROKER (otros elementos patrimoniales — casillas 1624-1654):"]
        for b, v in sorted(pb_1626.items()):
            pl_b_str = f"+{fmt_es(v['pl_neto'])}" if v['pl_neto'] >= 0 else fmt_es(v['pl_neto'])
            broker_opt_lines.append(
                f"  · {b:<12}: P&L neto {pl_b_str:>10} EUR  ({v['n']} serie(s))"
            )

    lines += [
        "",
        SEP2,
        "  RESUMEN OTROS ELEMENTOS PATRIMONIALES (casillas 1624-1654)",
        SEP2,
        f"  Primas cobradas (todas las ventas del año): {fmt_es(prima_total):>10} EUR",
        f"  (-) Excluidas por ejercicio (→ acciones) : {fmt_es(prima_ejercida):>10} EUR",
        f"  (-) Diferidas al 31/12 (→ {int(EJERCICIO)+1})           : {fmt_es(prima_diferida):>10} EUR",
        sep_lin,
        f"  Primas cobradas declarables (otros elem.) : {fmt_es(totales['primas_cobradas']):>10} EUR",
        f"  (-) Primas pagadas (buy-to-close)         : {fmt_es(totales['primas_pagadas']):>10} EUR",
        f"  (-) Gastos (comisiones)                   : {fmt_es(totales['gastos']):>10} EUR",
        sep_lin,
        f"  ▶ P&L neto — OTROS ELEMENTOS PATRIMONIALES : {pl_str_1626:>10} EUR",
        "",
        f"  Fecha primera apertura (1ª venta de prima): {fecha_primera_apertura or '—':>10}",
        f"  Fecha último cierre / expiración          : {fecha_ultimo_cierre or '—':>10}",
    ] + broker_opt_lines + [
        "",
        "  DÓNDE DECLARAR:",
        "  Ganancias → Transmisión de otros elementos patrimoniales → casillas 1624-1654",
        "  Introducir el importe neto en una única fila (o una por serie).",
    ]

    # ── SECCIÓN 2: Opciones ejercidas (integrar en precio de acciones) ────
    lines += [
        "",
        SEP2,
        "  SECCIÓN 2 — OPCIONES EJERCIDAS  (NO van a otros elementos patrimoniales)",
        "  La prima cobrada modifica el precio de venta/adquisición del subyacente.",
        SEP2,
    ]
    if ejercidas or mixtas or ejercidas_largas:
        # Ejercidas puras
        for d in ejercidas:
            lineas_extra = []
            cobradas = d['primas_cobradas']
            if cobradas > 0:
                if d['tipo_op'] == 'C':
                    lineas_extra.append(
                        f"  → CALL ejercida: añadir {fmt_es(cobradas)} EUR a los ingresos"
                        f" de la venta de acciones (sube precio de transmisión).")
                elif d['tipo_op'] == 'P':
                    lineas_extra.append(
                        f"  → PUT ejercida: restar {fmt_es(cobradas)} EUR del coste de adquisición"
                        f" de las acciones compradas (baja precio de adquisición).")
            blk = bloque_opcion(d, '  ⚠️ EJERCIDA')
            lines += blk + lineas_extra
        # Porción ejercida de posiciones mixtas
        for d in mixtas:
            tipo_str    = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
            prima_ej    = d['_prima_ejercida']
            n_ejercidas = d['n_ejercidas']
            lines += [
                "",
                SEP,
                f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}  ⚠️ MIXTA (porción ejercida)",
                SEP,
                f"  Broker                  : {d['brokers']}",
                f"  Contratos ejercidos     : {n_ejercidas}",
                f"  Prima a integrar        : {fmt_es(prima_ej)} EUR",
            ]
            if prima_ej > 0:
                if d['tipo_op'] == 'C':
                    lines.append(
                        f"  → CALL ejercida: añadir {fmt_es(prima_ej)} EUR a los ingresos"
                        f" de la venta de acciones (sube precio de transmisión).")
                elif d['tipo_op'] == 'P':
                    lines.append(
                        f"  → PUT ejercida: restar {fmt_es(prima_ej)} EUR del coste de adquisición"
                        f" de las acciones compradas (baja precio de adquisición).")
        # Largas ejercidas (lado comprador, V2172-21): prima PAGADA integra.
        for d in ejercidas_largas:
            tipo_str = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
            pagadas  = d['primas_pagadas']
            lines += [
                "",
                SEP,
                f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}  ⚠️ LARGA EJERCIDA",
                SEP,
                f"  Broker                  : {d['brokers']}",
                f"  Contratos ejercidos     : {d['n_ejercidas']}",
                f"  Prima pagada a integrar : {fmt_es(pagadas)} EUR",
            ]
            if pagadas > 0:
                if d['tipo_op'] == 'C':
                    lines.append(
                        f"  → CALL larga ejercida: sumar {fmt_es(pagadas)} EUR al coste de"
                        f" adquisición de las acciones compradas al strike (sube precio de adquisición).")
                elif d['tipo_op'] == 'P':
                    lines.append(
                        f"  → PUT larga ejercida: restar {fmt_es(pagadas)} EUR del valor de"
                        f" transmisión de las acciones vendidas al strike (baja precio de venta).")
        prima_integrar = totales['ejercidas_prima_integrar']
        lines += [
            "",
            f"  Prima total a integrar en acciones: {fmt_es(prima_integrar)} EUR",
        ]
        coste_larga_integrar = totales.get('ejercidas_larga_coste_integrar', Decimal('0'))
        if coste_larga_integrar:
            lines.append(
                f"  Prima pagada (largas ejercidas) a integrar: {fmt_es(coste_larga_integrar)} EUR")
        lines.append(
            f"  (No declarar en el bloque 1624-1654 — ajustar en transmisión de acciones, casillas 326-338)")
    else:
        lines += ["", "  (ninguna opción ejercida en este ejercicio)"]

    # ── SECCIÓN 3: Long abiertas al 31/12 (coste diferido) ───────────────
    lines += [
        "",
        SEP2,
        "  SECCIÓN 3 — OPCIONES LARGAS ABIERTAS AL 31/12  (coste diferido)",
        "  Prima pagada NO deducible en este ejercicio — diferir al año de cierre.",
        SEP2,
    ]
    if long_abiertas:
        for d in long_abiertas:
            blk = bloque_opcion(d, '  ⏳ ABIERTA')
            lines += blk + [
                f"  → Coste diferido: {fmt_es(d['primas_pagadas'])} EUR",
                f"     (declarar en el ejercicio en que se cierre, expire o ejerza)",
            ]
        coste_diferido = totales['long_abiertas_coste']
        lines += [
            "",
            f"  Coste total diferido: {fmt_es(coste_diferido)} EUR",
            f"  (NO incluir en casillas 1624-1654 de {EJERCICIO} (otros elementos patrimoniales))",
        ]
    else:
        lines += ["", "  (ninguna opción larga abierta al 31/12)"]

    # ── SECCIÓN 4: Short abiertas al 31/12 (prima diferida) ──────────────
    lines += [
        "",
        SEP2,
        "  SECCIÓN 4 — OPCIONES CORTAS ABIERTAS AL 31/12  (prima diferida)",
        "  Prima cobrada NO tributa en este ejercicio — diferir al año de extinción.",
        "  Fundamento: Art. 14.1.c LIRPF · DGT V2172-21 (30/07/2021).",
        "  La alteración patrimonial se produce al cerrar/expirar/ejercerse la opción.",
        SEP2,
    ]
    if short_abiertas or roll_abiertas:
        for d in short_abiertas:
            blk = bloque_opcion(d, '  ⏳ ABIERTA')
            lines += blk + [
                f"  → Prima diferida: {fmt_es(d['primas_cobradas'])} EUR",
                f"     (declarar en {int(EJERCICIO)+1} cuando se cierre, expire o ejerza)",
            ]
        for d in roll_abiertas:
            # Porción ABIERTA del roll: los contratos re-vendidos y aún abiertos al 31/12
            tipo_str   = {'C': 'CALL', 'P': 'PUT'}.get(d['tipo_op'], d['tipo_op'])
            n_abiertos = d['n_net_abiertos']
            prima_ab   = d['_prima_abierta_r']
            gastos_ab  = d['_gastos_abierta_r']
            lines += [
                "",
                SEP,
                f"  {d['subyacente']}  {tipo_str}  Strike {d['strike']}  Venc. {d['vencimiento']}  ⏳ ROLL (porción re-vendida abierta)",
                SEP,
                f"  Broker                  : {d['brokers']}",
                f"  Contratos re-vendidos abiertos: {n_abiertos}   (porción cerrada → Sección 1)",
                f"  Primas cobradas (re-venta)    : {fmt_es(prima_ab)} EUR",
                f"  Gastos (comisiones re-venta)  : {fmt_es(gastos_ab)} EUR",
                f"  → Prima diferida: {fmt_es(prima_ab)} EUR",
                f"     (declarar en {int(EJERCICIO)+1} cuando se cierre, expire o ejerza)",
            ]
        prima_diferida_short = totales['short_abiertas_prima']

        # Desglose por broker para diferidas
        pb_dif = defaultdict(Decimal)
        for d in short_abiertas:
            pb_dif[d['brokers']] += d['primas_cobradas']
        for d in roll_abiertas:
            pb_dif[d['brokers']] += d['_prima_abierta_r']
        dif_broker_lines = []
        if pb_dif:
            dif_broker_lines += ["", "  DESGLOSE POR BROKER (diferidas):"]
            for b, prima in sorted(pb_dif.items()):
                dif_broker_lines.append(f"  · {b:<12}: {fmt_es(prima):>9} EUR")

        lines += [
            "",
            f"  Prima total diferida (shorts abiertos): {fmt_es(prima_diferida_short)} EUR",
            f"  (NO incluir en casillas 1624-1654 de {EJERCICIO} (otros elementos patrimoniales))",
        ] + dif_broker_lines
    else:
        lines += ["", "  (ninguna opción corta abierta al 31/12)"]

    # ── SECCIÓN 5: Primas no localizadas en años anteriores ──────────────
    if no_encontradas:
        lines += [
            "",
            SEP2,
            "  SECCIÓN 5 — PRIMAS NO LOCALIZADAS EN AÑOS ANTERIORES  ⚠️ ACCIÓN MANUAL",
            "  Se detectaron opciones expiradas/ejercidas sin venta en el CSV del año actual.",
            f"  Se buscó en hasta {MAX_ANIOS_BUSQUEDA} años anteriores sin resultado.",
            "  Probable causa: venta en un año sin CSV disponible, o formato no reconocido.",
            "  ACCIÓN: localizar la prima original en extractos del broker y añadirla a",
            f"  casillas 1624-1654 de {EJERCICIO} (otros elementos patrimoniales) (Art. 14.1.c + 37.1.m LIRPF · DGT V2172-21).",
            SEP2,
        ]
        for item in no_encontradas:
            tipo_str = {'C': 'CALL', 'P': 'PUT'}.get(item.get('tipo_op', '?'), item.get('tipo_op', '?'))
            cierre   = "EJERCIDA" if item.get('es_ejercida') else "EXPIRADA"
            lines += [
                "",
                SEP,
                f"  {item.get('subyacente','')}  {tipo_str}  Strike {item.get('strike','')}  "
                f"Venc. {item.get('vencimiento','')}  [{cierre} en {EJERCICIO}]",
                SEP,
                f"  ISIN                    : {item.get('isin','')}",
                f"  Prima cobrada (origen)  : DESCONOCIDA — buscar en extracto del broker",
                f"  → Verificar y añadir manualmente a casillas 1624-1654 de {EJERCICIO} (otros elementos patrimoniales)",
            ]

    lines += ["", "FIN DEL INFORME"]

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    # --- Argparse para modo web (sobrescribe constantes de módulo) ---
    import argparse as _ap
    global BASE_DIR, EJERCICIO, DEGIRO_FILE, DEGIRO_CUENTA_FILE, IBKR_FILE, TR_FILE
    global OUTPUT_FILE, INFORME_FILE, INFORME_DIV_FILE, INFORME_OPT_FILE, INFORME_FX_FILE, DERECHOS_FILE
    _p = _ap.ArgumentParser(add_help=False)
    _p.add_argument('--base-path', default=None)
    _p.add_argument('--ejercicio', default=None)
    _p.add_argument('--no-interactive', action='store_true')
    _p.add_argument('--gp-override', default=None,
                    help='G/P acciones/ETFs neta real (sustituye al FIFO estimado)')
    _p.add_argument('--guardar-compensacion', action='store_true',
                    help='Actualizar perdidas_pendientes.json con el nuevo saldo')
    _args, _ = _p.parse_known_args()
    if _args.base_path: BASE_DIR = _args.base_path
    if _args.ejercicio: EJERCICIO = _args.ejercicio
    # Recalcular rutas derivadas si se sobrescriben base o ejercicio
    if _args.base_path or _args.ejercicio:
        DEGIRO_FILE        = os.path.join(BASE_DIR, f"DeGiro_Transacciones_{EJERCICIO}.csv")
        DEGIRO_CUENTA_FILE = os.path.join(BASE_DIR, f"DeGiro_Cuenta_{EJERCICIO}.csv")
        IBKR_FILE          = os.path.join(BASE_DIR, f"IBKR_Trades_{EJERCICIO}.csv")
        TR_FILE            = os.path.join(BASE_DIR, f"TR_Transacciones_{EJERCICIO}.csv")
        OUTPUT_FILE        = os.path.join(BASE_DIR, f"cartera_valores_irpf_{EJERCICIO}.csv")
        INFORME_FILE       = os.path.join(BASE_DIR, f"informe_corporativas_{EJERCICIO}.txt")
        INFORME_DIV_FILE   = os.path.join(BASE_DIR, f"informe_dividendos_{EJERCICIO}.txt")
        INFORME_OPT_FILE   = os.path.join(BASE_DIR, f"informe_opciones_{EJERCICIO}.txt")
        INFORME_FX_FILE    = os.path.join(BASE_DIR, f"informe_fx_{EJERCICIO}.txt")
        DERECHOS_FILE      = os.path.join(BASE_DIR, "derechos_clasificados.json")
    # -----------------------------------------------------------------

    print()
    print("=" * 65)
    print("  GENERADOR IRPF — Mi cartera de valores (RentaWEB AEAT)")
    print(f"  Ejercicio fiscal: {EJERCICIO}")
    print("=" * 65)

    # ── Paso 0: Verificar documentos ──────────────────────────────────────
    print(f"\n  Verificando documentos en {BASE_DIR} ...\n")
    brokers_cfg = [
        ('DeGiro transac.',  DEGIRO_FILE),
        ('DeGiro cuenta',    DEGIRO_CUENTA_FILE),
        ('IBKR',             IBKR_FILE),
        ('Trade Republic',   TR_FILE),
    ]
    ausentes = []
    for broker, path in brokers_cfg:
        if os.path.exists(path):
            size_kb = os.path.getsize(path) / 1024
            print(f"  ✅ {broker:<22} {os.path.basename(path)}  ({size_kb:.1f} KB)")
        else:
            print(f"  ❌ {broker:<22} {os.path.basename(path)}  — NO encontrado")
            ausentes.append(broker)

    if ausentes:
        print(f"\n  ⚠️  Sin fichero: {', '.join(ausentes)}.")
        print(f"       Nota: 'DeGiro cuenta' es necesario para dividendos (extracto de cuenta).")
        resp = 'S' if _args.no_interactive else input("\n  ¿Continuar sin ellos? [S/n]: ").strip().upper()
        if resp not in ('S', ''):
            print("\n  Operación cancelada.")
            sys.exit(0)

    # ── Paso 1: Parsear brokers ────────────────────────────────────────────
    print(f"\n  Parseando transacciones y acciones corporativas...\n")
    todas_ops    = []
    todas_sp     = []
    todos_corp   = []   # para el informe
    todos_divs   = []   # para informe dividendos
    todas_opts   = []   # para informe opciones
    todos_futuros = []   # para informe futuros IBKR (parse_ibkr_futures)
    degiro_raw_rows = []  # filas crudas para parser de opciones
    todos_corp_posibles_liberadas = []  # candidatos a acciones liberadas scrip (DeGiro)
    todas_no_soportadas = []  # categoría no soportada (cripto IBKR, derivados DeGiro/IBKR, bonds, etc.)
    bond_data_dg = None  # cupones de bonos DeGiro — se construye con DEGIRO_FILE
                         # o, si falta, en la sección del extracto de cuenta (F8)

    if os.path.exists(DEGIRO_FILE):
        # Tasas externas (ITF, Stamp Duty, FTT) viven en el CSV de Cuenta —
        # las pre-cargamos para que parse_degiro pueda sumarlas al gasto del
        # trade correspondiente (Art. 35.1.b LIRPF).
        external_fees_dg = _build_degiro_external_fees(DEGIRO_CUENTA_FILE)
        bond_data_dg = _build_degiro_bond_data(DEGIRO_CUENTA_FILE)
        cambios_producto_dg = _build_degiro_cambios_producto(DEGIRO_CUENTA_FILE)
        cambios_isin_dg     = _build_degiro_cambios_isin(DEGIRO_CUENTA_FILE)
        ops, sp_ops, desc, corp = parse_degiro(
            DEGIRO_FILE,
            external_fees_by_order=external_fees_dg,
            bond_data=bond_data_dg,
            cambios_producto=cambios_producto_dg,
            cambios_isin=cambios_isin_dg,
        )
        if bond_data_dg.get('isins'):
            print(f"    Bonos detectados (Cupón corrido): {len(bond_data_dg['isins'])}  → casillas 0027/0031")
        # Recoger raw_rows para el parser de opciones
        with open(DEGIRO_FILE, encoding='utf-8') as _f:
            _r = csv.reader(_f)
            next(_r, None)
            degiro_raw_rows = [row for row in _r if len(row) >= 15]
        print(f"  DeGiro:")
        print(f"    A/T incluidas            : {len(ops)}")
        print(f"    SP (splits/contrasplits) : {len(sp_ops)}")
        print(f"    Descartadas (opciones)   : {desc['opcion']}")
        print(f"    Descartadas (corporativas): {desc['corporativa'] + desc['precio_cero']}")
        if external_fees_dg:
            _total_ext = sum(
                (v['total'] for v in external_fees_dg.values()),
                Decimal('0'),
            )
            print(f"    Tasas externas detectadas: {len(external_fees_dg)} ordenes "
                  f"({_total_ext:.2f} EUR sumados al coste)")
        n_isin = len(corp.get('isin_chgs', []))
        n_rts  = len(corp.get('derechos', []))
        n_cplx = len(corp.get('complejos', []))
        n_plib = len(corp.get('posibles_liberadas', []))
        # name_changes incluye tanto cambios de nombre puros como spin-offs;
        # los distinguimos por tipo_ca para el log y para la materialización.
        all_name_evs  = corp.get('name_changes', [])
        spin_offs_dg  = [e for e in all_name_evs if e.get('tipo_ca') == CA_SPIN_OFF]
        name_chgs_dg  = [e for e in all_name_evs if e.get('tipo_ca') == CA_NAME_CHANGE]
        n_namc = len(name_chgs_dg)
        n_spin = len(spin_offs_dg)
        if n_isin:
            print(f"    Cambios ISIN detectados  : {n_isin}  (sin entrada fiscal)")
        if n_rts:
            print(f"    ⚠️  Derechos (RTS)         : {n_rts}  → acción manual requerida")
        if n_cplx:
            print(f"    ⚠️  Eventos complejos      : {n_cplx}  → revisión manual")
        if n_plib:
            print(f"    ℹ️  Liberadas candidatas   : {n_plib}  → se cruzarán con scrip TYPE B")
        if n_namc:
            print(f"    ℹ️  Cambios de nombre      : {n_namc}  (mismo ISIN, informativo)")
        if n_spin:
            print(f"    ⚠️  Escisiones (spin-off)  : {n_spin}  → prorratear coste manualmente")
        todas_ops.extend(ops)
        todas_sp.extend(sp_ops)
        todos_corp.extend(corp.get('splits', []) + corp.get('isin_chgs', []) +
                          corp.get('derechos', []) + corp.get('complejos', []) +
                          corp.get('name_changes', []) +
                          corp.get('rights_exercised', []) +
                          corp.get('market_transfers', []))
        todos_corp_posibles_liberadas.extend(corp.get('posibles_liberadas', []))
        todas_no_soportadas.extend(desc.get('categoria_no_soportada', []) or [])
        n_rxe = len(corp.get('rights_exercised', []))
        if n_rxe:
            print(f"    ✅ Rights issue ejercidos : {n_rxe}  → procesados automáticamente")
        n_mt = len(corp.get('market_transfers', []))
        if n_mt:
            print(f"    ✅ Cambios de mercado     : {n_mt}  → excluidos del FIFO (Art. 33 LIRPF)")
        n_nosop_dg = len(desc.get('categoria_no_soportada', []) or [])
        if n_nosop_dg:
            print(f"    ⚠️  Derivados estructurados: {n_nosop_dg}  → declarar manualmente en 1624-1654 clave 4")

    if os.path.exists(IBKR_FILE):
        # Pre-cargar tipos BCE: parse_ibkr convierte trades en divisa local
        # (USD/AED/DKK/GBP) a EUR vía BCE — necesita el cache poblado.
        # fetch_ecb_rates es idempotente, no descarga si ya está en disco.
        print(f"  Descargando tipos de cambio BCE para {EJERCICIO} (IBKR)...")
        fetch_ecb_rates({'USD', 'GBP', 'DKK', 'HKD', 'CHF', 'PLN', 'AED'}, EJERCICIO)
        ops, sp_ops, desc, corp = parse_ibkr(IBKR_FILE)
        print(f"  IBKR:")
        print(f"    A/T incluidas            : {len(ops)}")
        print(f"    SP (splits/contrasplits) : {len(sp_ops)}")
        print(f"    Descartadas (opciones)   : {desc['opcion']}")
        if desc['divisa_no_eur'] > 0:
            print(f"    ⚠️  En divisa no EUR       : {desc['divisa_no_eur']}  ← re-exportar con base EUR")
        todas_ops.extend(ops)
        todas_sp.extend(sp_ops)
        todos_corp.extend(corp.get('complejos', []))
        todas_no_soportadas.extend(desc.get('categoria_no_soportada', []) or [])
        n_nosop_ib = len(desc.get('categoria_no_soportada', []) or [])
        if n_nosop_ib:
            cats_set = sorted({d.get('asset_category', '?') for d in desc['categoria_no_soportada']})
            print(f"    ⚠️  No soportadas (IBKR)   : {n_nosop_ib}  → declarar manualmente. "
                  f"Categorías: {', '.join(cats_set)}")
        ibkr_divs  = parse_ibkr_dividendos(IBKR_FILE)
        ibkr_opts, ibkr_opts_desc  = parse_ibkr_opciones(IBKR_FILE)
        ibkr_futs, ibkr_futs_desc  = parse_ibkr_futures(IBKR_FILE)
        ibkr_fx_pl = parse_ibkr_fx_pl(IBKR_FILE)
        ibkr_interest = parse_ibkr_interest(IBKR_FILE)
        todos_divs.extend(ibkr_divs)
        todas_opts.extend(ibkr_opts)
        todos_futuros.extend(ibkr_futs)
        if ibkr_divs:
            print(f"    Dividendos/retenciones   : {len(ibkr_divs)}")
        if ibkr_opts:
            print(f"    Opciones                 : {len(ibkr_opts)}")
        if ibkr_futs:
            print(f"    Futuros (cierres)        : {len(ibkr_futs)}")
        # Descartes estructurados (uso embebido en backend): si aparecen
        # filas sin FX BCE, los printeamos como resumen — además del aviso
        # individual ya emitido al detectarse.
        if ibkr_opts_desc.get('sin_fx'):
            print(f"    ⚠️  Opciones descartadas sin FX: "
                  f"{len(ibkr_opts_desc['sin_fx'])}")
        if ibkr_futs_desc.get('sin_fx'):
            print(f"    ⚠️  Futuros descartados sin FX: "
                  f"{len(ibkr_futs_desc['sin_fx'])}")
        if ibkr_fx_pl['fx'] or ibkr_fx_pl['tbills']:
            print(f"    FX P&L (divisas)         : {len(ibkr_fx_pl['fx'])}")
            print(f"    Treasury Bills           : {len(ibkr_fx_pl['tbills'])}")
        if ibkr_interest:
            _credit_total = sum(r['importe_eur'] for r in ibkr_interest
                                if r['tipo'] in ('credit', 'bond_interest'))
            _debit_total  = sum(r['importe_eur'] for r in ibkr_interest
                                if r['tipo'] == 'debit')
            print(f"    Intereses (Interest)     : {len(ibkr_interest)} "
                  f"(credit/bond {fmt_es(_credit_total)} EUR -> casilla 0027, "
                  f"debit {fmt_es(_debit_total)} EUR informativo)")
        scrip_chains = corp.get('scrip_chains', [])
        if scrip_chains:
            print(f"    ✅ Scrip dividend mixtos : {len(scrip_chains)} cadena(s) sintetizadas")
            for c in scrip_chains:
                print(f"       · {c['nombre_matriz']} ({c['matriz_isin']}): "
                      f"+{c['qty_acciones']:.0f} acc. coste {fmt_es(c['coste_eur'])} EUR "
                      f"(derechos comprados consumidos)")
    else:
        ibkr_fx_pl = {'fx': [], 'tbills': []}
        ibkr_interest = []

    # ── Trade Republic ─────────────────────────────────────────────────────
    # TR Bank tiene sucursal en España desde mediados de 2025 y RETIENE IRPF
    # español al 19% sobre intereses y dividendos. El campo `tax` del CSV
    # viene con valor negativo cuando hay retención.
    tr_intereses = []
    tr_staking   = []
    if os.path.exists(TR_FILE):
        ops_raw, sp_ops_raw, desc = parse_tr(TR_FILE)
        # parse_tr devuelve TODAS las ops del CSV (TR exporta multi-año en
        # un solo fichero, distinto del patrón DeGiro/IBKR que es anual);
        # filtramos por el ejercicio fiscal en curso para que el XLSX target
        # no se contamine con ops futuras (savings plans recurrentes que
        # van llenando el CSV mes a mes). Las ops históricas necesarias
        # para FIFO multi-año se cargan desde los XLSX anteriores en el
        # pipeline `_generate_fifo_pdf(multi_anio=True)`.
        _ejercicio_str = f"/{EJERCICIO}"
        ops    = [o for o in ops_raw    if o.get('fecha', '').endswith(_ejercicio_str)]
        sp_ops = [s for s in sp_ops_raw if s.get('fecha', '').endswith(_ejercicio_str)]
        n_excluidas = (len(ops_raw) - len(ops)) + (len(sp_ops_raw) - len(sp_ops))
        print(f"  Trade Republic:")
        print(f"    A/T incluidas            : {len(ops)}"
              + (f"  (filtradas {n_excluidas} ops de otros años)" if n_excluidas else ""))
        if desc['tipo_no_operable'] > 0:
            print(f"    Otros tipos              : {desc['tipo_no_operable']}  "
                  f"(procesados por dividendos/intereses/staking)")
        if desc.get('corporate_action', 0) > 0:
            print(f"    Corporate actions        : {desc['corporate_action']}  "
                  f"(procesadas por scrip dividend handler)")
        if desc['migration'] > 0:
            print(f"    ℹ️  Migración custodio    : {desc['migration']}  "
                  f"(sin alteración patrimonial)")
        if desc['cash_movement'] > 0:
            print(f"    ℹ️  Movimientos cash      : {desc['cash_movement']}  "
                  f"(depósitos/transferencias propias)")
        if desc['tax_optimization'] > 0:
            print(f"    ⚠️  Tax Optimization (DE) : {desc['tax_optimization']}  "
                  f"→ ajuste fiscal alemán; NO aplica a residentes ES")
        if desc['sin_isin'] > 0:
            print(f"    ⚠️  Sin ISIN detectable   : {desc['sin_isin']}  → revisión manual")
        # Enriquecer con instrument_type ANTES de mezclar con el flujo global:
        # el TR CSV trae asset_class=STOCK / FUND / SYNTHETIC / CRYPTO, pero
        # excel_cartera y motor_fiscal leen op['instrument_type'] (clave del
        # esquema interno). Sin esta llamada, los ETFs UCITS comprados en TR
        # quedarían como STOCK por defecto y se enviarían a casillas 0326-0340
        # en vez de 2224-2236. DeGiro e IBKR ya hacen lo equivalente arriba
        # (líneas 2404 y 3226).
        _enrich_with_instrument_type(ops, broker='TR')
        _enrich_with_instrument_type(sp_ops, broker='TR')
        todas_ops.extend(ops)
        todas_sp.extend(sp_ops)

        # Splits/contrasplits que TR aplica silenciosamente sobre la posicion
        # (sin linea CSV — patron documentado en patrones_trade_republic.md).
        # Se inyectan desde catalogo estatico `splits_conocidos.json` solo
        # para ISINs presentes en el CSV con operaciones ANTERIORES al split,
        # evitando inflar cantidades cuando el usuario solo compro post-split.
        # Filtramos por ejercicio (igual que el resto de SP) — la cadena
        # multi-anio aplica el split en el ano correspondiente y los lotes
        # heredados ya viajan post-split a anos posteriores.
        tr_extra_sp_raw = parse_tr_splits(TR_FILE)
        tr_extra_sp = [s for s in tr_extra_sp_raw
                       if s.get('fecha', '').endswith(_ejercicio_str)]
        if tr_extra_sp_raw:
            print(f"    Splits catalogo (TR)     : {len(tr_extra_sp)}"
                  + (f"  (filtrados {len(tr_extra_sp_raw) - len(tr_extra_sp)} de otros anos)"
                     if len(tr_extra_sp_raw) > len(tr_extra_sp) else ""))
            for s in tr_extra_sp:
                print(f"       · {s['fecha']} {s['nombre']}: "
                      f"{int(s['cantidad'])}:{int(s['importe_eur'])}")
        _enrich_with_instrument_type(tr_extra_sp, broker='TR')
        todas_sp.extend(tr_extra_sp)

        # Scrip dividends (Art. 37.1.a §4 LIRPF + reforma Ley 26/2014).
        # Detecta clusters de eventos CORPORATE_ACTION y los clasifica como:
        #   - Opción A (canje por acciones) → fila A con coste 0 y es_scrip=True
        #   - Opción B (venta de derechos en mercado) → idem para que el FIFO
        #     cuadre con el SELL del SYNTHETIC ya procesado por parse_tr
        #   - Opción C (recompra por emisor) → dividendo casilla 0029 con
        #     retención si aplica (post-migración IBAN ES, BOE-A-2025-5909)
        # Devuelve todas las filas del CSV multi-año; se filtran por ejercicio.
        tr_corp_ops_raw, tr_corp_divs_raw, desc_corp = parse_tr_corporate_actions(TR_FILE)
        tr_corp_ops = [o for o in tr_corp_ops_raw
                       if o.get('fecha', '').endswith(_ejercicio_str)]
        tr_corp_divs = [d for d in tr_corp_divs_raw
                        if d.get('fecha', '').endswith(_ejercicio_str)]
        if desc_corp['ciclos_a'] + desc_corp['ciclos_b'] + desc_corp['ciclos_c'] > 0:
            print(f"    Scrip dividends detectados:")
            if desc_corp['ciclos_a'] > 0:
                print(f"       · Opción A (canje):      {desc_corp['ciclos_a']}  "
                      f"→ acciones con coste 0, prorrateo Art. 37.1.a §4 LIRPF")
            if desc_corp['ciclos_b'] > 0:
                print(f"       · Opción B (mercado):    {desc_corp['ciclos_b']}  "
                      f"→ ganancia patrimonial pura, coste 0")
            if desc_corp['ciclos_c'] > 0:
                print(f"       · Opción C (recompra):   {desc_corp['ciclos_c']}  "
                      f"→ RCM Art. 25.1.a LIRPF, casilla 0029")
        if desc_corp['ciclos_sin_match'] > 0:
            print(f"    ⚠️  Scrip sin matriz BUY/SELL: {desc_corp['ciclos_sin_match']}  "
                  f"→ revisión manual en informe corporativas")
        if desc_corp['ciclos_indeterminados'] > 0:
            print(f"    ⚠️  Scrip indeterminados     : {desc_corp['ciclos_indeterminados']}  "
                  f"→ ciclo a caballo entre años o evento huérfano")
        _enrich_with_instrument_type(tr_corp_ops, broker='TR')
        todas_ops.extend(tr_corp_ops)

        # Dividendos + retenciones IRPF español.
        # parse_tr_dividendos devuelve TODOS los años del CSV; filtramos por
        # el ejercicio fiscal en curso antes de imputar y de extender la
        # lista global de dividendos.
        tr_divs_raw = parse_tr_dividendos(TR_FILE)
        tr_divs = [d for d in tr_divs_raw
                   if d.get('fecha', '').endswith(_ejercicio_str)]
        if tr_divs:
            n_div = sum(1 for d in tr_divs if d['tipo'] == 'DIV')
            n_ret = sum(1 for d in tr_divs if d['tipo'] == 'RET_ES')
            bruto = sum(d['importe_eur'] for d in tr_divs if d['tipo'] == 'DIV')
            ret   = sum(d['importe_eur'] for d in tr_divs if d['tipo'] == 'RET_ES')
            print(f"    Dividendos (DIV)         : {n_div}  bruto {fmt_es(bruto)} EUR → casilla 0029")
            if n_ret > 0:
                print(f"    Retención IRPF ES (div)  : {n_ret} pagos  → {fmt_es(ret)} EUR "
                      f"(TR sucursal ES retiene 19%)")
            todos_divs.extend(tr_divs)

        # Inyectar dividendos de scrip Opción C (recompra por emisor) en el
        # flujo global de dividendos. Se hace AQUÍ y no antes para que el
        # resumen impreso por parse_tr_dividendos no incluya estos eventos
        # (el log de scrip ya los ha desglosado más arriba).
        if tr_corp_divs:
            bruto_c = sum(d['importe_eur'] for d in tr_corp_divs if d['tipo'] == 'DIV')
            ret_c   = sum(d['importe_eur'] for d in tr_corp_divs
                          if d['tipo'] == 'RET' and d.get('pais') == 'ES')
            print(f"    Scrip Opción C → RCM     : bruto {fmt_es(bruto_c)} EUR"
                  + (f", retención ES {fmt_es(ret_c)} EUR" if ret_c > 0 else "")
                  + " → casilla 0029")
            todos_divs.extend(tr_corp_divs)

        # Intereses cuenta remunerada — RCM Art. 25.2 LIRPF → casilla 0027.
        # parse_tr_intereses devuelve TODOS los años del CSV; filtramos por
        # el ejercicio fiscal en curso antes de imputar y de inyectar en la
        # lista global de intereses RCM.
        tr_intereses_raw = parse_tr_intereses(TR_FILE)
        tr_intereses = [it for it in tr_intereses_raw
                        if it.get('fecha', '').endswith(_ejercicio_str)]
        if tr_intereses:
            bruto_i = sum(i['importe_eur'] for i in tr_intereses)
            ret_i   = sum(i['retencion_es_eur'] for i in tr_intereses)
            print(f"    Intereses cuenta rem.    : {len(tr_intereses)} pagos  "
                  f"bruto {fmt_es(bruto_i)} EUR → casilla 0027")
            if ret_i > 0:
                print(f"    Retención IRPF ES (int)  : {fmt_es(ret_i)} EUR")

            # Inyectar en ibkr_interest (variable conservada por nombre
            # histórico — ahora contiene todos los intereses RCM Art. 25.2
            # LIRPF, no solo IBKR) con el mismo shape que parse_ibkr_interest.
            for it in tr_intereses:
                ibkr_interest.append({
                    'fecha':         it['fecha'],
                    'divisa':        'EUR',
                    'importe_local': it['importe_eur'],
                    'importe_eur':   it['importe_eur'],
                    'descripcion':   it.get('fuente', 'Cuenta remunerada Trade Republic'),
                    'tipo':          'credit',
                    'casilla':       '0027',
                    'broker':        'TR',
                    'retencion_es_eur': it.get('retencion_es_eur', Decimal('0')),
                })

        # Staking rewards (CRYPTO) — RCM Art. 25.2 LIRPF, DGT V1766-22 (26-7-2022)
        # parse_tr_staking también devuelve TODOS los años; filtrar por ejercicio.
        tr_staking_raw = parse_tr_staking(TR_FILE)
        tr_staking = [s for s in tr_staking_raw
                      if s.get('fecha', '').endswith(_ejercicio_str)]
        if tr_staking:
            bruto_s = sum(s['importe_eur'] for s in tr_staking)
            assets  = sorted({s['asset'] for s in tr_staking})
            print(f"    Staking rewards (CRYPTO) : {len(tr_staking)} eventos  "
                  f"valor {fmt_es(bruto_s)} EUR → casilla 0027 RCM staking")
            print(f"       · Doctrina: DGT V1766-22 — RCM Art. 25.2 LIRPF, valorado en")
            print(f"         EUR al momento de cada recepción (Art. 43.1 LIRPF, en especie).")
            print(f"       · Alternativa doctrinal: casilla 0031 (cuota idéntica en base ahorro).")

            # CL7 auditoría 2026-06-11: cada reward genera además un LOTE de
            # adquisición para el FIFO (coste = valor RCM de recepción). Sin
            # él, la venta posterior del cripto salía huérfana (coste 0) y el
            # valor de recepción tributaba dos veces. Mismo patrón de filtrado
            # por ejercicio que el resto de eventos TR: los lotes viajan a
            # años posteriores vía la cadena de XLSXs (parse_csv_irpf).
            staking_ops, staking_avisos = staking_a_lotes(tr_staking)
            for _av in staking_avisos:
                print(f"       ⚠️  {_av}")
            if staking_ops:
                todas_ops.extend(staking_ops)
                print(f"       · Lotes de adquisición creados: {len(staking_ops)} "
                      f"(coste = valor de recepción; la venta futura tributará "
                      f"solo por la plusvalía desde ese valor)")
            print(f"       · Activos: {', '.join(assets)}")

    # ── Dividendos DeGiro (extracto de cuenta) ─────────────────────────────
    ejercidas_isin_dg = set()
    gastos_plataforma_dg = []
    if os.path.exists(DEGIRO_CUENTA_FILE):
        # Pre-cargar tipos de cambio BCE para todas las divisas habituales
        print(f"  Descargando tipos de cambio BCE para {EJERCICIO}...")
        fetch_ecb_rates({'USD', 'GBP', 'DKK', 'HKD', 'CHF', 'PLN', 'AED'}, EJERCICIO)
        dg_divs, ejercidas_isin_dg, gastos_plataforma_dg = parse_degiro_cuenta(DEGIRO_CUENTA_FILE)
        todos_divs.extend(dg_divs)
        n_div = sum(1 for d in dg_divs if d['tipo'] == 'DIV')
        n_ret = sum(1 for d in dg_divs if d['tipo'] == 'RET')
        print(f"  DeGiro cuenta:")
        print(f"    Dividendos               : {n_div}")
        print(f"    Retenciones en origen    : {n_ret}")
        if ejercidas_isin_dg:
            print(f"    Opciones ejercidas       : {len(ejercidas_isin_dg)} ISINs detectados")
        if gastos_plataforma_dg:
            total_plat = sum(g['importe_eur'] for g in gastos_plataforma_dg)
            print(f"    Comisiones conectividad  : {len(gastos_plataforma_dg)} cargos = {total_plat:.2f} EUR")

        # ── Cupones periódicos de bonos DeGiro → RCM casilla 0027 (F8) ────
        # Se recolectaban en bond_data pero NUNCA se inyectaban en la lista
        # de intereses RCM → omitidos en informe/XLSX/PDF/sidecar.
        if bond_data_dg is None:
            bond_data_dg = _build_degiro_bond_data(DEGIRO_CUENTA_FILE)
        cupones_entries, cupones_avisos = cupones_bonos_a_intereses(
            bond_data_dg, EJERCICIO)
        for _aviso in cupones_avisos:
            print(f"  ⚠️  {_aviso}")
        if cupones_entries:
            ibkr_interest.extend(cupones_entries)
            bruto_cup = sum((e['importe_eur'] for e in cupones_entries),
                            Decimal('0'))
            print(f"    Cupones de bonos         : {len(cupones_entries)} pagos  "
                  f"bruto {fmt_es(bruto_cup)} EUR → casilla 0027 RCM")

    # ── Opciones DeGiro ────────────────────────────────────────────────────
    opts_anios_anteriores_no_encontradas = []  # para el informe
    if degiro_raw_rows:
        dg_opts = parse_opciones_degiro(degiro_raw_rows, ejercidas_isin=ejercidas_isin_dg)
        todas_opts.extend(dg_opts)
        if dg_opts:
            print(f"  DeGiro opciones          : {len(dg_opts)} operaciones detectadas")

        # ── Buscar primas de años anteriores (DGT V2172-21) ───────────────
        # Una opción expirada/ejercida en el año actual sin venta en el CSV actual
        # tuvo su prima cobrada en un año anterior → la prima tributa en este año
        # (la alteración patrimonial ocurre en el año de extinción del contrato).
        isins_con_venta = {op['isin'] for op in dg_opts
                           if op['accion'] == 'venta'
                           and not op['expirada'] and not op['ejercida']}
        orphan_info = {}
        for op in dg_opts:
            isin = op['isin']
            if isin and isin not in isins_con_venta and (op['expirada'] or op['ejercida']):
                if isin not in orphan_info:
                    orphan_info[isin] = {
                        'isin': isin, 'simbolo': op['simbolo'],
                        'subyacente': op['subyacente'], 'tipo_op': op['tipo_op'],
                        'strike': op['strike'], 'vencimiento': op['vencimiento'],
                        'es_ejercida': op['ejercida'],
                    }

        if orphan_info:
            print(f"  ⚠️  Opciones sin venta en {EJERCICIO}: {len(orphan_info)} — "
                  f"buscando prima en años anteriores...")
            recuperadas, opts_anios_anteriores_no_encontradas = \
                buscar_primas_anios_anteriores(orphan_info, BASE_DIR, EJERCICIO)

            for isin, data in recuperadas.items():
                # broker='DeGiro' para que agrupe con la expiración/ejercicio del año actual.
                # La nota del año de origen se guarda en 'nota_anio_anterior' y se mostrará
                # en el informe al detectar que la prima viene de un CSV anterior.
                todas_opts.append({
                    'fecha':              data['fecha'],
                    'simbolo':            data['simbolo'],
                    'isin':               data['isin'],
                    'tipo_op':            data['tipo_op'],
                    'subyacente':         data['subyacente'],
                    'strike':             data['strike'],
                    'vencimiento':        data['vencimiento'],
                    'accion':             'venta',
                    'cantidad':           Decimal('1'),
                    'prima_unitaria':     data['importe_eur'],
                    'importe_eur':        data['importe_eur'],
                    'gastos_eur':         data['gastos_eur'],
                    'expirada':           False,
                    'ejercida':           False,
                    'broker':             'DeGiro',
                    'nota_anio_anterior': data['anio_csv'],
                })
                print(f"     ✅ {data['simbolo']} [{data['anio_csv']}]: "
                      f"prima {fmt_es(data['importe_eur'])} EUR recuperada → casillas 1624-1654/{EJERCICIO}")

            if opts_anios_anteriores_no_encontradas:
                for item in opts_anios_anteriores_no_encontradas:
                    print(f"     ❓ {item['simbolo']}: prima no encontrada en CSVs anteriores "
                          f"→ verificar manualmente")

    # Solo abortamos si NO hay nada en absoluto que reportar: ni A/T, ni
    # corporativas (SP), ni RCM (dividendos / intereses / staking). Un año
    # con solo dividendos pero sin compras/ventas — caso típico de target+1
    # cuando se ha cerrado la cartera o cuando TR sigue emitiendo eventos
    # post-2025 sin operaciones nuevas — debe generar igualmente el XLSX
    # con las hojas Dividendos / Intereses pobladas y la hoja Operaciones
    # vacía, para que el usuario tenga todos los importes a la vista.
    if (not todas_ops and not todas_sp and not todos_divs
            and not ibkr_interest and not tr_staking):
        print("\n  No se encontraron operaciones ni rendimientos. Revisa los ficheros de entrada.")
        sys.exit(1)

    # ── Paso 2: Ordenar ────────────────────────────────────────────────────
    def sort_key(op):
        try:
            return datetime.strptime(op['fecha'], '%d/%m/%Y')
        except Exception:
            return datetime.min

    todas_ops.sort(key=sort_key)
    todas_sp.sort(key=sort_key)

    # ── Paso 2b: Clasificar derechos (scrip dividend vs. ampliación real) ──
    # Lee derechos_clasificados.json si existe y marca cada venta de derechos.
    #
    # DOCTRINA FISCAL (Art. 37.1.a LIRPF + DGT V2312-18, V0078-21):
    #
    #   TYPE A (ampliación real con desembolso):
    #     · Venta en mercado → ganancia/pérdida patrimonial → casillas 341-346 (derechos)
    #
    #   TYPE B (asignación gratuita / scrip dividend):
    #     · Venta en mercado ABIERTO (a tercero, con Order ID en broker) →
    #       GANANCIA/PÉRDIDA PATRIMONIAL → Art. 37.1.a LIRPF → casillas 341-346
    #       (se queda en el CSV como T; sí compensa con minusvalías de cartera)
    #     · Empresa RECOMPRA al "precio comprometido" (sin Order ID, acción
    #       corporativa automática) → RENDIMIENTO CAPITAL MOBILIARIO →
    #       Art. 25.1.a LIRPF → casilla 0029 (como dividendo; solo compensa
    #       RCM negativos o hasta 25% de G/P)
    #     · Recepción de acciones liberadas → sin renta; coste adquisición = 0
    #
    # En la práctica, TODAS las ventas que aparecen en todas_ops tienen Order ID
    # (son órdenes de mercado ejecutadas por el inversor) → SIEMPRE G/P casillas 341-346.
    # La recompra al precio comprometido aparece como acción corporativa sin Order ID
    # y no llega a todas_ops; si el broker la registra así, se documenta en el
    # informe_corporativas como "precio comprometido → casilla 0029 manualmente".

    derechos_clasificados: dict = {}
    if os.path.exists(DERECHOS_FILE):
        with open(DERECHOS_FILE, encoding='utf-8') as _f:
            derechos_clasificados = json.load(_f)

    # Subyacentes en cartera (por nombre base, A o T previos NO-RTS): permite
    # auto-detectar scrip dividend sin requerir clasificación manual. Un RTS
    # del mismo base que una acción en cartera es por definición una asignación
    # gratuita (TYPE B) — si fuera ampliación real (TYPE A) habría una compra
    # previa del propio ISIN del derecho.
    #
    # Se escanean también ficheros de años previos/posteriores en BASE_DIR,
    # porque generar_irpf.py se ejecuta año a año (el subyacente puede haberse
    # comprado en un ejercicio anterior al que se esté procesando).
    subyacentes_en_cartera = {
        base_company_name(op['nombre']).upper()
        for op in todas_ops
        if not is_rts(op['nombre']) and base_company_name(op['nombre'])
    }
    import glob as _glob
    # Patrón amplio: incluye DeGiro_Transacciones.csv (unificado multi-año),
    # DeGiro_Transacciones_YYYY.csv (por año) y DeGiro_Transacciones_partN.csv
    # (multi-upload). El subyacente puede venir de cualquiera de ellos.
    for _extra in _glob.glob(os.path.join(BASE_DIR, "DeGiro_Transacciones*.csv")):
        if _extra == DEGIRO_FILE:
            continue
        try:
            with open(_extra, encoding='utf-8') as _fh:
                _rd = csv.reader(_fh)
                next(_rd, None)
                for _row in _rd:
                    if len(_row) < 4:
                        continue
                    _nom = (_row[2] or '').strip()
                    if _nom and not is_rts(_nom):
                        _bc = base_company_name(_nom).upper()
                        if _bc:
                            subyacentes_en_cartera.add(_bc)
        except (OSError, StopIteration):
            continue

    def _es_subyacente_en_cartera(nombre_rts: str) -> bool:
        """Comprueba si el RTS corresponde a un subyacente en cartera.

        DeGiro a veces trunca el nombre del derecho respecto al subyacente
        ('ACS ACTIVIDADES DE CONST-RTS' vs 'ACS ACTIVIDADES DE CONSTRUCCION Y
        SERVICIOS SA'), por lo que se compara por prefijo bidireccional con
        al menos 4 caracteres de solape.
        """
        base = base_company_name(nombre_rts).upper()
        if not base or len(base) < 4:
            return False
        for u in subyacentes_en_cartera:
            if not u:
                continue
            if u == base or u.startswith(base) or base.startswith(u):
                return True
        return False

    # Auto-clasificar RIGHTS events (asignaciones detectadas via raw_rows)
    # cuando el subyacente está/estuvo en cartera. Esto permite que el cruce
    # con acciones liberadas funcione incluso si el derecho no se vendió
    # (ejercicio total a acción). Sin esto, los RIGHTS events no clasificados
    # quedan fuera de `derechos_events_b` y la liberada nunca se genera.
    for ev in todos_corp:
        if ev.get('tipo_ca') != CA_RIGHTS:
            continue
        isin_d = ev.get('isin', '')
        if isin_d in derechos_clasificados:
            continue
        nombre_ev = ev.get('nombre', '')
        if nombre_ev and _es_subyacente_en_cartera(nombre_ev):
            derechos_clasificados[isin_d] = {
                'tipo': 'B',
                'emisor': nombre_ev.upper(),
                'descripcion': 'Auto-detectado: asignación TYPE B '
                               '(subyacente en cartera, scrip dividend)',
                'auto': True,
            }

    # Marcar ventas de derechos pero MANTENERLAS en el CSV (G/P patrimonial)
    # Todas las ventas de derechos (TYPE A, B y sin clasificar) llevan flag
    # `_es_venta_derecho=True` → se emiten con código VD en el CSV (ver Paso 4).
    derechos_ventas_b_mercado = []  # TYPE B vendidos en mercado → G/P casillas 341-346 (en CSV)
    derechos_ventas_auto      = []  # TYPE B auto-detectados (sin clasificar)
    derechos_ventas_warn      = []  # sin clasificar y sin subyacente → revisión manual
    for op in todas_ops:
        if is_rts(op['nombre']) and op['tipo'] == 'T':   # solo ventas
            op['_es_venta_derecho'] = True
            isin_d = op['isin']
            info   = derechos_clasificados.get(isin_d)
            if info and info.get('tipo') == 'B':
                op['clasificacion'] = info
                op['_tipo_b_mercado'] = True   # marcador para informe
                derechos_ventas_b_mercado.append(op)
                # NO se excluye del CSV: venta en mercado = G/P patrimonial
            elif not info:
                if _es_subyacente_en_cartera(op['nombre']):
                    # Auto-clasificación: usuario tiene/tuvo el subyacente →
                    # el derecho es asignación gratuita TYPE B.
                    auto_info = {
                        'tipo': 'B',
                        'emisor': base_company_name(op['nombre']).upper(),
                        'descripcion': 'Auto-detectado: derecho TYPE B '
                                       '(subyacente en cartera, asignación gratuita)',
                        'auto': True,
                    }
                    op['clasificacion'] = auto_info
                    op['_tipo_b_mercado'] = True
                    derechos_clasificados[isin_d] = auto_info
                    derechos_ventas_b_mercado.append(op)
                    derechos_ventas_auto.append(op)
                else:
                    derechos_ventas_warn.append(op)

    if derechos_ventas_b_mercado:
        total_b = sum(op['importe_eur'] for op in derechos_ventas_b_mercado)
        print(f"  ✅ Derechos TIPO B en mercado: {len(derechos_ventas_b_mercado)} venta(s) "
              f"→ CSV como T (G/P patrimonial derechos, casillas 341-346) = {fmt_es(total_b)} EUR")
    if derechos_ventas_auto:
        isins_auto = ', '.join(op['isin'] for op in derechos_ventas_auto)
        print(f"  🤖 Auto-detectados como TYPE B (subyacente en cartera): "
              f"{len(derechos_ventas_auto)} venta(s) — {isins_auto}")
    if derechos_ventas_warn:
        isins_warn = ', '.join(op['isin'] for op in derechos_ventas_warn)
        print(f"  ⚠️  Derechos SIN CLASIFICAR: {len(derechos_ventas_warn)} venta(s) → en CSV como T (verificar casilla)")
        print(f"     ISINs: {isins_warn}")
        print(f"     → Ejecuta /irpf classify para investigar y actualizar derechos_clasificados.json")

    # ── Paso 2c: Acciones liberadas de scrip dividend TYPE B ───────────────
    # Los brokers registran las acciones liberadas como entradas de precio=0
    # sin Order ID. detect_corporate_actions_degiro las recoge como
    # 'posibles_liberadas'. Aquí las cruzamos con los RIGHTS events TYPE B
    # para generar filas A;ISIN;...;0,00;0,00 en el CSV.
    #
    # Criterio de emparejamiento: mismo nombre_base (normalizado) que el evento
    # RIGHTS, y fecha dentro de una ventana de 45 días desde la asignación.
    #
    # Art. 37.1.a §4 LIRPF: las acciones liberadas heredan el coste prorrateado
    # sobre la posición previa del subyacente. La fecha de adquisición legal es la
    # de las acciones originales de las que proceden (FIFO más antigua), pero el
    # CSV emite la fecha real de entrega por el broker (RentaWEB ajusta FIFO).
    # En ejercicios MIXTOS (asignados + derechos comprados en mercado), el coste
    # de los derechos comprados se transfiere a la acción liberada.

    liberadas_scrip = []   # A rows con coste 0 para acciones liberadas de scrip B

    # Recoger RIGHTS events clasificados como TYPE B
    derechos_events_b = [
        ev for ev in todos_corp
        if ev.get('tipo_ca') == CA_RIGHTS
        and derechos_clasificados.get(ev.get('isin', ''), {}).get('tipo') == 'B'
    ]

    def _nombres_compatibles(n1: str, n2: str, min_tokens: int = 3) -> bool:
        """Compara dos nombres de empresa con coincidencia flexible por tokens iniciales.
        Necesario porque DeGiro abrevia los nombres en distintos contextos
        (ej: 'ACS ACTIVIDADES DE CONST' vs 'ACS ACTIVIDADES DE CONSTRUCCION Y SERVICIOS SA').
        """
        t1 = n1.upper().split()
        t2 = n2.upper().split()
        shared = min(len(t1), len(t2), min_tokens)
        if shared == 0:
            return False
        return t1[:shared] == t2[:shared]

    if derechos_events_b:
        # posibles_liberadas viene de DeGiro (único broker que expone estas filas)
        degiro_posibles = todos_corp_posibles_liberadas

        used_plib_ids = set()
        for drecho_ev in derechos_events_b:
            nombre_drecho   = drecho_ev.get('nombre', '')
            fecha_drecho_dt = parse_date_dt(drecho_ev.get('fecha', ''))
            if not fecha_drecho_dt:
                continue
            isin_d = drecho_ev['isin']
            info   = derechos_clasificados.get(isin_d, {})

            for plib in degiro_posibles:
                if id(plib) in used_plib_ids:
                    continue
                fecha_lib_dt = plib.get('fecha_dt')
                if not fecha_lib_dt:
                    continue
                delta = abs(fecha_lib_dt - fecha_drecho_dt)
                if delta > timedelta(days=45):
                    continue
                nombre_base_lib = base_company_name(plib['nombre'])
                # Coincidencia flexible: 3 primeros tokens del nombre
                if not _nombres_compatibles(nombre_base_lib, nombre_drecho, min_tokens=3):
                    continue
                # Emparejado: es una acción liberada de scrip dividend TYPE B
                used_plib_ids.add(id(plib))
                liberadas_scrip.append({
                    'tipo':              'AL',
                    'isin':              plib['isin'],
                    'nombre':            plib['nombre'][:50],
                    'fecha':             plib['fecha'],
                    'cantidad':          Decimal(str(plib['cantidad'])),
                    'importe_eur':       Decimal('0'),
                    'gastos_eur':        Decimal('0'),
                    'broker':            'DeGiro',
                    '_es_liberada_scrip': True,
                    '_isin_derechos':     isin_d,
                    '_info_scrip':        info,
                })
                break  # cada liberada empareja con un único evento de derechos

    if liberadas_scrip:
        # ── Coste y tipo de ejercicio (puro vs mixto) ─────────────────────
        # PURO: solo derechos asignados → coste liberada = 0, prorrateo legal
        #        reparte el coste de la posición previa (Art. 37.1.a §4 LIRPF).
        # MIXTO: usuario compró derechos en mercado para completar el canje.
        #        Doctrina AEAT (Manual Cartera de Valores §4.3) — desdoblar
        #        en DOS filas:
        #          - acciones cubiertas íntegramente por derechos gratis
        #            → AL importe 0 (RentaWEB prorratea al vender)
        #          - acciones que requieren compra de derechos → AD
        #            importe = coste real total (absorbe el desembolso aunque
        #            parte del paquete sean derechos gratis residuales)
        #        Reparto: n_ad = ceil(derechos_comprados / ratio_canje), el
        #        resto AL puras. Las filas A de los derechos comprados se
        #        excluyen del CSV (consolidadas en la AD).
        derechos_comprados_a_excluir = []
        liberadas_split_extra: list[dict] = []  # entradas AL nuevas creadas por split mixto
        for lib in liberadas_scrip:
            isin_d          = lib.get('_isin_derechos', '')
            info            = lib.get('_info_scrip', {}) or {}
            ratio           = info.get('ratio_canje')
            qty_liberada    = int(lib['cantidad'])
            fecha_lib_dt    = parse_date_dt(lib['fecha'])

            # Localizar derechos comprados en mercado del mismo ISIN del derecho
            # (tipo A con Order ID) entre el RIGHTS assignment y el canje.
            # Si hay ratio_canje conocido se usa como tope; si no, se asume que
            # todas las compras previas al canje se consumieron (el broker nunca
            # las devuelve, así que si el usuario compró rights es porque fueron
            # para completar el canje).
            comprados_isin = [
                op for op in todas_ops
                if op['tipo'] == 'A' and op['isin'] == isin_d
                and not op.get('_es_liberada_scrip')
            ]
            if comprados_isin:
                comprados_isin.sort(key=lambda o: parse_date_dt(o['fecha']) or datetime.max)
                derechos_usados = int(ratio) * qty_liberada if ratio else None
                acumulado_qty   = 0
                coste_acum      = Decimal('0')
                gastos_acum     = Decimal('0')
                filas_usadas    = []
                for op in comprados_isin:
                    fc = parse_date_dt(op['fecha'])
                    if fecha_lib_dt and fc and fc > fecha_lib_dt:
                        break   # comprados después del canje no pueden alimentarlo
                    q = int(op['cantidad'])
                    if derechos_usados is not None and acumulado_qty >= derechos_usados:
                        break
                    acumulado_qty += q
                    coste_acum    += op['importe_eur']
                    gastos_acum   += op['gastos_eur']
                    filas_usadas.append(op)

                if acumulado_qty > 0:
                    # Ejercicio MIXTO. Reparto AEAT §4.3:
                    #   n_ad = ceil(derechos_comprados / ratio_canje)
                    #   n_al = qty_liberada - n_ad
                    if ratio and int(ratio) > 0:
                        ratio_int = int(ratio)
                        n_ad = (acumulado_qty + ratio_int - 1) // ratio_int  # ceil
                        n_ad = min(n_ad, qty_liberada)  # no exceder total
                    else:
                        # Sin ratio conocido: conservador — todas como AD
                        n_ad = qty_liberada
                    n_al = qty_liberada - n_ad

                    if n_al > 0 and n_ad > 0:
                        # Caso desdoblable: lib pasa a ser la AD; creamos
                        # una nueva entrada para la AL pura.
                        lib['cantidad']             = Decimal(str(n_ad))
                        lib['importe_eur']          = coste_acum
                        lib['gastos_eur']           = gastos_acum
                        lib['_ejercicio_mixto']     = True
                        lib['_derechos_comprados']  = acumulado_qty
                        # Crear hermana AL pura (importe 0, sin marca mixto)
                        lib_al = dict(lib)
                        lib_al['cantidad']          = Decimal(str(n_al))
                        lib_al['importe_eur']       = Decimal('0')
                        lib_al['gastos_eur']        = Decimal('0')
                        lib_al['_ejercicio_mixto']  = False
                        lib_al.pop('_derechos_comprados', None)
                        # Marca informativa: viene de un split mixto
                        lib_al['_es_split_mixto']   = True
                        liberadas_split_extra.append(lib_al)
                    else:
                        # Solo AD (todas las acciones requirieron compra)
                        lib['importe_eur']         = coste_acum
                        lib['gastos_eur']          = gastos_acum
                        lib['_ejercicio_mixto']    = True
                        lib['_derechos_comprados'] = acumulado_qty
                    derechos_comprados_a_excluir.extend(filas_usadas)

            # Prorrateo (escenario PURO): buscar posición previa del SUBYACENTE
            # y estimar coste_unitario = coste_total_subyacente / (cant_prev + liberadas).
            # Esto es informativo — el CSV mantiene importe = 0 (o el valor mixto) y
            # el informe muestra el prorrateo como referencia al vender en el futuro.
            #
            # Importante: el script corre año a año, así que `todas_ops` solo tiene
            # operaciones del ejercicio actual. Para calcular bien el prorrateo
            # necesitamos la posición NETA del subyacente al día de la liberada
            # considerando compras y ventas de TODOS los años anteriores, no solo
            # del actual. Se construye via escaneo de DeGiro_Transacciones_*.csv.
            subyacente_ops = _posicion_historica_subyacente(
                BASE_DIR, lib['isin'], fecha_lib_dt
            )
            # Completar con las compras del año actual ya parseadas (evita
            # duplicar ops que también vienen del escaneo histórico si el CSV
            # del año actual está en BASE_DIR)
            isins_historico_fecha = {
                (op['fecha'], str(op['cantidad']), str(op['importe_eur']))
                for op in subyacente_ops
            }
            for op in todas_ops:
                if (op['tipo'] == 'A' and op['isin'] == lib['isin']
                        and not op.get('_es_liberada_scrip')):
                    key = (op['fecha'], str(op['cantidad']), str(op['importe_eur']))
                    if key not in isins_historico_fecha:
                        subyacente_ops.append(op)

            if subyacente_ops:
                coste_prev     = sum(op['importe_eur'] + op['gastos_eur']
                                     for op in subyacente_ops if op['tipo'] == 'A')
                qty_comprada   = sum(int(op['cantidad'])
                                     for op in subyacente_ops if op['tipo'] == 'A')
                qty_vendida    = sum(int(op['cantidad'])
                                     for op in subyacente_ops if op['tipo'] == 'T')
                qty_prev       = qty_comprada - qty_vendida
                qty_total      = qty_prev + qty_liberada
                if qty_total > 0 and coste_prev > 0:
                    # Prorrateo sobre posición NETA al día del canje (compras − ventas previas)
                    # Para mantener el coste proporcional: coste_prev_neto ≈ coste_prev × qty_prev / qty_comprada
                    if qty_comprada > 0:
                        coste_prev_neto = coste_prev * Decimal(qty_prev) / Decimal(qty_comprada)
                    else:
                        coste_prev_neto = coste_prev
                    lib['_coste_unit_prorrateado'] = (coste_prev_neto / Decimal(qty_total)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    lib['_qty_previa_subyacente']  = qty_prev
                    lib['_coste_total_subyacente'] = coste_prev_neto
                    primer_op_dt = min(
                        (parse_date_dt(op['fecha']) or datetime.max
                         for op in subyacente_ops if op['tipo'] == 'A'),
                        default=datetime.max,
                    )
                    if primer_op_dt != datetime.max:
                        lib['_fecha_origen_fifo'] = primer_op_dt.strftime('%d/%m/%Y')

        # Añadir las AL extra creadas por split mixto (§4.3)
        if liberadas_split_extra:
            liberadas_scrip.extend(liberadas_split_extra)

        # Excluir del CSV las filas A de los derechos comprados que ya se consolidaron
        if derechos_comprados_a_excluir:
            _ids_excluir = {id(op) for op in derechos_comprados_a_excluir}
            todas_ops    = [op for op in todas_ops if id(op) not in _ids_excluir]

        total_lib = sum(int(lib['cantidad']) for lib in liberadas_scrip)
        n_mixto   = sum(1 for lib in liberadas_scrip if lib.get('_ejercicio_mixto'))
        print(f"  ✅ Acciones liberadas (scrip TYPE B): {len(liberadas_scrip)} entrada(s), "
              f"{total_lib} acción(es) → CSV como A con marca [🎁 LIBERADA]")
        if n_mixto:
            print(f"     · Ejercicios MIXTOS: {n_mixto} (coste transferido desde derechos comprados)")
        if derechos_comprados_a_excluir:
            print(f"     · Filas A de derechos consumidos excluidas del CSV: {len(derechos_comprados_a_excluir)}")
        todas_ops.extend(liberadas_scrip)
        todas_ops.sort(key=sort_key)

    # ── Paso 2d: Materializar acciones recibidas en escisiones (spin-offs) ─
    # DeGiro filtra las filas de precio 0 al construir todas_ops, por lo que
    # las acciones del nuevo valor (p.ej. 9 SOLVENTUM CORP el 22/04/2024) no
    # aparecen como compra y la venta posterior daría "venta sin lotes". Aquí
    # las materializamos como fila AD con coste 0 y marca clara para que el
    # usuario lo edite con el coste prorrateado real (Art. 37.1.a LIRPF).
    spin_offs_corp = [e for e in todos_corp if e.get('tipo_ca') == CA_SPIN_OFF]
    if spin_offs_corp:
        # Cargar catalogo de spin-offs con ratios conocidos. Si vacio, los
        # spin-offs detectados caen al flujo manual (coste 0 + comentario
        # amarillo del XLSX) — mismo comportamiento de hoy.
        _SPINOFFS_CATALOGO = _cargar_spinoffs_conocidos()
        spinoff_ops = []
        n_auto = 0
        for ev in spin_offs_corp:
            isin_nueva    = ev.get('isin', '')
            cantidad      = ev.get('cantidad', 0)
            fecha_eff     = ev.get('fecha_efectiva', ev.get('fecha', ''))
            nombre_nueva  = ev.get('nombre', '')
            nombre_matriz = ev.get('nombre_matriz', '')
            isin_matriz   = ev.get('isin_old', '')
            if not isin_nueva or cantidad <= 0:
                continue
            op = {
                'tipo':            'A',          # se emite como AD en el CSV
                'isin':            isin_nueva,
                'nombre':          nombre_nueva[:50],
                'fecha':           fecha_eff,
                'cantidad':        Decimal(str(cantidad)),
                'importe_eur':     Decimal('0'),
                'gastos_eur':      Decimal('0'),
                'broker':          'DeGiro',
                '_es_spinoff':     True,
                '_spinoff_matriz': nombre_matriz,
                '_spinoff_isin_matriz': isin_matriz,
            }

            # ── Aplicacion automatica si el ISIN escindida esta en el catalogo ──
            catalogo = _SPINOFFS_CATALOGO.get(isin_nueva)
            if catalogo:
                # Normalizar fecha_efectiva a `date`. El evento corp suele
                # traerla como 'DD/MM/YYYY' string (formato ES) — convertimos
                # para el helper de coste FIFO. Si parse falla, usamos la del
                # catalogo como autoridad.
                fecha_ev_date = catalogo['fecha_efectiva']
                if isinstance(fecha_eff, str) and re.match(r'^\d{2}/\d{2}/\d{4}$', fecha_eff):
                    try:
                        d, m, y = fecha_eff.split('/')
                        fecha_ev_date = date(int(y), int(m), int(d))
                    except ValueError:
                        pass
                elif isinstance(fecha_eff, date):
                    fecha_ev_date = fecha_eff
                coste_matriz = _coste_matriz_a_fecha(
                    todas_ops, catalogo['isin_matriz'], fecha_ev_date,
                )
                if coste_matriz > 0:
                    # PASO 1: coste prorrateado a la escindida.
                    op['importe_eur'] = (
                        coste_matriz * catalogo['ratio_coste_escindida']
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    op['_spinoff_resuelto_auto']     = True
                    op['_spinoff_fuente']            = catalogo['fuente']
                    op['_spinoff_ratio_escindida']   = catalogo['ratio_coste_escindida']
                    op['_spinoff_ratio_matriz']     = catalogo['ratio_coste_matriz_residual']
                    # Tambien anotar el evento corp para que `fmt_spin_off`
                    # (informe_corporativas TXT) lo recoja al escribir el bloque.
                    ev['_spinoff_resuelto_auto'] = True
                    ev['_spinoff_fuente']        = catalogo['fuente']
                    ev['_spinoff_coste_aplicado'] = op['importe_eur']
                    ev['_spinoff_ratio_escindida']   = catalogo['ratio_coste_escindida']
                    ev['_spinoff_ratio_matriz']     = catalogo['ratio_coste_matriz_residual']
                    # PASO 2 CRITICO: reducir cada lote de la matriz por
                    # ratio_residual. Si esto se olvida, el coste total se
                    # DUPLICA al vender la matriz (regresion silenciosa peor
                    # que el flujo manual).
                    _reducir_lotes_matriz_proporcional(
                        todas_ops,
                        catalogo['isin_matriz'],
                        fecha_ev_date,
                        catalogo['ratio_coste_matriz_residual'],
                    )
                    n_auto += 1
            # Si el ISIN no esta en catalogo o el coste matriz era 0 (sin
            # posicion previa registrada), op queda con coste 0 y el flujo
            # manual del XLSX se activa.

            spinoff_ops.append(op)

        if spinoff_ops:
            n_manual = len(spinoff_ops) - n_auto
            print(f"  ✅ Acciones de escisión: {len(spinoff_ops)} entrada(s) → CSV como AD")
            if n_auto:
                print(f"     ✓ {n_auto} resueltas automaticamente (catalogo spinoffs_conocidos.json — Art. 37.1.a §4 LIRPF)")
            if n_manual:
                print(f"     ⚠️  {n_manual} EDITA el coste prorrateado contra la matriz antes de presentar")
            todas_ops.extend(spinoff_ops)
            todas_ops.sort(key=sort_key)

    # ── Paso 3: Resumen ────────────────────────────────────────────────────
    compras = [op for op in todas_ops if op['tipo'] in ('A', 'AL')]
    ventas  = [op for op in todas_ops if op['tipo'] == 'T']
    total_invertido   = sum((op['importe_eur'] + op['gastos_eur'] for op in compras), Decimal('0'))
    total_transmitido = sum((op['importe_eur'] - op['gastos_eur'] for op in ventas),  Decimal('0'))
    total_filas       = len(todas_ops) + len(todas_sp)

    print(f"\n  {'─'*50}")
    print(f"  RESUMEN")
    print(f"  {'─'*50}")
    print(f"  Adquisiciones (A)        : {len(compras)}")
    print(f"  Transmisiones (T)        : {len(ventas)}")
    print(f"  Splits/Contrasplits (SP) : {len(todas_sp)}")
    print(f"  Total filas en CSV       : {total_filas}")
    print(f"  Importe invertido bruto  : {fmt_es(total_invertido)} EUR")
    print(f"  Importe transmitido neto : {fmt_es(total_transmitido)} EUR")

    # ── Pre-computar lookup de ejercicios de opciones ─────────────────────
    # (fecha_ddmmyyyy, trade_tipo, subyacente) → {strike, prima, tipo_op}
    # CALL ejercida → trade_tipo='T' (el writer entrega acciones = venta)
    # PUT  ejercida → trade_tipo='A' (el writer recibe acciones = compra)
    opts_por_contrato_pre = None
    opts_totales_pre      = None
    ejercicio_lookup: dict = {}

    def _add_ejercicio_lookup(fecha, tipo_trade, sub, info):
        """Inserta la entrada sin pisar otra ya presente con la misma key."""
        key = (fecha, tipo_trade, sub)
        ejercicio_lookup.setdefault(key, info)

    # Futuros IBKR: resumen agrupado por symbol — alimenta PDF/Excel.
    if todos_futuros:
        futuros_por_contrato, futuros_totales = calcular_resumen_futuros(todos_futuros)
        print(f"\n  Futuros IBKR procesados: "
              f"{futuros_totales['n_contratos_distintos']} contrato(s), "
              f"{futuros_totales['n_cierres_total']} cierre(s), "
              f"P&L neto {fmt_es(futuros_totales['pl_neto_eur'])} EUR → "
              f"casilla 1626 c.4")
    else:
        futuros_por_contrato = None
        futuros_totales = None

    if todas_opts:
        opts_por_contrato_pre, opts_totales_pre = calcular_resumen_opciones(todas_opts)
        for _d in opts_totales_pre['_ejercidas']:
            _fecha_venc = _venc_to_ddmmyyyy(_d.get('vencimiento', ''))
            _fecha_cierre = _d.get('fecha_cierre', '')
            _tipo_op    = _d['tipo_op']
            _trade_tipo = 'T' if _tipo_op == 'C' else 'A'
            _sub        = _d.get('subyacente', '')
            info = {
                'strike': _d['strike'],
                'prima':  _d['primas_cobradas'],
                'gastos': _d['gastos_cobradas'],   # comisión del contrato de opción
                'tipo_op': _tipo_op,
                'subyacente': _sub,
            }
            # Entry por fecha de vencimiento (DeGiro: ejercicio en vencimiento).
            if _fecha_venc:
                _add_ejercicio_lookup(_fecha_venc, _trade_tipo, _sub, info)
            # Entry adicional por fecha de cierre real (IBKR: asignación previa
            # al vencimiento). Si fecha_cierre coincide con vencimiento la
            # segunda inserción es no-op por _add_ejercicio_lookup.
            if _fecha_cierre and _fecha_cierre != _fecha_venc:
                _add_ejercicio_lookup(_fecha_cierre, _trade_tipo, _sub, info)
        for _d in opts_totales_pre['_mixtas']:
            _fecha_venc = _venc_to_ddmmyyyy(_d.get('vencimiento', ''))
            _fecha_cierre = _d.get('fecha_cierre', '')
            _tipo_op    = _d['tipo_op']
            _trade_tipo = 'T' if _tipo_op == 'C' else 'A'
            _sub        = _d.get('subyacente', '')
            _prima      = _d.get('_prima_ejercida', Decimal('0'))
            _gastos_ej  = _d.get('_gastos_ejercida', Decimal('0'))
            info = {
                'strike':  _d['strike'],
                'prima':   _prima,
                'gastos':  _gastos_ej,             # comisión proporcional a contratos ejercidos
                'tipo_op': _tipo_op,
                'subyacente': _sub,
            }
            if _fecha_venc:
                _add_ejercicio_lookup(_fecha_venc, _trade_tipo, _sub, info)
            if _fecha_cierre and _fecha_cierre != _fecha_venc:
                _add_ejercicio_lookup(_fecha_cierre, _trade_tipo, _sub, info)
        # Largas ejercidas (V2172-21, lado del COMPRADOR de la opción): la
        # prima PAGADA se integra en el subyacente. El sentido del trade es
        # el INVERSO al del vendedor: CALL larga ejercida → el titular COMPRA
        # acciones al strike ('A', coste += prima pagada); PUT larga ejercida
        # → el titular VENDE ('T', valor de transmisión −= prima pagada). El
        # ajuste de signo del consumidor (C → +prima, P → −prima) es el mismo
        # que para shorts. Antes estas primas acababan en 1626 como pérdida
        # vía la clasificación errónea como "mixta" (F9 auditoría 2026-06-11).
        for _d in opts_totales_pre['_ejercidas_largas']:
            _fecha_venc   = _venc_to_ddmmyyyy(_d.get('vencimiento', ''))
            _fecha_cierre = _d.get('fecha_cierre', '')
            _tipo_op      = _d['tipo_op']
            _trade_tipo   = 'A' if _tipo_op == 'C' else 'T'
            _sub          = _d.get('subyacente', '')
            info = {
                'strike':  _d['strike'],
                'prima':   _d['primas_pagadas'],
                'gastos':  _d['gastos_pagadas'],   # comisión de la opción larga
                'tipo_op': _tipo_op,
                'subyacente': _sub,
            }
            if _fecha_venc:
                _add_ejercicio_lookup(_fecha_venc, _trade_tipo, _sub, info)
            if _fecha_cierre and _fecha_cierre != _fecha_venc:
                _add_ejercicio_lookup(_fecha_cierre, _trade_tipo, _sub, info)

    # Cuenta de ops por (fecha, tipo) para saber si el paso "candidato único"
    # puede aplicarse sin ambigüedad. Si en una misma fecha hay varias compras
    # o ventas, el candidato único NO debe propagarse a cualquiera — solo a la
    # que coincida por strike o por nombre.
    ops_por_fecha_tipo: dict = defaultdict(int)
    for _op in todas_ops:
        if _op['tipo'] in ('A', 'T'):
            ops_por_fecha_tipo[(_op['fecha'], _op['tipo'])] += 1

    # Ejercicios ya aplicados a alguna op — evita que la misma prima se aplique
    # dos veces. Clave: (fecha, trade_tipo, subyacente) del lookup.
    ejercicio_consumido: set = set()

    def _match_ejercicio(op):
        """Devuelve el dict de info de ejercicio si el op corresponde a una opción ejercida.

        Estrategia (en orden de fiabilidad):
          1. Strike == precio unitario de la transacción (identificación exacta).
          2. Subyacente aparece en nombre de empresa (fuzzy).
          3. Única opción ejercida ese día con ese tipo de operación **Y** única op
             de ese tipo en el día (unívoca sin ambigüedad).

        Una vez matcheado, la entrada del lookup se marca como consumida para
        que la misma prima no se aplique a varias operaciones del mismo día.
        """
        if op['tipo'] not in ('A', 'T'):
            return None
        fecha = op['fecha']
        tipo  = op['tipo']

        candidatos = [
            (key, info) for key, info in ejercicio_lookup.items()
            if key[0] == fecha and key[1] == tipo and key not in ejercicio_consumido
        ]
        if not candidatos:
            return None

        def _consumir(key, info):
            ejercicio_consumido.add(key)
            return info

        # 1) Match por precio unitario == strike (más fiable)
        try:
            precio_u = float(op['importe_eur'] / op['cantidad']) if op['cantidad'] else None
        except Exception:
            precio_u = None
        if precio_u is not None:
            for key, info in candidatos:
                try:
                    if abs(precio_u - float(info['strike'])) < 0.02:
                        return _consumir(key, info)
                except (TypeError, ValueError):
                    pass

        # 2) Match por nombre de empresa
        nombre = op.get('nombre', '')
        for key, info in candidatos:
            sub_k = key[2]
            if _subyacente_en_nombre(sub_k, nombre):
                return _consumir(key, info)

        # 3) Candidato único — solo si hay una única op del tipo ese día (sin
        # ambigüedad). Evita emparejar la PUT ejercida de un subyacente X con
        # una compra normal de otro subyacente Y realizada el mismo día.
        if len(candidatos) == 1 and ops_por_fecha_tipo.get((fecha, tipo), 0) == 1:
            key, info = candidatos[0]
            return _consumir(key, info)

        return None

    # ── Paso 4: Construir lista enriquecida y escribir XLSX maestro ────────
    print(f"\n  Construyendo cartera...")
    todas_combinadas = todas_ops + todas_sp
    todas_combinadas.sort(key=sort_key)

    # Enriquecer cada op con los campos finales (tipo AEAT, denominación con
    # marcas, importe ajustado por opciones ejercidas, etc.). Esta lista
    # alimenta al generador XLSX y reemplaza al antiguo CSV.
    for op in todas_combinadas:
        ej = _match_ejercicio(op)
        if ej:
            prima_ej = ej['prima']
            if ej['tipo_op'] == 'C':   # CALL ejercida → venta forzada → +prima
                importe_csv = op['importe_eur'] + prima_ej
            else:                       # PUT ejercida → compra forzada → -prima
                importe_csv = op['importe_eur'] - prima_ej
        else:
            importe_csv = op['importe_eur']

        # Acciones totalmente liberadas (scrip TYPE A): según Manual AEAT
        # Cartera de Valores §4.3 + Art. 37.1.a §4 LIRPF, se introducen
        # con código AL e importe = 0 EUR. La aplicación AEAT prorratea
        # automáticamente el coste agregado entre las acciones existentes
        # del mismo ISIN al ejecutarse la venta posterior. Nuestro motor
        # FIFO también lo prorratea internamente (motor_fiscal._add_buy)
        # — no emitimos el coste manualmente.
        #
        # Excepción: ejercicio MIXTO (recibo derechos por canje + compré
        # más derechos en mercado). Aquí sí desembolsé efectivo (el de los
        # derechos comprados), así que importe_eur ya viene con ese coste
        # real desde el detector de scrip y se mantiene tal cual.
        if op.get('_es_liberada_scrip') and not op.get('_ejercicio_mixto'):
            importe_csv = Decimal('0')

        # Código AEAT "Mi cartera de valores" (Manual infrazk8/Ayuda_CV.pdf):
        #   AD  Adquisición ordinaria. Incluye liberadas MIXTAS (§4.3) y
        #       acciones recibidas en escisiones (con coste 0 a editar).
        #   AL  Acciones totalmente liberadas — entrega gratuita (§4.3).
        #   T   Transmisión.
        #   VD  Venta de Derechos de Suscripción (§4.4). Solo válido en
        #       Cartera de Valores para ventas anteriores a 1/1/2017;
        #       posteriores van directamente en Renta Web F2 → 341-346.
        #   SP  Split / Contrasplit.
        if op.get('_es_venta_derecho'):
            tipo_csv = 'VD'
        elif op['tipo'] == 'AL':
            tipo_csv = 'AD' if op.get('_ejercicio_mixto') else 'AL'
        elif op.get('_es_spinoff'):
            tipo_csv = 'AD'
        elif op['tipo'] == 'A':
            tipo_csv = 'AD'
        elif op['tipo'] == 'T':
            tipo_csv = 'T'   # mantenemos T (motor lo acepta como TR equivalente)
        else:
            tipo_csv = op['tipo']

        denom = op['nombre']
        requiere_revision = False
        if op.get('_es_liberada_scrip'):
            if op.get('_ejercicio_mixto'):
                marca = '[🎁 LIBERADA scrip MIXTO - coste de derechos comprados]'
            elif op.get('_coste_unit_prorrateado'):
                # Coste prorrateado calculado, pero se emite 0 al CSV
                # (RentaWEB lo prorratea sola). Mostramos la cifra como
                # información para que el usuario verifique al vender.
                marca = (f'[🎁 LIBERADA scrip - coste 0 (RentaWEB prorrateará a '
                         f'≈{fmt_es(op["_coste_unit_prorrateado"])} EUR/acc al vender)]')
            else:
                marca = '[🎁 LIBERADA scrip - coste 0 (RentaWEB prorrateará al vender, Art. 37.1.a §4)]'
            denom = f"{denom} {marca}"
        elif op.get('_es_venta_derecho'):
            anio_venta = op['fecha'][-4:] if isinstance(op['fecha'], str) else str(op['fecha'].year)
            if anio_venta >= '2017':
                nota_vd = '[VD post-2017 → NO usar Cartera de Valores; meter en Renta Web F2 casillas 341-346]'
            else:
                nota_vd = '[VD pre-2017 → minora coste de adquisición de las acciones origen]'
            if op.get('_tipo_b_mercado'):
                denom = f"{denom} [SCRIP-B coste 0 Art.37.1.a] {nota_vd}"
            else:
                denom = f"{denom} {nota_vd}"
        elif op.get('_es_spinoff'):
            matriz = op.get('_spinoff_matriz', '')
            isin_m = op.get('_spinoff_isin_matriz', '')
            if op.get('_spinoff_resuelto_auto'):
                # Cuádrate ya aplico el doble ajuste (Art. 37.1.a §4 LIRPF)
                # desde spinoffs_conocidos.json. Coste ya prorrateado en
                # esta fila + lotes de la matriz reducidos × ratio_residual.
                # Sin marca de revision: no requiere accion del usuario.
                coste_aplicado = op.get('importe_eur', Decimal('0'))
                denom = (
                    f"{denom} [ESCISIÓN resuelta auto — "
                    f"coste {coste_aplicado} EUR prorrateado Art.37.1.a §4 LIRPF; "
                    f"lotes de la matriz ({isin_m}) reducidos × ratio_residual]"
                )
                # NO marcar requiere_revision: ya esta hecho.
            else:
                # Sin catalogo: flujo manual. Coste provisional 0, el usuario
                # debe editar el XLSX.
                denom = (
                    f"{denom} [ESCISIÓN de {matriz} ({isin_m}) — "
                    f"EDITA coste con prorrateo Art.37.1.a + DGT V1766-12; "
                    f"reduce también coste de la matriz]"
                )
                requiere_revision = True
        elif op.get('_es_staking_reward'):
            denom = (f"{denom} [🪙 STAKING — lote con coste = valor RCM de "
                     f"recepción (V1766-22 + Art. 43.1 LIRPF); el ingreso ya "
                     f"tributó en 0027]")

        op['_tipo_csv']             = tipo_csv
        op['_denom_csv']            = denom
        op['_importe_csv']          = importe_csv
        op['_gastos_csv']           = op['gastos_eur'] + (ej['gastos'] if ej else Decimal('0'))
        op['_ejercicio_opcion_str'] = 'Y' if ej else ''
        op['_strike_str']           = ej['strike'] if ej else ''
        op['_prima_str']            = fmt_es(ej['prima']) if ej else ''
        op['_tipo_op_str']          = ('CALL' if ej['tipo_op'] == 'C' else 'PUT') if ej else ''
        op['_requiere_revision']    = requiere_revision

    # XLSX maestro: se genera al final del script, una vez calculados los
    # informes de dividendos/opciones y disponible la compensación.
    OUTPUT_XLSX = os.path.join(BASE_DIR, f"cartera_valores_irpf_{EJERCICIO}.xlsx")
    # Guardamos las ops enriquecidas en una variable accesible más adelante.
    todas_combinadas_enriquecidas = todas_combinadas

    # ── Paso 5: Informe corporativas ──────────────────────────────────────
    if todos_corp:
        print(f"  Escribiendo {INFORME_FILE} ...")
        write_informe_corporativas(todos_corp, INFORME_FILE,
                                   derechos_ventas_b_mercado=derechos_ventas_b_mercado,
                                   derechos_ventas_warn=derechos_ventas_warn,
                                   derechos_clasificados=derechos_clasificados,
                                   liberadas_scrip=liberadas_scrip)
        print(f"  ✅ Informe con {len(todos_corp)} eventos corporativos.\n")

    # ── Paso 6: Informe dividendos ─────────────────────────────────────────
    if todos_divs:
        resumen_divs = calcular_resumen_dividendos(todos_divs)
        print(f"  Escribiendo {INFORME_DIV_FILE} ...")
        write_informe_dividendos(resumen_divs, INFORME_DIV_FILE,
                                 registros=todos_divs,
                                 derechos_scrip=None,  # ventas en mercado son G/P, no van aquí
                                 gastos_plataforma=gastos_plataforma_dg if gastos_plataforma_dg else None,
                                 tbills=ibkr_fx_pl['tbills'] if ibkr_fx_pl['tbills'] else None,
                                 ibkr_interest=ibkr_interest if ibkr_interest else None,
                                 staking=tr_staking if tr_staking else None)
        total_bruto_d   = sum((d['bruto']       for d in resumen_divs),                               Decimal('0'))
        total_cdi_d     = sum((d['recuperable'] for d in resumen_divs if not d.get('es_nacional')),   Decimal('0'))
        # Casilla 0591 = retención española de TODAS las filas (nacional ACS +
        # 19% que practica TR Sucursal ES sobre dividendos extranjeros). Antes
        # sumaba `recuperable` de es_nacional, que tras el split de retención es
        # 0 → imprimía "0591 = 0,00" pese a haber retención real (243,23).
        total_ret_nac_d = sum((d.get('retencion_es', Decimal('0')) for d in resumen_divs), Decimal('0'))
        print(f"  ✅ {len(resumen_divs)} valores con dividendos.")
        print(f"     Bruto total: {fmt_es(total_bruto_d)} EUR  |  "
              f"CDI (0588): {fmt_es(total_cdi_d)} EUR  |  "
              f"Ret. ES (0591): {fmt_es(total_ret_nac_d)} EUR\n")
    else:
        print(f"  ℹ️  Sin datos de dividendos "
              f"(falta DeGiro_Cuenta_{EJERCICIO}.csv o secciones IBKR).\n")

    # ── Paso 7: Informe opciones ───────────────────────────────────────────
    if todas_opts:
        # Reutilizar el cálculo ya hecho para el lookup de ejercicios del CSV
        por_contrato = opts_por_contrato_pre
        totales_opt  = opts_totales_pre
        print(f"  Escribiendo {INFORME_OPT_FILE} ...")
        write_informe_opciones(por_contrato, totales_opt, INFORME_OPT_FILE,
                               no_encontradas=opts_anios_anteriores_no_encontradas)
        print(f"  ✅ {len(por_contrato)} contratos/series de opciones.")
        pl_str = f"+{fmt_es(totales_opt['pl_neto'])}" if totales_opt['pl_neto'] >= 0 \
                 else fmt_es(totales_opt['pl_neto'])
        print(f"     P&L neto total: {pl_str} EUR\n")
    else:
        print(f"  ℹ️  Sin operaciones de opciones detectadas.\n")

    # ── Paso 7.5: Informe FX P&L (solo IBKR) ───────────────────────────────
    # IBKR proporciona desglose de G/P por divisa en su Activity Statement.
    # DeGiro NO tiene esto. Generamos un informe propio si hay datos, sin
    # tocar el sidecar totals.json — la decisión de declarar (Art. 33 LIRPF
    # + minimis DGT V0563-09) queda en manos del usuario.
    if ibkr_fx_pl['fx'] or ibkr_fx_pl['tbills']:
        print(f"  Escribiendo {INFORME_FX_FILE} ...")
        write_informe_fx(ibkr_fx_pl, INFORME_FX_FILE)
        n_fx = len(ibkr_fx_pl['fx'])
        n_tb = len(ibkr_fx_pl['tbills'])
        bits = []
        if n_fx:
            bits.append(f"{n_fx} divisa(s)")
        if n_tb:
            bits.append(f"{n_tb} T-Bill(s)")
        print(f"  ✅ Informe FX con {' y '.join(bits)}.\n")

    # ── Paso 8: Compensación de pérdidas (Art. 49 LIRPF) ──────────────────
    # FIFO simplificado sobre todas_combinadas para estimar G/P patrimonial neta
    # de acciones/ETFs, cruzarla con RCM (dividendos) y aplicar saldos negativos
    # de ejercicios anteriores (perdidas_pendientes.json).
    compensacion_resultado = None
    if _COMPENSACION_DISPONIBLE:
        print(f"\n  ─────────────────────────────────────────────────────")
        print(f"  Calculando compensación de pérdidas patrimoniales...")
        print(f"  ─────────────────────────────────────────────────────")

        # FIFO por ISIN sobre la secuencia cronológica de operaciones A/T/SP/AL.
        # Replicamos el ajuste de primas (Art. 37.1.m) aplicado al CSV.
        # LIMITACIÓN: sólo ve operaciones del ejercicio actual → las ventas que
        # cierran posiciones compradas en años anteriores se marcan como
        # "huérfanas" (sin coste de adquisición) y se cuentan aparte para avisar.
        lotes = defaultdict(list)   # isin → [(qty, coste_unit_con_gastos)]
        gp_total = Decimal('0')
        ventas_huerfanas = []  # [(isin, nombre, qty, ingreso_neto)]
        for op in sorted(todas_ops + todas_sp, key=sort_key):
            isin = op['isin']
            tipo = op['tipo']
            qty  = op['cantidad']

            if tipo == 'SP':
                # op['cantidad']=titulos_antiguos, op['importe_eur']=titulos_nuevos
                qty_old = op['cantidad']
                qty_new = op['importe_eur']
                if qty_old <= 0 or qty_new <= 0:
                    continue
                ratio = qty_new / qty_old
                nuevos_lotes = []
                for l_qty, l_coste in lotes.get(isin, []):
                    nuevos_lotes.append((l_qty * ratio, l_coste / ratio))
                lotes[isin] = nuevos_lotes
                continue

            if tipo == 'AL':
                # Acción liberada scrip TYPE B. Coste:
                #  · Ejercicio PURO: 0 (el coste está ya distribuido entre las acciones originales).
                #  · Ejercicio MIXTO: coste = importe_eur (precio de los derechos comprados).
                coste_lote = op.get('importe_eur', Decimal('0'))
                lotes[isin].append((qty, coste_lote))
                continue

            # Ajuste de primas (Art. 37.1.m): reutilizar los importes YA
            # ajustados por el Paso 4 (op['_importe_csv'] / op['_gastos_csv']).
            # Antes se re-llamaba a _match_ejercicio aquí, pero el set
            # ejercicio_consumido quedaba drenado por el Paso 4 y este
            # segundo pase nunca casaba → la estimación de G/P para la
            # compensación perdía el ajuste de primas de las opciones
            # ejercidas (auditoría 2026-06-11, fleco ejercicio_consumido).
            importe_ajust = op.get('_importe_csv', op['importe_eur'])
            gastos_ajust = op.get('_gastos_csv', op['gastos_eur'])

            if tipo == 'A':
                if qty > 0:
                    coste_total = importe_ajust + gastos_ajust
                    lotes[isin].append((qty, coste_total / qty))
            elif tipo == 'T':
                ingreso_neto = importe_ajust - gastos_ajust
                precio_venta_unit = ingreso_neto / qty if qty > 0 else Decimal('0')
                restante = qty
                cola = lotes.get(isin, [])
                while restante > 0 and cola:
                    l_qty, l_coste = cola[0]
                    consumido = min(l_qty, restante)
                    gp_total += consumido * (precio_venta_unit - l_coste)
                    l_qty -= consumido
                    restante -= consumido
                    if l_qty <= 0:
                        cola.pop(0)
                    else:
                        cola[0] = (l_qty, l_coste)
                if restante > 0:
                    # Venta sin coste de adquisición identificado (derecho scrip TYPE B
                    # vendido sin registrar la asignación, o cartera histórica sin
                    # compras en este ejercicio) → G/P = ingreso íntegro.
                    ingreso_huerfano = restante * precio_venta_unit
                    gp_total += ingreso_huerfano
                    ventas_huerfanas.append(
                        (op['isin'], op.get('nombre', ''), restante, ingreso_huerfano)
                    )

        gp_acciones_fifo = gp_total.quantize(Decimal('0.01'), ROUND_HALF_UP)
        opciones_pl_calc = (opts_totales_pre['pl_neto'] if opts_totales_pre
                            else Decimal('0'))
        rcm_neto_calc = (sum(d['bruto'] for d in resumen_divs)
                         if todos_divs else Decimal('0'))

        if _args.gp_override is not None:
            gp_acciones_neto = Decimal(str(_args.gp_override).replace(',', '.'))
            print(f"  G/P acciones/ETFs (OVERRIDE)     : {fmt_es(gp_acciones_neto)} EUR")
            print(f"    (FIFO estimado habría sido   : {fmt_es(gp_acciones_fifo)} EUR)")
        else:
            gp_acciones_neto = gp_acciones_fifo
            print(f"  G/P acciones/ETFs (FIFO estimado): {fmt_es(gp_acciones_neto)} EUR")

        print(f"  P&L opciones                     : {fmt_es(opciones_pl_calc)} EUR")
        print(f"  RCM bruto (dividendos)           : {fmt_es(rcm_neto_calc)} EUR")

        if ventas_huerfanas and _args.gp_override is None:
            total_huerfano = sum(v[3] for v in ventas_huerfanas)
            print()
            print(f"  ⚠️  {len(ventas_huerfanas)} venta(s) SIN COSTE FIFO identificado")
            print(f"     (ingresos totales imputados como G/P íntegra: "
                  f"{fmt_es(total_huerfano)} EUR)")
            print(f"     → El FIFO sólo ve compras del ejercicio actual; si estas")
            print(f"       ventas cierran posiciones HISTÓRICAS, el G/P estimado")
            print(f"       estará INFLADO. Usa --gp-override con el valor real")
            print(f"       tras introducir el CSV en RentaWEB.")
            for isin, nombre, qty, ingreso in ventas_huerfanas[:5]:
                print(f"       · {isin}  {nombre[:30]:30s}  "
                      f"{qty} titulos = {fmt_es(ingreso)} EUR")
            if len(ventas_huerfanas) > 5:
                print(f"       · ... y {len(ventas_huerfanas)-5} más")

        try:
            compensacion_resultado = calcular_compensacion(
                ejercicio=int(EJERCICIO),
                gp_bruto=gp_acciones_neto,
                rcm_neto=rcm_neto_calc,
                opciones_pl=opciones_pl_calc,
                auto_guardar=_args.guardar_compensacion,
            )
            imprimir_resumen_compensacion(compensacion_resultado)
            if _args.guardar_compensacion:
                print(f"  💾 perdidas_pendientes.json actualizado.")
            elif compensacion_resultado.nuevo_saldo_negativo > Decimal('0') \
                    or compensacion_resultado.aplicadas_de_anteriores > Decimal('0'):
                print(f"  ℹ️  Para persistir el nuevo saldo: re-ejecuta con "
                      f"--guardar-compensacion")
        except Exception as e:
            print(f"  ⚠️  Error calculando compensación: {e}")
    else:
        print(f"\n  ℹ️  Módulo compensacion_perdidas no disponible — "
              f"paso de compensación omitido.")

    # ── Paso 8.5: XLSX maestro de cartera (sustituye al CSV antiguo) ──────
    try:
        from datetime import date as _date
        from motor_fiscal import calcular_fifo_from_ops
        # Path al módulo excel_cartera (vive en /app/720/webapp/). Resolvemos
        # desde la ubicación de ESTE script (__file__) para no depender de
        # BASE_DIR que apunta al upload_dir cuando se invoca desde el webapp.
        _script_dir = os.path.dirname(os.path.abspath(__file__))   # /app/720/irpf
        _excel_dir  = os.path.join(os.path.dirname(_script_dir), 'webapp')
        if _excel_dir not in sys.path:
            sys.path.insert(0, _excel_dir)
        from excel_cartera import generate_cartera_xlsx

        # Normalizar ops del script al formato esperado por motor_fiscal.
        def _norm_op(op):
            tipo_csv = op.get('_tipo_csv', op.get('tipo', ''))
            es_scrip   = False
            es_derecho = False
            if tipo_csv == 'AL':
                tipo_motor, es_scrip = 'A', True
            elif tipo_csv == 'AD':
                tipo_motor = 'A'
            elif tipo_csv == 'VD':
                tipo_motor, es_derecho = 'T', True
            elif tipo_csv == 'TR':
                tipo_motor = 'T'
            else:
                tipo_motor = tipo_csv  # A, T, SP
            fecha_raw = op.get('fecha')
            if isinstance(fecha_raw, str):
                d, m, y = fecha_raw.split('/')
                fecha_dt = _date(int(y), int(m), int(d))
            else:
                fecha_dt = fecha_raw
            return {
                'tipo':         tipo_motor,
                'isin':         op.get('isin', ''),
                'nombre':       op.get('_denom_csv', op.get('nombre', '')),
                'fecha':        fecha_dt,
                'cantidad':     op.get('cantidad', Decimal('0')),
                'importe_eur':  op.get('_importe_csv', op.get('importe_eur', Decimal('0'))),
                'gastos_eur':   op.get('_gastos_csv', op.get('gastos_eur', Decimal('0'))),
                'es_scrip':     es_scrip,
                'es_derecho':   es_derecho,
                'ejercicio_opcion': bool(op.get('_ejercicio_opcion_str') == 'Y'),
                'strike':       op.get('_strike_str') or None,
                'prima_eur':    None,
                'tipo_opcion':  op.get('_tipo_op_str', ''),
                'broker':       op.get('broker', ''),
                # Tipo de instrumento (STOCK/ETF/DERIVATIVE/CRYPTO/BOND) —
                # se preserva del enriquecimiento previo para que el motor
                # pueda asignarlo al Lot y propagarlo al FIFOMatch.
                'instrument_type':         op.get('instrument_type', 'STOCK'),
                'instrument_type_unknown': op.get('instrument_type_unknown', False),
                # Trade sintético inferido (FII.Maturity, no del Bond Maturity
                # del broker) — propagado al FIFOMatch para warning en PDF.
                '_amortizacion_inferida':  bool(op.get('_amortizacion_inferida', False)),
                # Flags de detección de corto forzado (§3.9 patrones_degiro.md).
                # Se propagan al motor FIFO; éste valida con inventario real
                # antes de aplicarlos (si hay lots disponibles, se ignoran).
                '_es_corto_apertura':   bool(op.get('_es_corto_apertura', False)),
                '_es_corto_cobertura':  bool(op.get('_es_corto_cobertura', False)),
            }

        ops_motor = [_norm_op(op) for op in todas_combinadas_enriquecidas]

        # Buscar XLSXs/CSVs de años ANTERIORES (estrictamente <) en BASE_DIR.
        # El año se extrae del nombre del fichero. Años posteriores al ejercicio
        # actual NO se incluyen (no tiene sentido para FIFO histórico).
        # Si para un mismo año coexisten XLSX y CSV (porque hay restos de
        # ejecuciones antiguas), preferir el XLSX (output principal actual).
        import glob as _glob
        import re as _re
        ej_int = int(EJERCICIO)
        candidatos = (
            _glob.glob(os.path.join(BASE_DIR, "cartera_valores_irpf_*.xlsx"))
            + _glob.glob(os.path.join(BASE_DIR, "cartera_valores_irpf_*.csv"))
        )
        paths_por_year: dict = {}
        for p in candidatos:
            m = _re.search(r"_(\d{4})\.(xlsx|csv)$", os.path.basename(p))
            if not m:
                continue
            year = int(m.group(1))
            if year >= ej_int:
                continue
            ext = m.group(2)
            actual = paths_por_year.get(year)
            if actual is None or (actual.endswith(".csv") and ext == "xlsx"):
                paths_por_year[year] = p
        paths_anteriores = sorted(paths_por_year.values())

        # ── Sidecar shorts cross-año ──────────────────────────────────────
        # Las ventas en corto que quedan vivas al cierre de un ejercicio
        # NO se reflejan en los XLSX maestros (los XLSX históricos no
        # persisten los flags `_es_corto_apertura`). Se serializan en
        # `shorts_pendientes_<año>.json` para que el ejercicio siguiente
        # las pueda restaurar y emparejar con su cobertura.
        from motor_fiscal import load_shorts_sidecar, save_shorts_sidecar
        _shorts_sidecar_prev = os.path.join(
            BASE_DIR, f"shorts_pendientes_{ej_int - 1}.json"
        )
        _shorts_sidecar_curr = os.path.join(
            BASE_DIR, f"shorts_pendientes_{ej_int}.json"
        )
        shorts_iniciales = load_shorts_sidecar(_shorts_sidecar_prev)
        if shorts_iniciales:
            print(f"\n  📂 {len(shorts_iniciales)} short(s) heredados de "
                  f"{ej_int - 1} (sidecar "
                  f"{os.path.basename(_shorts_sidecar_prev)})")

        # return_ops=True → recuperamos también la lista de todas las ops
        # (actuales + históricas) con `_lote_id` anotado en cada compra. Así
        # el generador del XLSX puede vincular cada fila de G_P_por_valor con
        # la fila concreta de la hoja Operaciones mediante fórmulas Excel.
        # persistir_shorts_al_cierre=True: shorts no cubiertos al cierre
        # quedan vivos y se exponen en results.shorts_pendientes para
        # serializarlos al sidecar (handoff al ejercicio siguiente).
        fifo_results, all_ops_with_ids = calcular_fifo_from_ops(
            ops_motor, paths_anteriores, return_ops=True,
            shorts_iniciales=shorts_iniciales,
            persistir_shorts_al_cierre=True,
        )

        # Escribir o limpiar sidecar del año actual.
        _shorts_pend_actual = getattr(fifo_results, 'shorts_pendientes', []) or []
        if _shorts_pend_actual:
            save_shorts_sidecar(_shorts_sidecar_curr, ej_int, _shorts_pend_actual)
            print(f"  💾 {len(_shorts_pend_actual)} short(s) sin cubrir al "
                  f"cierre de {ej_int} → "
                  f"{os.path.basename(_shorts_sidecar_curr)}")
            print(f"     Se restaurarán al procesar el ejercicio "
                  f"{ej_int + 1}.")
        elif os.path.exists(_shorts_sidecar_curr):
            # Idempotencia: si reprocesamos el año y ahora todos los shorts
            # están cubiertos, eliminar el sidecar para no dejar residuos
            # que el año siguiente intentaría restaurar.
            try:
                os.remove(_shorts_sidecar_curr)
                print(f"  🧹 Sidecar {os.path.basename(_shorts_sidecar_curr)} "
                      f"eliminado (todos los shorts cubiertos en {ej_int}).")
            except OSError:
                pass

        # Propagar `_lote_id` a las ops enriquecidas: generar_irpf y motor
        # comparten la identidad de la op (mismo dict), así que basta con
        # buscar por (isin, fecha_motor, cantidad). Las ops del año actual ya
        # son los mismos objetos que pasamos al motor → ya tienen _lote_id.
        # Pero las "históricas" se parsearon dentro del motor desde XLSX/CSV
        # y NO coinciden en identidad con las ops del año: para ellas pasamos
        # `all_ops_with_ids` al generador, que las usará para el histórico.

        generate_cartera_xlsx(
            ejercicio=int(EJERCICIO),
            output_path=OUTPUT_XLSX,
            operaciones=todas_combinadas_enriquecidas,
            ops_motor_con_ids=ops_motor,           # ops actuales con _lote_id
            fifo_results=fifo_results,
            dividendos_resumen=resumen_divs if 'resumen_divs' in dir() and todos_divs else None,
            opciones_por_contrato=por_contrato if 'por_contrato' in dir() and todas_opts else None,
            opciones_totales=totales_opt if 'totales_opt' in dir() and todas_opts else None,
            compensacion=(compensacion_resultado
                          if 'compensacion_resultado' in dir() else None),
            paths_anteriores=paths_anteriores,
            ops_historicas_con_ids=[
                op for op in all_ops_with_ids
                if op.get("fecha") and hasattr(op["fecha"], "year")
                and op["fecha"].year != int(EJERCICIO)
            ],
            fx_pl=ibkr_fx_pl if (ibkr_fx_pl.get('fx') or ibkr_fx_pl.get('tbills')) else None,
            ibkr_interest=ibkr_interest if ibkr_interest else None,
            tr_staking=tr_staking if tr_staking else None,
            gastos_plataforma=gastos_plataforma_dg if gastos_plataforma_dg else None,
            futuros_por_contrato=futuros_por_contrato,
            futuros_totales=futuros_totales,
        )
        # Sidecar JSON con eventos corporativos relevantes para el preflight UI.
        # Permite que el flujo de generación de PDF (que lee el XLSX maestro
        # vía `parse_csv_irpf` sin acceso a las ops in-memory ni sus flags)
        # detecte los cortos forzados y los pase al modal de confirmación
        # pre-pago. Estructura compacta: solo lo que el frontend necesita.
        try:
            _corp_sidecar_path = OUTPUT_XLSX.rsplit('.', 1)[0] + '.corp_events.json'
            _corp_compact = []
            for ev in todos_corp:
                tipo = ev.get('tipo_ca') or ''
                # Incluimos CA_SPIN_OFF SOLO cuando se resolvio automaticamente
                # (catalogo spinoffs_conocidos.json). El PDF de anyos
                # posteriores lo lee para informar al usuario de las
                # escisiones aplicadas que afectan a sus lotes vivos.
                is_spinoff_auto = (tipo == CA_SPIN_OFF
                                   and ev.get('_spinoff_resuelto_auto'))
                if tipo not in (CA_CORTO_FORZADO, CA_MARKET_TRANSFER, CA_ISIN_CHANGE) \
                        and not is_spinoff_auto:
                    continue
                fecha_ev = ev.get('fecha_efectiva') if is_spinoff_auto else ev.get('fecha')
                if hasattr(fecha_ev, 'strftime'):
                    fecha_str = fecha_ev.strftime('%d/%m/%Y')
                else:
                    fecha_str = str(fecha_ev or '')
                entry = {
                    'tipo_ca':        tipo,
                    'fecha':          fecha_str,
                    'isin':           ev.get('isin') or ev.get('isin_new') or ev.get('isin_old') or '',
                    'isin_old':       ev.get('isin_old') or '',
                    'isin_new':       ev.get('isin_new') or '',
                    'nombre':         ev.get('nombre') or '',
                    'cantidad':       float(ev.get('cantidad') or ev.get('qty_old') or 0),
                    'mercado_origen': ev.get('mercado_origen') or '',
                    'mercado_destino': ev.get('mercado_destino') or '',
                    'descripcion':    ev.get('descripcion') or '',
                }
                if is_spinoff_auto:
                    coste_aplicado = ev.get('_spinoff_coste_aplicado')
                    entry.update({
                        'resuelto_auto':           True,
                        'nombre_matriz':           ev.get('nombre_matriz', ''),
                        'isin_matriz':             ev.get('isin_old', ''),
                        'isin_escindida':          ev.get('isin', ''),
                        'fuente':                  ev.get('_spinoff_fuente', ''),
                        'coste_aplicado_eur':      (float(coste_aplicado)
                                                    if coste_aplicado else 0.0),
                        'ratio_coste_escindida':   (float(ev.get('_spinoff_ratio_escindida', 0))
                                                    if ev.get('_spinoff_ratio_escindida') else 0.0),
                        'ratio_coste_matriz':      (float(ev.get('_spinoff_ratio_matriz', 0))
                                                    if ev.get('_spinoff_ratio_matriz') else 0.0),
                    })
                _corp_compact.append(entry)
            with open(_corp_sidecar_path, 'w', encoding='utf-8') as _f:
                import json as _json
                _json.dump({
                    'ejercicio': int(EJERCICIO),
                    'eventos':   _corp_compact,
                }, _f, indent=2, ensure_ascii=False)
        except Exception as _e:
            print(f"  ⚠️  No se pudo escribir el sidecar corp_events: {_e}")

        print(f"\n  📊 XLSX maestro: {os.path.basename(OUTPUT_XLSX)}")
        print(f"     · Hoja Resumen: importes finales por casilla")
        print(f"     · Hoja G_P_por_valor: G/P FIFO con coste editable (amarillo)")

        # Volcar las descartadas no soportadas a un JSON al lado del XLSX para
        # que el PDF pueda mostrarlas en un banner. La lista incluye cripto,
        # bonds, futures, warrants, CFDs, structured products, mutual funds
        # (de IBKR vía Asset Category) y derivados estructurados (de DeGiro
        # vía heurística de nombre: SG/BNP/Vontobel/etc. + Factor/Turbo/KO).
        if todas_no_soportadas:
            DESCARTADAS_JSON = OUTPUT_XLSX.rsplit(".", 1)[0] + ".no_soportadas.json"
            try:
                # Serializar fechas a string ISO y Decimals a float para JSON.
                _serializable = []
                for d in todas_no_soportadas:
                    item = {}
                    for k, v in d.items():
                        if hasattr(v, 'isoformat'):
                            item[k] = v.isoformat()
                        elif isinstance(v, Decimal):
                            item[k] = float(v)
                        else:
                            item[k] = v
                    _serializable.append(item)
                with open(DESCARTADAS_JSON, 'w', encoding='utf-8') as fh:
                    json.dump(_serializable, fh, ensure_ascii=False, indent=2)
                print(f"  ⚠️  {os.path.basename(DESCARTADAS_JSON)}: "
                      f"{len(todas_no_soportadas)} ops no soportadas registradas")
            except Exception as _exc:
                print(f"  (warn) no se pudo escribir descartadas JSON: {_exc}",
                      file=sys.stderr)
    except Exception as exc:
        # FALLO DURO: el XLSX maestro es el output principal del script.
        # Si no se genera, el webapp no puede continuar — mejor crashear con
        # rc != 0 para que el error suba a la UI con detalle, en vez de tragar
        # la excepción con un print discreto.
        import traceback
        print(f"\n  ❌ ERROR FATAL al generar XLSX maestro: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)

    # ── Paso 9: Instrucciones ─────────────────────────────────────────────
    print("  FICHEROS GENERADOS:")
    print("  ─────────────────────────────────────────────────────")
    print(f"  📊 {os.path.basename(OUTPUT_XLSX)}")
    print(f"     → XLSX maestro: Resumen + Operaciones + FIFO por valor + Dividendos + Pérdidas + Opciones")
    print(f"     → {total_filas} filas en hoja Operaciones  |  Compras: {len(compras)}  Ventas: {len(ventas)}")
    if todos_corp:
        print(f"  📄 {os.path.basename(INFORME_FILE)}")
        print(f"     → Derechos RTS y eventos corporativos que requieren acción manual")
    if todos_divs:
        print(f"  📄 {os.path.basename(INFORME_DIV_FILE)}")
        print(f"     → Dividendos por valor, retención origen y deducción CDI (casilla 0588)")
    if todas_opts:
        print(f"  📄 {os.path.basename(INFORME_OPT_FILE)}")
        print(f"     → P&L de opciones por contrato (casillas 1624-1654 RentaWEB)")
    if ibkr_fx_pl['fx'] or ibkr_fx_pl['tbills']:
        print(f"  📄 {os.path.basename(INFORME_FX_FILE)}")
        print(f"     → G/P de divisa IBKR (Art. 33 LIRPF) e intereses T-Bills (casilla 0031)")
    print()
    print("  CÓMO DECLARAR EN RentaWEB:")
    print("  ─────────────────────────────────────────────────────")
    print(f"  · Acciones/ETFs cotizados → 'Ganancias y pérdidas' → casillas {C('acciones_detalle')} (apartado F2)")
    print("    Usar el CSV como hoja de trabajo e introducir manualmente,")
    print("    o usar Claude Desktop (computer use) para automatizar el relleno.")
    if todas_sp:
        print(f"  · Splits/contrasplits ({len(todas_sp)}) → introducir manualmente como SP")
        print(f"    (ver informe_corporativas_{EJERCICIO}.txt)")
    if todos_divs:
        print("  · Dividendos → casilla 0029 (rendimientos capital mobiliario)")
        print(f"  · Deducción doble imposición → casilla 0588 = {fmt_es(total_cdi_d)} EUR")
        print(f"  · Retención española (pagadores ES) → casilla 0591 = {fmt_es(total_ret_nac_d)} EUR")
    if derechos_ventas_b_mercado:
        total_b_print = sum(op['importe_eur'] for op in derechos_ventas_b_mercado)
        print(f"  · Derechos scrip vendidos en mercado → G/P patrimonial → casillas {C('derechos')} = {fmt_es(total_b_print)} EUR")
        print(f"    (incluidos en el CSV como VD; coste asignado = 0 EUR → G/P = importe íntegro)")
        print(f"    ⚠️  AEAT: el código VD en 'Mi cartera de valores' SOLO sirve para ventas")
        print(f"       anteriores a 1/1/2017. Las ventas posteriores se introducen DIRECTAMENTE")
        print(f"       en Renta Web → F2 → casillas 341-346 (NO usar 'Mi cartera de valores').")
        print(f"    ⚠️  Si alguno fue recomprado al precio comprometido → mover a casilla 0029 manualmente")
        print(f"    (ver instrucciones en informe_corporativas_{EJERCICIO}.txt)")
    if derechos_ventas_warn:
        print(f"  ⚠️  {len(derechos_ventas_warn)} derecho(s) SIN CLASIFICAR en CSV como T →")
        print(f"      verificar manualmente casilla correcta (0029 si scrip, 1626 si ampliación real)")
    if todas_opts:
        print(f"  · Opciones → casillas {C('otros')} (otros elementos patrimoniales; tipo en {C('otros_clave_tipo')})")
    print(f"\n  ⚠️  Verificar antes de presentar: importes vs. extractos reales.\n")


if __name__ == '__main__':
    main()
