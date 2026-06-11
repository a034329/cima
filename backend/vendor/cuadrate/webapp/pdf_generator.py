"""
Generador de PDF fiscal — renderiza datos del motor FIFO en PDF via Jinja2 + weasyprint.
"""
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from jinja2 import Environment, FileSystemLoader

# weasyprint se importa tardíamente (puede no estar instalado en todos los entornos)

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Umbral de matches por ISIN para colapsar el detalle por operacion a la
# vista "lista-para-RentaWeb" (max 2 registros por ISIN: A integrable + B
# diferida 2M). Por debajo de 20, el detalle completo cabe sin saturar al
# usuario; por encima, sumar mentalmente para trasladar a la casilla 0326
# es inviable y la separacion A/B se vuelve la unica vista util.
_COLLAPSE_DETALLE_THRESHOLD = 20

# Fraccion minima de operaciones intradia (fecha_compra == fecha_venta) para
# clasificar el ISIN como "daytrading". Por debajo es "swing intensivo".
_DAYTRADING_INTRADAY_RATIO = Decimal("0.5")


def _classify_isin_pattern(matches):
    """Clasifica el patron operativo del ISIN segun n_matches y % intradia.

    Returns:
        dict con keys: pattern ("daytrading"|"swing"|"normal"),
        n_intraday, ratio_intraday.
    """
    n = len(matches)
    n_intraday = sum(1 for m in matches if m.fecha_compra == m.fecha_venta)
    ratio = (Decimal(n_intraday) / Decimal(n)) if n else Decimal("0")
    if n < _COLLAPSE_DETALLE_THRESHOLD:
        pattern = "normal"
    elif ratio >= _DAYTRADING_INTRADAY_RATIO:
        pattern = "daytrading"
    else:
        pattern = "swing"
    return {
        "pattern":        pattern,
        "n_intraday":     n_intraday,
        "ratio_intraday": ratio,
    }


def _aggregate_for_rentaweb(matches):
    """Agrega los matches de un ISIN en hasta 2 registros listos para
    teclear directamente en RentaWeb (casillas 0326-0340 / 2224-2236 / etc.):

      · Registro A — Integrable: ops sin regla 2M (G/P se imputa en el
        ejercicio). Valor de adquisicion incluye PD aflorada (forma A
        doctrinal: Art. 33.5.f LIRPF ultimo parrafo).
      · Registro B — Diferida 2M: ops con `regla_2_meses=True`. Marca el
        checkbox "No imputacion de perdidas por recompra".

    Fechas: rango FIFO consumido (MIN fecha_compra → MAX fecha_venta) por
    bloque. Convencion practica de los certificados pre-IRPF de gestores
    (Renta4, Singular, Mediolanum) — el valor de adquisicion agregado es
    invariante respecto a la fecha individual de cada lote (no hay coefs.
    de antiguedad post-1994).

    Invariante matematico: Σ G/P_integrable(matches) == reg_A.gp + reg_B.gp.
    """
    reg_A_matches = [m for m in matches if not m.regla_2_meses]
    reg_B_matches = [m for m in matches if m.regla_2_meses]

    def _build_reg(label, ms, marca_2m):
        if not ms:
            return None
        coste_fifo  = sum((m.coste_adquisicion or Decimal("0")) for m in ms)
        pd_aflorada = sum(getattr(m, "perdida_diferida_aflorada_eur",
                                   Decimal("0")) for m in ms)
        importe     = sum((m.importe_transmision or Decimal("0")) for m in ms)
        gastos      = sum((m.gastos_venta or Decimal("0")) for m in ms)
        # G/P bruta = importe − gastos − coste FIFO. G/P integrable resta
        # ademas la PD aflorada (que se suma al coste — forma A doctrinal).
        gp_bruta       = sum(m.ganancia_perdida for m in ms)
        gp_integrable  = gp_bruta - pd_aflorada
        cantidad       = sum(m.cantidad for m in ms)
        fecha_min_adq  = min(m.fecha_compra for m in ms)
        fecha_max_trans= max(m.fecha_venta  for m in ms)
        return {
            "label":              label,
            "n_matches":          len(ms),
            "marca_2m":           marca_2m,
            "cantidad":           cantidad,
            "coste_fifo":         coste_fifo,
            "pd_aflorada":        pd_aflorada,
            "coste_a_declarar":   coste_fifo + pd_aflorada,
            "importe_transmision": importe,
            "gastos_venta":       gastos,
            "transmision_neta":   importe - gastos,
            "ganancia_perdida":   gp_integrable,
            "gp_bruta":           gp_bruta,
            "fecha_adquisicion":  fecha_min_adq,
            "fecha_transmision":  fecha_max_trans,
        }

    return [r for r in (
        _build_reg("Reg A — Integrable",      reg_A_matches, False),
        _build_reg("Reg B — Diferida 2M",     reg_B_matches, True),
    ) if r is not None]

# Añadir irpf/ al path para importar motor_fiscal
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "irpf"))


def format_eur(d) -> str:
    """Formatea Decimal/float como EUR con 2 decimales."""
    if d is None:
        return "—"
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    sign = "-" if d < 0 else ""
    abs_d = abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    int_part, dec_part = str(abs_d).split(".")
    groups = []
    for i, c in enumerate(reversed(int_part)):
        if i > 0 and i % 3 == 0:
            groups.append(".")
        groups.append(c)
    formatted_int = "".join(reversed(groups))
    return f"{sign}{formatted_int},{dec_part}"


def generate_fiscal_pdf(
    fifo_results,
    ejercicio: int,
    output_path: str,
    dividendos: dict | None = None,
    opciones: dict | None = None,
    futuros: dict | None = None,
    multi_anio: bool = False,
    anios_analizados: str = "",
    compensacion=None,
    complejos_investigaciones: dict | None = None,
    fx_pl: dict | None = None,
    is_demo: bool = False,
    spinoffs_aplicados_previos: list | None = None,
) -> str:
    """Genera el PDF fiscal a partir de los resultados del motor FIFO.

    Args:
        fifo_results: FIFOResults del motor_fiscal.
        ejercicio: Año fiscal (e.g., 2025).
        output_path: Ruta donde guardar el PDF.
        dividendos: Dict con {bruto_total, retencion_total, cdi_recuperable} o None.
        opciones: Dict con {pl_total} o None.
        multi_anio: Si es análisis multi-año.
        anios_analizados: String descriptivo (e.g., "2023-2025").
        compensacion: ResultadoCompensacion (Art. 49 LIRPF) o None.
        complejos_investigaciones: {isin_o_nombre: texto_investigacion} generado
            via WebSearch para cada evento [COMPLEX]. Se inyecta en el campo
            'investigacion' de cada complejo antes de renderizar.

    Returns:
        Ruta del PDF generado.
    """
    import weasyprint

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=False)
    env.globals["format_eur"] = format_eur
    template = env.get_template("informe_irpf.html")

    # Preparar matches agrupados por ISIN — SOLO acciones/ETFs (no derechos).
    # Las ventas de derechos (FIFOMatch.es_derecho=True) van en su propia
    # sección y casillas (0341-0355 en Renta 2025), no en 0326-0340.
    matches_by_isin_dict = defaultdict(list)
    for m in fifo_results.matches:
        if m.ejercicio_fiscal == ejercicio and not m.es_derecho:
            matches_by_isin_dict[m.isin].append(m)

    matches_by_isin = []
    for isin in sorted(matches_by_isin_dict.keys(), key=lambda i: matches_by_isin_dict[i][0].nombre):
        matches = matches_by_isin_dict[isin]
        total_gp = sum(m.ganancia_perdida for m in matches)
        # Pérdidas diferidas afloradas en este grupo (Art. 33.5.f LIRPF
        # último párrafo). Si alguna fila tiene PD, el coste a declarar y
        # la G/P integrable difieren de los importes brutos de la operación.
        total_pd_aflorada = sum(
            getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0")) for m in matches
        )
        total_gp_integrable = total_gp - total_pd_aflorada
        # Totales "estilo Mi cartera de valores" — los importes que ve el
        # usuario en la app oficial AEAT tras sincronizar:
        #   · Adquisición: incluye gastos de compra (= coste FIFO ya
        #     ajustado por Cuádrate) y la pérdida diferida aflorada si
        #     aplica (forma A doctrinal: sumar al valor de adquisición).
        #   · Transmisión: NETO de gastos de venta (= bruto − comisión),
        #     que es lo "efectivamente percibido" según Art. 35.1.c LIRPF.
        # Sirve al usuario para hacer match visual: la fila NVIDIA de su
        # Mi cartera = (total_adquisicion, total_transmision) de aquí.
        total_adquisicion = sum(
            (m.coste_adquisicion or Decimal("0")) for m in matches
        ) + total_pd_aflorada
        total_transmision = sum(
            (m.importe_transmision or Decimal("0")) - (m.gastos_venta or Decimal("0"))
            for m in matches
        )
        # Tipo del grupo (todos los matches del mismo ISIN comparten tipo).
        instr_type = getattr(matches[0], 'instrument_type', 'STOCK')
        # Colapso a vista "lista-para-RentaWeb" cuando el grupo supera el
        # umbral: hasta 2 registros por ISIN (A integrable + B diferida 2M)
        # con fechas en rango FIFO (MIN compra → MAX venta) que es
        # exactamente lo que el contribuyente debe teclear en RentaWeb.
        # El detalle por operacion sigue disponible en la hoja G_P_por_valor
        # del XLSX (con outline expandible y coste editable).
        clasif = _classify_isin_pattern(matches)
        pattern = clasif["pattern"]
        collapsed = pattern in ("daytrading", "swing")
        group_entry = {
            "isin": isin,
            "nombre": matches[0].nombre,
            "matches": matches,
            "total_gp": total_gp_integrable,
            "total_gp_bruto": total_gp,
            "total_pd_aflorada": total_pd_aflorada,
            "total_adquisicion": total_adquisicion,
            "total_transmision": total_transmision,
            "instrument_type": instr_type,
            "collapsed": collapsed,
            "pattern": pattern,
            "n_intraday": clasif["n_intraday"],
            "ratio_intraday": clasif["ratio_intraday"],
            "n_matches": len(matches),
        }
        if collapsed:
            group_entry["rentaweb_aggregate"] = _aggregate_for_rentaweb(matches)
        matches_by_isin.append(group_entry)

    # Estadisticas globales de ISINs con operativa intensiva (collapsed=True)
    # para el banner de cabecera del informe. El usuario daytrader/swing
    # necesita saber DE UN VISTAZO cuantos ISINs aparecen agrupados a max
    # 2 filas/ISIN para traslacion directa a RentaWeb.
    n_isins_daytrading = sum(1 for g in matches_by_isin if g.get("pattern") == "daytrading")
    n_isins_swing      = sum(1 for g in matches_by_isin if g.get("pattern") == "swing")
    n_isins_intensive  = n_isins_daytrading + n_isins_swing
    n_matches_intensive = sum(g["n_matches"] for g in matches_by_isin
                              if g.get("pattern") in ("daytrading", "swing"))
    intensive_isins_stats = {
        "n_daytrading":      n_isins_daytrading,
        "n_swing":           n_isins_swing,
        "n_total":           n_isins_intensive,
        "n_matches_total":   n_matches_intensive,
        "threshold":         _COLLAPSE_DETALLE_THRESHOLD,
    }

    # Separar SIEMPRE por instrument_type para mostrar tablas distintas en
    # el PDF, independientemente del año. La instrucción de RentaWEB cambia
    # según ejercicio (Renta 2024: ETFs y acciones juntos en 0326-0340;
    # Renta 2025+: ETFs en 2224-2236 separadas).
    matches_by_isin_acciones    = [g for g in matches_by_isin if g['instrument_type'] == 'STOCK']
    matches_by_isin_etfs        = [g for g in matches_by_isin if g['instrument_type'] == 'ETF']
    matches_by_isin_derivatives = [g for g in matches_by_isin if g['instrument_type'] == 'DERIVATIVE']
    matches_by_isin_crypto      = [g for g in matches_by_isin if g['instrument_type'] == 'CRYPTO']
    matches_by_isin_bonds       = [g for g in matches_by_isin if g['instrument_type'] == 'BOND']
    matches_by_isin_socimi      = [g for g in matches_by_isin if g['instrument_type'] == 'SOCIMI']
    # ETCs físicos (Exchange Traded Commodities colateralizados): tributan
    # como RCM por cesión a terceros (Art. 25.2.b LIRPF + DGT V0267-25) →
    # casilla 0031, NO con acciones (0326-0340) ni con ETFs (2224-2236).
    matches_by_isin_etcs        = [g for g in matches_by_isin if g['instrument_type'] == 'ETC']

    # Calcular totales — SOLO acciones/ETFs (excluye derechos).
    year_matches = [
        m for m in fifo_results.matches
        if m.ejercicio_fiscal == ejercicio and not m.es_derecho
    ]
    # Los totales de G/P PATRIMONIAL (casillas 0326-0340 / 2224-2236 / etc.)
    # excluyen los ETCs físicos: éstos generan RCM por cesión a terceros
    # (Art. 25.2.b LIRPF + DGT V0267-25) y se reportan aparte en casilla 0031.
    # Si se sumaran aquí contaminarían dos veces el informe.
    year_matches_patrimonial = [
        m for m in year_matches
        if getattr(m, 'instrument_type', 'STOCK') != 'ETC'
    ]
    # G/P INTEGRABLE: la G/P bruta de la operación menos la pérdida diferida
    # aflorada (que afloró tras la transmisión definitiva del lote
    # recomprado). El usuario al declarar en RentaWEB suma la PD al valor
    # de adquisición (casilla 0331) y, por tanto, la G/P calculada por
    # RentaWEB para esa operación ya refleja el ajuste. Por eso los totales
    # del informe deben basarse en la G/P integrable, no en la bruta.
    def _gp_int(m):
        return m.ganancia_perdida - getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0"))
    total_ganancias = sum(_gp_int(m) for m in year_matches_patrimonial if _gp_int(m) > 0)
    total_perdidas = sum(_gp_int(m) for m in year_matches_patrimonial if _gp_int(m) < 0)
    total_gp = total_ganancias + total_perdidas
    # Totales brutos (sin ajuste por PD) para mostrar el desglose en
    # informe si conviene — útil para auditar la composición.
    total_ganancias_brutas = sum(m.ganancia_perdida for m in year_matches_patrimonial if m.ganancia_perdida > 0)
    total_perdidas_brutas = sum(m.ganancia_perdida for m in year_matches_patrimonial if m.ganancia_perdida < 0)
    total_regla_2m = sum(
        m.ganancia_perdida for m in year_matches_patrimonial
        if m.regla_2_meses and m.ganancia_perdida < 0
    )
    total_gp_deducible = total_gp - total_regla_2m
    matches_regla_2m = [m for m in year_matches_patrimonial if m.regla_2_meses]

    # Split de matches_regla_2m por instrument_type — cada tipo se declara
    # en una casilla distinta (STOCK→0326-0340, ETF→2224-2236 Renta 2025+ o
    # 0326-0340 Renta <2025, DERIVATIVE→1624-1654 clave 4, CRYPTO→1800-1814,
    # BOND→0030-0033 sin checkbox). El template renderiza una sub-sección
    # por tipo presente con su mecánica RentaWEB específica.
    regla_2m_acciones    = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'STOCK']
    regla_2m_etfs        = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'ETF']
    regla_2m_derivatives = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'DERIVATIVE']
    regla_2m_crypto      = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'CRYPTO']
    regla_2m_bonds       = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'BOND']
    regla_2m_socimi      = [m for m in matches_regla_2m if getattr(m, 'instrument_type', 'STOCK') == 'SOCIMI']

    # Pérdidas diferidas (Art. 33.5.f LIRPF, último párrafo): las que
    # afloran en este ejercicio (transmisión definitiva del lote recomprado)
    # y las latentes al cierre del histórico (atadas a lotes aún en cartera
    # del contribuyente). La doctrina dice que se imputan como cómputo
    # separado e independiente de la G/P de la nueva transmisión
    # (manual AEAT, sección F2 "Integración diferida"). El motor las marca
    # en cada match con `perdida_diferida_aflorada_eur`; aquí las agregamos
    # para que el template las muestre como sección destacada.
    matches_perdida_diferida_aflorada = [
        m for m in year_matches if m.perdida_diferida_aflorada_eur > 0
    ]
    total_perdida_diferida_aflorada = sum(
        m.perdida_diferida_aflorada_eur
        for m in matches_perdida_diferida_aflorada
    )
    # ¿Alguna pérdida diferida aflorada en este mismo ejercicio donde se
    # originó? Casos "intra-anuales" del ciclo regla 2M: el template
    # añade nota explicativa para informar al usuario.
    hay_perdida_diferida_intra_anual = any(
        getattr(m, 'perdida_diferida_intra_anual', False)
        for m in matches_perdida_diferida_aflorada
    )

    # Pérdidas diferidas que afloraron en ejercicios ANTERIORES al target.
    # Si el usuario está declarando 2025 y el motor detecta que en 2023 o
    # 2024 hubo afloraciones (transmisiones definitivas de lotes con PD
    # atada), conviene avisarle por si esas pérdidas no se imputaron en
    # las declaraciones de aquellos años — toca rectificativa.
    afloraciones_anios_anteriores = []
    for m in fifo_results.matches:
        if m.ejercicio_fiscal >= ejercicio:
            continue
        if m.perdida_diferida_aflorada_eur <= 0:
            continue
        afloraciones_anios_anteriores.append({
            "ejercicio_fiscal": m.ejercicio_fiscal,
            "isin": m.isin,
            "nombre": m.nombre,
            "fecha_venta": m.fecha_venta,
            "importe_eur": m.perdida_diferida_aflorada_eur,
            "origen_texto": getattr(m, 'perdida_diferida_origen', ''),
            "desglose": list(getattr(m, 'perdida_diferida_desglose', [])),
        })
    # Agrupar por ejercicio para mostrar resumen
    from collections import defaultdict as _dd
    afloraciones_anios_por_ejercicio = _dd(list)
    for a in afloraciones_anios_anteriores:
        afloraciones_anios_por_ejercicio[a["ejercicio_fiscal"]].append(a)
    afloraciones_anios_por_ejercicio = dict(
        sorted(afloraciones_anios_por_ejercicio.items())
    )
    total_afloraciones_anios_anteriores = sum(
        a["importe_eur"] for a in afloraciones_anios_anteriores
    )
    perdidas_diferidas_latentes = list(
        getattr(fifo_results, 'perdidas_diferidas_latentes', [])
    )
    total_perdidas_diferidas_latentes = sum(
        pd.importe_eur for pd in perdidas_diferidas_latentes
    )

    def _sum_2m(ms):
        return sum(m.ganancia_perdida for m in ms if m.ganancia_perdida < 0)
    total_regla_2m_acciones    = _sum_2m(regla_2m_acciones)
    total_regla_2m_etfs        = _sum_2m(regla_2m_etfs)
    total_regla_2m_derivatives = _sum_2m(regla_2m_derivatives)
    total_regla_2m_crypto      = _sum_2m(regla_2m_crypto)
    total_regla_2m_bonds       = _sum_2m(regla_2m_bonds)
    total_regla_2m_socimi      = _sum_2m(regla_2m_socimi)

    # Split por instrument_type — siempre separamos visualmente, aunque la
    # instrucción de RentaWEB cambie según el ejercicio.
    matches_acciones    = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'STOCK']
    matches_etfs        = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'ETF']
    matches_derivatives = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'DERIVATIVE']
    matches_crypto      = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'CRYPTO']
    matches_bonds       = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'BOND']
    matches_socimi      = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'SOCIMI']
    # ETCs físicos: RCM Art. 25.2 LIRPF + DGT V0267-25 → casilla 0031.
    # La regla del 33.5.f no les aplica (es de G/P patrimoniales), pero el
    # último párrafo del Art. 25.2 contiene su equivalente para RCM: los
    # rendimientos negativos con recompra de activos financieros homogéneos
    # en ±2 meses se difieren. El motor los marca igual que a las acciones
    # (anclaje Art. 25.2 en el detalle) y la cascada PD les aplica.
    matches_etcs        = [m for m in year_matches if getattr(m, 'instrument_type', 'STOCK') == 'ETC']
    n_unknown = sum(1 for m in year_matches if getattr(m, 'instrument_type_unknown', False))

    def _gp_split(ms):
        # G/P INTEGRABLE: bruta menos pérdida diferida aflorada. Coherente
        # con el coste a declarar en RentaWEB (casilla 0331 con PD sumada).
        def _gpi(m):
            return m.ganancia_perdida - getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0"))
        gan = sum(_gpi(m) for m in ms if _gpi(m) > 0)
        per = sum(_gpi(m) for m in ms if _gpi(m) < 0)
        bruto = gan + per
        no_ded_2m = sum(m.ganancia_perdida for m in ms if m.regla_2_meses and m.ganancia_perdida < 0)
        pd_aflorada = sum(getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0")) for m in ms)
        return {
            'ganancias':       gan,
            'perdidas':        per,
            'bruto':           bruto,
            'no_deducible_2m': no_ded_2m,
            'pd_aflorada':     pd_aflorada,
            'neto_deducible':  bruto - no_ded_2m,
            'n_matches':       len(ms),
        }

    split_acciones    = _gp_split(matches_acciones)
    split_etfs        = _gp_split(matches_etfs)
    # Pre-2025 los ETFs van CON acciones en 0326-0340 (no tenían bloque
    # propio). Split dedicado STOCK+ETF para el sidecar de ejercicios
    # anteriores: el `total_*` (year_matches_patrimonial = todo menos ETC)
    # double-contaba SOCIMI/BOND/CRYPTO/DERIVATIVE, que tienen casilla propia
    # en cualquier año.
    split_acciones_pre2025 = _gp_split(matches_acciones + matches_etfs)
    split_derivatives = _gp_split(matches_derivatives)
    split_crypto      = _gp_split(matches_crypto)
    split_bonds       = _gp_split(matches_bonds)
    split_socimi      = _gp_split(matches_socimi)
    # ETCs: mismo split que el resto — `no_deducible_2m` recoge los RCM
    # negativos diferidos por el Art. 25.2 último párrafo (recompra de
    # homogéneos ±2M) y `pd_aflorada` su afloración. Rendimiento = importe
    # transmisión - coste FIFO ajustado por comisiones (Art. 25.2.b +
    # Art. 26.1.a LIRPF).
    split_etcs = _gp_split(matches_etcs)

    # Resumen agrupado de derivados estructurados (Factor, Turbo, Mini, KO,
    # Bonus, ETN, etc.) para mostrar la tabla única que el usuario puede
    # introducir en RentaWEB casilla 1626 clave 4 sin tener que crear N
    # entradas separadas. Misma técnica que en la sección de Opciones.
    if matches_derivatives:
        deriv_total_coste  = sum(m.coste_adquisicion for m in matches_derivatives)
        deriv_total_imp    = sum(m.importe_transmision for m in matches_derivatives)
        deriv_total_gastos = sum(m.gastos_venta for m in matches_derivatives)
        deriv_total_gp     = sum(m.ganancia_perdida for m in matches_derivatives)
        deriv_fechas_compra = [m.fecha_compra for m in matches_derivatives if m.fecha_compra]
        deriv_fechas_venta  = [m.fecha_venta for m in matches_derivatives if m.fecha_venta]
        deriv_resumen = {
            "n_matches":      len(matches_derivatives),
            "total_coste":    deriv_total_coste,
            "total_imp":      deriv_total_imp,
            "total_gastos":   deriv_total_gastos,
            "total_gp":       deriv_total_gp,
            "fecha_primera":  (min(deriv_fechas_compra).strftime("%d/%m/%Y")
                               if deriv_fechas_compra else ""),
            "fecha_ultima":   (max(deriv_fechas_venta).strftime("%d/%m/%Y")
                               if deriv_fechas_venta else ""),
        }
    else:
        deriv_resumen = None

    # Desglose por broker (acciones/ETFs) — informativo, antes del total agregado.
    # Se agrupa por `broker_venta` (broker que ejecutó la venta y atribuye la
    # plusvalía/minusvalía). Si el campo viene vacío (matches de XLSX antiguos
    # antes de la columna Broker), se etiqueta "(sin broker)".
    desglose_por_broker = defaultdict(lambda: {
        "n_matches": 0, "ganancias": Decimal("0"), "perdidas": Decimal("0"),
        "gp_total": Decimal("0"), "no_deducible_2m": Decimal("0"),
        "neto_deducible": Decimal("0"),
    })
    for m in year_matches:
        b = (getattr(m, "broker_venta", "") or "(sin broker)")
        d = desglose_por_broker[b]
        d["n_matches"] += 1
        if m.ganancia_perdida > 0:
            d["ganancias"] += m.ganancia_perdida
        else:
            d["perdidas"] += m.ganancia_perdida
        d["gp_total"] += m.ganancia_perdida
        if m.regla_2_meses and m.ganancia_perdida < 0:
            d["no_deducible_2m"] += m.ganancia_perdida
    for d in desglose_por_broker.values():
        d["neto_deducible"] = d["gp_total"] - d["no_deducible_2m"]
    desglose_brokers_lista = [
        {"broker": b, **vals}
        for b, vals in sorted(desglose_por_broker.items())
    ]

    # Matches de derechos del ejercicio — para casillas 0341-0355 (Renta 2025).
    derechos_matches = [
        m for m in fifo_results.matches
        if m.ejercicio_fiscal == ejercicio and m.es_derecho
    ]
    total_derechos_ganancias = sum(m.ganancia_perdida for m in derechos_matches if m.ganancia_perdida > 0)
    total_derechos_perdidas  = sum(m.ganancia_perdida for m in derechos_matches if m.ganancia_perdida < 0)
    total_derechos_gp        = total_derechos_ganancias + total_derechos_perdidas

    # Posiciones abiertas
    positions = fifo_results.positions
    total_coste_posiciones = sum(p.coste_total_eur for p in positions)

    # Warnings filtrados (sin duplicados triviales)
    warnings = fifo_results.warnings

    # ── Datos de acciones corporativas (liberadas, residuales, complejos, sin clasificar) ──
    reports_dir = os.path.dirname(output_path)
    corp_txt = os.path.join(reports_dir, f"informe_corporativas_{ejercicio}.txt")
    corp_data = parse_corporativas_txt(corp_txt)

    # Cruzar ISINs de derechos residuales recomprados por el emisor contra
    # los dividendos ya parseados del XLSX. Si DeGiro etiqueta el abono
    # como "Dividendo" en el CSV de cuenta, el ISIN del derecho (sufijo
    # R1/R2 en ISINs ES) aparece en informe_dividendos_*.txt y por tanto
    # YA está sumado en `dividendos.bruto_total`. Sin este cruce, el PDF
    # sumaría el residual otra vez en la sección B → casilla 0029 saldría
    # duplicada (ej. ACS-RTS 1,56 EUR contado como 3,12 EUR).
    div_txt = os.path.join(reports_dir, f"informe_dividendos_{ejercicio}.txt")
    div_brutos_by_isin = parse_dividendos_by_isin(div_txt)
    for r in corp_data.get("residuales", []):
        isin_r = (r.get("isin") or "").strip()
        r["ya_en_xlsx"] = bool(isin_r and isin_r in div_brutos_by_isin)

    # ── Productos no soportados (cripto, bonds, futures, warrants, CFDs,
    # structured products, mutual funds — IBKR vía Asset Category; derivados
    # estructurados — DeGiro vía heurística). Generados por generar_irpf.py
    # como sidecar JSON al lado del XLSX maestro. ──
    no_soportadas = []
    no_soportadas_json = os.path.join(reports_dir, f"cartera_valores_irpf_{ejercicio}.no_soportadas.json")
    if os.path.exists(no_soportadas_json):
        try:
            import json as _json
            with open(no_soportadas_json, encoding='utf-8') as fh:
                no_soportadas = _json.load(fh) or []
        except Exception:
            no_soportadas = []

    # Agrupar por asset_category para banner del PDF.
    no_soportadas_por_categoria = {}
    for d in no_soportadas:
        cat = d.get('asset_category', '?') or '?'
        bucket = no_soportadas_por_categoria.setdefault(cat, {
            'n_ops': 0, 'importe_eur': 0.0, 'ops': [], 'broker_set': set(),
        })
        bucket['n_ops'] += 1
        bucket['importe_eur'] += float(d.get('importe_eur', 0) or 0)
        bucket['ops'].append(d)
        bucket['broker_set'].add(d.get('broker', '?'))
    no_soportadas_resumen = []
    _CASILLA_HINT = {
        'Cryptocurrency':       ('1800-1806', 'F2 — Monedas virtuales (apartado propio Renta 2025+)'),
        'Bonds':                ('0027 / 0030 / depende', 'B — RCM (cupones / Letras / etc.)'),
        'Futures':              ('1624-1654 clave 4',     'F2 — Otros elementos patrimoniales'),
        'Warrants':             ('1624-1654 clave 4',     'F2 — Otros elementos patrimoniales'),
        'CFDs':                 ('1624-1654 clave 4',     'F2 — Otros elementos patrimoniales'),
        'Structured Products':  ('1624-1654 clave 4',     'F2 — Otros elementos patrimoniales'),
        'Mutual Funds':         ('Bloque IIC con retención', 'F2 — Transmisiones IIC con retención'),
        'Derivado estructurado (heurística)': (
            '1624-1654 clave 4',
            'F2 — Otros elementos patrimoniales (derivados apalancados, certificados, ETN)'),
    }
    for cat, bucket in sorted(no_soportadas_por_categoria.items()):
        casilla, apartado = _CASILLA_HINT.get(cat, ('—', '—'))
        no_soportadas_resumen.append({
            'categoria':   cat,
            'n_ops':       bucket['n_ops'],
            'importe_eur': bucket['importe_eur'],
            'casilla':     casilla,
            'apartado':    apartado,
            'brokers':     ', '.join(sorted(bucket['broker_set'])),
            'ops':         bucket['ops'],
        })

    # Inyectar investigacion web en cada evento complejo (si se provee)
    if complejos_investigaciones:
        for c in corp_data.get("complejos", []):
            key = (c.get("isin") or "").strip() or (c.get("nombre") or "").strip()
            if key and key in complejos_investigaciones:
                c["investigacion"] = complejos_investigaciones[key]

    # Semáforos: verde por defecto; ámbar/rojo según haya items a revisar
    def _semaforo_global():
        # Spin-offs requieren prorrateo manual obligatorio (Art. 37.1.a) →
        # tan crítico como un evento sin clasificar: rojo.
        if corp_data["complejos"] or corp_data["rts_sin_clasif"] or corp_data["spin_offs"]:
            return "rojo"
        if corp_data["residuales"] or corp_data["liberadas"]:
            return "ambar"
        return "verde"

    # ── FX P&L (Forex) y Treasury Bills (solo IBKR) ────────────────────────
    # Auto-discovery: si el caller no pasa fx_pl, intentamos cargarlo del
    # CSV de IBKR del directorio del output. Probamos varios nombres
    # canónicos porque el splitter (csv_splitter._split_ibkr) escribe
    # `IBKR_Trades_{Y}.csv` desde el fix F1 — antes era `IBKR_{Y}.csv`,
    # y algún CSV histórico puede seguir con ese nombre.
    if fx_pl is None:
        try:
            import sys as _sys
            _irpf_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "irpf")
            if _irpf_dir not in _sys.path:
                _sys.path.insert(0, _irpf_dir)
            from generar_irpf import parse_ibkr_fx_pl as _parse_ibkr_fx_pl  # type: ignore
            reports_dir = os.path.dirname(output_path)
            for candidate in (f"IBKR_Trades_{ejercicio}.csv",
                              f"IBKR_{ejercicio}.csv"):
                ibkr_path = os.path.join(reports_dir, candidate)
                if os.path.exists(ibkr_path):
                    fx_pl = _parse_ibkr_fx_pl(ibkr_path)
                    break
        except Exception:
            fx_pl = None  # silencioso: si falla, el PDF se genera sin estas secciones

    fx_data = None
    tbills_data = None
    if fx_pl:
        fx_rows = fx_pl.get("fx") or []
        if fx_rows:
            total_realized = sum(
                (Decimal(str(r.get("realized", 0))) for r in fx_rows),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
            total_unrealized = sum(
                (Decimal(str(r.get("unrealized", 0))) for r in fx_rows),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
            # Decisión recomendada con base en signo y magnitud — la AEAT
            # tolera en la práctica diferencias pequeñas para particulares,
            # aunque no es una regla escrita en la ley.
            if total_realized >= 0:
                # Ganancia: minimis te beneficia (no pagas).
                if abs(total_realized) < 1000:
                    recomendacion = (
                        "Ganancia inferior a 1.000 EUR: la AEAT suele tolerar "
                        "no declararla en la práctica para particulares (no "
                        "es un mínimo legal escrito). Aplicar este criterio "
                        "te ahorra tributar sobre esta cifra."
                    )
                    accion = "minimis"
                else:
                    recomendacion = (
                        "Ganancia superior a 1.000 EUR: declarar es la práctica "
                        "estándar; el umbral de minimis se considera superado."
                    )
                    accion = "declarar"
            else:
                # Pérdida: declararla te beneficia (compensa otras G/P).
                if abs(total_realized) < 1000:
                    recomendacion = (
                        "Pérdida inferior a 1.000 EUR: aunque la AEAT toleraría "
                        "no declararla, declararla te BENEFICIA — esta pérdida "
                        "compensa otras ganancias y reduce tu base imponible."
                    )
                    accion = "declarar-recomendado"
                else:
                    recomendacion = (
                        "Pérdida superior a 1.000 EUR: declarar es claramente "
                        "recomendable y obligatorio en la práctica. Compensa "
                        "otras ganancias patrimoniales."
                    )
                    accion = "declarar"
            fx_data = {
                "rows": sorted(fx_rows, key=lambda x: x.get("divisa", "")),
                "total_realized": total_realized,
                "total_unrealized": total_unrealized,
                "recomendacion": recomendacion,
                "accion": accion,  # 'minimis' | 'declarar-recomendado' | 'declarar'
            }
        tbills_rows = fx_pl.get("tbills") or []
        if tbills_rows:
            total_tbills = sum(
                (Decimal(str(t.get("realized", 0))) for t in tbills_rows),
                Decimal("0"),
            ).quantize(Decimal("0.01"))
            tbills_data = {
                "rows": tbills_rows,
                "total": total_tbills,
            }

    # Intereses IBKR (Credit / Debit / Bond Interest) — sección Interest del
    # Activity Statement. Credit + Bond → casilla 0027 (RCM). Debit es
    # informativo (no deducible automáticamente para particulares).
    interest_data = None
    if dividendos:
        ic = dividendos.get("interest_credit_total") or Decimal("0")
        idd = dividendos.get("interest_debit_total") or Decimal("0")
        if ic or idd:
            interest_data = {
                "credit_total": ic,
                "debit_total":  idd,
            }

    # Total intereses casilla 0027 = T-Bills (Realized & Unrealized) + Credit
    # + Bond IBKR. Lo agregamos para mostrarlo como una sola fila en el
    # resumen ejecutivo (el desglose por origen va en sus secciones).
    total_intereses_0023 = Decimal("0")
    if tbills_data:
        total_intereses_0023 += Decimal(str(tbills_data.get("total", 0) or 0))
    if interest_data:
        total_intereses_0023 += Decimal(str(interest_data.get("credit_total", 0) or 0))

    # Gastos del broker — Art. 26.1.a LIRPF, deducibles del RCM (casilla 0030
    # "Gastos de administración y depósito"). Vienen via parse_dividendos_txt.
    plataforma_data = None
    if dividendos:
        plat_total = dividendos.get("gastos_plataforma_total") or Decimal("0")
        plat_items = dividendos.get("gastos_plataforma_items") or []
        if plat_total > 0 or plat_items:
            plataforma_data = {
                "total": plat_total,
                # NOTA: "items" colisiona con dict.items() en Jinja → "lines".
                "lines": plat_items,
            }

    # Desglose por broker — leído del sidecar cartera_valores_irpf_{Y}.totals.json
    # que escribe excel_cartera.aggregate_por_broker. Si por algún motivo el
    # XLSX maestro no se ha generado todavía o el sidecar no existe (CLI
    # antiguo), seguimos sin la tabla y el template oculta la sección.
    por_broker = None
    staking_data = None
    try:
        import json as _json
        # output_path es informe_fiscal_2025.pdf → sidecar está en el mismo dir
        # como cartera_valores_irpf_2025.totals.json
        _sidecar_path = os.path.join(
            os.path.dirname(output_path),
            f"cartera_valores_irpf_{ejercicio}.totals.json",
        )
        if os.path.exists(_sidecar_path):
            with open(_sidecar_path, encoding='utf-8') as _f:
                _sidecar = _json.load(_f)
            por_broker = _sidecar.get("por_broker")
            # Staking de criptomonedas (RCM 0027, DGT V1766-22) — el PDF lo
            # omitía por completo: la cadena terminaba en stdout + hoja
            # Staking del XLSX. Auditoría visual 2026-06-11.
            _stk = _sidecar.get("casilla_0027_staking")
            if _stk and Decimal(str(_stk.get("total", 0) or 0)) > 0:
                staking_data = {
                    "total":     Decimal(str(_stk["total"])),
                    "n_eventos": int(_stk.get("n_eventos", 0) or 0),
                    "activos":   _stk.get("activos") or [],
                }
    except Exception as _e:
        print(f"[pdf_generator] No se pudo leer por_broker del sidecar: {_e}")

    # Retención IRPF española sobre intereses (TR Sucursal ES, cuenta
    # remunerada post-migración) → campo "Retenciones" del popup de 0027,
    # 100% acreditable. Se deriva del desglose por broker del sidecar
    # (columna int_ret), que ya separa la retención española de intereses.
    interest_ret_es_total = Decimal("0")
    if por_broker:
        for _b in por_broker.values():
            interest_ret_es_total += Decimal(str(_b.get("int_ret", 0) or 0))
    if interest_data is not None and interest_ret_es_total:
        interest_data["ret_es_total"] = interest_ret_es_total

    # ── Heurística perfil daytrader ───────────────────────────────────────
    # Detecta dos perfiles típicos:
    #   1. Diversificado: ≥ 50 matches del año (muchas ventas en muchos
    #      ISINs).
    #   2. Concentrado: ≥ 20 matches en un mismo ISIN (típico trader de
    #      SPY/QQQ/NVDA que rota la misma posición decenas de veces).
    # Si alguno se cumple, el PDF muestra un aviso con guía operativa y
    # link al post pilar de daytrading (educación + retención).
    # ETCs no cuentan para la heurística daytrader: el perfil daytrader
    # se define sobre G/P patrimoniales (Art. 33.5.f LIRPF), no sobre RCM.
    _matches_por_isin_cnt = defaultdict(int)
    for _m in year_matches_patrimonial:
        _matches_por_isin_cnt[_m.isin] += 1
    _n_matches_anuales = len(year_matches_patrimonial)
    _max_matches_por_isin = max(_matches_por_isin_cnt.values(), default=0)
    _isin_top = max(_matches_por_isin_cnt, key=_matches_por_isin_cnt.get,
                    default=None) if _matches_por_isin_cnt else None
    _n_isins_distintos = len(_matches_por_isin_cnt)
    # Match 2M es indicador adicional: el daytrader incurre muy a menudo.
    _n_matches_2m = len(matches_regla_2m)
    _ratio_2m = (_n_matches_2m / _n_matches_anuales) if _n_matches_anuales else 0
    perfil_daytrader = {
        "detected": (
            _n_matches_anuales >= 50
            or _max_matches_por_isin >= 20
        ),
        "n_matches_anuales":   _n_matches_anuales,
        "n_isins_distintos":   _n_isins_distintos,
        "max_matches_por_isin": _max_matches_por_isin,
        "isin_top":            _isin_top,
        "nombre_top": next(
            (_m.nombre for _m in year_matches if _m.isin == _isin_top), ""
        ) if _isin_top else "",
        "n_matches_2m":        _n_matches_2m,
        "ratio_2m_pct":        round(_ratio_2m * 100, 1),
        "tipo": (
            "concentrado" if _max_matches_por_isin >= 20
                            and _n_matches_anuales < 50
            else ("diversificado" if _n_matches_anuales >= 50
                                       and _max_matches_por_isin < 20
                  else "mixto")
        ),
        "url_pillar": "/blog/daytrader-degiro-ibkr-renta-2025-guia-fiscal",
    }

    context = {
        "is_demo": is_demo,
        "ejercicio": ejercicio,
        "fecha_generacion": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "multi_anio": multi_anio,
        "anios_analizados": anios_analizados,
        "matches_by_isin": matches_by_isin,
        "por_broker": por_broker,
        "total_ganancias": total_ganancias,
        "total_perdidas": total_perdidas,
        "total_gp": total_gp,
        "matches_perdida_diferida_aflorada": matches_perdida_diferida_aflorada,
        "total_perdida_diferida_aflorada":   total_perdida_diferida_aflorada,
        "hay_perdida_diferida_intra_anual":  hay_perdida_diferida_intra_anual,
        "afloraciones_anios_anteriores":          afloraciones_anios_anteriores,
        "afloraciones_anios_por_ejercicio":       afloraciones_anios_por_ejercicio,
        "total_afloraciones_anios_anteriores":    total_afloraciones_anios_anteriores,
        "perdidas_diferidas_latentes":       perdidas_diferidas_latentes,
        "total_perdidas_diferidas_latentes": total_perdidas_diferidas_latentes,
        "total_regla_2m": total_regla_2m,
        "total_gp_deducible": total_gp_deducible,
        "matches_regla_2m": matches_regla_2m,
        # Desglose 2M por instrument_type — el template renderiza una sub-sección
        # por tipo presente con su mecánica RentaWEB específica.
        "regla_2m_acciones":         regla_2m_acciones,
        "regla_2m_etfs":             regla_2m_etfs,
        "regla_2m_derivatives":      regla_2m_derivatives,
        "regla_2m_crypto":           regla_2m_crypto,
        "regla_2m_bonds":            regla_2m_bonds,
        "regla_2m_socimi":           regla_2m_socimi,
        "total_regla_2m_acciones":   total_regla_2m_acciones,
        "total_regla_2m_etfs":       total_regla_2m_etfs,
        "total_regla_2m_derivatives": total_regla_2m_derivatives,
        "total_regla_2m_crypto":     total_regla_2m_crypto,
        "total_regla_2m_bonds":      total_regla_2m_bonds,
        "total_regla_2m_socimi":     total_regla_2m_socimi,
        "num_matches": len(year_matches),
        "num_warnings": len(warnings),
        # Desglose por broker (acciones/ETFs). Suma a `total_gp` global.
        "desglose_brokers": desglose_brokers_lista,
        "n_brokers": len(desglose_brokers_lista),
        # Derechos de suscripción (casillas 0341-0355 — Renta 2025)
        "derechos_matches":          derechos_matches,
        "total_derechos_ganancias":  total_derechos_ganancias,
        "total_derechos_perdidas":   total_derechos_perdidas,
        "total_derechos_gp":         total_derechos_gp,
        "num_derechos":              len(derechos_matches),
        "warnings": warnings,
        "positions": sorted(positions, key=lambda p: p.nombre),
        "total_coste_posiciones": total_coste_posiciones,
        "dividendos": dividendos,
        "opciones": opciones,
        "futuros": futuros,
        "compensacion": compensacion,
        "fx_data": fx_data,
        "tbills_data": tbills_data,
        "interest_data": interest_data,
        "staking_data": staking_data,
        "total_intereses_0023": total_intereses_0023,  # legacy alias - usar interest_data["credit_total"]
        "plataforma_data": plataforma_data,
        # Split acciones/ETFs/derivados/cripto. Las tablas siempre se
        # separan visualmente; la instrucción de RentaWEB cambia según año
        # (etfs_bloque_separado=True solo en Renta 2025+).
        "etfs_bloque_separado":   (ejercicio >= 2025),
        "split_acciones":         split_acciones,
        "split_etfs":             split_etfs,
        "split_derivatives":      split_derivatives,
        "split_crypto":           split_crypto,
        "split_bonds":            split_bonds,
        "split_socimi":           split_socimi,
        "split_etcs":             split_etcs,
        "matches_acciones":       matches_acciones,
        "matches_etfs":           matches_etfs,
        "matches_derivatives":    matches_derivatives,
        "matches_crypto":         matches_crypto,
        "matches_bonds":          matches_bonds,
        "matches_socimi":         matches_socimi,
        "matches_etcs":           matches_etcs,
        "matches_by_isin_acciones":    matches_by_isin_acciones,
        "matches_by_isin_etfs":        matches_by_isin_etfs,
        "matches_by_isin_derivatives": matches_by_isin_derivatives,
        "deriv_resumen":               deriv_resumen,
        "matches_by_isin_crypto":      matches_by_isin_crypto,
        "matches_by_isin_bonds":       matches_by_isin_bonds,
        "matches_by_isin_socimi":      matches_by_isin_socimi,
        "matches_by_isin_etcs":        matches_by_isin_etcs,
        "n_unknown_classify":     n_unknown,
        # Productos no soportados (cripto, derivados estructurados, bonds,
        # futures, warrants, CFDs, structured, mutual funds) — banner PDF.
        "no_soportadas_resumen":  no_soportadas_resumen,
        "no_soportadas_total_ops": sum(b['n_ops'] for b in no_soportadas_resumen),
        "div_retencion_es": (dividendos.get("retencion_es") if dividendos else None),
        "format_eur": format_eur,
        # Nuevas variables FASE 2+3
        "corp_liberadas":      corp_data["liberadas"],
        "corp_residuales":     corp_data["residuales"],
        "corp_complejos":      corp_data["complejos"],
        "corp_sin_clasif":     corp_data["rts_sin_clasif"],
        "corp_venta_mercado":  corp_data["venta_mercado"],
        "corp_spin_offs":      corp_data["spin_offs"],
        "corp_rights_exercised": corp_data["rights_exercised"],
        # Spin-offs resueltos auto (catalogo Form 8937) que han afectado
        # al inventario aunque la fecha del evento sea anterior al target.
        # El motor ya aplico el doble ajuste — esta lista es informativa
        # para auditoria del usuario.
        "spinoffs_aplicados_previos": spinoffs_aplicados_previos or [],
        "semaforo_global":     _semaforo_global(),
        # Aviso de perfil daytrader (Art. 33.5.f LIRPF + intensidad de
        # trading). Solo se renderiza si perfil_daytrader.detected = True.
        "perfil_daytrader":    perfil_daytrader,
        # Estadisticas de ISINs agrupados a vista RentaWeb (max 2 filas/ISIN
        # cuando n_matches >= 20). Banner global en cabecera + tooltip por
        # ISIN con clasificacion daytrading / swing.
        "intensive_isins_stats": intensive_isins_stats,
    }

    html_content = template.render(**context)

    # Renderizar PDF. base_url apunta al directorio webapp/ para que las
    # rutas relativas del template (`static/assets/RentaWebGuide/X.png`)
    # se resuelvan al sistema de ficheros real de la instalación,
    # independientemente de dónde esté deployada la app (local, Railway,
    # Docker). Antes se usaba base_url='file:///' con rutas absolutas
    # `/app/720/webapp/...`, lo cual sólo funcionaba en el entorno local
    # del desarrollador y rompía las capturas en producción.
    webapp_dir = os.path.dirname(os.path.abspath(__file__))
    doc = weasyprint.HTML(string=html_content,
                          base_url=f'file://{webapp_dir}/')
    doc.write_pdf(output_path)

    # Sidecar JSON con los totales clave para tests de consistencia
    # (mismos valores que aparecen en la sección "Resumen ejecutivo" del PDF).
    # Permite al test unitario comparar PDF ↔ XLSX sin parsear texto del PDF.
    import json as _json
    # Solo cuenta como "a sumar manualmente" los residuales que NO están ya
    # en el bruto del XLSX (ver cruce con div_brutos_by_isin arriba).
    ns_div_residuales = Decimal("0")
    for r in corp_data["residuales"]:
        if r.get("importe_teorico") is None:
            continue
        if r.get("absorbido_por_comision"):
            continue
        if r.get("ya_en_xlsx"):
            continue
        ns_div_residuales += r["importe_teorico"]

    def _d(v):
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        return float(v)

    # Renta 2025+: ETFs van en bloque separado 2224-2236 (Art. 75.3.j RIRPF).
    # Para ejercicios anteriores ETFs van junto a acciones en 0326-0340.
    etfs_bloque_separado = (ejercicio >= 2025)

    sidecar = {
        "ejercicio": ejercicio,
        "multi_anio": multi_anio,
        # Casillas 0326-0340 — Acciones cotizadas (excluye derechos y ETFs en 2025+)
        "casilla_0326_0340": {
            "ganancias":        _d((split_acciones if etfs_bloque_separado else split_acciones_pre2025)['ganancias']),
            "perdidas":         _d((split_acciones if etfs_bloque_separado else split_acciones_pre2025)['perdidas']),
            "no_deducible_2m":  _d((split_acciones if etfs_bloque_separado else split_acciones_pre2025)['no_deducible_2m']),
            "neto_deducible":   _d((split_acciones if etfs_bloque_separado else split_acciones_pre2025)['neto_deducible']),
            "neto_bruto":       _d((split_acciones if etfs_bloque_separado else split_acciones_pre2025)['bruto']),
            "n_matches":        (split_acciones if etfs_bloque_separado else split_acciones_pre2025)['n_matches'],
        },
        # Casillas 2224-2236 — ETFs / IIC sin retención (Art. 75.3.j RIRPF, Renta 2025+)
        "casilla_2224_2236": {
            "ganancias":        _d(split_etfs['ganancias']),
            "perdidas":         _d(split_etfs['perdidas']),
            "no_deducible_2m":  _d(split_etfs['no_deducible_2m']),
            "neto_deducible":   _d(split_etfs['neto_deducible']),
            "neto_bruto":       _d(split_etfs['bruto']),
            "n_matches":        split_etfs['n_matches'],
        } if etfs_bloque_separado and split_etfs['n_matches'] > 0 else None,
        # Casillas 1624-1654 clave 4 — Derivados estructurados (Factor SG,
        # Turbos, Mini, KO, Bonus, Discount, Express, Reverse, ETN, Open End)
        "casilla_1624_1654_derivados": {
            "neto_deducible":   _d(split_derivatives['neto_deducible']),
            "neto_bruto":       _d(split_derivatives['bruto']),
            "no_deducible_2m":  _d(split_derivatives['no_deducible_2m']),
            "n_matches":        split_derivatives['n_matches'],
        } if split_derivatives['n_matches'] > 0 else None,
        # Casillas 1800-1806 — Criptomonedas (Renta 2025+)
        "casilla_1800_1806": {
            "neto_deducible":   _d(split_crypto['neto_deducible']),
            "neto_bruto":       _d(split_crypto['bruto']),
            "no_deducible_2m":  _d(split_crypto['no_deducible_2m']),
            "n_matches":        split_crypto['n_matches'],
        } if split_crypto['n_matches'] > 0 else None,
        # Casilla 0031 — Transmisión / amortización de otros activos financieros
        # (bonos individuales). Los cupones cobrados durante la tenencia van
        # a casilla 0027 (gestión aparte vía bond_data). no_deducible_2m =
        # RCM negativos diferidos por recompra de homogéneos ±2M (Art. 25.2
        # LIRPF último párrafo).
        "casilla_0031": {
            "neto_deducible":   _d(split_bonds['neto_deducible']),
            "neto_bruto":       _d(split_bonds['bruto']),
            "no_deducible_2m":  _d(split_bonds['no_deducible_2m']),
            "n_matches":        split_bonds['n_matches'],
        } if split_bonds['n_matches'] > 0 else None,
        # Casilla 0031 (ETCs físicos, DGT V0267-25) — misma casilla AEAT que
        # bonos pero desglosada aparte, coherente con "0031-ETC" del XLSX.
        "casilla_0031_etc": {
            "neto_deducible":   _d(split_etcs['neto_deducible']),
            "neto_bruto":       _d(split_etcs['bruto']),
            "no_deducible_2m":  _d(split_etcs['no_deducible_2m']),
            "n_matches":        split_etcs['n_matches'],
        } if split_etcs['n_matches'] > 0 else None,
        # Casillas 0324/0325 — SOCIMI españolas (Ley 11/2009): apartado F2,
        # subapartado IIC/SOCIMI. Solo SOCIMI nacionales — los REITs/SIIC
        # extranjeros van como acciones normales en 0326-0340.
        "casilla_0324_0325": {
            "ganancias":        _d(split_socimi['ganancias']),
            "perdidas":         _d(split_socimi['perdidas']),
            "no_deducible_2m":  _d(split_socimi['no_deducible_2m']),
            "neto_deducible":   _d(split_socimi['neto_deducible']),
            "neto_bruto":       _d(split_socimi['bruto']),
            "n_matches":        split_socimi['n_matches'],
        } if split_socimi['n_matches'] > 0 else None,
        "instrument_type_unknown_count": n_unknown,
        # Casillas 0341-0355 — Transmisión de derechos de suscripción
        "casilla_0341_0355": {
            "ganancias":  _d(total_derechos_ganancias),
            "perdidas":   _d(total_derechos_perdidas),
            "gp_total":   _d(total_derechos_gp),
            "n_matches":  len(derechos_matches),
        },
        # Casilla 0029 — Dividendos brutos
        "casilla_0029": {
            "bruto":                _d(dividendos["bruto_total"]) if dividendos else 0.0,
            "residuales_manual":    _d(ns_div_residuales),
            "total_a_declarar":     _d((dividendos["bruto_total"] if dividendos else Decimal("0")) + ns_div_residuales),
            # Retenciones IRPF español (pagadores ES con filial española) — se
            # introducen en el campo "Retenciones" del popup individual de 0029.
            # Brokers extranjeros (DeGiro/IBKR) NO retienen IRPF español.
            "retencion_es":         _d(dividendos.get("retencion_es", 0)) if dividendos else 0.0,
        } if dividendos or ns_div_residuales else None,
        # Casilla 0588 — Deducción doble imposición internacional
        "casilla_0588": {
            "cdi_recuperable":  _d(dividendos["cdi_recuperable"]) if dividendos else 0.0,
        } if dividendos else None,
        # Casilla 0027 — Intereses (cuentas, depósitos, Credit/Bond IBKR)
        "casilla_0027": {
            "ibkr_credit_bond": _d(interest_data["credit_total"]) if interest_data else 0.0,
            "intereses_total":  _d(interest_data["credit_total"]) if interest_data else 0.0,
            # Retención IRPF española de intereses (TR Sucursal ES) → campo
            # "Retenciones" del popup individual de 0027, 100% acreditable.
            "retencion_es":     _d(interest_ret_es_total),
        } if interest_data and interest_data.get("credit_total", 0) > 0 else None,
        # Casilla 0030 — Letras del Tesoro (transmisión/amortización)
        "casilla_0030": {
            "tbills_total":     _d(tbills_data["total"]) if tbills_data else 0.0,
            "n_items":          len(tbills_data.get("lines", [])) if tbills_data else 0,
        } if tbills_data and tbills_data.get("total", 0) > 0 else None,
        # Casilla 0027 (staking cripto) — RCM en especie DGT V1766-22.
        # Espejo de la clave homónima del sidecar XLSX (coherencia de capas).
        "casilla_0027_staking": {
            "total":     _d(staking_data["total"]),
            "n_eventos": staking_data.get("n_eventos", 0),
        } if staking_data else None,
        # Casilla 0037 — Gastos de administración y depósito (Art. 26.1.a LIRPF)
        # Se introducen en el campo "Gastos de administración y depósito" del
        # popup individual de cualquier rendimiento del capital mobiliario;
        # RentaWEB los totaliza automáticamente en 0037 y los resta del
        # rendimiento bruto sin permitir saldo negativo.
        "casilla_0037": {
            "gastos_admin_deposito": _d(plataforma_data["total"]) if plataforma_data else 0.0,
            "n_items":               len(plataforma_data["lines"]) if plataforma_data else 0,
        } if plataforma_data else None,
        # Casillas 1624-1654 — Otros elementos patrimoniales (clave 4: opciones, forex)
        # Sumarios: 0385 (pérdidas), 0386 (ganancias)
        "casilla_1624_1654": {
            "pl_opciones":  _d(opciones["pl_total"]) if opciones else 0.0,
            "fx_realized":  _d(fx_data["total_realized"]) if fx_data else 0.0,
        } if opciones or fx_data else None,
        # Casilla 1186+ — Saldos negativos arrastrados ejercicios anteriores
        "casilla_1186_plus": {
            "aplicable_este_ejercicio":  _d(compensacion.aplicadas_de_anteriores) if compensacion else 0.0,
        } if compensacion else None,
        "casos_revision_manual": (
            len(corp_data["complejos"])
            + len(corp_data["rts_sin_clasif"])
            + len(corp_data["residuales"])
            + len(corp_data["spin_offs"])
            + len(warnings)
            + n_unknown
        ),
    }

    sidecar_path = output_path.rsplit(".", 1)[0] + ".totals.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        _json.dump(sidecar, f, indent=2, ensure_ascii=False)

    # ── Sidecar de detalle por match FIFO ──────────────────────────────────
    # Pensado para herramientas de auditoría/reconciliación (ej. cruzar con
    # el Informe Anual del broker) y para tests automatizados que quieran
    # inspeccionar el FIFO en grano fino sin tener que re-procesar los CSVs.
    # Solo emite los matches del ejercicio target (los anteriores ya están
    # cerrados); incluye flags relevantes para atribuir diferencias con el
    # broker: ejercicio_opcion (DGT V2172-21), regla_2_meses (Art. 33.5.f),
    # es_scrip / es_derecho, instrument_type, broker_compra/venta.
    # Sidecar: incluir TODOS los matches del ejercicio (acciones/ETFs +
    # derechos). El PDF excluye derechos porque van a su propio bloque
    # (casillas 0341-0346), pero el sidecar es para auditoría y debe
    # reflejar todo. El reconciliador usa el flag `es_derecho` para
    # decidir cómo atribuir cada match.
    sidecar_matches = [
        m for m in fifo_results.matches if m.ejercicio_fiscal == ejercicio
    ]
    matches_sidecar = {
        "ejercicio": ejercicio,
        "matches": [
            {
                "isin":               m.isin,
                "nombre":             m.nombre,
                "fecha_compra":       m.fecha_compra.strftime("%d/%m/%Y") if m.fecha_compra else None,
                "fecha_venta":        m.fecha_venta.strftime("%d/%m/%Y") if m.fecha_venta else None,
                "cantidad":           _d(m.cantidad),
                "coste_adquisicion":  _d(m.coste_adquisicion),
                "importe_transmision": _d(m.importe_transmision),
                "gastos_venta":       _d(m.gastos_venta),
                "gastos_compra":      _d(getattr(m, "gastos_compra", Decimal("0"))),
                "ganancia_perdida":   _d(m.ganancia_perdida),
                "ejercicio_fiscal":   m.ejercicio_fiscal,
                "regla_2_meses":      bool(m.regla_2_meses),
                "regla_2_meses_detalle": m.regla_2_meses_detalle or "",
                "es_scrip":           bool(m.es_scrip),
                "ejercicio_opcion":   bool(m.ejercicio_opcion),
                "es_derecho":         bool(m.es_derecho),
                "amortizacion_inferida": bool(getattr(m, "amortizacion_inferida", False)),
                "broker_compra":      m.broker_compra or "",
                "broker_venta":       m.broker_venta or "",
                "instrument_type":    getattr(m, "instrument_type", "STOCK"),
                "instrument_type_unknown": bool(getattr(m, "instrument_type_unknown", False)),
                "lote_id":            getattr(m, "lote_id", 0),
                "es_corto":           bool(getattr(m, "es_corto", False)),
            }
            for m in sidecar_matches
        ],
    }
    matches_sidecar_path = output_path.rsplit(".", 1)[0] + ".matches.json"
    with open(matches_sidecar_path, "w", encoding="utf-8") as f:
        _json.dump(matches_sidecar, f, indent=2, ensure_ascii=False)

    return output_path


def parse_dividendos_txt(filepath: str) -> dict | None:
    """Extrae totales del informe_dividendos_YYYY.txt generado por generar_irpf.py.

    Busca las líneas con los totales al final del fichero.

    Devuelve dict con:
      - bruto_total / retencion_total / cdi_recuperable (dividendos)
      - interest_credit_total / interest_debit_total (intereses IBKR Credit/Bond
        + Debit, si la sección INTERESES IBKR está presente)
      - gastos_plataforma_total (Decimal) y gastos_plataforma_items
        (lista de {fecha, descripcion, importe}) — Art. 26.1.a LIRPF,
        gastos deducibles del RCM (casilla 0030).
    """
    if not os.path.exists(filepath):
        return None

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    bruto = Decimal("0")
    retencion = Decimal("0")
    cdi = Decimal("0")
    retencion_es = Decimal("0")  # popup individual de 0029 (acreditable 100%)
    # Bruto de los dividendos EXTRANJEROS con retención > 0 — campo
    # "Rendimientos netos reducidos del capital mobiliario obtenidos en el
    # extranjero incluidos en la base del ahorro" del segundo popup de 0588.
    # Excluye nacionales (van por 0591) y extranjeros con retención 0 %.
    bruto_ext_con_ret = Decimal("0")
    interest_credit = Decimal("0")
    interest_debit = Decimal("0")
    gastos_plataforma_total = Decimal("0")
    gastos_plataforma_items: list[dict] = []
    in_plataforma_section = False
    plataforma_header_passed = False  # tras el bloque de "Deducibles como..." viene el detalle

    # Regex para filas de detalle del bloque plataforma:
    #   "  01/12/2025    Comisión de conectividad mercado 2025             2,50 EUR"
    import re as _re
    _plat_row_re = _re.compile(
        r'^\s*(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([\d.,\-]+)\s*EUR\s*$'
    )

    for line in content.splitlines():
        line_stripped = line.strip()
        line_lower = line_stripped.lower()
        # Patrones del informe de generar_irpf.py:
        #   "Dividendo bruto total   : 3001,34 EUR"
        #   "Retención total pagada  : 524,25 EUR"
        #   "CDI recuperable (0588): 301,07 EUR"
        if "bruto total" in line_lower:
            bruto = _extract_amount(line_stripped)
        elif "retención total" in line_lower or "retencion total" in line_lower:
            retencion = _extract_amount(line_stripped)
        elif "cdi recuperable" in line_lower:
            cdi = _extract_amount(line_stripped)
        elif "bruto extranjero con retención" in line_lower or "bruto extranjero con retencion" in line_lower:
            bruto_ext_con_ret = _extract_amount(line_stripped)
        # Retención de pagador español → campo "Retenciones" del popup individual
        # de 0029. Patrón único del total: "✅ Retención ES           : 7,60 EUR
        # → campo 'Retenciones' del popup individual de 0029 ..."
        # (las otras líneas con "retencion es" son descripciones sin total).
        elif (line_stripped.startswith("✅ Retención ES")
              or line_stripped.startswith("Retención ES ")) and "EUR" in line_stripped:
            retencion_es = _extract_amount(line_stripped)
        # Sección INTERESES IBKR (write_informe_dividendos):
        #   "TOTAL Credit + Bond (declarable casilla 0027): 12,34 EUR"
        #   "TOTAL Debit (informativo, NO deducible automático): -5,06 EUR"
        elif "credit + bond" in line_lower:
            interest_credit = _extract_amount(line_stripped)
        elif "total debit" in line_lower and "informativo" in line_lower:
            interest_debit = _extract_amount(line_stripped)

        # Sección GASTOS DE PLATAFORMA — extraer total y detalle por línea.
        # Header: "GASTOS DE PLATAFORMA — Comisiones de conectividad (DeGiro)"
        # Total:  "TOTAL gastos plataforma deducibles: X,XX EUR"
        if "gastos de plataforma" in line_lower:
            in_plataforma_section = True
            plataforma_header_passed = False
            continue
        if in_plataforma_section:
            if "total gastos plataforma" in line_lower:
                gastos_plataforma_total = _extract_amount(line_stripped)
                in_plataforma_section = False
                continue
            # Tras el header textual ("Deducibles como..." / "Introducir en
            # RentaWEB..."), las filas con formato "DD/MM/YYYY desc importe EUR"
            # son el detalle. Capturarlas.
            m = _plat_row_re.match(line)
            if m:
                plataforma_header_passed = True
                fecha_p, desc_p, imp_str = m.group(1), m.group(2).strip(), m.group(3)
                try:
                    importe_p = Decimal(imp_str.replace(".", "").replace(",", "."))
                except Exception:
                    continue
                gastos_plataforma_items.append({
                    "fecha":      fecha_p,
                    "descripcion": desc_p,
                    "importe":    importe_p,
                })

    if (bruto == 0 and retencion == 0
            and interest_credit == 0 and interest_debit == 0
            and gastos_plataforma_total == 0):
        return None

    # Fallback: si el txt aún no incluye la línea nueva (informes antiguos),
    # se deja en None para que el template detecte el caso.
    return {
        "bruto_total": bruto,
        "retencion_total": retencion,
        "cdi_recuperable": cdi,
        "bruto_extranjero_con_retencion": bruto_ext_con_ret if bruto_ext_con_ret > 0 else None,
        "retencion_es": retencion_es,
        "interest_credit_total": interest_credit,
        "interest_debit_total":  interest_debit,
        "gastos_plataforma_total": gastos_plataforma_total,
        "gastos_plataforma_items": gastos_plataforma_items,
    }


def parse_opciones_txt(filepath: str) -> dict | None:
    """Extrae los totales del bloque RESUMEN del informe_opciones_YYYY.txt
    para mostrar la tabla de declaración agrupada en el PDF.

    Devuelve dict con:
      - pl_total              : P&L neto del bloque 1624-1654
      - primas_cobradas       : primas cobradas declarables
      - primas_pagadas        : primas pagadas (buy-to-close)
      - gastos                : gastos / comisiones
      - fecha_primera         : DD/MM/YYYY de la primera apertura del año
      - fecha_ultima          : DD/MM/YYYY del último cierre/expiración
    """
    import re as _re

    if not os.path.exists(filepath):
        return None

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    pl = Decimal("0")
    primas_cobradas = Decimal("0")
    primas_pagadas = Decimal("0")
    gastos = Decimal("0")
    fecha_primera = ""
    fecha_ultima = ""

    re_fecha = _re.compile(r"(\d{2}/\d{2}/\d{4})")

    for line in content.splitlines():
        line_stripped = line.strip()
        line_lower = line_stripped.lower()

        # P&L neto del bloque (línea con "▶")
        is_pnl_total = (
            ("p&l" in line_lower or "p&l" in line_stripped)
            and ("otros elementos patrimoniales" in line_lower
                 or "casilla 1626" in line_lower)
        )
        if is_pnl_total:
            pl = _extract_amount(line_stripped)
            continue

        if "primas cobradas declarables" in line_lower:
            primas_cobradas = _extract_amount(line_stripped)
        elif "primas pagadas (buy-to-close)" in line_lower:
            primas_pagadas = _extract_amount(line_stripped)
        elif "gastos (comisiones)" in line_lower and "(-)" in line_stripped:
            # La línea del RESUMEN: "(-) Gastos (comisiones)  :  20,52 EUR"
            # Distinguirla de las líneas de detalle por contrato (que no
            # tienen "(-)" delante).
            gastos = _extract_amount(line_stripped)
        elif "fecha primera apertura" in line_lower:
            m = re_fecha.search(line_stripped)
            if m:
                fecha_primera = m.group(1)
        elif "fecha último cierre" in line_lower or "fecha ultimo cierre" in line_lower:
            m = re_fecha.search(line_stripped)
            if m:
                fecha_ultima = m.group(1)

    if pl == 0 and primas_cobradas == 0:
        return None

    return {
        "pl_total":         pl,
        "primas_cobradas":  primas_cobradas,
        "primas_pagadas":   primas_pagadas,
        "gastos":           gastos,
        "fecha_primera":    fecha_primera,
        "fecha_ultima":     fecha_ultima,
    }


def parse_corporativas_txt(filepath: str) -> dict:
    """Extrae eventos de informe_corporativas_YYYY.txt para el PDF IRPF.

    Devuelve un dict con listas:
      - liberadas:       [{nombre, isin, fecha, cantidad, tipo (MIXTA|PRORRATEADA|SIN_HISTORICO),
                           coste_unit_prorrateado, qty_previa, coste_lib, derechos_comprados}]
      - residuales:      [{emisor, isin, fecha, cantidad, importe_teorico, absorbido_por_comision}]
      - complejos:       [{nombre, isin, fecha, descripcion}]
      - rts_sin_clasif:  [{isin, cantidad, importe}]
      - venta_mercado:   [{emisor, isin, fecha, cantidad, importe_eur}]
      - spin_offs:       [{nombre, isin_nueva, nombre_matriz, isin_matriz,
                           fecha_efectiva, cantidad}]

    El parsing es best-effort sobre el texto; si el fichero no existe o tiene
    un formato inesperado, devuelve listas vacías.
    """
    empty = {
        "liberadas":      [],
        "residuales":     [],
        "complejos":      [],
        "rts_sin_clasif": [],
        "venta_mercado":  [],
        "spin_offs":      [],
        "rights_exercised": [],
    }
    if not filepath or not os.path.exists(filepath):
        return empty

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    liberadas      = []
    residuales     = []
    complejos      = []
    rts_sin_clasif = []
    venta_mercado  = []
    spin_offs      = []
    rights_exercised = []

    # Los bloques RIGHTS, RESIDUAL, COMPLEX, SPIN_OFF, RIGHTS_EXERCISED...
    # se separan por "[TAG]" al inicio.
    import re
    blocks = re.split(r"\n(?=\s*\[(?:RIGHTS|RESIDUAL|COMPLEX|SPLIT|CONTRASPLIT|ISIN_CHANGE|SPIN_OFF|RIGHTS_EXERCISED)\])", content)

    def _grab(block, label, cast=str):
        m = re.search(rf"{re.escape(label)}\s*:\s*(.+?)\s*(?:\n|$)", block)
        if not m:
            return None
        val = m.group(1).strip()
        if cast is Decimal:
            try:
                return _extract_amount(val)
            except Exception:
                return None
        if cast is int:
            try:
                return int(float(val.replace(",", ".").split()[0]))
            except Exception:
                return None
        return val

    for block in blocks:
        if re.search(r"\[RIGHTS\]", block):
            nombre_m = re.search(r"\[RIGHTS\]\s+([^\|]+?)\s*\|\s*(\d{2}/\d{2}/\d{4})", block)
            nombre   = nombre_m.group(1).strip() if nombre_m else ""
            fecha    = nombre_m.group(2) if nombre_m else ""
            isin     = _grab(block, "ISIN derechos") or ""

            if "Liberada (MIXTA)" in block or "Liberada PURA" in block or "Liberada:" in block:
                coste_prorr_m = re.search(r"Coste unit\. recalculado\s*:\s*([\d.,]+)", block)
                qty_prev_m    = re.search(r"Posición previa\s*:\s*(\d+)", block)
                coste_mixto_m = re.search(r"coste\s+([\d.,]+)\s*EUR", block)
                derechos_c_m  = re.search(r"Incluye\s+(\d+)\s+derechos comprados", block)
                if "(MIXTA)" in block:
                    tipo = "MIXTA"
                elif coste_prorr_m:
                    tipo = "PRORRATEADA"
                else:
                    tipo = "SIN_HISTORICO"
                cantidad_m = re.search(r"Liberada[^:]*?:\s*(\d+)\s*acc", block)
                liberadas.append({
                    "nombre": nombre,
                    "isin":   isin,
                    "fecha":  fecha,
                    "cantidad": int(cantidad_m.group(1)) if cantidad_m else 1,
                    "tipo":   tipo,
                    "coste_unit_prorrateado": _extract_amount(coste_prorr_m.group(1)) if coste_prorr_m else None,
                    "qty_previa":  int(qty_prev_m.group(1)) if qty_prev_m else None,
                    "coste_lib":   _extract_amount(coste_mixto_m.group(1)) if (tipo == "MIXTA" and coste_mixto_m) else None,
                    "derechos_comprados": int(derechos_c_m.group(1)) if derechos_c_m else None,
                })

            if "Vendidos en mercado" in block:
                vnd_m = re.search(r"Vendidos en mercado:\s*(\d+)\s*→\s*([\d.,]+)\s*EUR", block)
                if vnd_m:
                    venta_mercado.append({
                        "emisor": nombre,
                        "isin":   isin,
                        "fecha":  fecha,
                        "cantidad": int(vnd_m.group(1)),
                        "importe_eur": _extract_amount(vnd_m.group(2)),
                    })

            if "SIN CLASIFICAR" in block:
                qty_m = re.search(r"venta de (\d+)\s*derechos detectada", block)
                imp_m = re.search(r"Importe:\s*([\d.,]+)\s*EUR", block)
                rts_sin_clasif.append({
                    "isin":      isin,
                    "emisor":    nombre,
                    "fecha":     fecha,
                    "cantidad":  int(qty_m.group(1)) if qty_m else None,
                    "importe":   _extract_amount(imp_m.group(1)) if imp_m else None,
                })

        elif re.search(r"\[RESIDUAL\]", block):
            head_m = re.search(r"\[RESIDUAL\]\s+([^\|]+?)\s*\|\s*(\d{2}/\d{2}/\d{4})", block)
            emisor = head_m.group(1).strip() if head_m else ""
            fecha  = head_m.group(2) if head_m else ""
            isin   = _grab(block, "ISIN derechos") or ""
            cant_m = re.search(r"Derechos retirados por el emisor\s*:\s*(\d+)", block)
            imp_m  = re.search(r"Importe teórico\s*:\s*([\d.,]+)\s*EUR", block)
            absorbido = "absorbido por la comisión" in block.lower() or "importe < 2 eur" in block.lower()
            residuales.append({
                "emisor":   emisor,
                "isin":     isin,
                "fecha":    fecha,
                "cantidad": int(cant_m.group(1)) if cant_m else 0,
                "importe_teorico": _extract_amount(imp_m.group(1)) if imp_m else None,
                "absorbido_por_comision": absorbido,
            })

        elif re.search(r"\[COMPLEX\]", block):
            head_m = re.search(r"\[COMPLEX\]\s+(.+?)(?:\s*\|\s*(\d{2}/\d{2}/\d{4}))?\s*\n", block)
            nombre = head_m.group(1).strip() if head_m else ""
            fecha  = head_m.group(2) if head_m else ""
            isin   = _grab(block, "ISIN origen") or ""
            desc   = _grab(block, "Descripción") or ""
            complejos.append({
                "nombre":      nombre,
                "isin":        isin,
                "fecha":       fecha,
                "descripcion": desc,
            })

        elif re.search(r"\[SPIN_OFF\]", block):
            # Cabecera: "[SPIN_OFF] 3M CO → SOLVENTUM CORP"
            head_m = re.search(r"\[SPIN_OFF\]\s+(.+?)\s*[→]\s*(.+?)\s*\n", block)
            nombre_matriz = head_m.group(1).strip() if head_m else ""
            nombre_nueva  = head_m.group(2).strip() if head_m else ""
            fecha_eff = _grab(block, "Fecha efectiva") or ""
            isin_matriz_full = _grab(block, "Empresa matriz") or ""
            isin_nueva_full  = _grab(block, "Empresa escindida") or ""
            cantidad_str = _grab(block, "Acciones recibidas") or "0"
            # Extraer ISINs de los strings "3M CO  (ISIN US88579Y1010) — sigue cotizando"
            isin_matriz_m = re.search(r"\(ISIN\s+([A-Z0-9]{12})\)", isin_matriz_full)
            isin_nueva_m  = re.search(r"\(ISIN\s+([A-Z0-9]{12})\)", isin_nueva_full)
            try:
                cantidad = int(float(cantidad_str.split()[0].replace(",", ".")))
            except Exception:
                cantidad = 0
            resuelto_auto_raw = (_grab(block, "Resuelto auto") or "").strip().lower()
            resuelto_auto = resuelto_auto_raw == "si"
            fuente_auto   = (_grab(block, "Fuente") or "").strip() if resuelto_auto else ""
            coste_aplicado_str = (_grab(block, "Coste aplicado") or "").strip()
            spin_offs.append({
                "nombre":         nombre_nueva,
                "isin":           isin_nueva_m.group(1) if isin_nueva_m else "",
                "isin_nueva":     isin_nueva_m.group(1) if isin_nueva_m else "",
                "nombre_matriz":  nombre_matriz,
                "isin_matriz":    isin_matriz_m.group(1) if isin_matriz_m else "",
                "fecha_efectiva": fecha_eff,
                "cantidad":       cantidad,
                "resuelto_auto":  resuelto_auto,
                "fuente_auto":    fuente_auto,
                "coste_aplicado": coste_aplicado_str,
            })

        elif re.search(r"\[RIGHTS_EXERCISED\]", block):
            head_m = re.search(r"\[RIGHTS_EXERCISED\]\s+(.+?)\s*\|\s*(\d{2}/\d{2}/\d{4})", block)
            nombre = head_m.group(1).strip() if head_m else ""
            fecha = head_m.group(2) if head_m else ""
            isin_ord = _grab(block, "ISIN acción ordinaria") or ""
            qty_str  = _grab(block, "Acciones recibidas") or "0"
            coste_str = _grab(block, "Coste real pagado") or "0"
            try:
                qty = int(float(qty_str.split()[0].replace(",", ".")))
            except Exception:
                qty = 0
            try:
                coste = _extract_amount(coste_str)
            except Exception:
                coste = Decimal("0")
            rights_exercised.append({
                "nombre":   nombre,
                "isin_ord": isin_ord,
                "fecha":    fecha,
                "cantidad": qty,
                "coste":    coste,
            })

    return {
        "liberadas":      liberadas,
        "residuales":     residuales,
        "complejos":      complejos,
        "rts_sin_clasif": rts_sin_clasif,
        "venta_mercado":  venta_mercado,
        "spin_offs":      spin_offs,
        "rights_exercised": rights_exercised,
    }


def parse_dividendos_by_isin(filepath: str) -> dict[str, Decimal]:
    """Extrae dividendo bruto por ISIN del informe_dividendos_YYYY.txt.

    Formato del fichero (secciones por empresa):
        ─────────
          NOMBRE  [PAIS]  ISIN
        ─────────
          Dividendo bruto         : 123,45 EUR
    """
    import re
    if not os.path.exists(filepath):
        return {}

    result = {}
    current_isin = None

    re_isin = re.compile(r'\b([A-Z]{2}[A-Z0-9]{10})\b')

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # Detect ISIN in section header line (between ─── separators)
            m = re_isin.search(stripped)
            if m and '─' not in stripped and 'bruto' not in stripped.lower():
                current_isin = m.group(1)
                continue
            # Extract bruto for current ISIN
            if current_isin and "dividendo bruto" in stripped.lower() and ":" in stripped:
                amount = _extract_amount(stripped)
                if amount > 0:
                    result[current_isin] = result.get(current_isin, Decimal("0")) + amount

    return result


def parse_primas_by_isin(filepath: str) -> dict[str, Decimal]:
    """Extrae primas netas de opciones por subyacente (ISIN) del informe_opciones_YYYY.txt.

    Formato:
        ─────────
          SUBYACENTE  CALL/PUT  Strike X  Venc. Y
        ─────────
          Primas cobradas (ventas): 123,45 EUR
          Primas pagadas (compras): 45,00 EUR

    Agrupa por subyacente y devuelve {isin_subyacente: primas_cobradas - primas_pagadas}.
    Solo incluye opciones de la sección de "otros elementos patrimoniales"
    (casillas 1624-1654), que son las normales cerradas/expiradas.
    """
    import re
    if not os.path.exists(filepath):
        return {}

    result = {}
    current_sub = None
    in_section_1626 = False

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            upper = stripped.upper()

            # Sección de opciones cerradas/expiradas (otros elementos patrimoniales).
            # Aceptar terminología nueva y la antigua "CASILLA 1626" por compatibilidad.
            if (("OTROS ELEMENTOS PATRIMONIALES" in upper and "CERRADAS" in upper)
                    or "CASILLA 1626" in upper
                    or ("CERRADAS" in upper and "EXPIRADAS" in upper)):
                in_section_1626 = True
            elif stripped.startswith("═") and in_section_1626:
                # New major section → stop
                in_section_1626 = False

            if not in_section_1626:
                continue

            # Section header: subyacente name before CALL/PUT
            if ("CALL" in upper or "PUT" in upper) and "Strike" in stripped and '─' not in stripped:
                # Extract subyacente (first token before CALL/PUT)
                parts = stripped.split()
                sub_parts = []
                for p in parts:
                    if p.upper() in ("CALL", "PUT"):
                        break
                    sub_parts.append(p)
                current_sub = " ".join(sub_parts).strip()
                continue

            if current_sub:
                if "primas cobradas" in stripped.lower() and ":" in stripped:
                    amount = _extract_amount(stripped)
                    result[current_sub] = result.get(current_sub, Decimal("0")) + amount
                elif "primas pagadas" in stripped.lower() and ":" in stripped:
                    amount = _extract_amount(stripped)
                    result[current_sub] = result.get(current_sub, Decimal("0")) - amount

    return result


def _extract_amount(line: str) -> Decimal:
    """Extrae el importe después del último ':' en la línea.

    Formatos soportados: '3001,34 EUR', '+1779,06 EUR', '-35,00 EUR'
    """
    import re
    # Tomar la parte después del último ':'
    if ":" in line:
        after_colon = line.rsplit(":", 1)[1].strip()
    else:
        after_colon = line.strip()
    # Eliminar 'EUR' y todo lo que venga después (e.g., "→ casilla 0588")
    if "EUR" in after_colon:
        after_colon = after_colon[:after_colon.index("EUR")].strip()
    after_colon = after_colon.strip()
    # Formato español con separador de miles: +3.001,34 o 524,25
    match = re.match(r'^[+-]?\d{1,3}(?:\.\d{3})*,\d{2}$', after_colon)
    if match:
        s = match.group().replace(".", "").replace(",", ".")
        return Decimal(s)
    # Sin separador de miles: +1779,06 o 301,07
    match = re.match(r'^[+-]?\d+,\d{2}$', after_colon)
    if match:
        s = match.group().replace(",", ".")
        return Decimal(s)
    return Decimal("0")
