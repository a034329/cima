"""
Motor Fiscal FIFO — Cálculo de ganancias/pérdidas patrimoniales.

Consume el CSV normalizado de generar_irpf.py (cartera_valores_irpf_YYYY.csv)
y calcula G/P reales usando método FIFO, detectando la regla de los 2 meses
(art. 33.5.f LIRPF).

Uso standalone:
    python motor_fiscal.py cartera_valores_irpf_2025.csv [cartera_valores_irpf_2024.csv ...]

Uso como módulo:
    from motor_fiscal import FIFOTracker, parse_csv_irpf
    tracker = FIFOTracker()
    for op in parse_csv_irpf("cartera_valores_irpf_2025.csv"):
        tracker.process(op)
    results = tracker.get_results()
"""

from __future__ import annotations

import csv
import re
import sys
import bisect
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional


# Patrón de derechos de suscripción/asignación (compartido con generar_irpf.py)
_RTS_RE = re.compile(r'\bRTS\b|\bRIGHTS?\b|\bDERECHOS?\b|\bRCT\b', re.IGNORECASE)
_RTS_SUFFIX_RE = re.compile(
    r'\s*-?\s*(NON.?TRADEABLE|RTS|RIGHTS?|NIL|DERECHOS?|RCT)\s*$',
    re.IGNORECASE,
)


def _is_rts(name: str) -> bool:
    return bool(_RTS_RE.search(name or ""))


def _base_company(name: str) -> str:
    """Nombre base de la empresa, quitando sufijos RTS/RIGHTS/DERECHOS."""
    return _RTS_SUFFIX_RE.sub('', name or '').strip().upper()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_es_decimal(s) -> Decimal:
    """Parsea número en formato español (coma decimal) a Decimal.

    Acepta también números nativos de Excel (int/float/Decimal) si vienen
    desde la lectura XLSX.
    """
    if s is None:
        return Decimal("0")
    if isinstance(s, Decimal):
        return s
    if isinstance(s, (int, float)):
        return Decimal(str(s))
    s = str(s).strip()
    if not s:
        return Decimal("0")
    return Decimal(s.replace(".", "").replace(",", "."))


def _parse_es_date(s) -> date:
    """Parsea fecha DD/MM/YYYY a date. Acepta también date/datetime nativos."""
    from datetime import datetime as _dt
    if isinstance(s, date) and not isinstance(s, _dt):
        return s
    if isinstance(s, _dt):
        return s.date()
    s = str(s).strip()
    d, m, y = s.split("/")
    return date(int(y), int(m), int(d))


def _format_es_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _format_eur(d: Decimal) -> str:
    """Formatea Decimal como EUR con 2 decimales y separador de miles."""
    sign = "-" if d < 0 else ""
    abs_d = abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    int_part, dec_part = str(abs_d).split(".")
    # Separador de miles con punto
    groups = []
    for i, c in enumerate(reversed(int_part)):
        if i > 0 and i % 3 == 0:
            groups.append(".")
        groups.append(c)
    formatted_int = "".join(reversed(groups))
    return f"{sign}{formatted_int},{dec_part} EUR"


TWO = Decimal("2")
ZERO = Decimal("0")
CENT = Decimal("0.01")


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class Lot:
    """Un lote de compra en el inventario FIFO."""
    isin: str
    nombre: str
    fecha_compra: date
    cantidad: Decimal           # acciones restantes en este lote
    cantidad_original: Decimal  # acciones al crear el lote
    coste_unitario_eur: Decimal # coste por acción (incl. gastos prorrateados)
    coste_total_eur: Decimal    # coste total (importe + gastos)
    gastos_eur: Decimal         # gastos de transacción
    es_scrip: bool = False      # True si es acción liberada (scrip dividend, coste=0)
    ejercicio_opcion: bool = False
    strike: Optional[Decimal] = None
    prima_eur: Optional[Decimal] = None
    tipo_opcion: str = ""       # CALL/PUT
    broker: str = ""            # DeGiro/IBKR/TradeRepublic — informativo (FIFO casa por ISIN)
    instrument_type: str = "STOCK"  # 'STOCK' | 'ETF' — para split casillas
                                    # 0326-0340 vs 2224-2236 (Renta 2025+).
    instrument_type_unknown: bool = False  # marginal → permite re-marcar.
    lote_id: int = 0            # identificador único del lote (asignado por el tracker).
                                # Permite que cada FIFOMatch referencie el lote origen
                                # y que el XLSX maestro pueda ligar la fila de G/P
                                # por valor con la fila de la compra en hoja
                                # Operaciones mediante fórmulas prorrateadas.


@dataclass
class FIFOMatch:
    """Match de un lote con una venta — una venta puede generar múltiples matches."""
    isin: str
    nombre: str
    fecha_compra: date
    fecha_venta: date
    cantidad: Decimal
    coste_adquisicion: Decimal  # coste base de este tramo (incluye gastos de compra)
    importe_transmision: Decimal  # importe bruto de este tramo (antes de gastos venta)
    gastos_venta: Decimal       # gastos de venta prorrateados a este tramo
    ganancia_perdida: Decimal   # G/P = importe - gastos_venta - coste
    ejercicio_fiscal: int       # año de la venta
    regla_2_meses: bool = False
    regla_2_meses_detalle: str = ""
    es_scrip: bool = False
    ejercicio_opcion: bool = False
    es_derecho: bool = False    # venta de derechos de suscripción (CSV código VD).
                                # Va a casillas 0341-0355 (Renta 2025), NO a 0326-0340.
    broker_compra: str = ""     # broker del lote origen (informativo)
    broker_venta: str = ""      # broker que ejecutó la venta (informativo)
    instrument_type: str = "STOCK"  # 'STOCK' | 'ETF' — clasificación heredada
                                    # del lote de compra. En Renta 2025+ los ETFs
                                    # van a casillas 2224-2236 (Art. 75.3.j RIRPF),
                                    # las acciones a 0326-0340.
    instrument_type_unknown: bool = False  # True si la clasificación es marginal
                                           # (UNKNOWN tratado como STOCK por defecto;
                                           # usuario puede re-marcar en Excel).
    amortizacion_inferida: bool = False    # True si la venta es un Trade
                                           # sintético inferido desde FII.Maturity
                                           # (no de un Trade real ni de Bond
                                           # Maturity). El PDF/Excel lo señalan
                                           # como "amortización inferida" para
                                           # que el usuario revise.
    # Trazabilidad del lote origen (para fórmulas prorrateadas en XLSX maestro).
    # `cantidad_lote_original` es la cantidad total del lote cuando se creó
    # (antes de ser consumido parcialmente por ventas posteriores). Con
    # (cantidad / cantidad_lote_original) obtenemos la fracción del coste del
    # lote que corresponde a este match.
    lote_id: int = 0
    cantidad_lote_original: Decimal = Decimal("0")
    gastos_compra: Decimal = Decimal("0")  # gastos de compra prorrateados a este
                                # tramo (comisión broker + AutoFX + tasas externas
                                # tipo Stamp Duty UK / ITF ES / FTT FR / HK).
                                # YA suman dentro de `coste_adquisicion`; el campo
                                # se expone aparte para auditoría y reconciliación
                                # con informes externos (DeGiro Annual Report
                                # excluye explícitamente las comisiones de compra).
    es_corto: bool = False      # True cuando el match proviene de la cobertura
                                # de una venta corta detectada en `_cubrir_cortos`
                                # (§3.9 patrones_degiro.md). Lo usa el preflight
                                # de la UI para avisar al usuario de posibles
                                # falsos positivos cuando el CSV histórico es
                                # incompleto.
    # Integración diferida (Art. 33.5.f LIRPF último párrafo): cuando un lote
    # recomprado que mantenía una pérdida diferida se transmite sin nueva
    # recompra en ventana 2M, la pérdida diferida aflora en este match (en su
    # ejercicio). Es independiente de `ganancia_perdida` (que sigue siendo la
    # G/P real de la operación). El informe la presenta como cómputo separado
    # — doctrinal: manual AEAT F2 "Integración diferida: pérdidas patrimoniales
    # derivadas de transmisiones con recompra del elemento".
    perdida_diferida_aflorada_eur: Decimal = Decimal("0")  # valor absoluto
    perdida_diferida_origen: str = ""                       # texto sintético:
                                # "ej. 2024, venta 15/10/2024 (lote #42)"
                                # (resumen — para info completa ver desglose)
    perdida_diferida_intra_anual: bool = False  # True si la venta original
                                # que generó la pérdida y la transmisión
                                # definitiva (donde aflora) ocurren en el
                                # MISMO ejercicio fiscal. El resultado fiscal
                                # neto es idéntico marcando o no el flag 2M
                                # en RentaWEB, aunque la doctrina (DGT V3282-18)
                                # exige marcar y aflorar (mecánica por fases).
                                # El template añade nota explicativa para
                                # informar al usuario.
    perdida_diferida_desglose: list[dict] = field(default_factory=list)
                                # Lista de orígenes acumulados que afloran
                                # en este match. Cada entry:
                                #   {"ejercicio": int, "fecha_venta": date,
                                #    "importe_eur": Decimal, "lote_origen": int}
                                # Permite al template/informe descomponer la
                                # PD acumulada por ejercicio de origen
                                # (útil cuando hay cadena de diferimientos).


@dataclass
class PositionSummary:
    """Resumen de posición abierta con PM real."""
    isin: str
    nombre: str
    cantidad_total: Decimal
    coste_total_eur: Decimal
    pm_ponderado_eur: Decimal   # precio medio ponderado
    num_lotes: int
    lote_mas_antiguo: Optional[date]
    lote_mas_reciente: Optional[date]
    es_mixta: bool = False      # True si tiene lotes scrip + normales


@dataclass
class OrphanSale:
    """Venta sin lotes de compra previos (CSV incompleto: faltan los años
    en que el usuario compró estos valores). El motor no puede calcular la
    G/P real — saldría como 100 % ganancia o cantidad parcial sin coste.
    El frontend la muestra en el modal de aviso pre-pago para que el
    usuario decida si subir más CSVs o continuar bajo su responsabilidad."""
    isin: str
    nombre: str
    fecha: date
    cantidad: Decimal
    importe_eur: Decimal
    broker: str = ""
    parcial: bool = False          # True si es venta parcial (lotes
                                    # insuficientes pero algunos hay)
    cantidad_faltante: Decimal = Decimal("0")  # solo relevante si parcial


@dataclass
class OpenShort:
    """Venta corta abierta: el usuario vendió N acciones de un ISIN del que
    no tenía inventario (el broker se las prestó). Queda pendiente hasta
    que una compra posterior del mismo ISIN+broker la cubra, momento en el
    que se realiza la G/P (Art. 33 LIRPF: ganancia/pérdida patrimonial por
    transmisión, realizada cuando se cierra la posición corta).

    Anclaje doctrinal: la jurisprudencia fiscal española trata la apertura
    del corto como diferimiento — no hay realización en el momento de la
    venta de las acciones prestadas, sí cuando se compran las acciones
    para devolverlas al prestamista. La G/P = importe venta − coste
    compra cobertura − comisiones (Art. 35.1.b LIRPF).
    """
    isin: str
    nombre: str
    fecha_apertura: date
    cantidad: Decimal
    importe_eur: Decimal            # proceeds de la venta corta (gross)
    gastos_eur: Decimal             # comisiones de la venta corta
    broker: str = ""
    ejercicio_opcion: bool = False  # True si la apertura del corto fue
                                    # via ejercicio de call descubierta
    instrument_type: str = "STOCK"
    instrument_type_unknown: bool = False


@dataclass
class PerdidaDiferida:
    """Pérdida patrimonial diferida por la regla 2 meses (Art. 33.5.f LIRPF,
    último párrafo). Atada al lote recomprado que mantuvo la pérdida en
    suspenso. Se integra como pérdida del ejercicio en que ese lote se
    transmite sin nueva recompra de homogéneos dentro de la ventana 2M
    (concepto de "transmisión definitiva" — DGT V3282-18).

    El informe la imputa como cómputo independiente de la G/P de la
    transmisión definitiva — manual AEAT, sección F2 "Integración
    diferida": la pérdida diferida es "independiente de la tributación
    que corresponda a la ganancia o pérdida originada" por la nueva
    transmisión.

    `origenes`: cuando varias ventas-pérdida tienen ventana 2M que cae en
    el mismo lote-recompra, o cuando hay cadena de diferimientos (varios
    lotes encadenados), los orígenes se acumulan. Permite reconstruir el
    desglose por ejercicio para que el usuario sepa qué pérdida viene de
    qué venta original.
    """
    isin: str
    nombre: str
    importe_eur: Decimal           # valor absoluto pendiente de aflorar
    cantidad_pendiente: Decimal    # acciones del lote_id_recompra aún en cartera
    fecha_venta_origen: date       # primera venta-origen (más antigua acumulada)
    ejercicio_origen: int          # año fiscal de la primera venta-origen
    lote_id_recompra: int          # lote recomprado que arrastra la pérdida
    # Desglose de las pérdidas acumuladas en este lote. Cada entry:
    #   {"ejercicio": int, "fecha_venta": date, "importe_eur": Decimal,
    #    "lote_origen": int}
    # Suma de importes == importe_eur (invariante a mantener tras
    # acumulación / prorrateo / cadena).
    origenes: list[dict] = field(default_factory=list)


@dataclass
class FIFOResults:
    """Resultado completo del motor FIFO."""
    matches: list[FIFOMatch] = field(default_factory=list)
    positions: list[PositionSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    orphan_sales: list[OrphanSale] = field(default_factory=list)
    # Pérdidas diferidas (Art. 33.5.f LIRPF) que NO afloraron dentro del
    # histórico procesado — siguen latentes porque los lotes recomprados
    # que las arrastran no se han transmitido aún (o se han transmitido
    # pero con nueva recompra en 2M, lo que prolonga el diferimiento).
    # El frontend las muestra como informativo: "estos importes aflorarán
    # cuando vendas los lotes X sin nueva recompra".
    perdidas_diferidas_latentes: list[PerdidaDiferida] = field(default_factory=list)
    # Posiciones cortas abiertas al cierre del histórico procesado que NO se
    # cubrieron dentro del rango. Solo se popula cuando el tracker se inicia
    # con `persistir_shorts_al_cierre=True` (modo cross-año). En modo legacy
    # (default) los shorts no cubiertos se reclasifican como orphan_sales y
    # esta lista queda vacía.
    # El orquestador externo serializa esto en shorts_pendientes_YYYY.json y
    # los pasa via `restore_open_shorts()` al tracker del año siguiente para
    # que las compras de cobertura cross-año cierren correctamente.
    shorts_pendientes: list[OpenShort] = field(default_factory=list)
    # Operaciones ignoradas por el corte temporal (solo se popula si el
    # caller pasó `corte_fecha` a calcular_fifo). El frontend lo usa para
    # avisar al usuario de que su CSV incluía datos posteriores al
    # ejercicio que está declarando — útiles para regla 2M hasta 28/02
    # del año fiscal+1, ignoradas a partir de ahí.
    n_ops_ignoradas: int = 0
    fecha_corte: Optional[date] = None
    fecha_max_ignorada: Optional[date] = None

    # Totales por ejercicio fiscal
    def total_gp_por_ejercicio(self) -> dict[int, Decimal]:
        totals: dict[int, Decimal] = defaultdict(Decimal)
        for m in self.matches:
            totals[m.ejercicio_fiscal] += m.ganancia_perdida
        return dict(totals)

    def total_gp_deducible_por_ejercicio(self) -> dict[int, Decimal]:
        """G/P excluyendo pérdidas no deducibles por regla 2 meses."""
        totals: dict[int, Decimal] = defaultdict(Decimal)
        for m in self.matches:
            if m.regla_2_meses and m.ganancia_perdida < 0:
                continue  # pérdida no deducible
            totals[m.ejercicio_fiscal] += m.ganancia_perdida
        return dict(totals)

    def matches_por_ejercicio(self, ejercicio: int) -> list[FIFOMatch]:
        return [m for m in self.matches if m.ejercicio_fiscal == ejercicio]

    def matches_regla_2m(self) -> list[FIFOMatch]:
        return [m for m in self.matches if m.regla_2_meses]

    def matches_con_perdida_diferida_aflorada(self, ejercicio: int | None = None
                                              ) -> list[FIFOMatch]:
        """Matches en los que aflora una pérdida diferida de un ejercicio
        anterior (transmisión definitiva del lote recomprado)."""
        out = [m for m in self.matches if m.perdida_diferida_aflorada_eur > 0]
        if ejercicio is not None:
            out = [m for m in out if m.ejercicio_fiscal == ejercicio]
        return out

    def total_perdida_diferida_aflorada_por_ejercicio(self) -> dict[int, Decimal]:
        """Suma de pérdidas diferidas afloradas por ejercicio (valor absoluto;
        el informe lo aplica como pérdida adicional a la base del ahorro)."""
        totals: dict[int, Decimal] = defaultdict(Decimal)
        for m in self.matches:
            if m.perdida_diferida_aflorada_eur > 0:
                totals[m.ejercicio_fiscal] += m.perdida_diferida_aflorada_eur
        return dict(totals)


# ── Parser CSV ───────────────────────────────────────────────────────────────

def parse_csv_irpf(filepath: str | Path, apply_year_filter: bool = True) -> list[dict]:
    """Lee cartera_valores_irpf_YYYY.{csv,xlsx} y devuelve lista de operaciones.

    Acepta:
      - CSV: formato legacy del proyecto (utf-8-sig, separador ';').
      - XLSX: formato actual con varias hojas. Lee la hoja "Operaciones"; si
        no existe, intenta la primera con cabecera reconocida.

    `apply_year_filter` (solo afecta XLSX): si True, descarta las filas cuyo
    año no coincide con el `_YYYY` del nombre del fichero (necesario en el
    flujo web que pasa varios XLSX por año, cada uno con históricos como
    referencia). Si False, se devuelven todas las filas (caso CLI con un
    único XLSX que es la fuente completa). Ver `_read_rows_xlsx`.

    Cada operación es un dict con:
        tipo: A|T|SP|AL
        isin, nombre, fecha (date), cantidad (Decimal), importe_eur, gastos_eur
        Para SP: cantidad=titulos_antiguos, importe_eur=titulos_nuevos, gastos_eur=nominal_antiguo
        Para opciones: ejercicio_opcion, strike, prima_eur, tipo_opcion
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()
    if ext == ".xlsx":
        rows = _read_rows_xlsx(filepath, apply_year_filter=apply_year_filter)
    else:
        rows = _read_rows_csv(filepath)

    ops: list[dict] = []
    for row_num, row in rows:
        if len(row) < 7:
            continue
        tipo = (row[0] or "").strip().upper() if row[0] is not None else ""
        if tipo not in ("A", "T", "SP", "AL", "AD", "TR", "VD"):
            continue

        isin = (row[1] or "").strip() if row[1] is not None else ""
        nombre = (row[2] or "").strip() if row[2] is not None else ""
        fecha = _parse_es_date(row[3])
        cantidad = _parse_es_decimal(row[4])
        importe_eur = _parse_es_decimal(row[5])
        gastos_eur = _parse_es_decimal(row[6])

        # Normalización a códigos internos del motor (A/T/SP) preservando
        # la semántica AEAT con flags `es_scrip` y `es_derecho`.
        es_derecho = False
        if tipo == "AL":
            # Acciones totalmente liberadas (Manual AEAT §4.3): coste 0,
            # se propaga es_scrip=True para que el motor reconozca su
            # tratamiento especial al liquidar el lote en el futuro.
            tipo = "A"
            es_scrip = True
        elif tipo == "AD":
            # Adquisición ordinaria (incluye MIXTO §4.3): coste real
            # desembolsado. Procesar como compra normal sin marca scrip.
            tipo = "A"
            es_scrip = False
        elif tipo == "TR":
            tipo = "T"
            es_scrip = False
        elif tipo == "VD":
            # Venta de Derechos de Suscripción (Manual AEAT §4.4): se
            # procesa como T (consume FIFO si hay lotes, o coste 0 si es
            # scrip TYPE B sin lote previo). El flag `es_derecho` se
            # propaga al FIFOMatch para que el informe lo excluya del
            # bloque de acciones/ETFs (casillas 326-338) y lo contabilice
            # en derechos (casillas 341-346 — Renta 2025).
            tipo = "T"
            es_derecho = True
            es_scrip = False
        else:
            # Fallback: fila A con importe=0 y gastos=0 también es scrip
            es_scrip = (tipo == "A" and importe_eur == ZERO and gastos_eur == ZERO)

        # Opciones ejercidas
        ej_raw = (row[7] if len(row) > 7 else None)
        ejercicio_opcion = (
            ej_raw is not None
            and str(ej_raw).strip().upper() == "Y"
        )
        strike_raw = (row[8] if len(row) > 8 else None)
        strike = _parse_es_decimal(strike_raw) if strike_raw not in (None, "", " ") else None
        prima_raw = (row[9] if len(row) > 9 else None)
        prima_eur = _parse_es_decimal(prima_raw) if prima_raw not in (None, "", " ") else None
        tipo_opcion = (str(row[10]).strip().upper() if len(row) > 10 and row[10] is not None else "")
        # Columna 12 (índice 11) = Broker. XLSX maestros previos a la incorporación
        # de IBKR no la tienen → se queda en '' por compatibilidad.
        broker = (str(row[11]).strip() if len(row) > 11 and row[11] is not None else "")

        # Columna 16 (índice 15) = Tipo activo. XLSX previos no la tienen →
        # fallback al classifier sobre (isin, nombre, broker).
        tipo_activo_raw = (str(row[15]).strip() if len(row) > 15 and row[15] is not None else "")
        _ta_low = tipo_activo_raw.lower()
        _ta_up  = tipo_activo_raw.upper()
        if _ta_up in ("ETF", "FONDO", "IIC", "FUND"):
            instrument_type = "ETF"
            instrument_type_unknown = False
        elif _ta_low in ("acción", "accion", "stock", "share"):
            instrument_type = "STOCK"
            instrument_type_unknown = False
        elif _ta_low in ("derivado", "derivative", "estructurado"):
            instrument_type = "DERIVATIVE"
            instrument_type_unknown = False
        elif _ta_low in ("cripto", "crypto", "criptomoneda"):
            instrument_type = "CRYPTO"
            instrument_type_unknown = False
        elif _ta_low in ("bono", "bond", "obligación", "obligacion"):
            instrument_type = "BOND"
            instrument_type_unknown = False
        elif _ta_up == "ETC":
            instrument_type = "ETC"
            instrument_type_unknown = False
        else:
            # Fallback: el classifier intenta clasificar desde el nombre+ISIN.
            try:
                from instrument_classifier import classify_isin
                instrument_type, _, instrument_type_unknown = classify_isin(
                    isin, nombre, broker=broker,
                )
            except Exception:
                instrument_type = "STOCK"
                instrument_type_unknown = False

        op = {
            "tipo": tipo,
            "isin": isin,
            "nombre": nombre,
            "fecha": fecha,
            "cantidad": cantidad,
            "importe_eur": importe_eur,
            "gastos_eur": gastos_eur,
            "es_scrip": es_scrip,
            "es_derecho": es_derecho,
            "ejercicio_opcion": ejercicio_opcion,
            "strike": strike,
            "prima_eur": prima_eur,
            "tipo_opcion": tipo_opcion,
            "broker": broker,
            "instrument_type": instrument_type,
            "instrument_type_unknown": instrument_type_unknown,
            "_row": row_num,
        }
        ops.append(op)

    return ops


def _read_rows_csv(filepath: Path):
    """Itera (num_fila, lista_celdas) sobre un CSV legacy del proyecto."""
    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader, None)   # cabecera
        for row_num, row in enumerate(reader, start=2):
            yield row_num, row


def _read_rows_xlsx(filepath: Path, apply_year_filter: bool = True):
    """Itera (num_fila, lista_celdas) sobre la hoja "Operaciones" del XLSX
    maestro (excel_cartera). Localiza la cabecera buscando 'Tipo' en col A.

    IMPORTANTE — filtro defensivo por año del fichero:

    Cuando el flujo web procesa varios `cartera_valores_irpf_YYYY.xlsx`
    (uno por año) y `excel_cartera` embebe filas de años previos como
    referencia visual, filtrar por el `YYYY` del nombre evita duplicaciones
    al hacer la unión global de operaciones.

    Pero el flujo CLI con histórico completo genera **un único XLSX**
    multi-año (todas las ops de 2017-2025 en `cartera_valores_irpf_2025.xlsx`).
    En ese caso el filtro descartaría las compras anteriores → FIFO mutilado.

    Como ambos casos son indistinguibles desde el XLSX mismo (el segundo
    también tiene >1 año), el caller (`calcular_fifo`) decide en función
    del número de ficheros y propaga la decisión vía `apply_year_filter`.

    Si el nombre no sigue el patrón `_YYYY.xlsx`, no se filtra (compatibilidad
    con ficheros personalizados).
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError(
            "openpyxl no está instalado: necesario para leer XLSX de cartera"
        ) from exc

    # Extraer el año del nombre del fichero (YYYY de 4 dígitos)
    import re as _re
    m = _re.search(r"_(\d{4})\.xlsx$", str(filepath))
    year_target = int(m.group(1)) if m else None

    # data_only=True → si el usuario editó costes y guardó en Excel, leemos
    # los valores resueltos (no las fórmulas). Si nunca se abrió en Excel,
    # las celdas con fórmulas devolverán None → no afecta a Operaciones que
    # usa valores literales, no fórmulas.
    wb = load_workbook(filepath, data_only=True, read_only=True)
    ws = wb["Operaciones"] if "Operaciones" in wb.sheetnames else wb.worksheets[0]

    # Localizar fila de cabecera (la que tiene "Tipo" en col A, "ISIN" en B)
    # y leer toda la cabecera para detectar el formato del XLSX (viejo de 12
    # cols o nuevo de 15 cols con desglose de gastos).
    header_row = None
    header_cells: list = []
    for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not row or len(row) < 2:
            continue
        a = str(row[0]).strip().lower() if row[0] is not None else ""
        b = str(row[1]).strip().lower() if row[1] is not None else ""
        if a == "tipo" and b == "isin":
            header_row = r_idx
            header_cells = list(row)
            break
    if header_row is None:
        wb.close()
        return

    # Detectar formato:
    #   Viejo (12 cols): Tipo, ISIN, Denom, Fecha, Cant, Coste, Gastos,
    #                    Ej.opción, Strike, Prima, Tipo opción, Broker
    #   Nuevo (15 cols): inserta Coms. broker, AutoFX, Tasas ext. en cols 8-10,
    #                    desplazando Ej.opción/Strike/Prima/Tipo opción/Broker
    #                    a 11-15. Detectamos por la presencia de "Coms" en
    #                    cualquier celda entre la 8 y la 10.
    formato_nuevo = any(
        h is not None
        and "coms" in str(h).strip().lower()
        and "broker" in str(h).strip().lower()
        for h in header_cells[7:11]
    )

    # Si el caller pidió no filtrar (XLSX único multi-año), o el nombre no
    # tiene patrón _YYYY.xlsx, el filtro queda inactivo en este fichero.
    filter_active = apply_year_filter and year_target is not None

    for r_idx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
        row_list = list(row)
        # Normalizar al esquema canónico (12 cols del formato viejo) para que
        # el caller pueda seguir usando índices fijos. En el formato nuevo,
        # eliminamos las 3 columnas insertadas (Coms. broker, AutoFX, Tasas ext.)
        # — esa información ya está sumada en col Gastos (idx 6) y en la hoja
        # Tasas_externas para auditoría visual, no se usa en el motor FIFO.
        # Si el XLSX trae además la columna 16 (Tipo activo), la conservamos
        # en idx 15 para que parse_csv_irpf la lea, añadiendo 3 cols None
        # de padding (las cols 12-14 del esquema canónico no tienen contenido).
        if formato_nuevo:
            tipo_activo_val = row_list[15] if len(row_list) > 15 else None
            row_list = row_list[:7] + row_list[10:15] + [None, None, None, tipo_activo_val]
        if filter_active and len(row_list) >= 4 and row_list[3] is not None:
            try:
                if _parse_es_date(row_list[3]).year != year_target:
                    continue
            except Exception:
                pass
        yield r_idx, row_list
    wb.close()


# ── Motor FIFO ───────────────────────────────────────────────────────────────

class FIFOTracker:
    """Tracker FIFO multi-año para cálculo de G/P patrimoniales.

    Alimentar con operaciones en orden cronológico. Las compras crean lotes,
    las ventas los consumen por FIFO, los splits ajustan lotes existentes.
    """

    def __init__(self, persistir_shorts_al_cierre: bool = False):
        # `persistir_shorts_al_cierre` (default False, comportamiento legacy):
        # los _open_shorts sin cubrir al final del proceso se reclasifican
        # como orphan_sales y se emite warning "Venta corta sin cobertura".
        # Cuando es True (modo cross-año), los shorts se conservan en
        # FIFOResults.shorts_pendientes para que el orquestador los persista
        # en un sidecar shorts_pendientes_YYYY.json y los restaure en el
        # tracker del año siguiente vía `restore_open_shorts`.
        self._persistir_shorts_al_cierre = persistir_shorts_al_cierre
        # {isin: deque[Lot]} — lotes abiertos ordenados por fecha (FIFO)
        self._lots: dict[str, deque[Lot]] = defaultdict(deque)
        # Todas las compras registradas (para regla 2 meses)
        self._all_buys: list[dict] = []
        # Nombres base de subyacentes con cualquier compra (para auto-detectar
        # scrip dividend: un RTS-sale de ACS solo es válido si el usuario
        # compró/compra ACS en algún momento del histórico).
        self._underlyings_seen: set[str] = set()
        # Matches generados
        self._matches: list[FIFOMatch] = []
        # Warnings
        self._warnings: list[str] = []
        # Ventas sin lotes (CSV incompleto): permiten al frontend mostrar
        # un modal de aviso pre-pago con la lista exacta de operaciones
        # afectadas y recomendar al usuario subir más años de extracto.
        self._orphan_sales: list[OrphanSale] = []
        # Posiciones cortas abiertas. Clave (isin, broker). Cuando una venta
        # excede el inventario se abre un corto en lugar de orphan; cuando
        # una compra posterior del mismo (isin, broker) llega, se cubre
        # primero el corto antes de añadir a `_lots`. Si al final del proceso
        # quedan cortos sin cubrir, se reclasifican como orphans (CSV
        # incompleto, no fueron en realidad cortos).
        self._open_shorts: dict[tuple[str, str], deque[OpenShort]] = defaultdict(deque)
        # Contador incremental para asignar lote_id único a cada lote creado.
        # Los FIFOMatch heredan el lote_id del lote consumido → el XLSX maestro
        # puede emitir fórmulas prorrateadas que enlazan la fila G/P por valor
        # con la fila de la compra origen en hoja Operaciones.
        self._next_lote_id: int = 1
        # Pérdidas diferidas (Art. 33.5.f LIRPF, último párrafo): mapeo
        # lote_id_recompra → PerdidaDiferida. Se rellena en
        # `_apply_regla_2_meses` cuando una pérdida queda no imputable por
        # recompra dentro de ventana 2M; se consume en `_apply_perdidas_diferidas`
        # cuando el lote recomprado se transmite sin nueva recompra
        # ("transmisión definitiva" — DGT V3282-18).
        self._perdidas_diferidas: dict[int, PerdidaDiferida] = {}

    def process(self, op: dict) -> list[FIFOMatch]:
        """Procesa una operación. Devuelve matches generados (solo para ventas)."""
        tipo = op["tipo"]
        if tipo == "A":
            self._add_buy(op)
            return []
        elif tipo == "T":
            return self._process_sell(op)
        elif tipo == "SP":
            self._apply_split(op)
            return []
        else:
            self._warnings.append(f"Tipo desconocido: {tipo} en fila {op.get('_row', '?')}")
            return []

    def process_all(self, ops: list[dict]) -> None:
        """Procesa todas las operaciones (deben estar ordenadas cronológicamente)."""
        for op in ops:
            self.process(op)
        # Cortos sin cubrir al final del proceso. Dos comportamientos según
        # el modo del tracker:
        #
        # (a) Modo legacy (`persistir_shorts_al_cierre=False`, default):
        #     reclasificar como orphans. Asume que la falta de cobertura es
        #     CSV incompleto en años posteriores o detección errónea — el
        #     usuario revisa manualmente.
        #
        # (b) Modo cross-año (`persistir_shorts_al_cierre=True`): los shorts
        #     quedan vivos en `_open_shorts` y se exponen en
        #     FIFOResults.shorts_pendientes. El orquestador externo los
        #     serializa al sidecar `shorts_pendientes_YYYY.json` y los
        #     restaura en el tracker del año siguiente vía
        #     `restore_open_shorts()`. Así una venta corta abierta en 2024
        #     y cubierta en 2025 se procesa correctamente.
        if not self._persistir_shorts_al_cierre:
            for shorts in self._open_shorts.values():
                for short in shorts:
                    self._warnings.append(
                        f"Venta corta sin cobertura: {short.nombre} ({short.isin}) "
                        f"{_format_es_date(short.fecha_apertura)} x{short.cantidad} "
                        f"— sin compra de cobertura posterior; reclasificada como "
                        f"venta sin lotes"
                    )
                    self._orphan_sales.append(OrphanSale(
                        isin=short.isin,
                        nombre=short.nombre,
                        fecha=short.fecha_apertura,
                        cantidad=short.cantidad,
                        importe_eur=short.importe_eur,
                        broker=short.broker,
                        parcial=False,
                        cantidad_faltante=short.cantidad,
                    ))
            self._open_shorts.clear()
        # En modo cross-año, get_results() leerá self._open_shorts intacto y
        # los devolverá como shorts_pendientes (ver más abajo).
        # Aplicar regla 2 meses al final (necesita ver todas las compras).
        self._apply_regla_2_meses()
        # Aplicar integración diferida (Art. 33.5.f LIRPF último párrafo):
        # detectar transmisiones definitivas y aflorar pérdidas atadas a
        # los lotes recomprados. Debe ir DESPUÉS de _apply_regla_2_meses
        # porque depende del registro de pérdidas no imputables.
        self._apply_perdidas_diferidas()
        # Sweep final: descartar pérdidas diferidas latentes cuyo lote-
        # recompra ya no está en inventario (eventos corporativos,
        # delistings, traspasos, CSV cortado). Emite warning para que el
        # usuario las revise manualmente — evita falsos positivos en
        # la sección "latentes" del informe.
        self._sweep_perdidas_diferidas_huérfanas()

    def get_results(self) -> FIFOResults:
        """Devuelve resultados completos: matches + posiciones abiertas."""
        # Pérdidas diferidas que quedan latentes al cierre del histórico
        # procesado: aún atadas a lotes en cartera (no transmitidos o
        # transmitidos pero con nueva recompra en 2M que prolonga el
        # diferimiento). El frontend las muestra como informativo.
        latentes = list(self._perdidas_diferidas.values())
        # Shorts vivos al cierre — solo en modo cross-año (persistir=True).
        # En modo legacy ya se vaciaron en process_all reclasificándolos
        # como orphan_sales.
        shorts_pendientes: list[OpenShort] = []
        if self._persistir_shorts_al_cierre:
            for queue in self._open_shorts.values():
                shorts_pendientes.extend(queue)
        return FIFOResults(
            matches=list(self._matches),
            positions=self._get_all_summaries(),
            warnings=list(self._warnings),
            orphan_sales=list(self._orphan_sales),
            perdidas_diferidas_latentes=latentes,
            shorts_pendientes=shorts_pendientes,
        )

    def restore_open_shorts(self, shorts: list) -> None:
        """Restaura posiciones cortas abiertas heredadas de un ejercicio
        previo. El caller pasa una lista de OpenShort (o dicts con las
        mismas claves) cargada típicamente de un sidecar
        `shorts_pendientes_YYYY.json` del año anterior.

        Debe llamarse ANTES de `process_all` para que las compras de
        cobertura del año actual cierren primero los shorts heredados.

        Acepta tanto OpenShort dataclass como dict con keys equivalentes
        (para facilitar la deserialización del JSON sin tener que
        reconstruir el dataclass en el caller).
        """
        for s in shorts:
            if isinstance(s, OpenShort):
                short_obj = s
            else:
                # Dict de sidecar JSON — reconstruir OpenShort.
                fecha = s.get('fecha_apertura')
                if isinstance(fecha, str):
                    fecha = date.fromisoformat(fecha)
                short_obj = OpenShort(
                    isin=s.get('isin', ''),
                    nombre=s.get('nombre', ''),
                    fecha_apertura=fecha,
                    cantidad=Decimal(str(s.get('cantidad', '0'))),
                    importe_eur=Decimal(str(s.get('importe_eur', '0'))),
                    gastos_eur=Decimal(str(s.get('gastos_eur', '0'))),
                    broker=s.get('broker', ''),
                    ejercicio_opcion=bool(s.get('ejercicio_opcion', False)),
                    instrument_type=s.get('instrument_type', 'STOCK'),
                    instrument_type_unknown=bool(
                        s.get('instrument_type_unknown', False)),
                )
            key = (short_obj.isin, short_obj.broker or '')
            self._open_shorts[key].append(short_obj)

    # ── Compras ──────────────────────────────────────────────────────────

    def _add_buy(self, op: dict) -> None:
        """Añade un lote de compra al inventario.

        Caso especial — acciones totalmente liberadas (scrip TYPE A,
        Art. 37.1.a §4 LIRPF + Manual Cartera de Valores §4.3): si la
        compra viene marcada `es_scrip=True` con coste 0 y ya hay lotes
        previos del mismo ISIN, prorrateamos el coste agregado de los
        lotes existentes entre el total nuevo de acciones (existentes +
        liberadas). Esto reproduce la fórmula de la AEAT — coste_total /
        (n_antiguas + n_liberadas) — sin que el usuario tenga que tocar
        nada y deja el lote AL listo para consumirse al precio prorrateado
        en una venta posterior.

        Si NO hay lotes previos (el subyacente se compró fuera del periodo
        cargado, p.ej. en años anteriores no procesados), el lote AL queda
        con coste 0; al venderlo después, la G/P será toda ganancia. Es
        la limitación esperada y consistente con el código AEAT — se avisa
        al usuario en el informe corporativo.
        """
        isin = op["isin"]
        cantidad = op["cantidad"]
        importe = op["importe_eur"]
        gastos = op["gastos_eur"]
        coste_total = importe + gastos
        broker = op.get("broker", "")

        # ── Cobertura de venta corta ─────────────────────────────────────
        # Si hay posiciones cortas abiertas del mismo (isin, broker), esta
        # compra cubre el corto antes de crear lote nuevo. Marcador
        # explícito `_es_corto_cobertura` opt-in para evitar interferir
        # con FIFO normal en escenarios de orphan legítimo (CSV incompleto).
        if op.get("_es_corto_cobertura"):
            cantidad_restante = self._cubrir_cortos(op, isin, broker, cantidad,
                                                    importe, gastos)
            if cantidad_restante == 0:
                # Todo se ha consumido cubriendo cortos — no queda lote
                # nuevo que añadir al inventario.
                return
            # Sólo continúa con cantidad sobrante (raro pero posible si la
            # compra cubre más que el corto y deja remanente para crear lote).
            cantidad = cantidad_restante
            # Prorratear importe y gastos al remanente.
            if op["cantidad"] > 0:
                ratio = cantidad / op["cantidad"]
                importe = (op["importe_eur"] * ratio).quantize(CENT, ROUND_HALF_UP)
                gastos = (op["gastos_eur"] * ratio).quantize(CENT, ROUND_HALF_UP)
                coste_total = importe + gastos

        es_scrip_flag = op.get("es_scrip", False)
        if (es_scrip_flag and coste_total == ZERO and cantidad > 0
                and self._lots.get(isin)):
            existing = [l for l in self._lots[isin] if l.cantidad > 0]
            if existing:
                coste_existente = sum((l.coste_total_eur for l in existing), ZERO)
                cantidad_existente = sum((l.cantidad for l in existing), Decimal("0"))
                cantidad_total = cantidad_existente + cantidad
                if cantidad_total > 0:
                    # La acción liberada no aporta coste: el coste de la
                    # posición se reparte sobre (existentes + nuevas). El TOTAL
                    # se conserva exacto — repartimos por cantidad y ajustamos
                    # el remanente de redondeo en el último lote para que la
                    # suma siga siendo `coste_existente` (no cuantizar el
                    # unitario antes de multiplicar, que perdía céntimos).
                    coste_unit_prorr = (coste_existente / cantidad_total).quantize(
                        CENT, ROUND_HALF_UP)
                    coste_lote_al = (coste_existente
                                     - (coste_unit_prorr * cantidad_existente)).quantize(
                                         CENT, ROUND_HALF_UP)
                    asignado = ZERO
                    for l in existing:
                        l.coste_unitario_eur = coste_unit_prorr
                        l.coste_total_eur = (coste_unit_prorr * l.cantidad).quantize(
                            CENT, ROUND_HALF_UP)
                        asignado += l.coste_total_eur
                    # El lote AL absorbe el remanente: coste_existente - lo ya
                    # asignado a los lotes existentes. Conserva el total.
                    coste_total = (coste_existente - asignado).quantize(
                        CENT, ROUND_HALF_UP)

        if cantidad > 0:
            coste_unitario = coste_total / cantidad
        else:
            coste_unitario = ZERO

        lote_id = self._next_lote_id
        self._next_lote_id += 1
        # Anotar el id en el dict de la op para que el caller (generar_irpf /
        # excel_cartera) pueda mapearlo a la fila de la hoja Operaciones.
        op["_lote_id"] = lote_id

        lot = Lot(
            isin=isin,
            nombre=op["nombre"],
            fecha_compra=op["fecha"],
            cantidad=cantidad,
            cantidad_original=cantidad,
            coste_unitario_eur=coste_unitario,
            coste_total_eur=coste_total,
            gastos_eur=gastos,
            es_scrip=es_scrip_flag,
            ejercicio_opcion=op.get("ejercicio_opcion", False),
            strike=op.get("strike"),
            prima_eur=op.get("prima_eur"),
            tipo_opcion=op.get("tipo_opcion", ""),
            broker=op.get("broker", ""),
            instrument_type=op.get("instrument_type", "STOCK"),
            instrument_type_unknown=op.get("instrument_type_unknown", False),
            lote_id=lote_id,
        )
        self._lots[isin].append(lot)
        # Registrar para regla 2 meses (`nombre` se usa también en el
        # tracking de pérdidas diferidas atadas al lote_id_recompra).
        self._all_buys.append({"isin": isin, "fecha": op["fecha"],
                                "nombre": op["nombre"],
                                "cantidad": cantidad, "lote_id": lote_id})
        # Registrar subyacente (no-RTS) para auto-detección de scrip dividend
        if not _is_rts(op["nombre"]):
            self._underlyings_seen.add(_base_company(op["nombre"]))

    def _cubrir_cortos(self, op: dict, isin: str, broker: str,
                       cantidad_compra: Decimal, importe_compra: Decimal,
                       gastos_compra: Decimal) -> Decimal:
        """Cubre posiciones cortas abiertas de (isin, broker) con esta compra.

        Genera FIFOMatch por cada short cubierto. La G/P realizada se computa
        como `importe_corto − gastos_venta_corto − coste_cobertura` donde el
        coste de cobertura incluye importe + gastos de la compra (Art. 35.1.b
        LIRPF: gastos inherentes a la adquisición suman al coste).

        Devuelve la cantidad sobrante de la compra (la que no fue consumida
        por cobertura y va a crear lote nuevo).
        """
        shorts = self._open_shorts.get((isin, broker))
        if not shorts:
            return cantidad_compra

        if cantidad_compra <= 0:
            return cantidad_compra

        # Precio unitario de cobertura (para prorratear a cada short).
        precio_compra_unit = importe_compra / cantidad_compra
        gastos_compra_unit = gastos_compra / cantidad_compra

        restante = cantidad_compra
        while restante > 0 and shorts:
            short = shorts[0]
            consumir = min(restante, short.cantidad)

            # Prorratear el corto a la cantidad consumida.
            if short.cantidad > 0:
                importe_corto_tramo = (short.importe_eur * consumir / short.cantidad).quantize(
                    CENT, ROUND_HALF_UP)
                gastos_corto_tramo = (short.gastos_eur * consumir / short.cantidad).quantize(
                    CENT, ROUND_HALF_UP)
            else:
                importe_corto_tramo = ZERO
                gastos_corto_tramo = ZERO

            # Prorratear la compra al tramo cubierto.
            importe_cobertura_tramo = (precio_compra_unit * consumir).quantize(
                CENT, ROUND_HALF_UP)
            gastos_cobertura_tramo = (gastos_compra_unit * consumir).quantize(
                CENT, ROUND_HALF_UP)
            coste_adq_tramo = importe_cobertura_tramo + gastos_cobertura_tramo

            gp = importe_corto_tramo - gastos_corto_tramo - coste_adq_tramo

            match = FIFOMatch(
                isin=isin,
                nombre=short.nombre,
                # fecha_compra = fecha de cobertura (cierre del corto)
                fecha_compra=op["fecha"],
                # fecha_venta = fecha de apertura del corto (la venta inicial)
                fecha_venta=short.fecha_apertura,
                cantidad=consumir,
                coste_adquisicion=coste_adq_tramo,
                importe_transmision=importe_corto_tramo,
                gastos_venta=gastos_corto_tramo,
                gastos_compra=gastos_cobertura_tramo,
                ganancia_perdida=gp,
                ejercicio_fiscal=op["fecha"].year,
                # G/P se realiza al cerrar el corto, por tanto se imputa al
                # ejercicio fiscal del cierre (cobertura).
                es_scrip=False,
                ejercicio_opcion=short.ejercicio_opcion,
                es_derecho=False,
                broker_compra=broker,
                broker_venta=short.broker,
                instrument_type=short.instrument_type,
                instrument_type_unknown=short.instrument_type_unknown,
                lote_id=0,
                es_corto=True,
            )
            self._matches.append(match)

            short.cantidad -= consumir
            short.importe_eur -= importe_corto_tramo
            short.gastos_eur -= gastos_corto_tramo
            restante -= consumir

            if short.cantidad <= 0:
                shorts.popleft()

        return restante

    # ── Ventas ───────────────────────────────────────────────────────────

    def _process_sell(self, op: dict) -> list[FIFOMatch]:
        """Consume lotes FIFO para una venta. Devuelve matches."""
        isin = op["isin"]
        cantidad_vender = op["cantidad"]
        importe_total = op["importe_eur"]
        gastos_total = op["gastos_eur"]
        fecha_venta = op["fecha"]
        nombre = op["nombre"]

        # ── Apertura de venta corta (validada contra inventario) ────────
        # Marcador `_es_corto_apertura` es TENTATIVO: el caller lo activa
        # cuando detecta un patrón candidato (par venta+compra mismo día
        # con Order ID, qty simétrica, centros distintos, precio cercano).
        # Pero un trade intra-día genuino encaja en ese patrón sin ser
        # corto. La validación final la hace el motor con el inventario
        # real: si la venta tiene lots disponibles, ignora el flag y
        # procesa como venta normal contra los lots. Si no, abre short.
        if op.get("_es_corto_apertura"):
            lots_actuales = self._lots.get(isin)
            inv_disponible = sum(
                (lot.cantidad for lot in (lots_actuales or [])), Decimal("0"))
            if inv_disponible < cantidad_vender:
                self._open_shorts[(isin, op.get("broker", ""))].append(OpenShort(
                    isin=isin,
                    nombre=nombre,
                    fecha_apertura=fecha_venta,
                    cantidad=cantidad_vender,
                    importe_eur=importe_total,
                    gastos_eur=gastos_total,
                    broker=op.get("broker", ""),
                    ejercicio_opcion=op.get("ejercicio_opcion", False),
                    instrument_type=op.get("instrument_type", "STOCK"),
                    instrument_type_unknown=op.get("instrument_type_unknown", False),
                ))
                return []
            # Si hay inventario, el flag era falso positivo (probable trade
            # intra-día). Caemos al flujo FIFO normal.

        lots = self._lots.get(isin)
        if not lots:
            # Auto-detección scrip dividend TYPE B (Art. 37.1.a LIRPF):
            # Un T sobre ISIN con nombre de derecho (RTS/RIGHTS/DERECHOS) sin
            # compra previa solo puede ser una asignación gratuita recibida via
            # acción corporativa. Si el usuario tiene/tuvo el subyacente, la
            # venta genera G/P con coste de adquisición 0 (casillas 341-346 en Renta 2025).
            #
            # Dos vías de detección (cualquiera sirve):
            # 1. Marcador '[SCRIP-B' en el nombre — generado por generar_irpf.py
            #    cuando clasifica el derecho (manual o auto). Es fiable incluso
            #    cuando el subyacente se compró en un año previo no cargado.
            # 2. Nombre con patrón RTS + subyacente visto en el histórico
            #    (funciona solo si se cargan los CSVs de años previos donde se
            #    compró el subyacente).
            tiene_marca_scrip = "[SCRIP-B" in nombre
            base_rts = _base_company(nombre)
            subyacente_match = bool(base_rts) and len(base_rts) >= 4 and any(
                u == base_rts or u.startswith(base_rts) or base_rts.startswith(u)
                for u in self._underlyings_seen if u
            )
            if tiene_marca_scrip or (_is_rts(nombre) and subyacente_match):
                gp = importe_total - gastos_total
                match = FIFOMatch(
                    isin=isin,
                    nombre=nombre,
                    fecha_compra=fecha_venta,   # sin lote real: asignación gratuita
                    fecha_venta=fecha_venta,
                    cantidad=cantidad_vender,
                    coste_adquisicion=ZERO,
                    importe_transmision=importe_total,
                    gastos_venta=gastos_total,
                    ganancia_perdida=gp,
                    ejercicio_fiscal=fecha_venta.year,
                    es_scrip=True,
                    es_derecho=op.get("es_derecho", False) or _is_rts(nombre),
                    broker_compra="",
                    broker_venta=op.get("broker", ""),
                    instrument_type=op.get("instrument_type", "STOCK"),
                    instrument_type_unknown=op.get("instrument_type_unknown", False),
                )
                self._matches.append(match)
                return [match]
            self._warnings.append(
                f"Venta sin lotes: {nombre} ({isin}) {_format_es_date(fecha_venta)} "
                f"x{cantidad_vender} — no hay compras previas registradas"
            )
            self._orphan_sales.append(OrphanSale(
                isin=isin,
                nombre=nombre,
                fecha=fecha_venta,
                cantidad=cantidad_vender,
                importe_eur=importe_total,
                broker=op.get("broker", ""),
                parcial=False,
                cantidad_faltante=cantidad_vender,
            ))
            return []

        # Precio unitario de venta (para prorratear a cada match)
        if cantidad_vender > 0:
            precio_venta_unit = importe_total / cantidad_vender
            gastos_venta_unit = gastos_total / cantidad_vender
        else:
            precio_venta_unit = ZERO
            gastos_venta_unit = ZERO

        matches: list[FIFOMatch] = []
        restante = cantidad_vender
        # Acumuladores para no perder céntimos en ventas multi-lote: el
        # quantize por tramo acumulaba ±1 céntimo por lote consumido y la
        # suma de tramos no cuadraba con el importe real de la operación
        # (auditoría 2026-06-11, [BAJO] prorrateo multi-tramo).
        importe_asignado = ZERO
        gastos_asignado = ZERO

        while restante > 0 and lots:
            lot = lots[0]
            consumir = min(restante, lot.cantidad)

            if consumir == restante:
                # Último tramo de la venta (totalmente casada): asignar el
                # RESTO exacto para que Σ tramos == importe/gastos totales.
                importe_tramo = importe_total - importe_asignado
                gastos_tramo = gastos_total - gastos_asignado
            else:
                importe_tramo = (precio_venta_unit * consumir).quantize(CENT, ROUND_HALF_UP)
                gastos_tramo = (gastos_venta_unit * consumir).quantize(CENT, ROUND_HALF_UP)
            importe_asignado += importe_tramo
            gastos_asignado += gastos_tramo

            if consumir == lot.cantidad:
                # Lote totalmente consumido: coste = remanente exacto del
                # lote (no quantize(unit × qty), que dejaba céntimos
                # huérfanos en lotes consumidos a lo largo de varias ventas).
                coste_tramo = lot.coste_total_eur
            else:
                coste_tramo = (lot.coste_unitario_eur * consumir).quantize(CENT, ROUND_HALF_UP)
            # Gastos de compra prorrateados al tramo. Ya están sumados dentro
            # de `coste_unitario_eur` (que es coste_total / cantidad_original);
            # los exponemos aparte para auditoría con informes de broker que
            # excluyen comisiones de compra (DeGiro Annual Report).
            if lot.cantidad_original > 0:
                gastos_compra_tramo = (lot.gastos_eur * consumir / lot.cantidad_original).quantize(
                    CENT, ROUND_HALF_UP)
            else:
                gastos_compra_tramo = ZERO
            gp = importe_tramo - gastos_tramo - coste_tramo

            match = FIFOMatch(
                isin=isin,
                nombre=nombre,
                fecha_compra=lot.fecha_compra,
                fecha_venta=fecha_venta,
                cantidad=consumir,
                coste_adquisicion=coste_tramo,
                importe_transmision=importe_tramo,
                gastos_venta=gastos_tramo,
                gastos_compra=gastos_compra_tramo,
                ganancia_perdida=gp,
                ejercicio_fiscal=fecha_venta.year,
                es_scrip=lot.es_scrip,
                ejercicio_opcion=lot.ejercicio_opcion or op.get("ejercicio_opcion", False),
                es_derecho=op.get("es_derecho", False),
                broker_compra=lot.broker,
                broker_venta=op.get("broker", ""),
                instrument_type=lot.instrument_type,
                instrument_type_unknown=lot.instrument_type_unknown,
                amortizacion_inferida=bool(op.get("_amortizacion_inferida", False)),
                lote_id=lot.lote_id,
                cantidad_lote_original=lot.cantidad_original,
            )
            matches.append(match)

            lot.cantidad -= consumir
            # Mantener coste_total_eur sincronizado con la cantidad viva. El
            # prorrateo de scrip (es_scrip, _add_buy) suma coste_total_eur de
            # los lotes vivos; si no se decrementa aquí, incluiría el coste de
            # acciones ya vendidas y duplicaría la base de coste del scrip.
            # Decrementamos el REMANENTE real (no quantize(unit × cantidad))
            # para que los céntimos del lote no se evaporen entre ventas.
            lot.coste_total_eur = (ZERO if lot.cantidad <= 0
                                   else lot.coste_total_eur - coste_tramo)
            restante -= consumir

            if lot.cantidad <= 0:
                lots.popleft()

        if restante > 0:
            self._warnings.append(
                f"Venta parcial sin lotes suficientes: {nombre} ({isin}) "
                f"{_format_es_date(fecha_venta)} — faltan {restante} acciones"
            )
            # Importe proporcional a la parte sin coste (precio_venta_unit
            # ya está calculado arriba para el prorrateo de matches).
            importe_orfano = (precio_venta_unit * restante).quantize(CENT, ROUND_HALF_UP)
            self._orphan_sales.append(OrphanSale(
                isin=isin,
                nombre=nombre,
                fecha=fecha_venta,
                cantidad=restante,
                importe_eur=importe_orfano,
                broker=op.get("broker", ""),
                parcial=True,
                cantidad_faltante=restante,
            ))

        self._matches.extend(matches)
        return matches

    # ── Splits ───────────────────────────────────────────────────────────

    def _apply_split(self, op: dict) -> None:
        """Ajusta lotes por split/contrasplit. El coste total no cambia
        (Art. 37.3 LIRPF); solo cambia la repartición en más/menos títulos.
        """
        isin = op["isin"]
        titulos_antiguos = op["cantidad"]       # col 5: TitulosAntiguos
        titulos_nuevos = op["importe_eur"]      # col 6: TitulosNuevos
        # nominal_antiguo = op["gastos_eur"]    # col 7: no lo necesitamos

        if titulos_antiguos <= 0 or titulos_nuevos <= 0:
            self._warnings.append(f"Split inválido para {isin}: {titulos_antiguos} → {titulos_nuevos}")
            return

        ratio = titulos_nuevos / titulos_antiguos  # >1 = split, <1 = contrasplit

        lots = self._lots.get(isin)
        if not lots:
            return

        for lot in lots:
            lot.cantidad = (lot.cantidad * ratio).quantize(Decimal("0.000001"))
            lot.cantidad_original = (lot.cantidad_original * ratio).quantize(Decimal("0.000001"))
            # El coste unitario baja/sube en la MISMA proporción que cantidad:
            # coste_unit_nuevo = coste_unit_antiguo / ratio
            # (NO usar coste_total_eur / cantidad_actual porque `cantidad_actual`
            # ya está reducido si hubo ventas previas al split — daría un
            # coste_unit inflado; Art. 37.3 LIRPF dice que el split no altera
            # el coste por acción del lote residual en términos absolutos.)
            if ratio != 0:
                lot.coste_unitario_eur = lot.coste_unitario_eur / ratio

        # Escalar también las cantidades en `_all_buys` por el mismo ratio. La
        # pérdida diferida (PerdidaDiferida.cantidad_pendiente) se inicializa
        # con `_all_buys["cantidad"]` (post-process); si no se ajusta aquí,
        # queda en unidades PRE-split mientras los matches que la consumen
        # vienen en unidades POST-split → la fracción de afloración sale mal
        # escalada (Art. 37.3 LIRPF: el split no altera el coste, pero sí el
        # número de títulos sobre el que se prorratea). `_apply_split` corre
        # cronológicamente, así que `_all_buys` solo contiene compras
        # anteriores al split — todas afectadas por él.
        for buy in self._all_buys:
            if buy["isin"] == isin:
                buy["cantidad"] = (buy["cantidad"] * ratio).quantize(Decimal("0.000001"))

    # ── Regla 2 meses ────────────────────────────────────────────────────

    def _build_buys_indexes(self) -> None:
        """Construye indices reutilizables sobre `_all_buys` para las pasadas
        post-process (`_apply_regla_2_meses` y `_apply_perdidas_diferidas`).

        Indexa una sola vez para evitar O(matches × all_buys) en cada llamada
        a `_hay_recompra_homogenea_en_2m` o al buscar un lote por id.
        Tambien precomputa `lotes_vivos` por ISIN (los lotes son inmutables
        en esta fase — `process_all` ya termino antes de llamar aqui).
        """
        # tuplas (fecha, lote_id) por ISIN, ordenadas por fecha asc.
        # Asi `_hay_recompra_homogenea_en_2m` puede usar bisect para saltar
        # directamente al rango [window_start, window_end] sin escanear toda
        # la lista (gana mucho con ISINs muy operados — NVIDIA daytrader
        # tiene ~1500 compras pero solo ~60 caen en la ventana 2M de cada
        # venta).
        self._buys_by_isin_tuples: dict[str, list[tuple[date, int]]] = defaultdict(list)
        # Paralela: solo fechas, para bisect (no podemos llamar bisect con
        # key= en Python <3.10).
        self._buys_by_isin_dates: dict[str, list[date]] = {}
        # buy dict por lote_id (para lookup O(1) de nombre/cantidad/fecha)
        self._buys_by_lote_id: dict[int, dict] = {}
        # set de lote_id vivos por ISIN (precomputado, lotes ya estaticos)
        self._lotes_vivos_por_isin: dict[str, set[int]] = {}

        for buy in self._all_buys:
            isin = buy["isin"]
            self._buys_by_isin_tuples[isin].append((buy["fecha"], buy["lote_id"]))
            self._buys_by_lote_id[buy["lote_id"]] = buy
        for isin, tups in self._buys_by_isin_tuples.items():
            tups.sort(key=lambda t: t[0])
            self._buys_by_isin_dates[isin] = [t[0] for t in tups]
        for isin, lots in self._lots.items():
            self._lotes_vivos_por_isin[isin] = {lot.lote_id for lot in lots
                                                if lot.cantidad > 0}

    def _apply_regla_2_meses(self) -> None:
        """Marca matches con pérdida donde se recompró valor homogéneo dentro
        de la ventana legal aplicable según el mercado de cotización.

        Doctrina:
          - Art. 33.5.f LIRPF: 2 meses para valores admitidos a negociación en
            mercados regulados MiFID II (acciones cotizadas, ETFs UCITS, SOCIMI
            Continuo, derivados estructurados listados, REITs/SIIC extranjeros
            en NYSE/Euronext/AMS, cripto vía RentaWEB).
          - Art. 33.5.g LIRPF: 1 año (12 meses) para valores no admitidos a
            mercado regulado — incluye SOCIMI cotizadas en BME Growth (SMN).
          - Art. 25.2 LIRPF último párrafo: regla equivalente para RCM —
            "los rendimientos negativos derivados de transmisiones de activos
            financieros, cuando el contribuyente hubiera adquirido activos
            financieros homogéneos dentro de los dos meses anteriores o
            posteriores a dichas transmisiones, se integrarán a medida que se
            transmitan los activos financieros que permanezcan". Cubre BOND y
            ETC (ambos tributan como RCM por cesión a terceros; los ETC según
            DGT V0267-25). Misma ventana de 2 meses y misma mecánica de
            diferimiento/afloración que el 33.5.f; cambia solo el anclaje
            legal, reflejado en `regla_2_meses_detalle`.

        El campo `match.regla_2_meses` se mantiene como nombre histórico
        (genérico para "regla anti-aplicación de pérdidas") aunque la ventana
        real puede ser de 1 año para SOCIMI Growth; el detalle lo deja claro.

        Usamos meses naturales (no días) conforme a la interpretación de la DGT.
        """
        from instrument_classifier import get_socimi_market

        # Construir indices reutilizables sobre _all_buys (compartidos con
        # _apply_perdidas_diferidas, llamado a continuacion).
        self._build_buys_indexes()
        buys_by_isin = self._buys_by_isin_tuples  # alias local para mantener legibilidad

        for match in self._matches:
            if match.ganancia_perdida >= 0:
                continue  # solo afecta a pérdidas

            # La regla anti-aplicación NO se aplica al cierre de una venta
            # corta (doctrina cerrada del proyecto: la 2M presupone que
            # conservas el valor en cartera tras venderlo con pérdida; en un
            # corto la "recompra" ES el cierre de la posición, no una nueva
            # tenencia). Además la ventana se anclaría en la apertura (no en
            # la realización) → sobre-tributación. Por eso se saltan.
            if getattr(match, 'es_corto', False):
                continue

            # ETCs y bonos tributan como RCM (Art. 25.2 LIRPF; ETCs según
            # DGT V0267-25), no como G/P patrimonial — la regla del 33.5.f
            # no les aplica, pero el último párrafo del Art. 25.2 contiene
            # su equivalente exacto para RCM negativos (recompra de activos
            # financieros homogéneos en ±2 meses → integración diferida).
            # Por eso NO se saltan: se marcan con la misma ventana de 2
            # meses, dejando el anclaje legal correcto en el detalle.
            isin = match.isin
            fv = match.fecha_venta

            # Ventana variable según mercado: SOCIMI Growth → 12 meses, resto → 2.
            window_months = 2
            if getattr(match, 'instrument_type', 'STOCK') == 'SOCIMI':
                if get_socimi_market(isin) == 'Growth':
                    window_months = 12

            window_start = _subtract_months(fv, window_months)
            window_end = _add_months(fv, window_months)
            window_label = "1 año" if window_months == 12 else f"{window_months} meses"

            # Triage lineal por prioridad (sin sort). La doctrina prefiere:
            #   1º. Lote POSTERIOR a la venta (recompra pura — arrastra
            #       posicion despues de la venta-perdida).
            #   2º. Lote ANTERIOR aun vivo en self._lots (no consumido por
            #       la propia venta-perdida).
            #   3º. Lote anterior ya consumido (fallback: activa la regla 2M
            #       pero ata la PD a un lote muerto, limpiado por el sweep).
            # Antes se ordenaba via `sorted` con un closure que evaluaba
            # lotes_vivos_isin por iteracion (~1.1M llamadas para 3000 ops).
            # Una pasada O(n) lo sustituye con el mismo orden de seleccion.
            lotes_vivos_isin = self._lotes_vivos_por_isin.get(isin, set())
            tups = buys_by_isin.get(isin, ())
            dates_isin = self._buys_by_isin_dates.get(isin, ())
            lo = bisect.bisect_left(dates_isin, window_start)
            hi = bisect.bisect_right(dates_isin, window_end)
            chosen_recompra_pura: tuple | None = None
            chosen_previa_viva: tuple | None = None
            chosen_previa_consumida: tuple | None = None
            for j in range(lo, hi):
                bd, bd_lote_id = tups[j]
                if bd_lote_id == match.lote_id:
                    continue
                if bd > fv:
                    chosen_recompra_pura = (bd, bd_lote_id)
                    break   # mejor prioridad — no hace falta seguir
                if chosen_previa_viva is None and bd_lote_id in lotes_vivos_isin:
                    chosen_previa_viva = (bd, bd_lote_id)
                elif chosen_previa_consumida is None:
                    chosen_previa_consumida = (bd, bd_lote_id)
            chosen = (chosen_recompra_pura
                      or chosen_previa_viva
                      or chosen_previa_consumida)

            # Recopilamos la eleccion en una lista de un solo elemento para
            # mantener la estructura del bucle posterior (que ya tiene
            # logica de skip/window — refactorizar mas profundamente seria
            # mas arriesgado de cara a la fiscalidad).
            candidatos = [chosen] if chosen is not None else []

            for (bd, bd_lote_id) in candidatos:
                if bd_lote_id == match.lote_id:
                    continue
                if window_start <= bd <= window_end:
                    match.regla_2_meses = True
                    _es_rcm = getattr(match, 'instrument_type', 'STOCK') in ('BOND', 'ETC')
                    _anclaje = ("Art. 25.2 LIRPF último párrafo (RCM)" if _es_rcm
                                else "Art. 33.5.f/g LIRPF")
                    match.regla_2_meses_detalle = (
                        f"Recompra {_format_es_date(bd)} dentro de ventana {window_label} "
                        f"(venta {_format_es_date(fv)}) — {_anclaje}"
                    )
                    importe_diferido = abs(match.ganancia_perdida)
                    origen_entry = {
                        "ejercicio": match.ejercicio_fiscal,
                        "fecha_venta": fv,
                        "importe_eur": importe_diferido,
                        "lote_origen": match.lote_id,
                    }
                    pd_existente = self._perdidas_diferidas.get(bd_lote_id)
                    if pd_existente is None:
                        # Lookup O(1) via indice precomputado en lugar de
                        # scan lineal sobre _all_buys.
                        buy_recompra = self._buys_by_lote_id.get(bd_lote_id)
                        if buy_recompra is not None:
                            nombre_lote = buy_recompra.get("nombre", match.nombre)
                            cantidad_lote = Decimal(str(buy_recompra.get("cantidad", "0")))
                        else:
                            nombre_lote = match.nombre
                            cantidad_lote = Decimal("0")
                        self._perdidas_diferidas[bd_lote_id] = PerdidaDiferida(
                            isin=isin,
                            nombre=nombre_lote,
                            importe_eur=importe_diferido,
                            cantidad_pendiente=cantidad_lote,
                            fecha_venta_origen=fv,
                            ejercicio_origen=match.ejercicio_fiscal,
                            lote_id_recompra=bd_lote_id,
                            origenes=[origen_entry],
                        )
                    else:
                        # Cadena de diferimientos: la pérdida nueva se
                        # suma a la que ya arrastraba el lote, y se
                        # añade un origen al desglose para trazabilidad.
                        pd_existente.importe_eur += importe_diferido
                        pd_existente.origenes.append(origen_entry)
                    break

    def _hay_recompra_homogenea_en_2m(self, isin: str, fecha_v: date,
                                       exclude_lote_id: int,
                                       instrument_type: str = "STOCK") -> tuple[bool, int]:
        """¿Hay alguna compra del mismo ISIN dentro de la ventana 2M
        (anterior o posterior) a `fecha_v`, excluyendo el lote `exclude_lote_id`
        (el consumido por la venta)?

        Devuelve (hay_recompra, lote_id_recompra). Si no hay, (False, 0).
        Si hay, lote_id_recompra es el de la primera recompra encontrada
        (orden de aparición en _all_buys).

        La ventana es 12 meses para SOCIMI Growth (Art. 33.5.g LIRPF),
        2 meses para el resto (Art. 33.5.f). Misma lógica que
        _apply_regla_2_meses, pero independiente de que la G/P sea
        pérdida — la usa _apply_perdidas_diferidas para detectar
        transmisiones definitivas (donde la G/P puede ser positiva).
        """
        from instrument_classifier import get_socimi_market

        window_months = 2
        if instrument_type == 'SOCIMI' and get_socimi_market(isin) == 'Growth':
            window_months = 12
        window_start = _subtract_months(fecha_v, window_months)
        window_end = _add_months(fecha_v, window_months)

        # Recolectar todos los candidatos en ventana (excluyendo el lote
        # consumido por el match). Hay tres categorías en orden de
        # preferencia para mantener la cadena 2M doctrinalmente correcta:
        #   1. VIVOS al final del proceso → la cadena sigue activa.
        #   2. MUERTOS post-fecha-del-match → un match futuro los consume y
        #      aflorará la PD en cadena cuando se procese (caso Nagarro).
        #   3. MUERTOS pre-fecha → compras previas ya consumidas, sin futuro
        #      consumo. Atar la PD aquí es archivarla en un cementerio: el
        #      sweep huérfano la perdería. Doctrinalmente equivale a falta
        #      de cadena viva → la PD debe AFLORAR en el match actual.
        # Fix del bug de PD huérfanas en daytraders con cascadas largas:
        # antes este método devolvía el primer candidato cronológico (a
        # menudo un lote pre-fecha consumido) y la PD acababa archivada
        # en un lote muerto.
        # Usar indice por ISIN si esta disponible (_build_buys_indexes ya
        # ejecutado por _apply_regla_2_meses). Fallback al scan O(N) por si
        # se llama antes (defensivo — no deberia ocurrir en el flujo normal).
        if hasattr(self, '_buys_by_isin_tuples'):
            tups = self._buys_by_isin_tuples.get(isin, ())
            dates = self._buys_by_isin_dates.get(isin, ())
            # bisect para acotar el slice a [window_start, window_end].
            # ISINs con cientos/miles de operaciones (daytraders) ahorran
            # casi todo el scan: solo iteramos los pocos buys realmente
            # dentro de ventana 2M (~60 en lugar de ~1500).
            lo = bisect.bisect_left(dates, window_start)
            hi = bisect.bisect_right(dates, window_end)
            candidatos: list[tuple[int, date]] = []
            for j in range(lo, hi):
                bd, bd_lote_id = tups[j]
                if bd_lote_id == exclude_lote_id:
                    continue
                candidatos.append((bd_lote_id, bd))
        else:
            candidatos = []
            for buy in self._all_buys:
                if buy["isin"] != isin:
                    continue
                if buy["lote_id"] == exclude_lote_id:
                    continue
                bd = buy["fecha"]
                if window_start <= bd <= window_end:
                    candidatos.append((buy["lote_id"], bd))
        if not candidatos:
            return (False, 0)
        lotes_vivos = (self._lotes_vivos_por_isin.get(isin, set())
                       if hasattr(self, '_lotes_vivos_por_isin')
                       else {lot.lote_id for lot in self._lots.get(isin, ())
                             if lot.cantidad > 0})
        vivos = [lid for lid, _ in candidatos if lid in lotes_vivos]
        if vivos:
            return (True, vivos[0])
        muertos_post = [lid for lid, bd in candidatos if bd > fecha_v]
        if muertos_post:
            return (True, muertos_post[0])
        # Solo quedan candidatos pre-fecha consumidos → no hay cadena viable.
        return (False, 0)

    def _apply_perdidas_diferidas(self) -> None:
        """Detecta transmisiones definitivas y aflora pérdidas diferidas
        atadas a lotes recomprados.

        Algoritmo (después de _apply_regla_2_meses):
          - Para cada match en orden cronológico de fecha_venta:
            - Si match.lote_id está en self._perdidas_diferidas, ese match
              consumió (parcial o totalmente) un lote que arrastraba una
              pérdida diferida.
              - Si NO hay recompra de homogéneos en ±2M de la venta:
                TRANSMISIÓN DEFINITIVA. Aflora la pérdida diferida
                proporcional a la cantidad consumida del lote. La pérdida
                aflora en el ejercicio de esta venta (NO del original).
              - Si SÍ hay recompra en 2M: la pérdida diferida NO aflora;
                se traslada al nuevo lote-recompra (cadena de
                diferimientos). Esto es independiente de que la G/P de
                este match sea positiva o negativa — la regla 33.5.f
                aplica a la mecánica del diferimiento, no al signo.

        Edge case prorrateo: si el lote recomprado de 200 acciones con
        pérdida diferida de 400 € se transmite parcialmente (50 acciones),
        la pérdida aflorada es 400 × (50/200) = 100 €, y 300 € siguen
        diferidos atados al lote (que ahora tiene 150 pendientes).
        """
        # Ordenar matches cronológicamente para procesar las transmisiones
        # en el mismo orden en que ocurrieron.
        matches_orden = sorted(self._matches, key=lambda m: m.fecha_venta)

        for match in matches_orden:
            pd = self._perdidas_diferidas.get(match.lote_id)
            if pd is None:
                continue
            if pd.cantidad_pendiente <= 0:
                continue

            # Fracción consumida del lote en este match
            consumir = min(match.cantidad, pd.cantidad_pendiente)
            if consumir <= 0:
                continue
            fraccion = (consumir / pd.cantidad_pendiente
                        if pd.cantidad_pendiente > 0 else Decimal("0"))
            importe_proporcional = (pd.importe_eur * fraccion).quantize(
                CENT, ROUND_HALF_UP)

            # ¿Es transmisión definitiva? Comprobar si hay nueva recompra
            # de homogéneos en ventana ±2M de la fecha de esta venta,
            # excluyendo el lote que se está consumiendo (el del propio
            # match — el lote recomprado original).
            hay_recompra, lote_nueva_recompra = (
                self._hay_recompra_homogenea_en_2m(
                    isin=match.isin,
                    fecha_v=match.fecha_venta,
                    exclude_lote_id=match.lote_id,
                    instrument_type=getattr(match, 'instrument_type', 'STOCK'),
                )
            )

            # Prorrateo del desglose por origen: cada entry de `pd.origenes`
            # contribuye al `importe_proporcional` según su peso relativo.
            # Si la fracción es 1.0 (consumo total), pasa el desglose
            # íntegro; si es parcial, prorratea cada origen y el ÚLTIMO se
            # lleva el resto exacto — el quantize por origen hacía que la
            # suma del desglose no cuadrara con importe_proporcional
            # (auditoría 2026-06-11, [BAJO] desglose PD).
            desglose_prorrateado = []
            asignado_desglose = ZERO
            n_origenes = len(pd.origenes)
            for i, origen in enumerate(pd.origenes):
                importe_origen = origen["importe_eur"]
                if fraccion == Decimal("1"):
                    aportacion = importe_origen
                elif i == n_origenes - 1:
                    aportacion = importe_proporcional - asignado_desglose
                else:
                    aportacion = (importe_origen * fraccion).quantize(
                        CENT, ROUND_HALF_UP)
                asignado_desglose += aportacion
                desglose_prorrateado.append({
                    "ejercicio":   origen["ejercicio"],
                    "fecha_venta": origen["fecha_venta"],
                    "importe_eur": aportacion,
                    "lote_origen": origen["lote_origen"],
                })

            if not hay_recompra:
                # Transmisión definitiva: la pérdida aflora en este match.
                match.perdida_diferida_aflorada_eur = importe_proporcional
                match.perdida_diferida_origen = (
                    f"ej. {pd.ejercicio_origen}, venta "
                    f"{_format_es_date(pd.fecha_venta_origen)} (lote #{pd.lote_id_recompra})"
                )
                match.perdida_diferida_desglose = desglose_prorrateado
                # Marca intra-anual: ciclo regla 2M completo dentro del mismo
                # ejercicio (venta original con pérdida + recompra + transmisión
                # definitiva, todo en el mismo año fiscal). El resultado fiscal
                # neto es idéntico marcando o no el flag 2M, pero la doctrina
                # exige marcar y aflorar (DGT V3282-18, mecánica por fases).
                if pd.ejercicio_origen == match.ejercicio_fiscal:
                    match.perdida_diferida_intra_anual = True
            else:
                # Cadena: la pérdida proporcional se traslada al nuevo
                # lote-recompra. Si ya hay una entrada para ese lote
                # (poco habitual pero posible), acumular.
                pd_nuevo = self._perdidas_diferidas.get(lote_nueva_recompra)
                if pd_nuevo is None:
                    # Lookup O(1) via indice precomputado (en lugar de scan
                    # lineal sobre _all_buys, que era O(matches × all_buys)
                    # acumulado en este metodo).
                    buy_nuevo = (self._buys_by_lote_id.get(lote_nueva_recompra)
                                 if hasattr(self, '_buys_by_lote_id') else None)
                    if buy_nuevo is not None:
                        cantidad_nuevo_lote = Decimal(str(buy_nuevo.get("cantidad", "0")))
                        nombre_nuevo_lote = buy_nuevo.get("nombre", match.nombre)
                    else:
                        cantidad_nuevo_lote = Decimal("0")
                        nombre_nuevo_lote = match.nombre
                    self._perdidas_diferidas[lote_nueva_recompra] = PerdidaDiferida(
                        isin=pd.isin,
                        nombre=nombre_nuevo_lote,
                        importe_eur=importe_proporcional,
                        cantidad_pendiente=cantidad_nuevo_lote,
                        fecha_venta_origen=pd.fecha_venta_origen,
                        ejercicio_origen=pd.ejercicio_origen,
                        lote_id_recompra=lote_nueva_recompra,
                        origenes=desglose_prorrateado,
                    )
                else:
                    pd_nuevo.importe_eur += importe_proporcional
                    pd_nuevo.origenes.extend(desglose_prorrateado)

            # En ambos casos, descontamos la fracción consumida del lote
            # anterior (puede agotarse y desaparecer del tracking). El
            # desglose se reduce restando lo que se ha aflorado/trasladado.
            pd.importe_eur -= importe_proporcional
            pd.cantidad_pendiente -= consumir
            # Restar la fracción consumida de cada origen
            if fraccion != Decimal("1"):
                origenes_restantes = []
                for origen, aportado in zip(pd.origenes, desglose_prorrateado):
                    resto_eur = origen["importe_eur"] - aportado["importe_eur"]
                    if resto_eur > 0:
                        origenes_restantes.append({
                            "ejercicio":   origen["ejercicio"],
                            "fecha_venta": origen["fecha_venta"],
                            "importe_eur": resto_eur,
                            "lote_origen": origen["lote_origen"],
                        })
                pd.origenes = origenes_restantes
            else:
                pd.origenes = []
            if pd.cantidad_pendiente <= 0 or pd.importe_eur <= 0:
                del self._perdidas_diferidas[match.lote_id]

    def _sweep_perdidas_diferidas_huérfanas(self) -> None:
        """Sweep defensivo: descarta pérdidas diferidas atadas a lotes que
        ya no tienen inventario en `self._lots`.

        Si un lote-recompra desapareció sin un `FIFOMatch` que lo consumiera
        (delisting, redenominación, cambio de ISIN, traspaso entre brokers,
        histórico de CSV incompleto), el motor no puede determinar
        automáticamente el ejercicio de afloración. Mostrarlo como "latente"
        sería un falso positivo (el lote no está en cartera). Emitimos
        warning para que el usuario decida si imputar la pérdida
        manualmente en el ejercicio de salida real del valor.
        """
        lotes_con_inventario: set[int] = set()
        for lots_isin in self._lots.values():
            for lot in lots_isin:
                if lot.cantidad > 0:
                    lotes_con_inventario.add(lot.lote_id)

        huérfanas = [
            (lote_id, pd) for lote_id, pd in self._perdidas_diferidas.items()
            if lote_id not in lotes_con_inventario
        ]
        for lote_id, pd in huérfanas:
            self._warnings.append(
                f"Pérdida diferida sin transmisión detectada: {pd.nombre} "
                f"({pd.isin}) — ejercicio origen {pd.ejercicio_origen}, "
                f"venta original {_format_es_date(pd.fecha_venta_origen)}, "
                f"importe diferido {pd.importe_eur} € atado al lote #{lote_id}, "
                f"pero ese lote ya no está en cartera y el motor no detectó "
                f"una venta que lo consumiera (posible delisting, cambio de ISIN, "
                f"traspaso entre brokers o histórico de CSV incompleto). "
                f"Revisar manualmente si procede imputar esta pérdida en el "
                f"ejercicio de salida real del valor."
            )
            del self._perdidas_diferidas[lote_id]

    # ── Posiciones abiertas ──────────────────────────────────────────────

    def _get_all_summaries(self) -> list[PositionSummary]:
        summaries = []
        for isin, lots in sorted(self._lots.items()):
            active = [lot for lot in lots if lot.cantidad > 0]
            if not active:
                continue

            cantidad_total = sum(l.cantidad for l in active)
            coste_total = sum(
                (l.coste_unitario_eur * l.cantidad).quantize(CENT, ROUND_HALF_UP)
                for l in active
            )
            pm = coste_total / cantidad_total if cantidad_total > 0 else ZERO

            summaries.append(PositionSummary(
                isin=isin,
                nombre=active[0].nombre,
                cantidad_total=cantidad_total,
                coste_total_eur=coste_total,
                pm_ponderado_eur=pm.quantize(CENT, ROUND_HALF_UP),
                num_lotes=len(active),
                lote_mas_antiguo=min(l.fecha_compra for l in active),
                lote_mas_reciente=max(l.fecha_compra for l in active),
                es_mixta=any(l.es_scrip for l in active) and any(not l.es_scrip for l in active),
            ))

        return summaries

    def get_position(self, isin: str) -> Optional[PositionSummary]:
        """Devuelve resumen de una posición específica."""
        for s in self._get_all_summaries():
            if s.isin == isin:
                return s
        return None


# ── Utilidades de fecha para regla 2 meses ───────────────────────────────────

def _add_months(d: date, months: int) -> date:
    """Suma meses naturales a una fecha."""
    m = d.month + months
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    # Ajustar día si el mes destino tiene menos días
    import calendar
    max_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


def _subtract_months(d: date, months: int) -> date:
    """Resta meses naturales a una fecha."""
    m = d.month - months
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    import calendar
    max_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


# ── Helpers sidecar shorts cross-año ────────────────────────────────────────

def save_shorts_sidecar(path: str | Path, ejercicio: int,
                        shorts: list) -> None:
    """Serializa la lista de OpenShort al sidecar JSON
    `shorts_pendientes_YYYY.json`. Acepta tanto OpenShort como dicts.

    El sidecar permite al motor del año siguiente restaurar el estado de
    posiciones cortas abiertas que crucen el cierre del ejercicio actual,
    vía `FIFOTracker.restore_open_shorts(shorts)`.
    """
    import json
    from datetime import datetime, timezone

    def _ser(s):
        if isinstance(s, OpenShort):
            return {
                'isin': s.isin,
                'nombre': s.nombre,
                'fecha_apertura': (s.fecha_apertura.isoformat()
                                   if s.fecha_apertura else ''),
                'cantidad': str(s.cantidad),
                'importe_eur': str(s.importe_eur),
                'gastos_eur': str(s.gastos_eur),
                'broker': s.broker,
                'ejercicio_opcion': s.ejercicio_opcion,
                'instrument_type': s.instrument_type,
                'instrument_type_unknown': s.instrument_type_unknown,
            }
        return dict(s)

    payload = {
        'schema_version': 1,
        'ejercicio': int(ejercicio),
        'fecha_generacion': datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        'shorts': [_ser(s) for s in shorts],
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_shorts_sidecar(path: str | Path) -> list:
    """Lee el sidecar y devuelve la lista de dicts de shorts. Lista vacía
    si el fichero no existe (caso normal cuando es la primera vez que se
    procesa un ejercicio con el código de cross-año activo)."""
    import json
    import os as _os
    if not _os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            payload = json.load(f)
        return list(payload.get('shorts', []))
    except Exception:
        return []


# ── Función de conveniencia ──────────────────────────────────────────────────

def calcular_fifo(csv_paths: list[str | Path],
                  corte_fecha: date | None = None,
                  shorts_iniciales: list | None = None,
                  persistir_shorts_al_cierre: bool = False) -> FIFOResults:
    """Procesa múltiples CSVs/XLSXs en orden y devuelve resultados FIFO.

    Args:
        csv_paths: Lista de rutas a cartera_valores_irpf_YYYY.{csv,xlsx}
            ordenadas cronológicamente.
        corte_fecha: Si se pasa, ignora todas las operaciones con
            `fecha > corte_fecha`. Útil cuando el CSV del usuario incluye
            datos posteriores al ejercicio que está declarando: el wrapper
            lo fija típicamente en 28/02 del año fiscal+1 para cubrir la
            ventana de la regla 2M post-año (Art. 33.5.f LIRPF) sin
            arrastrar al informe operaciones que pertenecen al ejercicio
            siguiente.
        shorts_iniciales: Lista de shorts heredados del ejercicio previo
            (cargada del sidecar `shorts_pendientes_<año-previo>.json`
            vía `load_shorts_sidecar`). Si se pasa, se restauran en el
            tracker ANTES de procesar las ops, así las compras de
            cobertura del año actual pueden cerrar shorts cross-año.
        persistir_shorts_al_cierre: Si True, los shorts no cubiertos al
            final NO se reclasifican como orphan_sales — quedan vivos en
            FIFOResults.shorts_pendientes para que el caller los serialice
            al sidecar del año actual. Default False (legacy: reclasificar
            a orphan).

    Returns:
        FIFOResults con matches, posiciones y warnings. Si se aplicó
        corte_fecha, el campo `n_ops_ignoradas` indica cuántas se
        descartaron (informativo para el frontend).
    """
    tracker = FIFOTracker(persistir_shorts_al_cierre=persistir_shorts_al_cierre)
    if shorts_iniciales:
        tracker.restore_open_shorts(shorts_iniciales)
    all_ops: list[dict] = []

    # Cuando solo hay un fichero, asumimos que contiene el histórico completo
    # (flujo CLI / consolidado externo) y desactivamos el filtro por año del
    # nombre — el filtro existe para evitar duplicados al unir N XLSX con
    # referencias cruzadas, no aplica al caso de un único XLSX.
    apply_year_filter = len(csv_paths) > 1

    for path in csv_paths:
        ops = parse_csv_irpf(path, apply_year_filter=apply_year_filter)
        all_ops.extend(ops)

    n_ignoradas = 0
    fecha_max_ignorada: date | None = None
    if corte_fecha is not None:
        ops_filtradas = []
        for op in all_ops:
            if op.get("fecha") and op["fecha"] > corte_fecha:
                n_ignoradas += 1
                if fecha_max_ignorada is None or op["fecha"] > fecha_max_ignorada:
                    fecha_max_ignorada = op["fecha"]
            else:
                ops_filtradas.append(op)
        all_ops = ops_filtradas

    # Ordenar todas las operaciones cronológicamente
    all_ops.sort(key=lambda op: (op["fecha"], 0 if op["tipo"] in ("A", "SP") else 1))

    tracker.process_all(all_ops)
    res = tracker.get_results()
    res.n_ops_ignoradas = n_ignoradas
    res.fecha_corte = corte_fecha
    res.fecha_max_ignorada = fecha_max_ignorada
    return res


def calcular_fifo_from_ops(ops_actuales: list[dict],
                           paths_anteriores: list[str | Path] | None = None,
                           return_ops: bool = False,
                           shorts_iniciales: list | None = None,
                           persistir_shorts_al_cierre: bool = False):
    """Variante de calcular_fifo que acepta operaciones in-memory.

    Útil cuando ya tenemos las operaciones del ejercicio actual procesadas
    (p.ej. tras enriquecerlas con marcadores de spin-off, scrip liberadas,
    etc.) y solo queremos cargar de disco los ficheros de años previos.

    Las ops in-memory deben tener la misma forma que las de parse_csv_irpf
    (tipo, isin, nombre, fecha, cantidad, importe_eur, gastos_eur,
    es_scrip, es_derecho, ejercicio_opcion, strike, prima_eur, tipo_opcion).

    Args:
        return_ops: si True, devuelve tupla (FIFOResults, lista_todas_las_ops).
            Cada compra (tipo A) en la lista tiene `_lote_id` anotado para que
            el caller pueda mapearla a su fila en el XLSX maestro y emitir
            fórmulas prorrateadas en la hoja G_P_por_valor.
        shorts_iniciales: ver `calcular_fifo`.
        persistir_shorts_al_cierre: ver `calcular_fifo`.
    """
    tracker = FIFOTracker(persistir_shorts_al_cierre=persistir_shorts_al_cierre)
    if shorts_iniciales:
        tracker.restore_open_shorts(shorts_iniciales)
    all_ops: list[dict] = []

    # Coherente con calcular_fifo: si solo hay un path previo, lo tratamos
    # como XLSX completo (sin filtro). Con >1 path, filtro activo para
    # evitar duplicaciones por referencias históricas embebidas.
    paths = paths_anteriores or []
    apply_year_filter = len(paths) > 1
    for path in paths:
        all_ops.extend(parse_csv_irpf(path, apply_year_filter=apply_year_filter))
    all_ops.extend(ops_actuales)

    all_ops.sort(key=lambda op: (op["fecha"], 0 if op["tipo"] in ("A", "SP") else 1))

    tracker.process_all(all_ops)
    results = tracker.get_results()
    if return_ops:
        return results, all_ops
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Uso: python motor_fiscal.py <cartera_valores_irpf_YYYY.csv> [...]")
        print("  Procesa uno o más CSVs y muestra G/P calculadas por FIFO.")
        sys.exit(1)

    csv_paths = sys.argv[1:]
    results = calcular_fifo(csv_paths)

    # ── Resumen ──
    print("=" * 80)
    print("MOTOR FISCAL FIFO — Resultados")
    print("=" * 80)

    # Warnings
    if results.warnings:
        print(f"\n⚠️  AVISOS ({len(results.warnings)}):")
        for w in results.warnings:
            print(f"  • {w}")

    # G/P por ejercicio
    totals = results.total_gp_por_ejercicio()
    deducibles = results.total_gp_deducible_por_ejercicio()
    print(f"\n📊 GANANCIAS/PÉRDIDAS POR EJERCICIO:")
    for year in sorted(totals.keys()):
        gp = totals[year]
        gp_ded = deducibles.get(year, ZERO)
        emoji = "🟢" if gp >= 0 else "🔴"
        print(f"  {emoji} {year}: {_format_eur(gp)}")
        if gp != gp_ded:
            print(f"      (deducible: {_format_eur(gp_ded)} — regla 2 meses aplica)")

    # Regla 2 meses
    r2m = results.matches_regla_2m()
    if r2m:
        print(f"\n⚠️  REGLA 2 MESES — {len(r2m)} operaciones afectadas:")
        for m in r2m:
            print(f"  • {m.nombre} ({m.isin}) venta {_format_es_date(m.fecha_venta)}")
            print(f"    Pérdida: {_format_eur(m.ganancia_perdida)} → NO DEDUCIBLE")
            print(f"    Motivo: {m.regla_2_meses_detalle}")

    # Detalle de matches por ISIN
    matches_by_isin: dict[str, list[FIFOMatch]] = defaultdict(list)
    for m in results.matches:
        matches_by_isin[m.isin].append(m)

    print(f"\n📋 DETALLE DE OPERACIONES ({len(results.matches)} matches):")
    for isin in sorted(matches_by_isin.keys()):
        matches = matches_by_isin[isin]
        nombre = matches[0].nombre
        total_gp = sum(m.ganancia_perdida for m in matches)
        emoji = "🟢" if total_gp >= 0 else "🔴"
        print(f"\n  {emoji} {nombre} ({isin}):")
        for m in matches:
            flag = " ⚠️2M" if m.regla_2_meses else ""
            scrip = " [SCRIP]" if m.es_scrip else ""
            opcion = " [OPC]" if m.ejercicio_opcion else ""
            print(
                f"    Compra {_format_es_date(m.fecha_compra)} → "
                f"Venta {_format_es_date(m.fecha_venta)} | "
                f"{m.cantidad} uds | "
                f"Coste {_format_eur(m.coste_adquisicion)} → "
                f"Venta {_format_eur(m.importe_transmision)} | "
                f"G/P: {_format_eur(m.ganancia_perdida)}"
                f"{flag}{scrip}{opcion}"
            )
        print(f"    Subtotal: {_format_eur(total_gp)}")

    # Posiciones abiertas
    if results.positions:
        print(f"\n📦 POSICIONES ABIERTAS ({len(results.positions)}):")
        print(f"  {'ISIN':<15} {'Nombre':<35} {'Cant':>8} {'PM Real':>12} {'Coste Total':>14}")
        print(f"  {'─'*15} {'─'*35} {'─'*8} {'─'*12} {'─'*14}")
        total_coste = ZERO
        for p in sorted(results.positions, key=lambda x: x.nombre):
            print(
                f"  {p.isin:<15} {p.nombre[:35]:<35} "
                f"{p.cantidad_total:>8.2f} "
                f"{_format_eur(p.pm_ponderado_eur):>12} "
                f"{_format_eur(p.coste_total_eur):>14}"
            )
            total_coste += p.coste_total_eur
        print(f"  {'':>15} {'TOTAL':>35} {'':>8} {'':>12} {_format_eur(total_coste):>14}")

    print()


if __name__ == "__main__":
    main()
