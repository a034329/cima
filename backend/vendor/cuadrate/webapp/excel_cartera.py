"""
excel_cartera — XLSX maestro de IRPF (sustituye al CSV plano).

Genera un único fichero `cartera_valores_irpf_YYYY.xlsx` con varias hojas
inter-conectadas por fórmulas. El usuario puede editar manualmente los costes
(típicamente tras una escisión/spin-off donde hay que aplicar el prorrateo
del Art. 37.1.a LIRPF) y los totales, G/P y casillas de RentaWEB se
recalculan automáticamente.

Hojas:
  1. Resumen        — tabla maestra "qué casilla y qué importe" para RentaWEB.
                     Cada celda total es una fórmula que apunta a las otras hojas.
  2. Operaciones    — todas las filas A/AD/AL/T/VD/SP del ejercicio. Editable.
                     Las que requieren revisión van resaltadas en amarillo.
  3. G_P_por_valor  — una fila por ISIN vendido en el año con coste FIFO
                     agregado (EDITABLE) e importe de transmisión. G/P = fórmula.
                     Total por casilla con SUMA. Esta es la hoja CRÍTICA: aquí
                     el usuario aplica los ajustes manuales (prorrateo de
                     spin-offs, costes corregidos, etc.).
  4. Dividendos     — por pagador con bruto, retención origen, retención ES y
                     deducción CDI recuperable. Totales con SUMA.
  5. Perdidas_arrastradas — saldos negativos de ejercicios anteriores con
                     cálculo de aplicable este año (casilla 1186+).
  6. Opciones       — primas por contrato/serie y P&L (casillas 1624-1654).

Disclaimer y branding Cuádrate en cada hoja (logo + leyenda + protección de
las celdas que no deben editarse).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side
)
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

from clasificacion_origen import clasificar_isin


def _safe_cell(v):
    """Neutraliza la inyección de fórmulas en celdas XLSX (CSV/formula
    injection): Excel evalúa una celda de texto que empieza por = + - @ (o
    tab/CR) como fórmula. Los nombres de empresa y descripciones vienen del
    CSV del usuario y el XLSX se abre por el propio usuario o su asesor (y se
    envía por email). Prefijamos un apóstrofo para forzar interpretación como
    texto. Solo aplica a strings; números/fechas/None pasan intactos.
    """
    if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


# ── Paleta Cuádrate (mismos colores que el PDF y el email) ────────────────────
C_AZUL       = "0B2B8F"
C_AZUL_2     = "1E40AF"   # secundario para gradientes y bordes
C_ORO        = "E6B763"   # acento del wordmark [Cuádrate] (corchetes y "e" cursiva final)
C_TINTA      = "0B1220"
C_GRIS       = "718096"
C_BORDE      = "e2e8f0"
C_FONDO      = "f7fafc"
C_AMARILLO   = "fde68a"   # editable / requiere revisión (visible sobre blanco)
C_AMARILLO_2 = "f59e0b"
C_AMARILLO_3 = "fef3c7"   # tono más suave para fondos grandes
C_VERDE      = "d1fae5"
C_ROJO       = "fee2e2"
C_AZUL_SOFT  = "eff6ff"

# ── Estilos comunes ───────────────────────────────────────────────────────────
THIN  = Side(border_style="thin", color=C_BORDE)
THICK = Side(border_style="medium", color=C_AZUL)
NO_BORDER = Border()
BORDER_ALL = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
BORDER_BOTTOM_THICK = Border(left=THIN, right=THIN, top=THIN, bottom=THICK)

FONT_TITLE  = Font(name="Calibri", size=18, bold=True, color=C_TINTA)
FONT_SUB    = Font(name="Calibri", size=11, color=C_GRIS)
FONT_H1     = Font(name="Calibri", size=14, bold=True, color=C_AZUL)
FONT_H2     = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
FONT_BOLD   = Font(name="Calibri", size=10, bold=True, color=C_TINTA)
FONT_BODY   = Font(name="Calibri", size=10, color=C_TINTA)
FONT_MUTED  = Font(name="Calibri", size=9, color=C_GRIS, italic=True)
FONT_TOTAL  = Font(name="Calibri", size=11, bold=True, color=C_AZUL)
FONT_EDIT   = Font(name="Calibri", size=10, bold=True, color="92400e")

FILL_HEADER     = PatternFill("solid", fgColor=C_AZUL)
FILL_SUBHEADER  = PatternFill("solid", fgColor=C_AZUL_SOFT)
FILL_TOTAL      = PatternFill("solid", fgColor=C_FONDO)
FILL_EDITABLE   = PatternFill("solid", fgColor=C_AMARILLO)
FILL_CALCULATED = PatternFill("solid", fgColor="e5e7eb")   # gris claro: celda con fórmula calculada, no editar directamente
FILL_DISCLAIMER = PatternFill("solid", fgColor="fff8e1")
FILL_LOGO_BG    = PatternFill("solid", fgColor="FFFFFF")

ALIGN_LEFT   = Alignment(horizontal="left",   vertical="center", wrap_text=False)
ALIGN_RIGHT  = Alignment(horizontal="right",  vertical="center")
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_WRAP   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

EUR_FMT       = '#,##0.00 €;[Red]-#,##0.00 €;"—"'
EUR_FMT_BOLD  = '#,##0.00 €;[Red]-#,##0.00 €;"—"'
NUM_FMT       = '#,##0.00;[Red]-#,##0.00'
INT_FMT       = '#,##0;[Red]-#,##0'

# Disclaimer estándar — varía ligeramente por hoja
DISCLAIMER_BASE = (
    "Aviso legal: este informe es una herramienta de preparación fiscal "
    "elaborada a partir de los datos aportados. No constituye asesoramiento "
    "fiscal vinculante. El usuario es responsable de verificar los datos antes "
    "de presentar la declaración ante la AEAT."
)
DISCLAIMER_EDIT = (
    "  •  Las celdas con fondo amarillo son editables: si modificas un coste "
    "(p. ej. el prorrateo tras una escisión), las G/P, totales por casilla y "
    "compensaciones se RECALCULAN automáticamente en todas las hojas."
)


def _to_dec(x) -> Decimal:
    """Convierte a Decimal de forma robusta (acepta None, str, float)."""
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _to_float(x) -> float:
    """Convierte a float para escritura segura en celdas Excel."""
    return float(_to_dec(x))


def _read_raw_operaciones(path: str) -> list[dict]:
    """Lee un cartera_valores_irpf_*.xlsx (o .csv legacy) y devuelve filas
    crudas para mostrarlas en la hoja Operaciones.

    Mantiene el código AEAT original (AD/AL/T/VD/SP) y la denominación tal cual,
    sin normalizar al motor — esta función es solo para presentación visual,
    no para FIFO.

    Devuelve lista de dicts con los mismos campos que enriquece generar_irpf:
        _tipo_csv, isin, _denom_csv, fecha (str), cantidad, _importe_csv,
        _gastos_csv, _ejercicio_opcion_str, _strike_str, _prima_str, _tipo_op_str
    """
    import os, csv, re
    if not os.path.exists(path):
        return []
    # Año del nombre del fichero (cartera_valores_irpf_YYYY.xlsx). Si existe,
    # solo conservamos las filas cuya fecha caiga en ese año: el XLSX maestro
    # incluye un bloque histórico con ops de años anteriores como referencia
    # visual, y leerlas tal cual provoca duplicación exponencial cuando el
    # generador encadena años (2018 absorbe 2017; 2019 absorbe 2017+2018 con
    # 2017 ya duplicado; etc. → ops 2017 × 2^(N-2018) en el XLSX del año N).
    m_year = re.search(r"_(\d{4})\.(?:xlsx|csv)$", os.path.basename(path))
    year_target = int(m_year.group(1)) if m_year else None
    out: list[dict] = []
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        if "Operaciones" not in wb.sheetnames:
            wb.close()
            return []
        ws = wb["Operaciones"]
        # Localizar cabecera y leerla para detectar formato (viejo 12 cols vs
        # nuevo 15 cols con desglose de gastos en cols 8-10).
        header_row = None
        header_cells: list = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if not row:
                continue
            a = str(row[0]).strip().lower() if row[0] is not None else ""
            if a == "tipo":
                header_row = r_idx
                header_cells = list(row)
                break
        if header_row is None:
            wb.close()
            return []
        # Detectar formato nuevo: "Coms. broker" en alguna celda entre 8-10
        formato_nuevo = any(
            h is not None
            and "coms" in str(h).strip().lower()
            and "broker" in str(h).strip().lower()
            for h in header_cells[7:11]
        )
        for row in ws.iter_rows(min_row=header_row + 1, values_only=True):
            if not row or row[0] is None:
                continue
            tipo = str(row[0]).strip().upper()
            if tipo not in ("A", "AD", "AL", "T", "TR", "VD", "SP"):
                continue
            row_list = list(row)
            # Normalizar al esquema canónico (12 cols viejas) eliminando las
            # 3 columnas insertadas en el formato nuevo. Las cifras desglosadas
            # están sumadas en Gastos (idx 6); no se necesitan aquí.
            if formato_nuevo:
                row_list = row_list[:7] + row_list[10:]
            fecha_str = _format_fecha_xlsx(row_list[3])
            if year_target is not None and fecha_str:
                m_fecha = re.search(r"(\d{4})", fecha_str)
                if m_fecha and int(m_fecha.group(1)) != year_target:
                    continue
            out.append({
                "_tipo_csv":   tipo,
                "isin":        str(row_list[1] or "").strip(),
                "_denom_csv":  str(row_list[2] or "").strip(),
                "fecha":       fecha_str,
                "cantidad":    _to_dec(row_list[4]),
                "_importe_csv": _to_dec(row_list[5]),
                "_gastos_csv":  _to_dec(row_list[6]),
                "_ejercicio_opcion_str": str(row_list[7] or "").strip() if len(row_list) > 7 else "",
                "_strike_str":  str(row_list[8] or "").strip() if len(row_list) > 8 else "",
                "_prima_str":   str(row_list[9] or "").strip() if len(row_list) > 9 else "",
                "_tipo_op_str": str(row_list[10] or "").strip() if len(row_list) > 10 else "",
            })
        wb.close()
    else:
        # CSV legacy
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader, None)  # cabecera
            for row in reader:
                if len(row) < 7:
                    continue
                tipo = (row[0] or "").strip().upper()
                if tipo not in ("A", "AD", "AL", "T", "TR", "VD", "SP"):
                    continue
                fecha_str = row[3].strip()
                if year_target is not None and fecha_str:
                    m_fecha = re.search(r"(\d{4})", fecha_str)
                    if m_fecha and int(m_fecha.group(1)) != year_target:
                        continue
                out.append({
                    "_tipo_csv":   tipo,
                    "isin":        row[1].strip(),
                    "_denom_csv":  row[2].strip(),
                    "fecha":       fecha_str,
                    "cantidad":    _parse_num_es(row[4]),
                    "_importe_csv": _parse_num_es(row[5]),
                    "_gastos_csv":  _parse_num_es(row[6]),
                    "_ejercicio_opcion_str": row[7].strip() if len(row) > 7 else "",
                    "_strike_str":  row[8].strip() if len(row) > 8 else "",
                    "_prima_str":   row[9].strip() if len(row) > 9 else "",
                    "_tipo_op_str": row[10].strip() if len(row) > 10 else "",
                })
    return out


def _parse_num_es(s) -> Decimal:
    """Decimal desde número español (coma decimal, punto miles)."""
    if s is None or (isinstance(s, str) and not s.strip()):
        return Decimal("0")
    if isinstance(s, (int, float, Decimal)):
        return _to_dec(s)
    s = str(s).strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _format_fecha_xlsx(v) -> str:
    """Devuelve fecha como string dd/mm/yyyy, acepta date/datetime/str."""
    from datetime import date, datetime
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return str(v).strip()


# ── Cabecera Cuádrate y disclaimer (helpers) ──────────────────────────────────

def _put_brand_header(ws, ejercicio: int, doctype: str, fecha_gen: str,
                      ncols: int = 8) -> int:
    """Coloca el logo Cuádrate + título + disclaimer al inicio de una hoja.

    Devuelve el número de la primera fila libre tras la cabecera.
    """
    # Wordmark "[Cuádrate]" con runs de color (Calibri no es Fraunces, pero el
    # patrón visual se mantiene: corchetes y "e" cursiva final en oro,
    # "Cuádrat" en tinta. Mismos colores que el SVG de la SPA, los PDFs y el
    # email).
    from openpyxl.cell.text import InlineFont
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    f_oro       = InlineFont(rFont="Calibri", sz=22, b=True, color=C_ORO)
    f_oro_ital  = InlineFont(rFont="Calibri", sz=22, b=True, color=C_ORO, i=True)
    f_tinta     = InlineFont(rFont="Calibri", sz=22, b=True, color=C_TINTA)
    wordmark = CellRichText(
        TextBlock(f_oro,      "["),
        TextBlock(f_tinta,    "Cuádrat"),
        TextBlock(f_oro_ital, "e"),
        TextBlock(f_oro,      "]"),
    )
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    cell = ws.cell(row=1, column=1, value=wordmark)
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=0)
    ws.row_dimensions[1].height = 32

    # Tipo de documento + ejercicio
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    sub = ws.cell(row=2, column=1, value=f"{doctype} · Ejercicio {ejercicio}")
    sub.font = FONT_TITLE
    sub.alignment = ALIGN_LEFT
    ws.row_dimensions[2].height = 24

    # Fecha + leyenda corta
    ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=ncols)
    leg = ws.cell(row=3, column=1,
                  value=f"Generado el {fecha_gen}  ·  Casillas referidas a la "
                        f"campaña Renta {ejercicio} (la AEAT renumera el "
                        f"formulario cada año)")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_LEFT

    # Disclaimer
    ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=ncols)
    disc = ws.cell(row=4, column=1, value=DISCLAIMER_BASE + DISCLAIMER_EDIT)
    disc.font = Font(name="Calibri", size=9, color="92400e", italic=True)
    disc.fill = FILL_DISCLAIMER
    disc.alignment = ALIGN_WRAP
    disc.border = Border(left=Side("thin", color=C_AMARILLO_2),
                         right=Side("thin", color=C_AMARILLO_2),
                         top=Side("thin", color=C_AMARILLO_2),
                         bottom=Side("medium", color=C_AMARILLO_2))
    ws.row_dimensions[4].height = 38

    # Espaciado
    ws.row_dimensions[5].height = 8
    return 6


def _put_section_title(ws, row: int, title: str, ncols: int = 8) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=title)
    c.font = FONT_H1
    c.alignment = ALIGN_LEFT
    c.border = Border(bottom=Side("medium", color=C_AZUL))
    ws.row_dimensions[row].height = 22
    return row + 2


def _put_table_header(ws, row: int, headers: list[str], col_widths: list[int]):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = FONT_H2
        c.fill = FILL_HEADER
        c.alignment = ALIGN_CENTER
        c.border = BORDER_ALL
    ws.row_dimensions[row].height = 22
    for i, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _editable_cell(cell, value, *, eur=True, comment: str | None = None):
    """Marca una celda como editable (amarilla) y aplica formato."""
    cell.value = value
    cell.fill = FILL_EDITABLE
    cell.font = FONT_EDIT
    cell.number_format = EUR_FMT if eur else NUM_FMT
    cell.border = BORDER_ALL
    cell.alignment = ALIGN_RIGHT
    if comment:
        from openpyxl.comments import Comment
        cell.comment = Comment(comment, "Cuádrate")


def _data_cell(cell, value, *, eur=False, bold=False, align=None):
    cell.value = value
    cell.font = FONT_BOLD if bold else FONT_BODY
    cell.alignment = align or (ALIGN_RIGHT if eur or isinstance(value, (int, float, Decimal)) else ALIGN_LEFT)
    cell.border = BORDER_ALL
    if eur:
        cell.number_format = EUR_FMT


# ─────────────────────────────────────────────────────────────────────────────
# GENERADOR PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def generate_cartera_xlsx(
    *,
    ejercicio: int,
    output_path: str,
    operaciones: list[dict],         # filas del ejercicio actual (enriquecidas)
    fifo_results,                    # FIFOResults del motor
    dividendos_resumen: Optional[list[dict]] = None,   # de calcular_resumen_dividendos
    opciones_por_contrato: Optional[list[dict]] = None,
    opciones_totales: Optional[dict] = None,
    compensacion=None,
    fecha_generacion: Optional[str] = None,
    paths_anteriores: Optional[list[str]] = None,  # cartera_*.{xlsx,csv} de años previos
    ops_motor_con_ids: Optional[list[dict]] = None,
    ops_historicas_con_ids: Optional[list[dict]] = None,
    fx_pl: Optional[dict] = None,    # {fx: [...], tbills: [...]} de parse_ibkr_fx_pl (solo IBKR)
    ibkr_interest: Optional[list[dict]] = None,  # de parse_ibkr_interest (intereses IBKR Credit/Debit/Bond)
    tr_staking: Optional[list[dict]] = None,  # de parse_tr_staking (FREE_RECEIPT cripto en TR) → RCM 0027 vía DGT V1766-22
    gastos_plataforma: Optional[list[dict]] = None,  # comisiones DeGiro conectividad/custodia → campo "Gastos administración y depósito" del popup de 0029 (totaliza 0037)
    futuros_por_contrato: Optional[list[dict]] = None,  # de calcular_resumen_futuros — IBKR Asset Category=Futures
    futuros_totales: Optional[dict] = None,
) -> str:
    """Genera el XLSX maestro. Devuelve `output_path`.

    `paths_anteriores` se usan para cargar las operaciones de años previos
    como informativas en la hoja Operaciones (separadas por bloques de año).

    `ops_motor_con_ids` y `ops_historicas_con_ids` son las mismas operaciones
    procesadas por el motor FIFO con `_lote_id` anotado en cada compra. Se
    usan para emparejar cada compra con su fila Excel y emitir las fórmulas
    prorrateadas en `G_P_por_valor` que ligan el coste con hoja Operaciones.
    """
    if fecha_generacion is None:
        fecha_generacion = datetime.now().strftime("%d/%m/%Y %H:%M")

    wb = Workbook()
    # Eliminar hoja por defecto
    wb.remove(wb.active)

    # ISINs y matrices de spin-offs detectados — para resaltar en G_P_por_valor
    spin_off_info = {}   # {isin_escindida: {nombre_matriz, isin_matriz}}
    for op in operaciones:
        if op.get('_es_spinoff'):
            spin_off_info[op.get('isin', '')] = {
                'nombre_matriz': op.get('_spinoff_matriz', ''),
                'isin_matriz':   op.get('_spinoff_isin_matriz', ''),
            }
    # Las matrices también van resaltadas (al usuario hay que recordarle que
    # debe REDUCIR su coste al añadir el de la escindida). Recolectamos las
    # claves PRIMERO y luego las añadimos para evitar mutar el dict mientras
    # se itera.
    matrices_a_marcar = [
        info['isin_matriz'] for info in list(spin_off_info.values())
        if info.get('isin_matriz')
    ]
    for isin_m in matrices_a_marcar:
        spin_off_info.setdefault(isin_m, {})['es_matriz'] = True

    # Cargar operaciones históricas (años anteriores), agrupadas por año.
    # Leemos el contenido presentacional (con código AEAT + denominación) de
    # los XLSX/CSV antiguos. Después emparejamos cada una con la op del motor
    # (que tiene `_lote_id`) para poder emitir fórmulas en G_P_por_valor.
    operaciones_historicas: dict = {}
    if paths_anteriores:
        for path in paths_anteriores:
            try:
                ops_raw = _read_raw_operaciones(path)
            except Exception:
                continue
            for op in ops_raw:
                f = op.get("fecha", "")
                if "/" in f and len(f) == 10:
                    year = int(f.split("/")[-1])
                    operaciones_historicas.setdefault(year, []).append(op)
        operaciones_historicas.pop(ejercicio, None)

    # Enriquecer las ops (actuales e históricas) con el `_lote_id` asignado
    # por el motor, haciendo lookup por (isin, fecha, cantidad).
    def _key_op(isin: str, fecha: str, cantidad) -> tuple:
        """Clave de emparejamiento op_raw ↔ op_motor."""
        return (isin, str(fecha), str(_to_dec(cantidad)))

    lote_lookup: dict = {}
    for src in (ops_motor_con_ids or []):
        if src.get("tipo") == "A" and src.get("_lote_id"):
            f = src.get("fecha")
            f_str = f.strftime("%d/%m/%Y") if hasattr(f, "strftime") else str(f)
            lote_lookup[_key_op(src["isin"], f_str, src["cantidad"])] = src["_lote_id"]
    for src in (ops_historicas_con_ids or []):
        if src.get("tipo") == "A" and src.get("_lote_id"):
            f = src.get("fecha")
            f_str = f.strftime("%d/%m/%Y") if hasattr(f, "strftime") else str(f)
            lote_lookup[_key_op(src["isin"], f_str, src["cantidad"])] = src["_lote_id"]

    def _anotar_lote_ids(ops: list[dict]) -> None:
        for op in ops:
            tipo_csv = op.get("_tipo_csv", op.get("tipo", ""))
            if tipo_csv not in ("A", "AD", "AL"):
                continue
            if op.get("_lote_id"):
                continue
            key = _key_op(op.get("isin", ""), op.get("fecha", ""), op.get("cantidad", 0))
            lid = lote_lookup.get(key)
            if lid:
                op["_lote_id"] = lid

    _anotar_lote_ids(operaciones)
    for year_ops in operaciones_historicas.values():
        _anotar_lote_ids(year_ops)

    # 1. Operaciones (la creo primero porque G_P_por_valor la referencia)
    ws_ops, op_row_by_isin, lote_id_to_row = _build_operaciones(
        wb, ejercicio, fecha_generacion, operaciones,
        operaciones_historicas=operaciones_historicas,
    )

    # 2. G/P por valor (FIFO) — fórmulas que referencian filas de Operaciones
    ws_gp, gp_totales_por_casilla, gp_row_ranges = _build_gp_por_valor(
        wb, ejercicio, fecha_generacion, fifo_results, op_row_by_isin,
        spin_off_info=spin_off_info,
        lote_id_to_row=lote_id_to_row,
    )

    # 3. Dividendos
    ws_div, div_totales = _build_dividendos(
        wb, ejercicio, fecha_generacion, dividendos_resumen
    )

    # 4. Pérdidas arrastradas
    ws_perd, perd_totales = _build_perdidas_arrastradas(
        wb, ejercicio, fecha_generacion, compensacion
    )

    # 5. Opciones
    ws_opt, opt_totales = _build_opciones(
        wb, ejercicio, fecha_generacion, opciones_por_contrato, opciones_totales
    )

    # 5.4. Futuros financieros IBKR — agregado por contrato.
    # BETA. Realized P/L que IBKR consolida por contrato (incluye multiplier
    # y FX). Doctrina: Manual práctico AEAT cap 11 §14 → casilla 1626 c.4.
    fut_pl_ref = None
    if futuros_por_contrato:
        _, _fut_info = _build_futuros(
            wb, ejercicio, fecha_generacion,
            futuros_por_contrato, futuros_totales,
        )
        fut_pl_ref = _fut_info.get('pl_ref')

    # 5.5. Forex y Treasury Bills (solo IBKR — informativas, no suman al Resumen)
    fx_total_ref = None
    tbills_total_ref = None
    if fx_pl:
        if fx_pl.get('fx'):
            _, fx_total_ref = _build_forex(wb, ejercicio, fecha_generacion, fx_pl['fx'])
        if fx_pl.get('tbills'):
            _, tbills_total_ref = _build_treasury_bills(wb, ejercicio, fecha_generacion, fx_pl['tbills'])

    # 5.6. Tasas externas (informativa — Art. 35.1.b LIRPF)
    # Trazabilidad por jurisdiccion (ITF, UK Stamp Duty, FTT, HK Stamp Duty).
    # YA estan sumadas al coste en hoja Operaciones — esta hoja es auditoria.
    _build_tasas_externas(wb, ejercicio, fecha_generacion, operaciones,
                          operaciones_historicas=operaciones_historicas)

    # 5.65. Gastos del broker (Art. 26.1.a LIRPF) — campo "Gastos de
    # administración y depósito" del popup individual de RCM, totaliza en 0037.
    plataforma_total_ref = None
    if gastos_plataforma:
        _, plataforma_total_ref = _build_gastos_plataforma(
            wb, ejercicio, fecha_generacion, gastos_plataforma)

    # 5.7. Intereses (IBKR Credit/Debit/Bond + TR cuenta remunerada) — casilla 0027
    # NOTA: el nombre del parámetro `ibkr_interest` se conserva por historia;
    # la lista incluye TR Sucursal ES desde 2a00e34. Cada fila tiene un
    # campo `broker` ('IBKR' o 'TR'); las filas sin broker explícito (IBKR
    # pre-cambio) se tratan como 'IBKR' por defecto.
    interest_credit_ref = None
    interest_ret_es_ref = None
    if ibkr_interest:
        # Solo crea la hoja si el total Credit+Bond > 0 sera referenciable
        # desde el Resumen. Si solo hay Debit, la hoja se crea para trazabilidad
        # pero interest_credit_ref se queda en None (Debit no va al Resumen).
        _, candidate_ref, candidate_ret_es_ref = _build_intereses(
            wb, ejercicio, fecha_generacion, ibkr_interest)
        # Solo referenciamos al Resumen si hay credit/bond > 0
        from decimal import Decimal as _Dec
        _has_credit = any(
            r.get('tipo') in ('credit', 'bond_interest')
            and _Dec(str(r.get('importe_eur', 0))) > 0
            for r in ibkr_interest
        )
        if _has_credit:
            interest_credit_ref = candidate_ref
        # Retención española de intereses (TR Sucursal ES) → línea 0027 (popup).
        interest_ret_es_ref = candidate_ret_es_ref

    # 5.7b. Staking de criptomonedas (Trade Republic) — RCM Art. 25.2 LIRPF,
    # DGT V1766-22 (26-7-2022), valorado en EUR al precio de mercado en el
    # momento de cada recepción (Art. 43.1 LIRPF, satisfecho en especie).
    # Casilla 0027 (analogía con intereses); alternativa doctrinal 0031 — la
    # propia consulta NO fija casilla. Hoja propia para trazabilidad por evento.
    staking_total_ref = None
    if tr_staking:
        _, staking_total_ref = _build_staking(
            wb, ejercicio, fecha_generacion, tr_staking)

    # 5.8. Desglose por broker — sirve de auditoría cruzada contra los
    # datos que cada broker reporta a la AEAT (Modelo 198 para sucursales
    # españolas, nada para los extranjeros). Le pasamos las mismas dos
    # fuentes de RCM que alimentan los totales: el resumen por ISIN de
    # dividendos (que mantiene la lista de `eventos` por broker) y la lista
    # combinada IBKR/TR de intereses.
    _build_por_broker(
        wb, ejercicio, fecha_generacion,
        dividendos_resumen=dividendos_resumen,
        intereses=ibkr_interest,
    )

    # 6. Resumen — la creo al final pero la pongo como primera hoja
    ws_res = _build_resumen(
        wb, ejercicio, fecha_generacion,
        gp_totales_por_casilla=gp_totales_por_casilla,
        div_totales=div_totales,
        perd_totales=perd_totales,
        opt_totales=opt_totales,
        fx_total_ref=fx_total_ref,
        tbills_total_ref=tbills_total_ref,
        interest_credit_ref=interest_credit_ref,
        interest_ret_es_ref=interest_ret_es_ref,
        staking_total_ref=staking_total_ref,
        plataforma_total_ref=plataforma_total_ref,
        futuros_totales=futuros_totales,
        fut_pl_ref=fut_pl_ref,
    )

    # Re-ordenar para que Resumen sea la primera
    wb.move_sheet(ws_res, offset=-len(wb.sheetnames) + 1)

    wb.save(output_path)

    # Sidecar JSON con los totales esperados (lo que las fórmulas evalúan al
    # abrir el XLSX). Sirve de referencia para el test de consistencia
    # PDF ↔ XLSX sin necesidad de evaluar fórmulas Excel con librerías
    # externas. Los valores se computan aquí desde los mismos matches FIFO
    # que alimentan el template del PDF → si coinciden, el usuario verá los
    # mismos números en ambos ficheros.
    import json as _json

    # G/P por casilla, excluyendo regla 2M del total deducible (el que va a
    # RentaWEB; coincide con la fila "TOTAL DEDUCIBLE" del XLSX y con
    # "Resultado neto deducible" del PDF).
    matches_anyo_xlsx = [m for m in fifo_results.matches if m.ejercicio_fiscal == ejercicio]
    def _gpi(m):
        # G/P integrable: la G/P bruta menos la pérdida diferida aflorada
        # (Art. 33.5.f LIRPF último párrafo). El coste a declarar en
        # RentaWEB ya incluye la PD aflorada sumada a la adquisición (forma
        # A doctrinal), por tanto los totales del XLSX deben usar la G/P
        # integrable para cuadrar con el PDF y con lo que RentaWEB calcula.
        return m.ganancia_perdida - getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0"))
    def _por_casilla(pred):
        return sum((_gpi(m) for m in matches_anyo_xlsx if pred(m)), Decimal("0"))

    def _split_casilla(pred):
        """Devuelve (neto_deducible, no_deducible_2m) para los matches del
        bloque `pred`, con la MISMA fórmula que pdf_generator._gp_split:
            no_deducible_2m = Σ G/P bruta de los matches 2M con pérdida
            neto_deducible  = Σ G/P integrable (gpi) − no_deducible_2m
        Antes el XLSX restaba la PD aflorada también de la parte no-deducible
        (usaba _gpi en ambos buckets), divergiendo del PDF justo cuando un
        match es 2M Y aflora una PD (esquina de cascada). Unificado al PDF."""
        bruto = sum((_gpi(m) for m in matches_anyo_xlsx if pred(m)), Decimal("0"))
        no_ded = sum((m.ganancia_perdida for m in matches_anyo_xlsx
                      if pred(m) and m.regla_2_meses and m.ganancia_perdida < 0),
                     Decimal("0"))
        return (bruto - no_ded, no_ded)

    # Casilla 0326-0340 = SOLO acciones cotizadas (instrument_type=STOCK).
    # En Renta 2025+ los ETFs van a 2224-2236; los derivados a 1624-1654;
    # la cripto a 1800-1806; las SOCIMI ES a 0324/0325. Antes, casilla_0326_0340
    # incluía todo y generaba inconsistencia entre PDF (que ya separa) y XLSX.
    _es_stock = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'STOCK'
    _es_socimi = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'SOCIMI'
    total_326_338_deducible, total_326_338_no_ded_2m = _split_casilla(
        lambda m: (not m.es_derecho) and _es_stock(m))
    total_341_346 = _por_casilla(lambda m: m.es_derecho)
    # Casillas 0324/0325 — SOCIMI españolas (Ley 11/2009).
    total_324_325_deducible, total_324_325_no_ded_2m = _split_casilla(
        lambda m: (not m.es_derecho) and _es_socimi(m))
    # Casilla 0031 — RCM por transmisión de activos financieros: bonos
    # (BOND) y ETCs físicos (ETC, DGT V0267-25) por separado. El RCM
    # negativo con recompra de homogéneos ±2M se difiere por el Art. 25.2
    # LIRPF último párrafo (espejo de la 2M en RCM) — mismo split
    # deducible / no-deducible que en acciones.
    _es_bond = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'BOND'
    _es_etc = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'ETC'
    total_0031_bonos_deducible, total_0031_bonos_no_ded_2m = _split_casilla(_es_bond)
    total_0031_etc_deducible, total_0031_etc_no_ded_2m = _split_casilla(_es_etc)
    # Casillas 2224-2236 (ETFs UCITS), 1800-1806 (cripto) y 1624-1654 c.4
    # (derivados estructurados). Se añaden al sidecar para que el test de
    # consistencia PDF↔XLSX cubra TODAS las casillas, no solo acciones/0031.
    _es_etf = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'ETF'
    _es_crypto = lambda m: getattr(m, 'instrument_type', 'STOCK') == 'CRYPTO'
    _es_deriv = lambda m: getattr(m, 'instrument_type', 'STOCK') in ('DERIVATIVE', 'STRUCTURED')
    total_2224_deducible, total_2224_no_ded_2m = _split_casilla(_es_etf)
    total_1800_deducible, total_1800_no_ded_2m = _split_casilla(_es_crypto)
    total_1624_deducible, total_1624_no_ded_2m = _split_casilla(_es_deriv)
    n_etf = sum(1 for m in matches_anyo_xlsx if _es_etf(m))
    n_crypto = sum(1 for m in matches_anyo_xlsx if _es_crypto(m))
    n_deriv = sum(1 for m in matches_anyo_xlsx if _es_deriv(m))

    def _d(x):
        if x is None:
            return None
        if isinstance(x, Decimal):
            return float(x)
        return float(x)

    # Importes de dividendos (suma por casilla) — reflejo de lo que apuntan
    # las fórmulas del Resumen a la hoja Dividendos.
    div_bruto = sum(
        (d.get("bruto", Decimal("0")) for d in (dividendos_resumen or [])),
        Decimal("0")
    )
    # Retención española (0591): el campo retencion_es de TODAS las filas —
    # incluye tanto emisores ES (ACS) como dividendos extranjeros con el 19%
    # que practica TR Sucursal ES post-migración (J&J). Antes sumaba
    # `recuperable` solo de es_nacional, perdiendo la parte española de los
    # extranjeros (que caía erróneamente como exceso CDI).
    div_ret_es = sum(
        (d.get("retencion_es", Decimal("0"))
         for d in (dividendos_resumen or [])),
        Decimal("0")
    )
    # CDI (casilla 0588) = solo recuperable de pagadores EXTRANJEROS.
    # La retención de pagadores nacionales se introduce en el campo "Retenciones"
    # del popup individual de 0029 (no tiene casilla independiente; sumario
    # calculado por RentaWEB).
    div_cdi = sum(
        (d.get("recuperable", Decimal("0"))
         for d in (dividendos_resumen or [])
         if not d.get("es_nacional")),
        Decimal("0")
    )

    # Opciones P&L: el total agregado viene del dict `opciones_totales`
    opt_pl = None
    if opciones_totales is not None and "pl_neto" in opciones_totales:
        opt_pl = opciones_totales.get("pl_neto")

    # T-Bills (Letras del Tesoro, casilla 0030) — solo IBKR.
    # Son DECLARABLES por defecto: Art. 25.2 LIRPF no admite minimis.
    tbills_total = None
    if fx_pl and fx_pl.get("tbills"):
        tbills_total = sum(
            (Decimal(str(t.get("realized", 0))) for t in fx_pl["tbills"]),
            Decimal("0"),
        )

    sidecar = {
        "ejercicio": ejercicio,
        # Casillas 0326-0340 — Acciones cotizadas (Renta 2025+: ETFs separados en 2224-2236)
        "casilla_0326_0340": {
            "neto_deducible": _d(total_326_338_deducible),
            "no_deducible_2m": _d(total_326_338_no_ded_2m),
        },
        # Casillas 0341-0355 — Transmisión derechos suscripción
        "casilla_0341_0355": {
            "gp_total": _d(total_341_346),
        },
        # Casillas 0324/0325 — SOCIMI españolas (Ley 11/2009): G/P de
        # transmisión van a subapartado IIC/SOCIMI del F2, distinto de las
        # acciones cotizadas normales (0326-0340). Solo SOCIMI nacionales —
        # los REITs/SIIC extranjeros se tratan como acciones normales.
        "casilla_0324_0325": {
            "neto_deducible": _d(total_324_325_deducible),
            "no_deducible_2m": _d(total_324_325_no_ded_2m),
        },
        # Casilla 0031 — Bonos individuales (RCM transmisión/amortización).
        # no_deducible_2m = RCM negativos diferidos Art. 25.2 últ. párrafo.
        "casilla_0031": {
            "neto_deducible": _d(total_0031_bonos_deducible),
            "no_deducible_2m": _d(total_0031_bonos_no_ded_2m),
        },
        # Casilla 0031 (ETCs físicos) — misma casilla AEAT, desglose aparte
        # (coherente con la etiqueta "0031-ETC" de la hoja G_P_por_valor).
        "casilla_0031_etc": {
            "neto_deducible": _d(total_0031_etc_deducible),
            "no_deducible_2m": _d(total_0031_etc_no_ded_2m),
        },
        # Casillas 2224-2236 — ETFs UCITS (Renta 2025+). Solo si hay matches.
        "casilla_2224_2236": ({
            "neto_deducible": _d(total_2224_deducible),
            "no_deducible_2m": _d(total_2224_no_ded_2m),
        } if n_etf > 0 else None),
        # Casillas 1800-1806 — Criptomonedas.
        "casilla_1800_1806": ({
            "neto_deducible": _d(total_1800_deducible),
            "no_deducible_2m": _d(total_1800_no_ded_2m),
        } if n_crypto > 0 else None),
        # Casillas 1624-1654 clave 4 — Derivados estructurados.
        "casilla_1624_1654_derivados": ({
            "neto_deducible": _d(total_1624_deducible),
            "no_deducible_2m": _d(total_1624_no_ded_2m),
        } if n_deriv > 0 else None),
        # Casilla 0029 — Dividendos brutos. La retención IRPF española y los
        # gastos de administración y depósito se introducen DENTRO del popup
        # individual de 0029, no en casillas independientes.
        "casilla_0029": {
            "bruto": _d(div_bruto),
            "retencion_es": _d(div_ret_es),  # campo del popup, sumario calculado
        },
        # Casilla 0588 — Deducción doble imposición internacional
        "casilla_0588": {
            "cdi_recuperable": _d(div_cdi),
        },
        # Casillas 1624-1654 — Otros elementos patrimoniales (clave 4: opciones, forex)
        "casilla_1624_1654": {
            "pl_opciones": _d(opt_pl) if opt_pl is not None else None,
        },
        # Casilla 0027 (staking cripto) — RCM Art. 25.2 LIRPF satisfecho en
        # especie (DGT V1766-22), valorado en EUR al momento de cada
        # recepción. Alternativa doctrinal: 0031 (cuota idéntica). Esta
        # clave alimenta también la sección de staking del PDF (que la lee
        # de este sidecar) — antes el staking solo existía en stdout y en
        # la hoja Staking del XLSX y el PDF lo omitía.
        "casilla_0027_staking": ({
            "total": _d(sum((Decimal(str(s.get("importe_eur", 0)))
                             for s in tr_staking), Decimal("0"))),
            "n_eventos": len(tr_staking),
            "activos": sorted({s.get("asset", "") for s in tr_staking
                               if s.get("asset")}),
        } if tr_staking else None),
    }
    # Casilla 0030 — Letras del Tesoro (sólo si IBKR aporta T-Bills).
    # FX (Art. 33 LIRPF) NO entra al sidecar: aplica minimis y la decisión
    # es del usuario (sigue como informativo en el Resumen).
    if tbills_total is not None and tbills_total != 0:
        sidecar["casilla_0030"] = {
            "tbills_total": _d(tbills_total),
        }

    # Desglose RCM por broker (mismo origen de datos que la hoja
    # Por_broker del XLSX). Permite que pdf_generator pueda mostrar la
    # tabla equivalente en el PDF sin re-parsear el XLSX.
    broker_agg = aggregate_por_broker(dividendos_resumen, ibkr_interest)
    sidecar["por_broker"] = {
        broker: {
            "div_bruto":   _d(totals['div_bruto']),
            "div_ret_org": _d(totals['div_ret_org']),
            "div_cdi":     _d(totals['div_cdi']),
            "div_ret_nac": _d(totals['div_ret_nac']),
            "int_bruto":   _d(totals['int_bruto']),
            "int_ret":     _d(totals['int_ret']),
        }
        for broker, totals in broker_agg.items()
        if any(v != 0 for v in totals.values())
    }

    sidecar_path = output_path.rsplit(".", 1)[0] + ".totals.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        _json.dump(sidecar, f, indent=2, ensure_ascii=False)

    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Operaciones
# ─────────────────────────────────────────────────────────────────────────────

def _build_operaciones(wb, ejercicio: int, fecha_gen: str,
                      operaciones: list[dict],
                      operaciones_historicas: dict | None = None) -> tuple:
    """Crea la hoja con las operaciones del ejercicio actual seguidas de
    bloques de años anteriores (informativos para FIFO multi-año).

    Devuelve (worksheet,
              dict {isin: [filas_excel_de_compra_del_ejercicio]},
              dict {lote_id: fila_excel}).
    """
    ws = wb.create_sheet("Operaciones")
    next_row = _put_brand_header(ws, ejercicio, "Operaciones — ejercicio + histórico", fecha_gen, ncols=16)

    # Leyenda
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=16)
    leg = ws.cell(row=next_row, column=1,
                  value="Códigos AEAT: AD = Adquisición · AL = Acción totalmente "
                        "liberada (importe 0; RentaWEB prorratea al vender) · "
                        "T = Transmisión · VD = Venta de derechos · SP = Split. "
                        "Las columnas 'Coste/Importe' (F) y 'Gastos' (G) de las "
                        "compras están en AMARILLO = son editables. La columna G "
                        "es el TOTAL de gastos; las columnas H/I/J desglosan ese "
                        "total en Comisión broker, AutoFX (cambio divisa) y Tasas "
                        "externas (ITF, UK Stamp Duty, French FTT, HK Stamp Duty "
                        "— Art. 35.1.b LIRPF). Si cambias el total en G, las G/P "
                        "y totales de la hoja G_P_por_valor y del Resumen se "
                        "recalculan AUTOMÁTICAMENTE. Las operaciones de años "
                        "anteriores aparecen en bloques separados: también son "
                        "editables y también propagan al total (FIFO histórico real). "
                        "💼 Esta hoja está mapeada 1:1 con 'Mi cartera de valores' "
                        "de RentaWEB — puedes copiar las filas tal cual a la "
                        "aplicación oficial AEAT (mismo orden de columnas y "
                        "mismos códigos de operación) y mantener así la cartera "
                        "viva entre campañas, sin volver a cargar el histórico "
                        "cada año.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 78
    next_row += 2

    headers = ["Tipo", "ISIN", "Denominación", "Fecha", "Cantidad",
               "Coste/Importe (EUR)", "Gastos (EUR)",
               "Coms. broker", "AutoFX", "Tasas ext.",
               "Ej. opción", "Strike", "Prima (EUR)", "Tipo opción",
               "Broker", "Tipo activo"]
    widths  = [8, 16, 60, 12, 10, 16, 12, 11, 9, 11, 10, 10, 12, 12, 14, 12]

    op_row_by_isin: dict = defaultdict(list)
    lote_id_to_row: dict = {}         # lote_id → fila excel de la compra
    requiere_revision_count = [0]     # mutable para el helper

    def _seccion_titulo(title: str, color_hex: str = C_AZUL,
                        font_color: str = "FFFFFF") -> int:
        nonlocal next_row
        ws.merge_cells(start_row=next_row, start_column=1,
                       end_row=next_row, end_column=16)
        c = ws.cell(row=next_row, column=1, value=title)
        c.font = Font(name="Calibri", size=12, bold=True, color=font_color)
        c.fill = PatternFill("solid", fgColor=color_hex)
        c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[next_row].height = 22
        next_row += 1
        return next_row

    def _escribir_fila(op: dict, *, registrar_indice: bool = True) -> int:
        nonlocal next_row
        tipo_csv = op.get('_tipo_csv', op.get('tipo', ''))
        denom    = op.get('_denom_csv', op.get('nombre', ''))
        fecha    = op.get('fecha', '')
        cantidad = _to_float(op.get('cantidad', 0))
        importe  = _to_float(op.get('_importe_csv', op.get('importe_eur', 0)))
        gastos   = _to_float(op.get('_gastos_csv', op.get('gastos_eur', 0)))
        # Desglose del coste (suma a `gastos`). En operaciones que no proceden
        # del parser de DeGiro/IBKR (corporativas, SP, derechos sintetizados)
        # estos campos pueden no existir → fallback a 0.
        gastos_broker_v   = _to_float(op.get('gastos_broker', 0) or 0)
        gastos_autofx_v   = _to_float(op.get('gastos_autofx', 0) or 0)
        gastos_externos_v = _to_float(op.get('gastos_externos', 0) or 0)
        ej       = op.get('_ejercicio_opcion_str', '')
        strike   = op.get('_strike_str', '')
        prima    = op.get('_prima_str', '')
        tipo_op  = op.get('_tipo_op_str', '')

        # Los spin-offs resueltos automaticamente (catalogo spinoffs_conocidos)
        # NO requieren accion del usuario — coste ya aplicado al ratio del
        # Form 8937 + lotes de la matriz ajustados. Solo los spin-offs sin
        # catalogo (flujo manual) llevan amarillo "EDITA esta celda".
        es_spinoff_pendiente = (op.get('_es_spinoff')
                                and not op.get('_spinoff_resuelto_auto'))
        requiere_amarillo = bool(es_spinoff_pendiente or op.get('_requiere_revision'))

        ws.cell(row=next_row, column=1, value=tipo_csv)
        ws.cell(row=next_row, column=2, value=op.get('isin', ''))
        ws.cell(row=next_row, column=3, value=_safe_cell(denom))
        ws.cell(row=next_row, column=4, value=fecha)
        ws.cell(row=next_row, column=5, value=cantidad)
        c_imp = ws.cell(row=next_row, column=6, value=importe)
        c_gas = ws.cell(row=next_row, column=7, value=gastos)
        # Desglose informativo (no editable). Solo se rellena si hay valor > 0.
        c_brk = ws.cell(row=next_row, column=8,
                        value=gastos_broker_v if gastos_broker_v else None)
        c_afx = ws.cell(row=next_row, column=9,
                        value=gastos_autofx_v if gastos_autofx_v else None)
        c_ext = ws.cell(row=next_row, column=10,
                        value=gastos_externos_v if gastos_externos_v else None)
        ws.cell(row=next_row, column=11, value=ej)
        ws.cell(row=next_row, column=12, value=strike if strike else None)
        ws.cell(row=next_row, column=13, value=prima if prima else None)
        ws.cell(row=next_row, column=14, value=tipo_op)
        ws.cell(row=next_row, column=15, value=op.get('broker', ''))
        # Columna 16 — Tipo activo (Acción/ETF/Derivado/Cripto/Bono/SOCIMI/ETC).
        # Editable solo si la clasificación es marginal (UNKNOWN).
        from openpyxl.comments import Comment
        instr_type = op.get('instrument_type', 'STOCK')
        instr_unknown = bool(op.get('instrument_type_unknown', False))
        _tipo_label = {
            'STOCK': 'Acción',
            'ETF': 'ETF',
            'DERIVATIVE': 'Derivado',
            'CRYPTO': 'Cripto',
            'BOND': 'Bono',
            'SOCIMI': 'SOCIMI ES',
            'ETC': 'ETC',
        }.get(instr_type, 'Acción')
        c_itype = ws.cell(row=next_row, column=16, value=_tipo_label)
        if instr_unknown:
            c_itype.fill = FILL_EDITABLE
            c_itype.font = FONT_EDIT
            c_itype.comment = Comment(
                'Clasificación marginal — verificar tipo.\n'
                'Valores válidos: "Acción", "ETF", "Derivado", "Cripto", "Bono", "SOCIMI ES", "ETC".\n'
                'Casillas AEAT por tipo:\n'
                '  · Acción → 0326-0340 (incluye REITs/SIIC extranjeros)\n'
                '  · ETF (Renta 2025+) → 2224-2236; (Renta 2024 o ant.) → 0326-0340\n'
                '  · Derivado (Factor, Turbo, Mini, KO, Bonus, ETN) → 1624-1654 clave 4\n'
                '  · Cripto → 1800-1806\n'
                '  · Bono → 0031 (transmisión) + 0027 (cupones cobrados)\n'
                '  · SOCIMI ES (Ley 11/2009, sólo nacionales) → 0324/0325\n'
                '  · ETC (oro/plata/platino físico, DGT V0267-25) → 0031 RCM',
                'Cuádrate'
            )

        for col in range(1, 17):
            c = ws.cell(row=next_row, column=col)
            c.border = BORDER_ALL
            c.font = FONT_BODY
            if col == 1:
                c.alignment = ALIGN_CENTER
                c.font = FONT_BOLD
            elif col == 4:
                c.alignment = ALIGN_CENTER
            elif col in (5, 6, 7, 8, 9, 10, 12, 13):
                c.alignment = ALIGN_RIGHT
            else:
                c.alignment = ALIGN_LEFT
        ws.cell(row=next_row, column=5).number_format = NUM_FMT
        c_imp.number_format = EUR_FMT
        c_gas.number_format = EUR_FMT
        c_brk.number_format = EUR_FMT
        c_afx.number_format = EUR_FMT
        c_ext.number_format = EUR_FMT

        # Celda "Coste/Importe" (F) de las compras es la EDITABLE por defecto:
        # cambiar ahí se propaga automáticamente a G_P_por_valor y al Resumen
        # mediante las fórmulas prorrateadas por lote. La columna G (Gastos)
        # también es editable porque forma parte del coste de adquisición FIFO.
        if tipo_csv in ('A', 'AD', 'AL'):
            c_imp.fill = FILL_EDITABLE
            c_imp.font = FONT_EDIT
            c_gas.fill = FILL_EDITABLE
            c_gas.font = FONT_EDIT

        # ─── Spin-off resuelto automaticamente via catalogo ───
        # Va FUERA del bloque amarillo: la fila NO requiere accion del
        # usuario (coste ya aplicado + lotes matriz ya reducidos). Solo
        # comentario informativo con la trazabilidad y fuente.
        if op.get('_es_spinoff') and op.get('_spinoff_resuelto_auto'):
            from openpyxl.comments import Comment
            matriz = op.get('_spinoff_matriz', '')
            isin_m = op.get('_spinoff_isin_matriz', '')
            fuente = op.get('_spinoff_fuente', 'spinoffs_conocidos.json')
            ratio_e = op.get('_spinoff_ratio_escindida', '')
            ratio_m = op.get('_spinoff_ratio_matriz', '')
            c_imp.comment = Comment(
                f"ESCISIÓN resuelta automáticamente.\n\n"
                f"Coste prorrateado aplicado segun Art. 37.1.a §4 LIRPF:\n"
                f"   ratio_escindida = {ratio_e}\n"
                f"   ratio_matriz_residual = {ratio_m}\n"
                f"Fuente: {fuente}\n\n"
                f"Las filas AD anteriores de {matriz} ({isin_m}) en esta\n"
                f"hoja YA estan ajustadas (multiplicadas por ratio_matriz).\n"
                f"Total preservado: el coste original se reparte entre\n"
                f"matriz y escindida, no se duplica.\n\n"
                f"Si discrepas con el ratio del catalogo (ej. tu broker o\n"
                f"asesor usa una asignacion distinta), EDITA esta celda y\n"
                f"recalcula tambien las filas AD de la matriz por el\n"
                f"complemento (1 - tu_ratio_escindida). Las G/P se\n"
                f"recalculan automaticamente.",
                "Cuádrate"
            )
            c_imp.comment.width = 400
            c_imp.comment.height = 250

        if requiere_amarillo:
            requiere_revision_count[0] += 1
            # Marcar TODA la fila en amarillo para resaltar la fila que
            # necesita revisión (escisiones manuales, eventos sin clasificar).
            for col in range(1, 17):
                c = ws.cell(row=next_row, column=col)
                c.fill = FILL_EDITABLE
            if op.get('_es_spinoff'):
                # ─── Flujo manual (catalogo vacio o no cubre este ISIN) ───
                # Coste provisional 0 + comentario amarillo con metodologia
                # y ejemplo numerico (3M -> Solventum).
                from openpyxl.comments import Comment
                matriz = op.get('_spinoff_matriz', '')
                isin_m = op.get('_spinoff_isin_matriz', '')
                c_imp.comment = Comment(
                    f"ESCISIÓN de {matriz} ({isin_m}) — coste provisional 0.\n\n"
                    f"Aplica el coste prorrateado según Art. 37.1.a §4 LIRPF:\n"
                    f"   r = ValorMdo_escindida / (ValorMdo_matriz_post + ValorMdo_escindida)\n"
                    f"en la fecha efectiva. (Form 8937 IRS o nota CNMV del emisor.)\n\n"
                    f"PASO 1 — Esta celda: EDITA con coste = (coste_total_matriz × r).\n\n"
                    f"PASO 2 — TODAS las filas AD anteriores de {matriz} ({isin_m}):\n"
                    f"   multiplica el Coste/Importe de cada una por (1 − r).\n"
                    f"   NO restes un importe fijo. El reparto es PROPORCIONAL al coste\n"
                    f"   de cada lote (lotes caros siguen siendo proporcionalmente caros).\n\n"
                    f"PASO 3 — Si tu cartera está en 'Mi cartera de valores' de RentaWEB:\n"
                    f"   edita el 'Valor de adquisición' de cada operación AD original\n"
                    f"   de {matriz} bajándolo por (1 − r). No hay código MCV específico\n"
                    f"   para escisión; la edición es legítima — no rectifica declaraciones\n"
                    f"   pasadas, sólo ajusta la base FIFO para futuras transmisiones.\n\n"
                    f"─── EJEMPLO (3M → Solventum, abr-2024, r = 0,1552 oficial Form 8937) ───\n"
                    f"Tenías 3 lotes de 3M:\n"
                    f"  L1  2017  10 acc × 180 € = 1.800 €\n"
                    f"  L2  2019  20 acc × 200 € = 4.000 €\n"
                    f"  L3  2022  30 acc × 130 € = 3.900 €\n"
                    f"  Total matriz pre = 9.700 €\n\n"
                    f"Coste asignado a Solventum (esta fila):\n"
                    f"  9.700 × 0,1552 = 1.505,44 €    ← edita esta celda\n\n"
                    f"Lotes 3M post (multiplicar cada uno × 0,8448):\n"
                    f"  L1 → 1.800 × 0,8448 = 1.520,64 €  (152,06 €/acc)\n"
                    f"  L2 → 4.000 × 0,8448 = 3.379,20 €  (168,96 €/acc)\n"
                    f"  L3 → 3.900 × 0,8448 = 3.294,72 €  (109,82 €/acc)\n"
                    f"  Total matriz post = 8.194,56 €\n\n"
                    f"Comprobación: 8.194,56 + 1.505,44 = 9.700,00 €  ✓ (el coste se reparte, no se duplica)\n"
                    f"─────────────────────────────────────────────────────",
                    "Cuádrate"
                )
                # Comment por defecto es 144×80 pt — insuficiente para este texto
                # extendido. Ampliamos para que el usuario lo lea sin scroll horizontal
                # al pasar el ratón en Excel.
                c_imp.comment.width = 500
                c_imp.comment.height = 540

        # ── Filas AD de la matriz ajustadas por spin-off catalogado ──
        # No llevan amarillo (no requieren accion del usuario) — solo un
        # comentario informativo para que el lector entienda por que el
        # coste de esa fila es distinto del importe del extracto original.
        if (not op.get('_es_spinoff')
                and op.get('_matriz_ajustada_por_spinoff')):
            from openpyxl.comments import Comment
            c_imp.comment = Comment(
                "Coste ajustado automáticamente por escisión (spin-off): "
                "este lote se multiplicó por ratio_matriz_residual segun "
                "Art. 37.1.a §4 LIRPF. El coste original del extracto se "
                "ha redistribuido entre la matriz y la escindida (ver fila "
                "AD de la escindida del mismo emisor para detalles).",
                "Cuádrate"
            )
            c_imp.comment.width = 360
            c_imp.comment.height = 140

        if tipo_csv in ('A', 'AD', 'AL'):
            if registrar_indice:
                op_row_by_isin[op.get('isin', '')].append(next_row)
            # lote_id_to_row se indexa SIEMPRE (tanto del ejercicio como del
            # histórico) — las fórmulas de G_P_por_valor pueden apuntar a
            # compras de años anteriores también.
            lote_id = op.get('_lote_id')
            if lote_id:
                lote_id_to_row[lote_id] = next_row

        row_written = next_row
        next_row += 1
        return row_written

    # ── Bloque ejercicio actual ────────────────────────────────────────────
    _seccion_titulo(f"▼ Ejercicio {ejercicio} (en curso) — operaciones a declarar")
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1
    for op in operaciones:
        _escribir_fila(op, registrar_indice=True)
    last_row_actual = next_row - 1
    # Autofilter sobre el bloque del ejercicio actual: el usuario puede
    # filtrar por Tipo (AD/AL/T/VD/SP), por ISIN, por Broker, por
    # "Tipo activo" (Acción/ETF/Derivado/Cripto/Bono) y demás. Los bloques
    # históricos (debajo) se quedan sin filtro para evitar mezclar años.
    if last_row_actual > header_row:
        ws.auto_filter.ref = f"A{header_row}:P{last_row_actual}"

    # ── Bloques de años históricos (informativos) ──────────────────────────
    if operaciones_historicas:
        # Nota destacada
        next_row += 1
        ws.merge_cells(start_row=next_row, start_column=1,
                       end_row=next_row, end_column=16)
        c = ws.cell(row=next_row, column=1,
                    value="ℹ️  Operaciones de años anteriores — INFORMATIVAS. "
                          "Se incluyen para que veas el FIFO completo. Si necesitas "
                          "corregir un coste agregado, edita la hoja G_P_por_valor "
                          "(no esta hoja: Excel no puede recalcular el FIFO multi-año).")
        c.font = Font(name="Calibri", size=10, italic=True, color="92400e")
        c.fill = FILL_DISCLAIMER
        c.alignment = ALIGN_WRAP
        c.border = Border(left=Side("thin", color=C_AMARILLO_2),
                          right=Side("thin", color=C_AMARILLO_2),
                          top=Side("thin", color=C_AMARILLO_2),
                          bottom=Side("thin", color=C_AMARILLO_2))
        ws.row_dimensions[next_row].height = 38
        next_row += 2

        for year in sorted(operaciones_historicas.keys(), reverse=True):
            ops_y = operaciones_historicas[year]
            if not ops_y:
                continue
            _seccion_titulo(
                f"▸ Ejercicio {year} — histórico ({len(ops_y)} operaciones, informativo)",
                color_hex="64748b"
            )
            _put_table_header(ws, next_row, headers, widths)
            next_row += 1
            for op in ops_y:
                # Históricas: no las indexamos (no afectan al FIFO referenciable)
                _escribir_fila(op, registrar_indice=False)

    # Congelar la cabecera de la primera tabla
    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return ws, dict(op_row_by_isin), lote_id_to_row


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: G/P por valor (FIFO)
# ─────────────────────────────────────────────────────────────────────────────

# Columnas de la hoja Operaciones para el coste agregado del lote.
# Coinciden con las columnas de `_build_operaciones`:
#   F (col 6) = Coste/Importe de la compra
#   G (col 7) = Gastos de la compra
# El motor FIFO suma ambos para el coste de adquisición del lote → la fórmula
# Excel debe hacer lo mismo para reproducir fielmente el cálculo.
_OPS_COL_IMPORTE = "F"
_OPS_COL_GASTOS  = "G"


def _build_formula_coste_fifo(matches: list, lote_id_to_row: dict) -> Optional[str]:
    """Construye una fórmula Excel que suma la contribución prorrateada de
    cada lote consumido en los matches del ejercicio.

    Para cada match:
      contribución = (cantidad_match / cantidad_lote) × (importe_lote + gastos_lote)

    Si todos los matches tienen mapeo a fila, devuelve la fórmula completa
    (con ROUND al céntimo). Si algún match no tiene lote mapeable (p.ej.
    scrip TYPE B con coste 0 sin lote real), devuelve None para que el
    caller use el valor numérico literal como fallback.
    """
    if not matches or not lote_id_to_row:
        return None
    partes = []
    for m in matches:
        lote_id = getattr(m, "lote_id", 0)
        if not lote_id:
            return None
        fila = lote_id_to_row.get(lote_id)
        if not fila:
            return None
        cant_lote = getattr(m, "cantidad_lote_original", None)
        if not cant_lote or cant_lote == 0:
            return None
        cant = m.cantidad
        # Coste total del lote = importe + gastos (col F + col G de Operaciones)
        ref_lote = f"(Operaciones!{_OPS_COL_IMPORTE}{fila}+Operaciones!{_OPS_COL_GASTOS}{fila})"
        if cant == cant_lote:
            # Venta consume el lote entero → coste íntegro del lote.
            partes.append(ref_lote)
        else:
            # Venta consume parte del lote → fracción literal evaluada en
            # Python. La cantidad se "congela" (editar la cantidad rompe el
            # FIFO; editar solo el coste sigue funcionando, que es el caso común).
            try:
                frac = float(cant) / float(cant_lote)
            except Exception:
                return None
            partes.append(f"{frac:.6f}*{ref_lote}")
    if not partes:
        return None
    # ROUND a 2 decimales para alinear con el formato EUR.
    return "=ROUND(" + "+".join(partes) + ",2)"


def _build_formula_coste_fifo_match(m, lote_id_to_row: dict) -> Optional[str]:
    """Variante por-match de `_build_formula_coste_fifo`. Devuelve la fórmula
    Excel para el coste de un único `FIFOMatch` (la contribución prorrateada
    del lote consumido en este match). None si el match no tiene lote
    mapeable (caso scrip TYPE B con coste 0).
    """
    if not lote_id_to_row:
        return None
    lote_id = getattr(m, "lote_id", 0)
    if not lote_id:
        return None
    fila = lote_id_to_row.get(lote_id)
    if not fila:
        return None
    cant_lote = getattr(m, "cantidad_lote_original", None)
    if not cant_lote or cant_lote == 0:
        return None
    cant = m.cantidad
    ref_lote = f"(Operaciones!{_OPS_COL_IMPORTE}{fila}+Operaciones!{_OPS_COL_GASTOS}{fila})"
    if cant == cant_lote:
        return f"=ROUND({ref_lote},2)"
    try:
        frac = float(cant) / float(cant_lote)
    except Exception:
        return None
    return f"=ROUND({frac:.6f}*{ref_lote},2)"


def _build_gp_por_valor(wb, ejercicio: int, fecha_gen: str,
                       fifo_results, op_row_by_isin: dict,
                       spin_off_info: dict | None = None,
                       lote_id_to_row: dict | None = None) -> tuple:
    """Hoja G/P por LOTE consumido — una fila por `FIFOMatch`.

    Cada match (par lote-de-compra ↔ tramo de venta) se emite como una fila
    independiente. La columna "Lote consumido" es un HYPERLINK a la fila
    exacta de la compra en la hoja Operaciones (incluido el histórico
    multi-año). El coste se sigue emitiendo como **fórmula** referenciada a
    Operaciones (prorrateo del lote según `cantidad_match / cantidad_lote`).

    Las pérdidas diferidas afloradas (Art. 33.5.f LIRPF último párrafo, DGT
    V3282-18) se muestran en una columna independiente; no se suman al
    coste de adquisición (decisión del usuario: presentación didáctica vs
    "forma A" doctrinal estricta). El neto fiscal que entra en el sidecar
    sigue siendo `G/P bruta − PD aflorada` para conservar la equivalencia
    con el PDF y con la mecánica de RentaWEB.

    Las filas de matches del mismo ISIN quedan contiguas y ordenadas
    cronológicamente (FIFO natural). Cada grupo de ISIN tiene una fila
    resumen al inicio (outline_level=0) con totales de las hijas
    (outline_level=1), permitiendo colapsar el detalle y volver a una vista
    "antigua" (1 fila por ISIN) con el botón [-] de Excel.

    Pie por casilla: tres líneas con SUMIFS sobre los rangos completos:
       · Pérdidas NO deducibles 2M (G/P bruta de matches 2M)
       · Pérdidas diferidas afloradas (Art. 33.5.f)
       · TOTAL DEDUCIBLE → RentaWEB (G/P fiscal de matches no-2M)

    Devuelve (worksheet, dict {casilla: (col_letter, fila_total)}, dict
    {casilla: [filas_match]}).
    """
    from openpyxl.comments import Comment
    lote_id_to_row = lote_id_to_row or {}
    ws = wb.create_sheet("G_P_por_valor")
    next_row = _put_brand_header(ws, ejercicio,
                                 "Ganancias y pérdidas FIFO por lote consumido",
                                 fecha_gen, ncols=16)

    # Outline: el resumen por ISIN va ARRIBA del bloque de hijas.
    ws.sheet_properties.outlinePr.summaryBelow = False
    ws.sheet_properties.outlinePr.summaryRight = False

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=16)
    leg = ws.cell(row=next_row, column=1,
                  value="Una fila por LOTE consumido en cada venta (FIFO). Los matches "
                        "del mismo ISIN aparecen contiguos y en orden cronológico. "
                        "Click en el botón [−] de la izquierda para COLAPSAR cada ISIN "
                        "a una sola fila resumen (vista \"agregada\" clásica). "
                        "La columna \"Lote consumido\" (G) enlaza directamente a "
                        "la compra origen en la hoja Operaciones — click para saltar. "
                        "La columna \"Coste FIFO\" (H) es una FÓRMULA prorrateada que "
                        "referencia el coste de la compra en Operaciones (columna F + G). "
                        "✏️  PARA CAMBIAR UN COSTE → edita la columna F (Coste/Importe) "
                        "de la compra en Operaciones. La fila aquí, la fila resumen del "
                        "ISIN, los totales por casilla y el Resumen se recalculan "
                        "automáticamente. La columna \"PD aflorada\" (M) es la pérdida "
                        "diferida que aflora por transmisión definitiva del lote "
                        "recomprado (Art. 33.5.f LIRPF último párrafo, DGT V3282-18) — "
                        "RESTA al G/P fiscal del match. La columna \"2M\" (L) marca "
                        "las pérdidas no deducibles por regla 2 meses (no entran en "
                        "el TOTAL DEDUCIBLE).")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 110
    next_row += 2

    # ─── Matches del ejercicio target ──────────────────────────────────────
    matches_anyo = [m for m in fifo_results.matches if m.ejercicio_fiscal == ejercicio]

    # Casilla por match (instrument_type + ejercicio):
    etfs_bloque_separado = (ejercicio >= 2025)
    def _casilla_de(m) -> str:
        itype = getattr(m, 'instrument_type', 'STOCK')
        if m.es_derecho:
            return "0341-0355"
        if itype == 'ETF':
            return "2224-2236" if etfs_bloque_separado else "0326-0340"
        if itype == 'DERIVATIVE':
            return "1624-1654"
        if itype == 'CRYPTO':
            return "1800-1806"
        if itype == 'BOND':
            return "0031"
        if itype == 'ETC':
            # Misma casilla AEAT que bonos (0031, RCM Art. 25.2 LIRPF) pero
            # diferenciada visualmente en la hoja G_P_por_valor y en el
            # Resumen para que el usuario sepa qué fila es bono y qué fila
            # es ETC físico. Doctrina: DGT V0267-25.
            return "0031-ETC"
        if itype == 'SOCIMI':
            return "0324/0325"
        return "0326-0340"

    _tipo_label_casilla = {
        "0326-0340": "Acción",
        "2224-2236": "ETF",
        "1624-1654": "Derivado/Opción",
        "1800-1806": "Cripto",
        "0341-0355": "Derecho suscripción",
        "0031":      "Bono",
        "0031-ETC":  "ETC físico",
        "0324/0325": "SOCIMI ES",
    }

    # Orden global: casilla → ISIN (alfabético por nombre) → fecha venta → fecha compra.
    # Las cabeceras de bloque (acciones → ETFs → derechos → otros) se respetan por
    # el orden de las casillas en `_orden_casillas`.
    _orden_casillas = ["0326-0340", "0324/0325", "2224-2236", "0341-0355",
                       "0031", "0031-ETC", "1624-1654", "1800-1806"]
    def _peso_casilla(c: str) -> int:
        try:
            return _orden_casillas.index(c)
        except ValueError:
            return 99

    enriched = []
    for m in matches_anyo:
        c = _casilla_de(m)
        enriched.append((c, m.nombre[:60], m))
    enriched.sort(key=lambda t: (
        _peso_casilla(t[0]), t[0],         # casilla (orden lógico, luego alfabético si no listado)
        t[2].isin, t[1],                   # ISIN, nombre
        t[2].fecha_venta, t[2].fecha_compra,
        getattr(t[2], 'lote_id', 0),
    ))

    # Cabecera
    headers = ["Casilla", "ISIN", "Empresa", "Origen",
               "Fecha venta", "Cantidad",
               "Lote consumido (origen)",
               "Coste FIFO", "Importe transm.", "Gastos venta",
               "G/P bruta", "2M",
               "PD aflorada", "G/P fiscal (→ casilla)",
               "Tipo activo", "Notas"]
    widths  = [11, 16, 32, 11, 12, 10,
               34,
               14, 14, 12,
               14, 6,
               13, 18,
               12, 40]
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1

    # Agrupar por (casilla, ISIN, nombre) manteniendo el orden ya establecido
    grupos_orden: list[tuple] = []
    grupos_dict: dict = defaultdict(list)
    seen = set()
    for c, nombre, m in enriched:
        key = (c, m.isin, nombre)
        if key not in seen:
            seen.add(key)
            grupos_orden.append(key)
        grupos_dict[key].append(m)

    first_data_row = next_row
    casilla_match_rows: dict = defaultdict(list)  # casilla → filas-match (para devolver)

    # Constantes de columnas (1-indexado)
    COL_CASILLA, COL_ISIN, COL_EMPRESA, COL_ORIGEN          = 1, 2, 3, 4
    COL_FVENTA, COL_CANTIDAD, COL_LOTE, COL_COSTE           = 5, 6, 7, 8
    COL_IMPORTE, COL_GASTOS, COL_GPBRUTA, COL_2M            = 9, 10, 11, 12
    COL_PD, COL_GPFISCAL, COL_TIPO, COL_NOTAS               = 13, 14, 15, 16

    L_CASILLA  = get_column_letter(COL_CASILLA)
    L_COSTE    = get_column_letter(COL_COSTE)
    L_IMPORTE  = get_column_letter(COL_IMPORTE)
    L_GASTOS   = get_column_letter(COL_GASTOS)
    L_GPBRUTA  = get_column_letter(COL_GPBRUTA)
    L_2M       = get_column_letter(COL_2M)
    L_PD       = get_column_letter(COL_PD)
    L_GPFISCAL = get_column_letter(COL_GPFISCAL)
    L_CANTIDAD = get_column_letter(COL_CANTIDAD)

    def _estilo_fila(row: int, fill: Optional[PatternFill] = None,
                     fuente: Optional[Font] = None):
        for col in range(1, 17):
            c = ws.cell(row=row, column=col)
            c.border = BORDER_ALL
            if fuente:
                c.font = fuente
            else:
                if c.font is None or c.font.name is None:
                    c.font = FONT_BODY
            if fill is not None:
                c.fill = fill
            if col == COL_CASILLA:
                c.alignment = ALIGN_CENTER
                if not fuente:
                    c.font = FONT_BOLD
            elif col == COL_ISIN:
                c.alignment = ALIGN_LEFT
                c.font = Font(name="Consolas", size=9, color=C_TINTA)
            elif col in (COL_ORIGEN, COL_FVENTA, COL_2M):
                c.alignment = ALIGN_CENTER
            elif col in (COL_CANTIDAD, COL_COSTE, COL_IMPORTE,
                         COL_GASTOS, COL_GPBRUTA, COL_PD, COL_GPFISCAL):
                c.alignment = ALIGN_RIGHT
            else:
                c.alignment = ALIGN_LEFT

    # Umbral de operativa intensiva (idéntico al del PDF). Si un ISIN supera
    # este número, la fila resumen indica el patrón detectado (DAYTRADING /
    # SWING). POLÍTICA UX (decisión 2026-06-09, test_rentaweb_aggregate):
    # las hijas arrancan SIEMPRE visibles — collapsed+hidden con
    # summaryBelow=False no renderiza el botón [+] en Excel real y el
    # usuario no descubriría el detalle oculto. El colapso es manual ([−]).
    _INTENSIVE_THRESHOLD = 20
    _DAYTRADING_INTRADAY_RATIO = 0.5

    def _clasif_pattern(ms):
        n = len(ms)
        if n < _INTENSIVE_THRESHOLD:
            return ("normal", 0, 0.0)
        n_intra = sum(1 for m in ms if m.fecha_compra == m.fecha_venta)
        ratio = n_intra / n if n else 0.0
        return (
            "daytrading" if ratio >= _DAYTRADING_INTRADAY_RATIO else "swing",
            n_intra,
            ratio,
        )

    # ─── 1ª pasada: escribir filas resumen + hijas ─────────────────────────
    for key in grupos_orden:
        (casilla, isin, nombre) = key
        grupo_matches = grupos_dict[key]
        n_hijas = len(grupo_matches)
        pattern, n_intra, ratio_intra = _clasif_pattern(grupo_matches)
        es_intensivo = pattern in ("daytrading", "swing")

        # Fila resumen (outline_level=0)
        summary_row = next_row
        primer_hijo = summary_row + 1
        ultimo_hijo = summary_row + n_hijas

        rng_coste    = f"{L_COSTE}{primer_hijo}:{L_COSTE}{ultimo_hijo}"
        rng_importe  = f"{L_IMPORTE}{primer_hijo}:{L_IMPORTE}{ultimo_hijo}"
        rng_gastos   = f"{L_GASTOS}{primer_hijo}:{L_GASTOS}{ultimo_hijo}"
        rng_gpbruta  = f"{L_GPBRUTA}{primer_hijo}:{L_GPBRUTA}{ultimo_hijo}"
        rng_pd       = f"{L_PD}{primer_hijo}:{L_PD}{ultimo_hijo}"
        rng_gpfiscal = f"{L_GPFISCAL}{primer_hijo}:{L_GPFISCAL}{ultimo_hijo}"
        rng_cant     = f"{L_CANTIDAD}{primer_hijo}:{L_CANTIDAD}{ultimo_hijo}"

        ws.cell(row=summary_row, column=COL_CASILLA, value=casilla)
        ws.cell(row=summary_row, column=COL_ISIN, value=isin)
        ws.cell(row=summary_row, column=COL_EMPRESA, value=_safe_cell(nombre))
        ws.cell(row=summary_row, column=COL_ORIGEN, value=clasificar_isin(isin))
        ws.cell(row=summary_row, column=COL_FVENTA,
                value=f"{n_hijas} match{'es' if n_hijas != 1 else ''}")
        ws.cell(row=summary_row, column=COL_CANTIDAD,
                value=f"=SUM({rng_cant})")
        ws.cell(row=summary_row, column=COL_LOTE,
                value="▾ resumen del ISIN (expande para ver lotes)")
        ws.cell(row=summary_row, column=COL_COSTE,
                value=f"=SUM({rng_coste})")
        ws.cell(row=summary_row, column=COL_IMPORTE,
                value=f"=SUM({rng_importe})")
        ws.cell(row=summary_row, column=COL_GASTOS,
                value=f"=SUM({rng_gastos})")
        ws.cell(row=summary_row, column=COL_GPBRUTA,
                value=f"=SUM({rng_gpbruta})")
        # Columna 2M en RESUMEN: SIEMPRE vacía. Es el discriminador que usa
        # el pie con SUMIFS para excluir resúmenes (filtra por "Sí" o "No";
        # las celdas vacías quedan fuera del filtro automáticamente).
        ws.cell(row=summary_row, column=COL_2M, value="")
        ws.cell(row=summary_row, column=COL_PD,
                value=f"=SUM({rng_pd})")
        ws.cell(row=summary_row, column=COL_GPFISCAL,
                value=f"=SUM({rng_gpfiscal})")
        ws.cell(row=summary_row, column=COL_TIPO,
                value=_tipo_label_casilla.get(casilla, "Acción"))
        if es_intensivo:
            etiqueta = ("DAYTRADING" if pattern == "daytrading"
                        else "SWING INTENSIVO")
            ws.cell(
                row=summary_row, column=COL_NOTAS,
                value=(
                    f"⚡ {etiqueta} ({n_intra}/{n_hijas} intradia, "
                    f"{int(round(ratio_intra * 100))}%) — pulsa [−] del "
                    f"margen para colapsar el detalle"
                ),
            )
        else:
            ws.cell(row=summary_row, column=COL_NOTAS,
                    value="Resumen ISIN — colapsa con el botón [−] para vista agregada")

        _estilo_fila(summary_row, fill=FILL_SUBHEADER, fuente=FONT_BOLD)
        for col in (COL_COSTE, COL_IMPORTE, COL_GASTOS, COL_GPBRUTA,
                    COL_PD, COL_GPFISCAL):
            ws.cell(row=summary_row, column=col).number_format = EUR_FMT
        ws.cell(row=summary_row, column=COL_CANTIDAD).number_format = NUM_FMT
        ws.cell(row=summary_row, column=COL_GPFISCAL).font = FONT_TOTAL
        ws.row_dimensions[summary_row].outline_level = 0

        next_row += 1

        # Spin-off info: aplica al ISIN completo, marcamos resumen e hijas
        spin_meta = (spin_off_info or {}).get(isin)

        # Filas hijas (outline_level=1)
        for m in grupo_matches:
            r = next_row
            es_2m = bool(m.regla_2_meses and m.ganancia_perdida < 0)
            pd_eur = getattr(m, 'perdida_diferida_aflorada_eur', Decimal("0"))
            tiene_pd = pd_eur > 0

            ws.cell(row=r, column=COL_CASILLA, value=casilla)
            ws.cell(row=r, column=COL_ISIN, value=m.isin)
            ws.cell(row=r, column=COL_EMPRESA, value=_safe_cell(nombre))
            ws.cell(row=r, column=COL_ORIGEN, value=clasificar_isin(m.isin))
            ws.cell(row=r, column=COL_FVENTA,
                    value=m.fecha_venta.strftime("%d/%m/%Y"))
            ws.cell(row=r, column=COL_CANTIDAD, value=_to_float(m.cantidad))

            # Lote consumido — texto + hyperlink interno a Operaciones
            try:
                pm_lote = float(m.coste_adquisicion) / float(m.cantidad) if m.cantidad else 0.0
            except Exception:
                pm_lote = 0.0
            cant_str = (f"{float(m.cantidad):,.4f}".rstrip('0').rstrip('.')
                        .replace(',', ' '))
            texto_lote = (f"{m.fecha_compra.strftime('%d/%m/%Y')}  ×  "
                          f"{cant_str}  @  {pm_lote:,.4f} €")
            c_lote = ws.cell(row=r, column=COL_LOTE, value=texto_lote)
            lote_id = getattr(m, 'lote_id', 0)
            fila_op = lote_id_to_row.get(lote_id) if lote_id else None
            if fila_op:
                c_lote.hyperlink = f"#Operaciones!A{fila_op}"
                c_lote.font = Font(name="Calibri", size=10, color="0563C1",
                                   underline="single")
            else:
                # Fallback (scrip B sin lote real): sin hyperlink
                c_lote.value = texto_lote + "  (sin lote — scrip)"
                c_lote.font = FONT_MUTED

            # Coste FIFO: fórmula prorrateada del lote en Operaciones
            formula_coste = _build_formula_coste_fifo_match(m, lote_id_to_row)
            if formula_coste is not None:
                c_coste = ws.cell(row=r, column=COL_COSTE, value=formula_coste)
                c_coste.fill = FILL_CALCULATED
                c_coste.font = FONT_BODY
                c_coste.comment = Comment(
                    "Celda calculada (fórmula prorrateada del lote en Operaciones).\n"
                    "Para cambiar el coste, edita la columna F (Coste/Importe) de la "
                    "fila correspondiente en la hoja Operaciones — el hyperlink en "
                    "la columna 'Lote consumido' te lleva directamente.\n\n"
                    "Sobrescribir esta celda con un número rompe el enlace — "
                    "hazlo solo si necesitas aplicar un ajuste agregado (return "
                    "of capital, prorrateo de escisión manual).",
                    "Cuádrate"
                )
            else:
                # Fallback (scrip TYPE B sin lote): valor literal editable
                c_coste = ws.cell(row=r, column=COL_COSTE,
                                  value=_to_float(m.coste_adquisicion))
                c_coste.fill = FILL_EDITABLE
                c_coste.font = FONT_EDIT
            c_coste.number_format = EUR_FMT

            c_imp = ws.cell(row=r, column=COL_IMPORTE,
                            value=_to_float(m.importe_transmision))
            c_imp.number_format = EUR_FMT
            c_gas = ws.cell(row=r, column=COL_GASTOS,
                            value=_to_float(m.gastos_venta))
            c_gas.number_format = EUR_FMT

            # G/P bruta = Importe − Gastos − Coste
            c_gpb = ws.cell(row=r, column=COL_GPBRUTA,
                            value=f"={L_IMPORTE}{r}-{L_GASTOS}{r}-{L_COSTE}{r}")
            c_gpb.number_format = EUR_FMT
            c_gpb.font = FONT_BOLD

            # Badge 2M
            c_2m = ws.cell(row=r, column=COL_2M, value=("Sí" if es_2m else "No"))
            if es_2m:
                c_2m.font = Font(name="Calibri", size=10, bold=True, color="991b1b")

            # PD aflorada
            c_pd = ws.cell(row=r, column=COL_PD,
                           value=_to_float(pd_eur) if tiene_pd else None)
            c_pd.number_format = EUR_FMT

            # G/P fiscal = IF(2M="Sí", 0, G/P bruta - PD)
            #  - matches 2M no computan en RentaWEB (pérdida diferida hasta
            #    transmisión definitiva del lote recomprado)
            #  - en matches no-2M, la PD aflorada se RESTA del G/P bruto
            #    (equivalente a sumarla al valor de adquisición — forma A
            #    doctrinal del Art. 33.5.f LIRPF, DGT V3282-18)
            c_gpf = ws.cell(
                row=r, column=COL_GPFISCAL,
                value=(f'=IF({L_2M}{r}="Sí",0,'
                       f"{L_GPBRUTA}{r}-IF(ISBLANK({L_PD}{r}),0,{L_PD}{r}))")
            )
            c_gpf.number_format = EUR_FMT
            c_gpf.font = FONT_TOTAL

            ws.cell(row=r, column=COL_TIPO,
                    value=_tipo_label_casilla.get(casilla, "Acción"))

            # Notas
            notas = []
            if es_2m:
                notas.append(
                    "🚫 NO DEDUCIBLE — regla 2 meses, Art. 33.5.f LIRPF. "
                    "La pérdida se difiere al lote recomprado."
                )
            if tiene_pd:
                desglose = getattr(m, 'perdida_diferida_desglose', []) or []
                por_ej: dict = defaultdict(Decimal)
                for entry in desglose:
                    por_ej[entry["ejercicio"]] += entry["importe_eur"]
                if por_ej:
                    txt = "; ".join(f"{ej}: {imp:.2f} €"
                                    for ej, imp in sorted(por_ej.items()))
                    notas.append(
                        f"↑ PD aflora ({pd_eur:.2f} €) — Art. 33.5.f LIRPF "
                        f"último párrafo. Origen — {txt}. RESTA al G/P bruto."
                    )
                else:
                    notas.append(
                        f"↑ PD aflora ({pd_eur:.2f} €) — Art. 33.5.f LIRPF "
                        f"último párrafo. RESTA al G/P bruto."
                    )
                if getattr(m, 'perdida_diferida_intra_anual', False):
                    notas.append("PD intra-anual (mismo ejercicio)")
            if formula_coste is not None:
                notas.append("coste enlazado ✓")
            if spin_meta:
                if spin_meta.get('es_matriz'):
                    notas.append("⚠️ MATRIZ DE ESCISIÓN")
                else:
                    matriz = spin_meta.get('nombre_matriz', '')
                    isin_m = spin_meta.get('isin_matriz', '')
                    notas.append(f"⚠️ ESCISIÓN de {matriz} ({isin_m})")
            ws.cell(row=r, column=COL_NOTAS, value=" · ".join(notas))

            # Estilo de fila + tinte 2M
            fill_row = PatternFill("solid", fgColor=C_ROJO) if es_2m else None
            _estilo_fila(r, fill=fill_row)
            ws.row_dimensions[r].outline_level = 1

            # Spin-off: borde rojo
            if spin_meta:
                from openpyxl.styles import Side as _Side, Border as _Border
                red = _Side(border_style="medium", color="DC2626")
                for col in range(1, 17):
                    cc = ws.cell(row=r, column=col)
                    cc.border = _Border(left=red, right=red, top=red, bottom=red)

            casilla_match_rows[casilla].append(r)
            next_row += 1

    last_data_row = next_row - 1

    # Autofilter sobre la tabla de datos (sin pie)
    L_LAST = get_column_letter(16)
    if last_data_row > header_row:
        ws.auto_filter.ref = f"A{header_row}:{L_LAST}{last_data_row}"

    # ─── Pie: totales por casilla (SUMIFS por columna A=Casilla, L=2M) ────
    next_row += 1
    totales_por_casilla: dict = {}
    if last_data_row >= first_data_row:
        rango_casilla   = f"{L_CASILLA}{first_data_row}:{L_CASILLA}{last_data_row}"
        rango_2m        = f"{L_2M}{first_data_row}:{L_2M}{last_data_row}"
        rango_gpbruta   = f"{L_GPBRUTA}{first_data_row}:{L_GPBRUTA}{last_data_row}"
        rango_pd        = f"{L_PD}{first_data_row}:{L_PD}{last_data_row}"
        rango_gpfiscal  = f"{L_GPFISCAL}{first_data_row}:{L_GPFISCAL}{last_data_row}"
    else:
        rango_casilla = rango_2m = rango_gpbruta = rango_pd = rango_gpfiscal = None

    todas_casillas = sorted(casilla_match_rows.keys(), key=_peso_casilla)
    for casilla in todas_casillas:
        # Línea: pérdidas NO deducibles 2M (informativa)
        any_2m = any(
            ws.cell(row=r, column=COL_2M).value == "Sí"
            for r in casilla_match_rows[casilla]
        )
        if any_2m and rango_casilla:
            ws.merge_cells(start_row=next_row, start_column=1,
                           end_row=next_row, end_column=COL_GASTOS)
            c_lbl = ws.cell(row=next_row, column=1,
                            value=f"Pérdidas NO deducibles 2M casilla {casilla}")
            c_lbl.font = Font(name="Calibri", size=10, bold=True, color="991b1b")
            c_lbl.fill = PatternFill("solid", fgColor=C_ROJO)
            c_lbl.alignment = ALIGN_RIGHT
            c_2m_tot = ws.cell(
                row=next_row, column=COL_GPBRUTA,
                value=(f'=SUMIFS({rango_gpbruta},{rango_casilla},"{casilla}",'
                       f'{rango_2m},"Sí")')
            )
            c_2m_tot.font = Font(name="Calibri", size=10, bold=True, color="991b1b")
            c_2m_tot.fill = PatternFill("solid", fgColor=C_ROJO)
            c_2m_tot.alignment = ALIGN_RIGHT
            c_2m_tot.number_format = EUR_FMT
            ws.cell(row=next_row, column=COL_NOTAS,
                    value="No computa — Art. 33.5.f LIRPF").font = FONT_MUTED
            ws.cell(row=next_row, column=COL_NOTAS).fill = PatternFill(
                "solid", fgColor=C_ROJO)
            next_row += 1

        # Línea: PD aflorada (informativa, Art. 33.5.f LIRPF último párrafo)
        any_pd = any(
            (ws.cell(row=r, column=COL_PD).value or 0) > 0
            for r in casilla_match_rows[casilla]
        )
        if any_pd and rango_casilla:
            ws.merge_cells(start_row=next_row, start_column=1,
                           end_row=next_row, end_column=COL_GASTOS)
            c_lbl = ws.cell(row=next_row, column=1,
                            value=f"PD aflorada casilla {casilla} (Art. 33.5.f LIRPF)")
            c_lbl.font = Font(name="Calibri", size=10, bold=True, color="92400e")
            c_lbl.fill = PatternFill("solid", fgColor=C_AMARILLO_3)
            c_lbl.alignment = ALIGN_RIGHT
            c_pd_tot = ws.cell(
                row=next_row, column=COL_PD,
                value=(f'=SUMIFS({rango_pd},{rango_casilla},"{casilla}",'
                       f'{rango_2m},"No")')
            )
            c_pd_tot.font = Font(name="Calibri", size=10, bold=True, color="92400e")
            c_pd_tot.fill = PatternFill("solid", fgColor=C_AMARILLO_3)
            c_pd_tot.alignment = ALIGN_RIGHT
            c_pd_tot.number_format = EUR_FMT
            ws.cell(row=next_row, column=COL_NOTAS,
                    value="Ya RESTADO en G/P fiscal de cada match").font = FONT_MUTED
            ws.cell(row=next_row, column=COL_NOTAS).fill = PatternFill(
                "solid", fgColor=C_AMARILLO_3)
            next_row += 1

        # Línea: TOTAL DEDUCIBLE → RentaWEB
        ws.merge_cells(start_row=next_row, start_column=1,
                       end_row=next_row, end_column=COL_GASTOS)
        c_lbl = ws.cell(row=next_row, column=1,
                        value=f"TOTAL DEDUCIBLE casilla {casilla} (→ RentaWEB)")
        c_lbl.font = FONT_TOTAL
        c_lbl.fill = FILL_TOTAL
        c_lbl.alignment = ALIGN_RIGHT
        c_lbl.border = BORDER_BOTTOM_THICK
        if rango_casilla:
            formula_total = (f'=SUMIFS({rango_gpfiscal},{rango_casilla},'
                             f'"{casilla}",{rango_2m},"No")')
        else:
            formula_total = 0
        c_tot = ws.cell(row=next_row, column=COL_GPFISCAL, value=formula_total)
        c_tot.font = FONT_TOTAL
        c_tot.fill = FILL_TOTAL
        c_tot.alignment = ALIGN_RIGHT
        c_tot.border = BORDER_BOTTOM_THICK
        c_tot.number_format = EUR_FMT
        ws.cell(row=next_row, column=COL_NOTAS).border = BORDER_BOTTOM_THICK
        totales_por_casilla[casilla] = (L_GPFISCAL, next_row)
        next_row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return ws, totales_por_casilla, dict(casilla_match_rows)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Dividendos
# ─────────────────────────────────────────────────────────────────────────────

def _build_dividendos(wb, ejercicio: int, fecha_gen: str,
                     resumen: Optional[list[dict]]) -> tuple:
    ws = wb.create_sheet("Dividendos")
    next_row = _put_brand_header(ws, ejercicio, "Dividendos", fecha_gen, ncols=12)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=12)
    leg = ws.cell(row=next_row, column=1,
                  value="Dividendos brutos por pagador con retención en origen, "
                        "límite CDI España y deducción recuperable. "
                        "Total bruto → casilla 0029. Retención de pagadores españoles → "
                        "popup individual de 0029 (sin casilla). Deducción doble imposición → casilla 0588. "
                        "Cada ISIN agrupa sus pagos individuales: pulsa el botón [+] del margen para "
                        "expandir el desglose (fecha, broker e importe por evento) y auditar la cifra agregada. "
                        "Si recibes derechos residuales recomprados por el emisor (RCM), "
                        "añádelos manualmente como filas adicionales.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 60
    next_row += 2

    # Layout (13 columnas):
    # A ISIN | B Empresa | C País | D Fecha | E Bruto | F Ret.origen EUR |
    # G Ret.España 0591 EUR | H % Ret.origen | I % Tope CDI | J Límite CDI EUR |
    # K Recuperable 0588 | L Es nacional | M Broker
    #
    # Fila resumen (outline_level=0):
    #   - A-C: ISIN/Empresa/País del pagador
    #   - D (Fecha): vacío (no aplica al agregado)
    #   - E (Bruto), F (Ret.origen), G (Ret.España 0591), I (% Tope CDI) editables (amarillo)
    #   - H = F/E, J = E*I, K = IF(L="SÍ",0,MIN(F,J))
    #   - F = retención de ORIGEN extranjera (→ 0588); G = retención española 19%
    #     (pagador ES o TR Sucursal ES sobre dividendo extranjero → 0591).
    #   - M: lista de brokers reportadores (concat)
    #
    # Filas hijas (outline_level=1) — una por evento (fecha, broker) del CSV:
    #   - D: fecha de cobro
    #   - E: bruto del pago | F: retención de origen | G: retención española
    #   - M: broker (un solo broker)
    #   - Resto: vacías. Son meramente informativas (auditoría); las fórmulas
    #     y SUMIF de los totales filtran por col A no vacía para excluirlas.
    headers = ["ISIN", "Empresa", "País", "Fecha", "Bruto (EUR)",
               "Retención origen (EUR)", "Ret. España 0591 (EUR)",
               "% Ret. origen", "% Tope CDI",
               "Límite CDI (EUR)", "Recuperable (0588)", "Es nacional", "Broker"]
    widths  = [16, 30, 6, 11, 13, 18, 18, 11, 11, 14, 16, 9, 22]
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1

    NCOLS = 13
    # Los códigos de formato de Excel son independientes del locale y usan
    # SIEMPRE punto decimal; "0,00%" se interpretaba como separador de
    # millares y el porcentaje se mostraba mal (auditoría 2026-06-11 [BAJO]).
    PCT_FMT = "0.00%"

    if not resumen:
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=NCOLS)
        c = ws.cell(row=next_row, column=1, value="(sin datos de dividendos)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_CENTER
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        return ws, {"bruto_total": None, "ret_es_total": None,
                    "bruto_ext_con_ret": None, "cdi_total": None}

    fila_inicio = next_row
    for d in resumen:
        r = next_row
        # ── Fila resumen del ISIN (outline_level=0) ──
        ws.cell(row=r, column=1, value=d.get('isin', ''))
        ws.cell(row=r, column=2, value=_safe_cell(d.get('nombre', '')))
        ws.cell(row=r, column=3, value=d.get('pais', ''))
        ws.cell(row=r, column=4, value="")  # fecha: vacía en agregado

        c_bruto = ws.cell(row=r, column=5, value=_to_float(d.get('bruto', 0)))
        # F: retención de ORIGEN extranjera (→ casilla 0588 / crédito CDI).
        # Clave canónica del motor: 'ret_origen' (ver generar_irpf.py).
        # Aceptamos también 'retencion_origen' por compatibilidad si algún
        # pre-procesado del resumen renombra la clave.
        c_ret   = ws.cell(row=r, column=6,
                          value=_to_float(d.get('ret_origen',
                                                 d.get('retencion_origen', 0))))
        # G: retención ESPAÑOLA 19% (→ casilla 0591). Cubre tanto pagadores ES
        # (ACS) como dividendos extranjeros con el 19% que practica TR Sucursal
        # ES post-migración (J&J). 100% acreditable, NO entra en el tope CDI.
        c_ret_es = ws.cell(row=r, column=7,
                           value=_to_float(d.get('retencion_es', 0)))
        # H: % efectivo de origen aplicado por el broker — derivado de F y E.
        ws.cell(row=r, column=8,
                value=f"=IFERROR(F{r}/E{r},0)")
        # I: tasa máxima del CDI España–país. None → 0 (sin CDI o nacional).
        tasa_cdi = d.get('tasa_cdi')
        c_pct_cdi = ws.cell(row=r, column=9,
                            value=float(tasa_cdi) if tasa_cdi is not None else 0)
        # J: Límite CDI en EUR — fórmula = bruto × % tope CDI.
        ws.cell(row=r, column=10, value=f"=ROUND(E{r}*I{r},2)")
        # K: Recuperable (0588) — solo crédito CDI EXTRANJERO. Si es nacional,
        # 0 (la retención española va por 0591, columna G, no por CDI). Si es
        # extranjero, MIN(retención de origen, límite CDI).
        ws.cell(row=r, column=11,
                value=f'=IF(L{r}="SÍ",0,MIN(F{r},J{r}))')

        es_nac  = "SÍ" if d.get('es_nacional') else ""
        ws.cell(row=r, column=12, value=es_nac)
        ws.cell(row=r, column=13, value=d.get('brokers', ''))

        # Estilos fila resumen
        for col in range(1, NCOLS + 1):
            c = ws.cell(row=r, column=col)
            c.border = BORDER_ALL
            c.font = FONT_BODY
            c.alignment = ALIGN_RIGHT if col in (5, 6, 7, 8, 9, 10, 11) else ALIGN_LEFT
            if col == 12:
                c.alignment = ALIGN_CENTER
        for col in (5, 6, 7, 10, 11):
            ws.cell(row=r, column=col).number_format = EUR_FMT
        for col in (8, 9):
            ws.cell(row=r, column=col).number_format = PCT_FMT

        # Editables (los datos que el usuario podría querer corregir).
        for c_edit in (c_bruto, c_ret, c_ret_es, c_pct_cdi):
            c_edit.fill = FILL_EDITABLE
            c_edit.font = FONT_EDIT
        ws.row_dimensions[r].outline_level = 0
        next_row += 1

        # ── Filas hijas: eventos individuales (outline_level=1) ──
        # El motor ya agrupa por (fecha, broker) y separa bruto y retención
        # en campos distintos, así que cada hijo es un pago real con su DIV
        # y su RET en columnas adyacentes (E y F).
        for ev in d.get('eventos', []):
            rc = next_row
            ws.cell(row=rc, column=4, value=ev.get('fecha', ''))
            bruto_ev   = _to_float(ev.get('bruto', 0))
            # Separar retención de origen (F) y española (G) por evento. El
            # fallback a 'retencion' cubre eventos generados antes del split.
            ret_org_ev = _to_float(ev.get('retencion_origen', ev.get('retencion', 0)))
            ret_es_ev  = _to_float(ev.get('retencion_es', 0))
            if bruto_ev:
                c_b = ws.cell(row=rc, column=5, value=bruto_ev)
                c_b.number_format = EUR_FMT
            if ret_org_ev:
                c_r = ws.cell(row=rc, column=6, value=ret_org_ev)
                c_r.number_format = EUR_FMT
            if ret_es_ev:
                c_re = ws.cell(row=rc, column=7, value=ret_es_ev)
                c_re.number_format = EUR_FMT
            ws.cell(row=rc, column=13, value=ev.get('broker', ''))
            # Estilo muted para los eventos hijos
            for col in range(1, NCOLS + 1):
                c = ws.cell(row=rc, column=col)
                c.font = FONT_MUTED
                c.alignment = ALIGN_RIGHT if col in (5, 6, 7) else ALIGN_LEFT
            ws.row_dimensions[rc].outline_level = 1
            next_row += 1

    fila_fin = next_row - 1

    # Total. Las SUMIF/SUMIFS filtran por col A no vacía (ISIN) para
    # contar solo las filas resumen — las filas hijas tienen col A vacía y
    # quedan excluidas automáticamente, evitando doble conteo.
    isin_col   = get_column_letter(1)   # A — discriminador resumen/hijo
    bruto_col  = get_column_letter(5)   # E — Bruto EUR
    ret_col    = get_column_letter(6)   # F — Retención origen EUR
    ret_es_col = get_column_letter(7)   # G — Retención española 0591 EUR
    lim_col    = get_column_letter(10)  # J — Límite CDI EUR
    rec_col    = get_column_letter(11)  # K — Recuperable 0588
    nac_col    = get_column_letter(12)  # L — Es nacional

    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    c_lbl = ws.cell(row=next_row, column=1, value="TOTALES")
    c_lbl.font = FONT_TOTAL
    c_lbl.fill = FILL_TOTAL
    c_lbl.alignment = ALIGN_RIGHT
    c_lbl.border = BORDER_BOTTOM_THICK
    # Sumas en EUR: E (bruto), F (ret. origen), G (ret. España 0591), J (límite),
    # K (recuperable 0588). Las columnas de % (H, I) no se suman.
    for col in (5, 6, 7, 10, 11):
        colL = get_column_letter(col)
        c = ws.cell(row=next_row, column=col)
        c.value = (f'=SUMIF({isin_col}{fila_inicio}:{isin_col}{fila_fin},'
                   f'"<>",{colL}{fila_inicio}:{colL}{fila_fin})')
        c.font = FONT_TOTAL
        c.fill = FILL_TOTAL
        c.alignment = ALIGN_RIGHT
        c.border = BORDER_BOTTOM_THICK
        c.number_format = EUR_FMT
    # Sombrear el resto del bloque de totales (col 8, 9, 12, 13) sin fórmulas.
    for col in (8, 9, 12, 13):
        c = ws.cell(row=next_row, column=col)
        c.fill = FILL_TOTAL
        c.border = BORDER_BOTTOM_THICK

    bruto_ref = (get_column_letter(5), next_row)
    fila_total_recup = next_row

    # Retención española (casilla 0591) → campo "Retenciones" del popup
    # individual de 0029. Suma la columna G entera: incluye tanto pagadores
    # nacionales (ACS) como dividendos extranjeros con el 19% que practica TR
    # Sucursal ES post-migración (J&J). Filtra por col A no vacía (solo padres).
    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=10)
    c_ret_es_lbl = ws.cell(row=next_row, column=1,
                            value='Retención española 19% (campo "Retenciones" del popup de 0029) — nacional + TR Sucursal ES')
    c_ret_es_lbl.font = FONT_TOTAL
    c_ret_es_lbl.fill = FILL_TOTAL
    c_ret_es_lbl.alignment = ALIGN_RIGHT
    c_ret_es = ws.cell(row=next_row, column=11,
                       value=f'=SUMIF({isin_col}{fila_inicio}:{isin_col}{fila_fin},'
                             f'"<>",{ret_es_col}{fila_inicio}:{ret_es_col}{fila_fin})')
    c_ret_es.number_format = EUR_FMT
    c_ret_es.font = FONT_TOTAL
    c_ret_es.fill = FILL_TOTAL
    c_ret_es.alignment = ALIGN_RIGHT
    for col in (12, 13):
        ws.cell(row=next_row, column=col).fill = FILL_TOTAL
    ret_es_ref = (get_column_letter(11), next_row)
    fila_ret_es = next_row

    # Bruto extranjero con retención (campo "Rendimientos netos reducidos del
    # capital mobiliario obtenidos en el extranjero incluidos en la base del
    # ahorro" del segundo popup de la casilla 0588). Excluye dividendos
    # españoles (van por 0591) y extranjeros con retención 0 % en origen
    # (p. ej. UK), que no generan crédito CDI.
    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=10)
    c_bx_lbl = ws.cell(row=next_row, column=1,
                       value='Bruto extranjero con retención (campo "Rendimientos cap. mobiliario" del popup 0588)')
    c_bx_lbl.font = FONT_TOTAL
    c_bx_lbl.fill = FILL_TOTAL
    c_bx_lbl.alignment = ALIGN_RIGHT
    c_bx = ws.cell(row=next_row, column=11,
                   value=f'=SUMIFS({bruto_col}{fila_inicio}:{bruto_col}{fila_fin},'
                         f'{isin_col}{fila_inicio}:{isin_col}{fila_fin},"<>",'
                         f'{nac_col}{fila_inicio}:{nac_col}{fila_fin},"<>SÍ",'
                         f'{ret_col}{fila_inicio}:{ret_col}{fila_fin},">0")')
    c_bx.number_format = EUR_FMT
    c_bx.font = FONT_TOTAL
    c_bx.fill = FILL_TOTAL
    c_bx.alignment = ALIGN_RIGHT
    for col in (12, 13):
        ws.cell(row=next_row, column=col).fill = FILL_TOTAL
    bruto_ext_ref = (get_column_letter(11), next_row)

    # Deducción CDI internacional (casilla 0588) = recuperable EXTRANJERO.
    # Suma la columna Recuperable (K) entera: los pagadores nacionales ya
    # aportan 0 a esa columna (su retención va por 0591, columna G), así que el
    # total coincide con el crédito CDI extranjero sin restar nada.
    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=10)
    c_cdi_lbl = ws.cell(row=next_row, column=1,
                         value='Deducción CDI internacional (campo "Impuesto satisfecho en el extranjero" del popup 0588)')
    c_cdi_lbl.font = FONT_TOTAL
    c_cdi_lbl.fill = FILL_TOTAL
    c_cdi_lbl.alignment = ALIGN_RIGHT
    c_cdi = ws.cell(row=next_row, column=11,
                    value=f'=SUMIF({isin_col}{fila_inicio}:{isin_col}{fila_fin},'
                          f'"<>",{rec_col}{fila_inicio}:{rec_col}{fila_fin})')
    c_cdi.number_format = EUR_FMT
    c_cdi.font = FONT_TOTAL
    c_cdi.fill = FILL_TOTAL
    c_cdi.alignment = ALIGN_RIGHT
    for col in (12, 13):
        ws.cell(row=next_row, column=col).fill = FILL_TOTAL
    cdi_ref = (get_column_letter(11), next_row)

    # Outline summary: en openpyxl, summaryBelow=False indica que el "+/-"
    # de colapso aparece arriba del grupo (donde está la fila resumen). Esto
    # alinea el control con la convención de la hoja G_P_por_valor.
    ws.sheet_properties.outlinePr.summaryBelow = False

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return ws, {"bruto_total": bruto_ref, "ret_es_total": ret_es_ref,
                "bruto_ext_con_ret": bruto_ext_ref, "cdi_total": cdi_ref}


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Pérdidas arrastradas (Art. 49 LIRPF)
# ─────────────────────────────────────────────────────────────────────────────

def _build_perdidas_arrastradas(wb, ejercicio: int, fecha_gen: str,
                                compensacion) -> tuple:
    ws = wb.create_sheet("Perdidas_arrastradas")
    next_row = _put_brand_header(ws, ejercicio,
                                 "Saldos negativos de ejercicios anteriores",
                                 fecha_gen, ncols=7)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=7)
    leg = ws.cell(row=next_row, column=1,
                  value="Pérdidas patrimoniales pendientes de los últimos 4 ejercicios. "
                        "El total aplicable este año va a casilla 1186+ (RentaWEB no las "
                        "aplica automáticamente — DEBES introducirlas manualmente). "
                        "Si tu registro local del JSON no estaba al día, ajústalo con tus "
                        "declaraciones reales y regenera.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 48
    next_row += 2

    headers = ["Origen", "Importe original", "Ya compensado",
               "Pendiente inicio", "Aplicable en " + str(ejercicio),
               "Queda pendiente", "Expira"]
    widths  = [10, 16, 14, 14, 16, 14, 10]
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1

    aplicable_ref = None
    if not compensacion or not getattr(compensacion, 'perdidas_anteriores', None):
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=7)
        c = ws.cell(row=next_row, column=1, value="(sin pérdidas pendientes de ejercicios anteriores)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_CENTER
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        return ws, {"aplicable_total": None}

    detalle_by_origen = {d['ejercicio_origen']: d
                         for d in (compensacion.detalle_aplicacion or [])}

    fila_inicio = next_row
    for p in compensacion.perdidas_anteriores:
        info = detalle_by_origen.get(p.ejercicio_origen)
        ws.cell(row=next_row, column=1, value=p.ejercicio_origen)
        ws.cell(row=next_row, column=2, value=_to_float(p.importe_original_eur))
        ws.cell(row=next_row, column=3, value=_to_float(p.compensado_eur))
        ws.cell(row=next_row, column=4,
                value=_to_float(info.pendiente_antes if info else p.pendiente_eur))
        ws.cell(row=next_row, column=5,
                value=_to_float(info.aplicado if info else 0))
        ws.cell(row=next_row, column=6,
                value=_to_float(info.pendiente_despues if info else p.pendiente_eur))
        ws.cell(row=next_row, column=7, value=p.expira)

        for col in range(1, 8):
            c = ws.cell(row=next_row, column=col)
            c.border = BORDER_ALL
            c.font = FONT_BODY
            if col in (1, 7):
                c.alignment = ALIGN_CENTER
            else:
                c.alignment = ALIGN_RIGHT
        for col in (2, 3, 4, 5, 6):
            ws.cell(row=next_row, column=col).number_format = EUR_FMT
        # Resaltar "aplicable" si > 0
        if info and info.aplicado > 0:
            ws.cell(row=next_row, column=5).font = FONT_BOLD
        next_row += 1

    fila_fin = next_row - 1

    # Total aplicable
    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    c_lbl = ws.cell(row=next_row, column=1,
                    value=f"TOTAL aplicable en {ejercicio} (casilla 1186+)")
    c_lbl.font = FONT_TOTAL
    c_lbl.fill = FILL_TOTAL
    c_lbl.alignment = ALIGN_RIGHT
    c_lbl.border = BORDER_BOTTOM_THICK
    for col in (5, 6, 7):
        ws.cell(row=next_row, column=col).fill = FILL_TOTAL
        ws.cell(row=next_row, column=col).border = BORDER_BOTTOM_THICK
    apl_col = get_column_letter(5)
    c_apl = ws.cell(row=next_row, column=5,
                    value=f"=SUM({apl_col}{fila_inicio}:{apl_col}{fila_fin})")
    c_apl.number_format = EUR_FMT
    c_apl.font = FONT_TOTAL
    c_apl.alignment = ALIGN_RIGHT
    aplicable_ref = (get_column_letter(5), next_row)

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return ws, {"aplicable_total": aplicable_ref}


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Opciones (casillas 1624-1654)
# ─────────────────────────────────────────────────────────────────────────────

def _build_opciones(wb, ejercicio: int, fecha_gen: str,
                   por_contrato: Optional[list[dict]],
                   totales: Optional[dict]) -> tuple:
    """Hoja Opciones del Excel maestro. Muestra TODOS los contratos del año
    con su estado fiscal (cerrada / expirada / ejercida / mixta / abierta);
    el TOTAL P&L del bloque "cerradas + expiradas" es lo único que se suma
    a la casilla 1626 (otros elementos patrimoniales). Las ejercidas se
    listan como informativo — su prima ya está integrada en el coste/precio
    de las acciones (OPC, Art. 37.1.m LIRPF). Las abiertas a 31/12 son
    diferidas al año de extinción (DGT V2172-21).

    Refactor 2026-05-06: antes el filtro solo mostraba cerradas/expiradas
    pero usaba flag erróneo (`_ejercicio` / `_mixto`) que NO existe en los
    dicts; los flags reales son `es_ejercida` / `es_mixta`. Resultado: las
    ejercidas (caso Globant) se colaban en la tabla y se sumaban al P&L
    inflando casilla 1626. Ahora se muestran TODAS pero clasificadas por
    estado, y solo cerradas/expiradas suman al TOTAL fiscal.
    """
    ws = wb.create_sheet("Opciones")
    next_row = _put_brand_header(ws, ejercicio,
                                 "Opciones — todas las posiciones del ejercicio",
                                 fecha_gen, ncols=9)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=9)
    leg = ws.cell(row=next_row, column=1,
                  value="Esta hoja lista TODAS las opciones del ejercicio con su "
                        "estado fiscal en la columna 'Estado'. Solo el bloque "
                        "DECLARABLES suma al TOTAL P&L de la casilla 1626 "
                        "(otros elementos patrimoniales); las posiciones MIXTAS "
                        "y los ROLLS aparecen partidos en dos filas (porción "
                        "cerrada → declarable; porción ejercida/abierta → "
                        "informativa) para que el TOTAL cuadre al céntimo con el "
                        "PDF y el informe. Las EJERCIDAS aparecen como "
                        "informativo: su prima ya está integrada en el coste "
                        "o precio del subyacente en la hoja Operaciones (filas con "
                        "marca OPC, Art. 37.1.m LIRPF). Las ABIERTAS a 31/12 se "
                        "difieren al año de extinción (DGT V2172-21).")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 92
    next_row += 2

    headers = ["Subyacente", "Tipo", "Strike", "Vencimiento", "Estado",
               "Primas cobradas", "Primas pagadas", "Gastos", "P&L"]
    widths  = [30, 8, 10, 12, 24, 18, 18, 12, 16]
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1

    if not por_contrato:
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=9)
        c = ws.cell(row=next_row, column=1, value="(sin operaciones de opciones)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_CENTER
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        return ws, {"pl_total": None}

    def _filas_render(d: dict) -> list[dict]:
        """Convierte un contrato en 1-2 filas de render con su grupo fiscal:
            'declarable' : suma al TOTAL casilla 1626
            'opc'        : prima integrada en acciones (informativo)
            'diferida'   : abierta al 31/12 → tributa en año de extinción
        MIXTAS y ROLLS se parten en dos filas (C2 auditoría 2026-06-11: el
        TOTAL de la hoja excluía su porción cerrada, divergiendo del
        pl_neto del sidecar/PDF/stdout). Cada fila lleva sus gastos para
        que P&L = cobradas − pagadas − gastos cuadre con el pl_neto fiscal.
        """
        base = {
            'subyacente':  d.get('subyacente', ''),
            'tipo_op':     d.get('tipo_op', '?'),
            'strike':      d.get('strike', 0),
            'vencimiento': d.get('vencimiento', ''),
        }
        if d.get('es_mixta'):
            return [
                {**base, 'etiqueta': 'MIXTA — porción cerrada', 'grupo': 'declarable',
                 'cobradas': d.get('_prima_cerrada', 0),
                 'pagadas':  d.get('primas_pagadas', 0),
                 'gastos':   d.get('_gastos_cerrado', 0)},
                {**base, 'etiqueta': 'MIXTA — porción ejercida (en acciones)', 'grupo': 'opc',
                 'cobradas': d.get('_prima_ejercida', 0),
                 'pagadas':  0,
                 'gastos':   d.get('_gastos_ejercida', 0)},
            ]
        if d.get('es_roll_abierta'):
            return [
                {**base, 'etiqueta': 'ROLL — porción cerrada', 'grupo': 'declarable',
                 'cobradas': d.get('_prima_cerrada_r', 0),
                 'pagadas':  d.get('primas_pagadas', 0),
                 'gastos':   d.get('_gastos_cerrado_r', 0)},
                {**base, 'etiqueta': 'ROLL — porción abierta al 31/12', 'grupo': 'diferida',
                 'cobradas': d.get('_prima_abierta_r', 0),
                 'pagadas':  0,
                 'gastos':   d.get('_gastos_abierta_r', 0)},
            ]
        fila = {**base,
                'cobradas': d.get('primas_cobradas', 0),
                'pagadas':  d.get('primas_pagadas', 0),
                'gastos':   d.get('gastos', 0)}
        if d.get('es_ejercida_larga'):
            fila.update(etiqueta='LARGA EJERCIDA (prima al subyacente)', grupo='opc')
        elif d.get('es_ejercida'):
            fila.update(etiqueta='EJERCIDA (prima en acciones)', grupo='opc')
        elif d.get('es_long_abierta'):
            fila.update(etiqueta='ABIERTA long — diferida', grupo='diferida')
        elif d.get('es_short_abierta'):
            fila.update(etiqueta='ABIERTA short — diferida', grupo='diferida')
        elif d.get('expiradas', 0) > 0:
            fila.update(etiqueta='EXPIRADA', grupo='declarable')
        else:
            fila.update(etiqueta='CERRADA', grupo='declarable')
        return [fila]

    # Renderizar filas en 3 grupos: declarable, opc, diferida.
    # Dentro de cada grupo, ordenar por subyacente + vencimiento.
    grupo_orden = {'declarable': 0, 'opc': 1, 'diferida': 2}
    filas_clasificadas = []
    for d in por_contrato:
        for fila in _filas_render(d):
            filas_clasificadas.append((grupo_orden[fila['grupo']], fila))
    filas_clasificadas.sort(
        key=lambda x: (x[0], x[1]['subyacente'], x[1]['vencimiento'])
    )

    fila_inicio_decl = None  # primera fila del grupo declarable (para SUMA)
    fila_fin_decl    = None  # última fila del grupo declarable
    grupo_actual     = None
    pl_opc_total     = Decimal('0')   # informativo (prima ejercida ya en acciones)
    pl_dif_total     = Decimal('0')   # informativo (diferida al año de cierre)

    for orden, fila in filas_clasificadas:
        grupo = fila['grupo']
        # Cabecera de bloque cuando cambia el grupo
        if grupo != grupo_actual:
            grupo_actual = grupo
            sep_label = {
                'declarable': '── DECLARABLES (cerradas/expiradas + porción cerrada de mixtas/rolls) → casilla 1626 ──',
                'opc':        '── EJERCIDAS / porción ejercida (informativo, ya en acciones con OPC) ──',
                'diferida':   '── ABIERTAS al 31/12 (diferidas al año de extinción) ──',
            }[grupo]
            ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=9)
            c = ws.cell(row=next_row, column=1, value=sep_label)
            c.font = FONT_BOLD
            c.fill = FILL_SUBHEADER
            c.alignment = ALIGN_LEFT
            next_row += 1

        ws.cell(row=next_row, column=1, value=fila['subyacente'])
        ws.cell(row=next_row, column=2,
                value={'C': 'CALL', 'P': 'PUT'}.get(fila['tipo_op'], '?'))
        ws.cell(row=next_row, column=3, value=_to_float(fila['strike']))
        ws.cell(row=next_row, column=4, value=fila['vencimiento'])
        ws.cell(row=next_row, column=5, value=fila['etiqueta'])
        c_cob = ws.cell(row=next_row, column=6, value=_to_float(fila['cobradas']))
        c_pag = ws.cell(row=next_row, column=7, value=_to_float(fila['pagadas']))
        c_gas = ws.cell(row=next_row, column=8, value=_to_float(fila['gastos']))
        col_f = get_column_letter(6)
        col_g = get_column_letter(7)
        col_h = get_column_letter(8)
        c_pl = ws.cell(row=next_row, column=9,
                       value=f"={col_f}{next_row}-{col_g}{next_row}-{col_h}{next_row}")

        for col in range(1, 10):
            c = ws.cell(row=next_row, column=col)
            c.border = BORDER_ALL
            c.font = FONT_BODY
            if col in (2, 4, 5):
                c.alignment = ALIGN_CENTER
            elif col in (3, 6, 7, 8, 9):
                c.alignment = ALIGN_RIGHT
            else:
                c.alignment = ALIGN_LEFT
        ws.cell(row=next_row, column=3).number_format = NUM_FMT
        c_cob.number_format = EUR_FMT
        c_pag.number_format = EUR_FMT
        c_gas.number_format = EUR_FMT
        c_pl.number_format = EUR_FMT
        c_pl.font = FONT_BOLD

        pl_fila = (Decimal(str(fila['cobradas'])) - Decimal(str(fila['pagadas']))
                   - Decimal(str(fila['gastos'])))
        # Solo las declarables son editables (las ejercidas/abiertas son
        # informativas; modificarlas aquí no afectaría a la fiscalidad real)
        if grupo == 'declarable':
            c_cob.fill = FILL_EDITABLE
            c_pag.fill = FILL_EDITABLE
            c_gas.fill = FILL_EDITABLE
            c_cob.font = FONT_EDIT
            c_pag.font = FONT_EDIT
            c_gas.font = FONT_EDIT
            if fila_inicio_decl is None:
                fila_inicio_decl = next_row
            fila_fin_decl = next_row
        elif grupo == 'opc':
            pl_opc_total += pl_fila
        else:  # diferida
            pl_dif_total += pl_fila
        next_row += 1

    next_row += 1

    # Total declarable (casilla 1626) — única fila que el Resumen referencia.
    # Con las porciones cerradas de mixtas/rolls y la columna Gastos, esta
    # SUMA coincide al céntimo con opciones_totales['pl_neto'] (sidecar/PDF).
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=5)
    c_lbl = ws.cell(row=next_row, column=1,
                    value="TOTAL P&L opciones declarables (casilla 1626)")
    c_lbl.font = FONT_TOTAL
    c_lbl.fill = FILL_TOTAL
    c_lbl.alignment = ALIGN_RIGHT
    c_lbl.border = BORDER_BOTTOM_THICK
    if fila_inicio_decl is not None:
        for col in (6, 7, 8, 9):
            colL = get_column_letter(col)
            c = ws.cell(row=next_row, column=col)
            c.value = f"=SUM({colL}{fila_inicio_decl}:{colL}{fila_fin_decl})"
            c.font = FONT_TOTAL
            c.fill = FILL_TOTAL
            c.alignment = ALIGN_RIGHT
            c.border = BORDER_BOTTOM_THICK
            c.number_format = EUR_FMT
        pl_ref = (get_column_letter(9), next_row)
    else:
        # No hay declarables → 0 explícito y sin referencia al Resumen
        for col in (6, 7, 8, 9):
            c = ws.cell(row=next_row, column=col, value=0)
            c.font = FONT_TOTAL
            c.fill = FILL_TOTAL
            c.alignment = ALIGN_RIGHT
            c.border = BORDER_BOTTOM_THICK
            c.number_format = EUR_FMT
        pl_ref = None
    next_row += 2

    # Subtotales informativos (no van a casilla 1626)
    if pl_opc_total != 0:
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=8)
        c = ws.cell(row=next_row, column=1,
                    value="Subtotal P&L primas EJERCIDAS / porción ejercida — informativo "
                          "(ya integrado en acciones via OPC)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_RIGHT
        c9 = ws.cell(row=next_row, column=9, value=float(pl_opc_total))
        c9.font = FONT_MUTED
        c9.number_format = EUR_FMT
        c9.alignment = ALIGN_RIGHT
        next_row += 1
    if pl_dif_total != 0:
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=8)
        c = ws.cell(row=next_row, column=1,
                    value="Subtotal P&L primas DIFERIDAS — informativo (tributarán "
                          "en el año de extinción)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_RIGHT
        c9 = ws.cell(row=next_row, column=9, value=float(pl_dif_total))
        c9.font = FONT_MUTED
        c9.number_format = EUR_FMT
        c9.alignment = ALIGN_RIGHT
        next_row += 1

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    return ws, {"pl_total": pl_ref}


def _build_futuros(wb, ejercicio: int, fecha_gen: str,
                   por_contrato: Optional[list[dict]],
                   totales: Optional[dict]) -> tuple:
    """Hoja Futuros del Excel maestro. Una fila por contrato con su
    Realized P/L EUR consolidado por IBKR (incluye multiplier, FX y las
    COMISIONES ya neteadas en la base de coste — guía oficial de informes
    IBKR: "for the purpose of cost basis and realized profit or loss,
    commissions are netted"). La columna Comisiones es INFORMATIVA y NO
    se resta del P&L (V3 auditoría 2026-06-11: restarla duplicaba la
    deducción). El TOTAL P&L del bloque es lo que va a la casilla 1626
    c.4 (Otros elementos patrimoniales) de la base imponible del ahorro
    — Manual práctico AEAT cap 11 §14.

    Devuelve (ws, {"pl_ref": (col_letter, row) | None}).
    El Resumen usa pl_ref para enlazar la celda del P&L neto.
    """
    ws = wb.create_sheet("Futuros")
    next_row = _put_brand_header(ws, ejercicio,
                                 "Futuros financieros IBKR — agregado por contrato",
                                 fecha_gen, ncols=8)

    ws.merge_cells(start_row=next_row, start_column=1,
                   end_row=next_row, end_column=8)
    leg = ws.cell(
        row=next_row, column=1,
        value=(
            "Una fila por Symbol (cada contrato/vencimiento) del Activity "
            "Statement IBKR (Asset Category=Futures estricto). Realized P/L "
            "consolidado por IBKR — incluye multiplier (ES=50, MES=5, NQ=20, "
            "MNQ=2, CL=1.000, GC=100, etc.), conversión a EUR al tipo de "
            "cambio oficial publicado por el BCE del día de cada operación, "
            "y las comisiones YA NETEADAS en la base de coste (guía IBKR). "
            "La columna Comisiones es informativa: NO se resta otra vez. "
            "El TOTAL P&L va a la casilla 1626 con clave 4 — Manual "
            "práctico AEAT cap 11 §14, imputado al ejercicio en que se "
            "liquida la posición o se extingue el contrato (cita literal). "
            "Cross-año automático: si el contrato se abrió en el año "
            "anterior y se cierra en este ejercicio, IBKR consolida el P&L "
            "en el statement del año del cierre — el motor lo lee tal cual."
        ),
    )
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 90
    next_row += 2

    headers = ["Symbol", "Descripción", "Multiplier", "Cierres",
               "Moneda", "Realized P/L (EUR)", "Comisiones (EUR, info.)",
               "P&L (EUR)"]
    widths  = [12, 35, 12, 10, 10, 20, 18, 18]
    _put_table_header(ws, next_row, headers, widths)
    header_row = next_row
    next_row += 1

    if not por_contrato:
        ws.merge_cells(start_row=next_row, start_column=1,
                       end_row=next_row, end_column=8)
        c = ws.cell(row=next_row, column=1,
                    value="(sin cierres de futuros en el ejercicio)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_CENTER
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
        return ws, {"pl_ref": None}

    fila_inicio = next_row

    for d in por_contrato:
        ws.cell(row=next_row, column=1, value=d.get('symbol', ''))
        ws.cell(row=next_row, column=2, value=_safe_cell(d.get('descripcion', '')))
        mult = d.get('multiplier')
        ws.cell(row=next_row, column=3,
                value=_to_float(mult) if mult is not None else '')
        ws.cell(row=next_row, column=4, value=int(d.get('n_cierres', 0)))
        ws.cell(row=next_row, column=5, value=d.get('currency_origen', ''))
        c_rpl  = ws.cell(row=next_row, column=6,
                         value=_to_float(d.get('realized_pl_eur', 0)))
        c_gas  = ws.cell(row=next_row, column=7,
                         value=_to_float(d.get('gastos_eur', 0)))
        col_f = get_column_letter(6)
        # P&L = Realized P/L tal cual: las comisiones YA están neteadas
        # dentro del Realized P/L de IBKR (col G es solo informativa).
        c_pl  = ws.cell(row=next_row, column=8,
                        value=f"={col_f}{next_row}")

        for col in range(1, 9):
            c = ws.cell(row=next_row, column=col)
            c.border = BORDER_ALL
            c.font = FONT_BODY
            if col == 1:
                c.alignment = ALIGN_LEFT
                c.font = FONT_BOLD
            elif col in (3, 4, 5):
                c.alignment = ALIGN_CENTER
            elif col in (6, 7, 8):
                c.alignment = ALIGN_RIGHT
            else:
                c.alignment = ALIGN_LEFT
        if mult is not None:
            ws.cell(row=next_row, column=3).number_format = NUM_FMT
        c_rpl.number_format = EUR_FMT
        c_gas.number_format = EUR_FMT
        c_pl.number_format = EUR_FMT
        c_pl.font = FONT_BOLD

        next_row += 1

    fila_fin = next_row - 1
    next_row += 1

    # Total — única fila que el Resumen referencia para la casilla 1626 c.4
    ws.merge_cells(start_row=next_row, start_column=1,
                   end_row=next_row, end_column=5)
    c_lbl = ws.cell(
        row=next_row, column=1,
        value="TOTAL P&L futuros (casilla 1626 c.4 · Manual AEAT cap 11 §14)",
    )
    c_lbl.font = FONT_TOTAL
    c_lbl.fill = FILL_TOTAL
    c_lbl.alignment = ALIGN_RIGHT
    c_lbl.border = BORDER_BOTTOM_THICK
    for col in (6, 7, 8):
        colL = get_column_letter(col)
        c = ws.cell(row=next_row, column=col)
        c.value = f"=SUM({colL}{fila_inicio}:{colL}{fila_fin})"
        c.font = FONT_TOTAL
        c.fill = FILL_TOTAL
        c.alignment = ALIGN_RIGHT
        c.border = BORDER_BOTTOM_THICK
        c.number_format = EUR_FMT
    pl_ref = (get_column_letter(8), next_row)
    next_row += 2

    # Pie informativo con la doctrina (recordatorio)
    ws.merge_cells(start_row=next_row, start_column=1,
                   end_row=next_row, end_column=8)
    pie = ws.cell(
        row=next_row, column=1,
        value=(
            "Doctrina: Manual práctico AEAT IRPF cap 11 §14 "
            "(Operaciones realizadas en los mercados de futuros y opciones) "
            "+ Art. 33 LIRPF — base imponible del ahorro, casilla 1626 c.4. "
            "Sin retención en origen. La regla 2 meses (Art. 33.5.f) NO "
            "aplica a futuros (son derivados, no valores admitidos a "
            "negociación en el sentido del precepto). Posiciones abiertas "
            "a 31-dic con MTM unrealizado: NO se declaran hasta el cierre "
            "del contrato (sin liquidación no hay devengo)."
        ),
    )
    pie.font = FONT_MUTED
    pie.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 70

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    return ws, {"pl_ref": pl_ref}


# ─────────────────────────────────────────────────────────────────────────────
# Hojas: Forex y Treasury_Bills (solo IBKR — informativas, NO suman al Resumen)
# ─────────────────────────────────────────────────────────────────────────────

def _build_forex(wb, ejercicio: int, fecha_gen: str, fx_rows: list[dict]):
    """Hoja con G/P de divisa por moneda. Datos de la sección
    'Realized & Unrealized Performance Summary' del Activity Statement IBKR.

    NO suma al Resumen — la decisión fiscal (declarar o aplicar la
    tolerancia práctica para diferencias pequeñas) queda en manos del
    usuario.
    """
    ws = wb.create_sheet("Forex")
    next_row = _put_brand_header(ws, ejercicio, "Forex — G/P de divisa (IBKR)", fecha_gen, ncols=4)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    leg = ws.cell(row=next_row, column=1,
                  value="Pérdidas y ganancias por diferencia de cambio en saldos en "
                        "divisa extranjera (Art. 33 LIRPF). Hecho imponible: cada "
                        "consumo del saldo. Casillas RentaWEB Renta 2025: 1624-1654 "
                        "(otros elementos patrimoniales). Tipo en casilla 1626. En la "
                        "práctica, para diferencias de cambio pequeñas en particulares "
                        "(orden de magnitud ~1.000 EUR) la AEAT suele tolerar su no "
                        "declaración; no es una regla escrita en la ley sino una "
                        "tolerancia operativa, así que la decisión queda en el "
                        "usuario. Esta hoja NO se suma al Resumen.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 80
    next_row += 2

    headers = ["Divisa", "Realized (EUR)", "Unrealized (EUR)", "Total (EUR)"]
    widths  = [12, 18, 18, 18]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    total_real = Decimal('0')
    total_unr  = Decimal('0')
    for r in sorted(fx_rows, key=lambda x: x['divisa']):
        realized   = _to_dec(r['realized']).quantize(Decimal('0.01'), ROUND_HALF_UP)
        unrealized = _to_dec(r['unrealized']).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_real += realized
        total_unr  += unrealized
        ws.cell(row=next_row, column=1, value=r['divisa']).font = FONT_BOLD
        c2 = ws.cell(row=next_row, column=2, value=float(realized))
        c3 = ws.cell(row=next_row, column=3, value=float(unrealized))
        c4 = ws.cell(row=next_row, column=4, value=float(realized + unrealized))
        for c in (c2, c3, c4):
            c.number_format = EUR_FMT
            c.alignment = ALIGN_RIGHT
        for col in range(1, 5):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
        next_row += 1

    # Total
    total_row = next_row
    ws.cell(row=next_row, column=1, value="TOTAL").font = FONT_BOLD
    for col, val in [(2, float(total_real)),
                     (3, float(total_unr)),
                     (4, float(total_real + total_unr))]:
        c = ws.cell(row=next_row, column=col, value=val)
        c.font = FONT_BOLD
        c.number_format = EUR_FMT
        c.alignment = ALIGN_RIGHT
        c.fill = FILL_SUBHEADER
    for col in range(1, 5):
        c = ws.cell(row=next_row, column=col)
        c.border = BORDER_ALL
        if col == 1:
            c.fill = FILL_SUBHEADER
    next_row += 2

    # Nota fiscal
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    nota = ws.cell(row=next_row, column=1,
                   value=f"Realized total: {float(total_real):.2f} EUR. "
                         f"Si decides declararlo: RentaWEB → F2 → casillas 1624-1654 "
                         f"→ Tipo (1626) = 'Resto'. Importe negativo = pérdida.")
    nota.font = FONT_MUTED
    nota.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 32

    # Devolver la celda del Realized total para que el Resumen lo referencie.
    return ws, ('B', total_row)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Tasas externas (informativa — Art. 35.1.b LIRPF)
# ─────────────────────────────────────────────────────────────────────────────

def _build_tasas_externas(wb, ejercicio: int, fecha_gen: str,
                          operaciones: list[dict],
                          operaciones_historicas: dict | None = None):
    """Hoja informativa con desglose por jurisdiccion de tasas externas
    (ITF Espana, UK Stamp Duty, French FTT, HK Stamp Duty, etc.) detectadas
    en las operaciones del ejercicio.

    Estas tasas YA estan sumadas al coste de adquisicion en la hoja
    Operaciones (col J 'Tasas ext.' y col G 'Gastos'). Esta hoja es trazabilidad
    pura: que has pagado, donde, en que operacion.

    Tributo inherente a la adquisicion/transmision (Art. 35.1.b LIRPF).

    Si no se detectan tasas externas, la hoja sigue creandose con un mensaje
    informativo (no se silencia para que el usuario sepa que se ha buscado).
    """
    # Importar el calculador desde irpf/generar_irpf.py (mismo patron que pdf_generator).
    import os as _os
    import sys as _sys
    _irpf_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "..", "irpf")
    if _irpf_dir not in _sys.path:
        _sys.path.insert(0, _irpf_dir)
    from generar_irpf import compute_external_fees_summary  # type: ignore

    summary = compute_external_fees_summary(operaciones)

    ws = wb.create_sheet("Tasas_externas")
    next_row = _put_brand_header(
        ws, ejercicio,
        "Tasas externas — Tributos por transaccion (Art. 35.1.b LIRPF)",
        fecha_gen, ncols=6,
    )

    # Leyenda con la doctrina fiscal
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=6)
    leg = ws.cell(row=next_row, column=1,
                  value="Tributos inherentes a la adquisicion/transmision: ITF "
                        "espanol (Ley 5/2020), UK/Dublin Stamp Duty, French FTT, "
                        "Hong Kong Stamp Duty, SEC/FINRA fees. Forman parte del "
                        "coste de adquisicion (Art. 35.1.b LIRPF). "
                        "⚠️ ESTAS CIFRAS YA ESTAN SUMADAS AL COSTE DE CADA OPERACION "
                        "EN LA HOJA OPERACIONES (col J 'Tasas ext.' + col G 'Gastos'). "
                        "NO reintroducir en 'Otros gastos' ni en otra casilla — "
                        "seria contar el gasto dos veces. Esta hoja es trazabilidad "
                        "pura por jurisdiccion para auditoria del informe.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 95
    next_row += 2

    # ── Resumen por jurisdiccion ──────────────────────────────────────────
    if summary['total_global'] > 0:
        ws.cell(row=next_row, column=1, value="RESUMEN POR JURISDICCION").font = Font(
            name="Calibri", size=12, bold=True, color="0B2B8F")
        next_row += 1

        headers = ["Jurisdiccion", "Importe (EUR)", "Operaciones"]
        widths  = [50, 16, 14]
        _put_table_header(ws, next_row, headers, widths)
        next_row += 1

        for jur in summary['jur_order']:
            tot = summary['por_jur_total'][jur]
            n   = summary['por_jur_ops'][jur]
            if tot <= 0:
                continue
            ws.cell(row=next_row, column=1, value=summary['labels'][jur]).font = FONT_BODY
            c2 = ws.cell(row=next_row, column=2, value=float(tot))
            c2.number_format = EUR_FMT
            c2.alignment = ALIGN_RIGHT
            c3 = ws.cell(row=next_row, column=3, value=n)
            c3.alignment = ALIGN_CENTER
            for col in range(1, 4):
                ws.cell(row=next_row, column=col).border = BORDER_ALL
            next_row += 1

        # Total
        ws.cell(row=next_row, column=1, value="TOTAL").font = FONT_BOLD
        ws.cell(row=next_row, column=1).fill = FILL_SUBHEADER
        c2 = ws.cell(row=next_row, column=2, value=float(summary['total_global']))
        c2.font = FONT_BOLD
        c2.fill = FILL_SUBHEADER
        c2.number_format = EUR_FMT
        c2.alignment = ALIGN_RIGHT
        n_total = sum(summary['por_jur_ops'].values())
        c3 = ws.cell(row=next_row, column=3, value=n_total)
        c3.font = FONT_BOLD
        c3.fill = FILL_SUBHEADER
        c3.alignment = ALIGN_CENTER
        for col in range(1, 4):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
        next_row += 2

        # ── Detalle por operacion ─────────────────────────────────────────
        ws.cell(row=next_row, column=1, value="DETALLE POR OPERACION").font = Font(
            name="Calibri", size=12, bold=True, color="0B2B8F")
        next_row += 1

        headers = ["Fecha", "Tipo", "ISIN", "Empresa", "Cant.", "Jur.", "Importe (EUR)", "Broker"]
        widths  = [12, 8, 16, 40, 10, 8, 14, 12]
        # Ajusta las columnas (esta hoja arranca en 6 cols pero la tabla detalle
        # usa 8). Ampliamos.
        _put_table_header(ws, next_row, headers, widths)
        next_row += 1
        for d in summary['detalles']:
            ws.cell(row=next_row, column=1, value=d['fecha']).alignment = ALIGN_CENTER
            ws.cell(row=next_row, column=2, value=d['op_tipo']).alignment = ALIGN_CENTER
            ws.cell(row=next_row, column=3, value=d['isin'])
            ws.cell(row=next_row, column=4, value=_safe_cell(d['nombre']))
            c5 = ws.cell(row=next_row, column=5, value=float(d['cantidad']))
            c5.number_format = NUM_FMT
            c5.alignment = ALIGN_RIGHT
            ws.cell(row=next_row, column=6, value=d['jur'].upper()).alignment = ALIGN_CENTER
            c7 = ws.cell(row=next_row, column=7, value=float(d['importe']))
            c7.number_format = EUR_FMT
            c7.alignment = ALIGN_RIGHT
            ws.cell(row=next_row, column=8, value=d['broker']).alignment = ALIGN_CENTER
            for col in range(1, 9):
                ws.cell(row=next_row, column=col).border = BORDER_ALL
                ws.cell(row=next_row, column=col).font = FONT_BODY
            next_row += 1
    else:
        ws.cell(row=next_row, column=1,
                value="No se han detectado tasas externas en el ejercicio.").font = FONT_BODY
        next_row += 2
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=6)
        ws.cell(row=next_row, column=1,
                value="Esto es normal si no operas con valores sujetos a ITF "
                      "espanol (acciones IBEX > 1.000M EUR), UK Stamp Duty "
                      "(LSE/Dublin), French FTT (FR > 1B EUR) o HK Stamp Duty.").font = FONT_MUTED
        ws.row_dimensions[next_row].height = 32

    # Fuentes
    next_row += 2
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=8)
    src = ws.cell(row=next_row, column=1,
                  value="Fuentes: DeGiro extracto de cuenta — filas 'Stamp Duty', "
                        "'Impuesto de transaccion Frances', 'Spanish Transaction Tax', "
                        "enlazadas al trade por ID Orden. IBKR Activity Statement, "
                        "seccion 'Transaction Fees', enlazada al trade por (Symbol, Date, Quantity).")
    src.font = FONT_MUTED
    src.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 38

    return ws


def _build_treasury_bills(wb, ejercicio: int, fecha_gen: str, tbills_rows: list[dict]):
    """Hoja con Treasury Bills (intereses) — RCM, casilla 0027.

    NO suma al Resumen para evitar conflictos con TR cuenta remunerada y otros
    intereses. Visibilidad informativa.
    """
    ws = wb.create_sheet("Treasury_Bills")
    next_row = _put_brand_header(ws, ejercicio, "Treasury Bills — Intereses (IBKR)", fecha_gen, ncols=2)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=2)
    leg = ws.cell(row=next_row, column=1,
                  value="Treasury Bills (T-Bills) detectados en la sección 'Realized & "
                        "Unrealized Performance Summary' del Activity Statement IBKR. "
                        "El P&L realizado es el INTERÉS implícito al redimir/vender el "
                        "T-Bill. Tributa como rendimiento de capital mobiliario "
                        "(intereses) — Renta 2025: casilla 0030 (T-Bills) o 0027 (intereses). Si IBKR retuvo en "
                        "origen sobre el T-Bill, ver sección Withholding Tax y declarar "
                        "en casilla 0588 (CDI tope 10% para US según convenio).")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 80
    next_row += 2

    headers = ["Symbol / Description", "Realized (EUR)"]
    widths  = [60, 18]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    total = Decimal('0')
    for t in tbills_rows:
        realized = _to_dec(t['realized']).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total += realized
        ws.cell(row=next_row, column=1, value=t['symbol'])
        c2 = ws.cell(row=next_row, column=2, value=float(realized))
        c2.number_format = EUR_FMT
        c2.alignment = ALIGN_RIGHT
        for col in range(1, 3):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
        next_row += 1

    total_row = next_row
    ws.cell(row=next_row, column=1, value="TOTAL — casilla 0027").font = FONT_BOLD
    c = ws.cell(row=next_row, column=2, value=float(total))
    c.font = FONT_BOLD
    c.number_format = EUR_FMT
    c.alignment = ALIGN_RIGHT
    c.fill = FILL_SUBHEADER
    for col in range(1, 3):
        cc = ws.cell(row=next_row, column=col)
        cc.border = BORDER_ALL
        if col == 1:
            cc.fill = FILL_SUBHEADER

    # Devolver la celda del total para que el Resumen lo referencie.
    return ws, ('B', total_row)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Intereses (IBKR Credit/Debit/Bond + TR cuenta remunerada, casilla 0027)
# ─────────────────────────────────────────────────────────────────────────────

def _build_intereses(wb, ejercicio: int, fecha_gen: str,
                     interest_rows: list[dict]):
    """Hoja con desglose de los intereses RCM (casilla 0027), agregando dos
    fuentes en la misma tabla:

        - IBKR sección `Interest` del Activity Statement:
            · Credit Interest (cobrado al cliente)        -> RCM, casilla 0027
            · Bond Interest (cupón de bonos)              -> RCM, casilla 0027
            · Debit Interest (pagado al broker)           -> Informativo.
              La doctrina mayoritaria considera estos intereses NO deducibles
              para el inversor particular (Art. 26.1.a LIRPF limita los
              gastos del RCM por cesión a terceros a administración y
              depósito). No se suma automáticamente al Resumen.

        - Trade Republic Sucursal ES — pagos mensuales de la cuenta
          remunerada IBAN ES: tributan como intereses (Art. 25.2 LIRPF) y
          van a casilla 0027 con retención del 19 % a casilla 0591 cuando
          aplica.

    La columna `broker` de cada fila identifica el origen (IBKR / TR).

    Devuelve (ws, total_credit_ref) donde total_credit_ref = (col_letter, row)
    para que el Resumen pueda referenciar el total que va a casilla 0027.
    """
    ws = wb.create_sheet("Intereses")
    next_row = _put_brand_header(
        ws, ejercicio,
        "Intereses RCM — IBKR Credit/Debit/Bond + TR cuenta remunerada",
        fecha_gen, ncols=6,
    )

    # Leyenda
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=6)
    leg = ws.cell(row=next_row, column=1,
                  value="Intereses RCM agregados de dos fuentes: la sección "
                        "'Interest' del Activity Statement IBKR (Credit/Bond/Debit) "
                        "y los pagos mensuales de la cuenta remunerada de Trade "
                        "Republic (post-migración IBAN ES). Credit Interest y Bond "
                        "Interest (IBKR) + intereses TR ES van a casilla 0027 (RCM "
                        "intereses de cuenta / obligaciones). Debit Interest (IBKR, "
                        "pagado al broker por margen o saldo deudor) NO se suma "
                        "automáticamente: la doctrina mayoritaria considera estos "
                        "intereses no deducibles para el inversor particular "
                        "(Art. 26.1.a LIRPF limita los gastos del RCM por cesión "
                        "a terceros a administración y "
                        "deposito). Si tu asesor identifica una via de deduccion en "
                        "tu caso, escuchalo, pero la posicion conservadora es no "
                        "incluirlo. Conversion a EUR via BCE del dia de cada interes.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 78
    next_row += 2

    # Col 5 "Ret. ES 0591": retención IRPF española 19% que practica TR Sucursal
    # ES sobre los intereses de la cuenta remunerada post-migración (Modelo 198).
    # 100% acreditable — campo "Retenciones" del popup individual de 0027.
    headers = ["Fecha", "Divisa", "Importe local", "Importe (EUR)",
               "Ret. ES 0591 (EUR)", "Tipo", "Descripcion"]
    widths  = [12, 8, 14, 14, 16, 14, 50]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    TIPO_LABEL = {
        'credit':        "Credit (RCM 0027)",
        'bond_interest': "Bond Int. (0027)",
        'debit':         "Debit (informativo)",
    }

    total_credit  = Decimal('0')   # va a casilla 0027
    total_debit   = Decimal('0')   # informativo
    total_ret_es  = Decimal('0')   # retención española 19% (TR Sucursal ES) → 0591
    for r in sorted(interest_rows, key=lambda x: x.get('fecha', '')):
        importe_eur = _to_dec(r['importe_eur']).quantize(Decimal('0.01'),
                                                         ROUND_HALF_UP)
        ret_es_eur = _to_dec(r.get('retencion_es_eur', 0)).quantize(
            Decimal('0.01'), ROUND_HALF_UP)
        if r['tipo'] in ('credit', 'bond_interest'):
            total_credit += importe_eur
            total_ret_es += ret_es_eur
        elif r['tipo'] == 'debit':
            total_debit += importe_eur
        ws.cell(row=next_row, column=1, value=r.get('fecha', '')).alignment = ALIGN_CENTER
        ws.cell(row=next_row, column=2, value=r.get('divisa', '')).alignment = ALIGN_CENTER
        c3 = ws.cell(row=next_row, column=3,
                     value=float(r.get('importe_local', 0) or 0))
        c3.number_format = NUM_FMT
        c3.alignment = ALIGN_RIGHT
        c4 = ws.cell(row=next_row, column=4, value=float(importe_eur))
        c4.number_format = EUR_FMT
        c4.alignment = ALIGN_RIGHT
        if ret_es_eur:
            c5 = ws.cell(row=next_row, column=5, value=float(ret_es_eur))
            c5.number_format = EUR_FMT
            c5.alignment = ALIGN_RIGHT
        ws.cell(row=next_row, column=6,
                value=TIPO_LABEL.get(r['tipo'], r['tipo'])).alignment = ALIGN_CENTER
        ws.cell(row=next_row, column=7, value=_safe_cell(r.get('descripcion', '')))
        for col in range(1, 8):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
            ws.cell(row=next_row, column=col).font = FONT_BODY
        next_row += 1

    next_row += 1

    # Subtotal Credit/Bond -> casilla 0027
    ws.cell(row=next_row, column=1,
            value="TOTAL Credit + Bond (suma a casilla 0027 RCM)").font = FONT_BOLD
    ws.cell(row=next_row, column=1).fill = FILL_SUBHEADER
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=3)
    c_credit = ws.cell(row=next_row, column=4, value=float(total_credit))
    c_credit.font = FONT_BOLD
    c_credit.fill = FILL_SUBHEADER
    c_credit.number_format = EUR_FMT
    c_credit.alignment = ALIGN_RIGHT
    # Misma fila: total de retención española en col 5 (resaltado).
    c_credit_ret = ws.cell(row=next_row, column=5, value=float(total_ret_es))
    c_credit_ret.font = FONT_BOLD
    c_credit_ret.fill = FILL_SUBHEADER
    c_credit_ret.number_format = EUR_FMT
    c_credit_ret.alignment = ALIGN_RIGHT
    for col in range(1, 8):
        ws.cell(row=next_row, column=col).border = BORDER_ALL
    credit_row = next_row
    ret_es_row = next_row
    next_row += 1

    # Subtotal Debit -> informativo
    ws.cell(row=next_row, column=1,
            value="TOTAL Debit (informativo, NO se suma automaticamente)").font = FONT_BOLD
    ws.cell(row=next_row, column=1).fill = FILL_DISCLAIMER
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=3)
    c_debit = ws.cell(row=next_row, column=4, value=float(total_debit))
    c_debit.font = FONT_BOLD
    c_debit.fill = FILL_DISCLAIMER
    c_debit.number_format = EUR_FMT
    c_debit.alignment = ALIGN_RIGHT
    for col in range(1, 8):
        ws.cell(row=next_row, column=col).border = BORDER_ALL

    # Línea aclaratoria de la retención española de intereses (solo si la hay).
    if total_ret_es > 0:
        next_row += 1
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=7)
        c_nota = ws.cell(row=next_row, column=1,
                         value=f'Retención IRPF española de intereses (TR Sucursal ES): '
                               f'{float(total_ret_es):.2f} EUR → campo "Retenciones" del popup '
                               f'individual de la casilla 0027 (100% acreditable).')
        c_nota.font = FONT_MUTED
        c_nota.alignment = ALIGN_WRAP
        ws.row_dimensions[next_row].height = 30

    # ret_es_ref solo si hay retención (mismo patrón que interest_credit_ref).
    ret_es_ref = ('E', ret_es_row) if total_ret_es > 0 else None
    return ws, ('D', credit_row), ret_es_ref


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Staking de criptomonedas — RCM Art. 25.2 LIRPF, DGT V1766-22
# ─────────────────────────────────────────────────────────────────────────────

def _build_staking(wb, ejercicio: int, fecha_gen: str,
                   staking_rows: list[dict]):
    """Hoja con recompensas de staking de criptomonedas (Trade Republic).

    Doctrina — DGT V1766-22 (26-7-2022): los rendimientos del staking se
    califican como rendimientos del capital mobiliario por cesión a terceros
    de capitales propios SATISFECHO EN ESPECIE (Art. 25.2 LIRPF). Valoración
    en EUR al precio de mercado en el momento de cada recepción (Art. 43.1
    LIRPF, regla para operaciones en especie).

    Casilla RentaWEB 2025: 0027 (intereses de cuentas, depósitos y activos
    financieros), por analogía con un rendimiento periódico por cesión de
    capital. La V1766-22 NO fija casilla, así que existe alternativa doctrinal
    en 0031 (transmisión / amortización de otros activos financieros) por ser
    el rendimiento satisfecho en especie. Implicación práctica: ambas tributan
    en base del ahorro al mismo tipo → la cuota es idéntica; la elección
    afecta solo a coherencia formal y trazabilidad ante un requerimiento AEAT.

    Devuelve (ws, total_ref) para que el Resumen referencie el total → 0027.
    """
    ws = wb.create_sheet("Staking")
    next_row = _put_brand_header(
        ws, ejercicio,
        "Staking de criptomonedas — RCM por cesión a terceros (Trade Republic)",
        fecha_gen, ncols=6,
    )

    # Leyenda con doctrina + implicaciones prácticas
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=6)
    leg = ws.cell(row=next_row, column=1,
                  value="Recompensas de staking (DELIVERY/FREE_RECEIPT de cripto en "
                        "Trade Republic). Doctrina DGT V1766-22 (26-7-2022): se "
                        "califican como rendimientos del capital mobiliario por la "
                        "cesión a terceros de capitales propios satisfecho EN ESPECIE "
                        "(Art. 25.2 LIRPF), valorados en EUR al precio de mercado en "
                        "el momento de cada recepción (Art. 43.1 LIRPF). La consulta "
                        "NO fija casilla. Cuádrate los enruta a CASILLA 0027 "
                        "(intereses de cuentas, depósitos y activos financieros) por "
                        "analogía con un rendimiento periódico por cesión de capital. "
                        "Alternativa doctrinal: casilla 0031 (transmisión / "
                        "amortización de otros activos financieros) por ser el "
                        "rendimiento satisfecho en especie. Implicación práctica: "
                        "ambas tributan en base del ahorro al mismo tipo → la cuota "
                        "es idéntica; la elección solo afecta a coherencia formal y "
                        "trazabilidad ante un requerimiento AEAT.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 110
    next_row += 2

    headers = ["Fecha", "Activo", "Cantidad", "Precio EUR/ud.", "Importe (EUR)", "Broker"]
    widths  = [12, 12, 18, 16, 14, 10]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    total = Decimal('0')
    for r in sorted(staking_rows, key=lambda x: str(x.get('fecha', ''))):
        importe = _to_dec(r.get('importe_eur', 0)).quantize(
            Decimal('0.01'), ROUND_HALF_UP)
        total += importe
        ws.cell(row=next_row, column=1, value=str(r.get('fecha', ''))).alignment = ALIGN_CENTER
        ws.cell(row=next_row, column=2, value=str(r.get('asset', ''))).alignment = ALIGN_CENTER
        c3 = ws.cell(row=next_row, column=3,
                     value=float(r.get('cantidad', 0) or 0))
        c3.number_format = "0.00000000"
        c3.alignment = ALIGN_RIGHT
        c4 = ws.cell(row=next_row, column=4,
                     value=float(r.get('precio_unit_eur', 0) or 0))
        c4.number_format = EUR_FMT
        c4.alignment = ALIGN_RIGHT
        c5 = ws.cell(row=next_row, column=5, value=float(importe))
        c5.number_format = EUR_FMT
        c5.alignment = ALIGN_RIGHT
        ws.cell(row=next_row, column=6, value=str(r.get('broker', 'TR'))).alignment = ALIGN_CENTER
        for col in range(1, 7):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
            ws.cell(row=next_row, column=col).font = FONT_BODY
        next_row += 1

    next_row += 1
    # Subtotal -> casilla 0027 (referenciable desde Resumen).
    ws.cell(row=next_row, column=1,
            value="TOTAL Staking (suma a casilla 0027 RCM — alternativa 0031)").font = FONT_BOLD
    ws.cell(row=next_row, column=1).fill = FILL_SUBHEADER
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    c_total = ws.cell(row=next_row, column=5, value=float(total))
    c_total.font = FONT_BOLD
    c_total.fill = FILL_SUBHEADER
    c_total.number_format = EUR_FMT
    c_total.alignment = ALIGN_RIGHT
    for col in range(1, 7):
        ws.cell(row=next_row, column=col).border = BORDER_ALL
    total_row = next_row

    return ws, ('E', total_row)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Gastos del broker (administración y depósito · Art. 26.1.a LIRPF)
# ─────────────────────────────────────────────────────────────────────────────

def _build_gastos_plataforma(wb, ejercicio: int, fecha_gen: str,
                             gastos: list[dict]):
    """Hoja con las comisiones del broker (conectividad mensual DeGiro,
    custodia, mantenimiento) que reducen el RCM neto. Art. 26.1.a LIRPF.

    `gastos` es la lista que devuelve `parse_degiro_cuenta` (3er valor):
        [{fecha, descripcion, importe_eur}, ...]

    Devuelve (ws, total_ref) con la celda del total en la columna C de la
    fila TOTAL — referenciada desde el Resumen.
    """
    ws = wb.create_sheet("Gastos_plataforma")
    next_row = _put_brand_header(
        ws, ejercicio,
        "Gastos del broker — administración y depósito (Art. 26.1.a LIRPF)",
        fecha_gen, ncols=3,
    )

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=3)
    leg = ws.cell(row=next_row, column=1,
                  value="Comisiones que el broker cobra por mantener cuenta o "
                        "acceso a mercados (conectividad mensual DeGiro, custodia, "
                        "mantenimiento). Deducibles del rendimiento íntegro del "
                        "capital mobiliario antes de tributar (Art. 26.1.a LIRPF — "
                        "gastos de administración y depósito de valores negociables). "
                        "NO pueden hacer el RCM neto negativo. "
                        "🔍 IMPORTANTE: NO existe casilla independiente. Se introducen "
                        "en el campo \"Gastos de administración y depósito\" del popup "
                        "individual al editar cualquier rendimiento del capital "
                        "mobiliario (típicamente al editar 0029 Dividendos). RentaWEB "
                        "los totaliza automáticamente en la casilla 0037 del bloque B.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 64
    next_row += 2

    headers = ["Fecha", "Concepto", "Importe (EUR)"]
    widths  = [14, 60, 16]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    total = Decimal('0')
    for g in sorted(gastos, key=lambda x: x.get('fecha', '')):
        importe = _to_dec(g.get('importe_eur', 0)).quantize(
            Decimal('0.01'), ROUND_HALF_UP)
        total += importe
        ws.cell(row=next_row, column=1, value=str(g.get('fecha', ''))).alignment = ALIGN_CENTER
        ws.cell(row=next_row, column=2, value=_safe_cell(g.get('descripcion', '')))
        c3 = ws.cell(row=next_row, column=3, value=float(importe))
        c3.number_format = EUR_FMT
        c3.alignment = ALIGN_RIGHT
        for col in range(1, 4):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
            ws.cell(row=next_row, column=col).font = FONT_BODY
        next_row += 1

    total_row = next_row
    ws.cell(row=next_row, column=1,
            value="TOTAL — al popup de 0029 (campo \"Gastos admón. y depósito\"); RentaWEB suma a 0037").font = FONT_BOLD
    ws.cell(row=next_row, column=1).fill = FILL_SUBHEADER
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=2)
    c3 = ws.cell(row=next_row, column=3, value=float(total))
    c3.font = FONT_BOLD
    c3.fill = FILL_SUBHEADER
    c3.number_format = EUR_FMT
    c3.alignment = ALIGN_RIGHT
    for col in range(1, 4):
        ws.cell(row=next_row, column=col).border = BORDER_ALL

    return ws, ('C', total_row)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Por_broker (auditoría cruzada contra Modelo 198 / borrador AEAT)
# ─────────────────────────────────────────────────────────────────────────────

# Información sobre obligación informativa AEAT por broker — usada en la
# columna "Reporta a AEAT" para que el usuario sepa qué importes esperar
# precargados en el borrador y cuáles tiene que añadir él manualmente.
_BROKER_AEAT_INFO = {
    'DeGiro': (
        "No (broker NL, sin sucursal ES)",
        "Los dividendos cobrados a través de DeGiro NO llegan precargados "
        "en tu borrador AEAT. Hay que declararlos íntegramente con "
        "retención en origen (CDI por país).",
    ),
    'IBKR': (
        "No (broker IE, sin sucursal ES)",
        "Los dividendos y la sección Interest del Activity Statement NO "
        "llegan precargados en tu borrador AEAT. Hay que declararlos a "
        "mano usando este informe.",
    ),
    'TR': (
        "Mixto — sí post-migración",
        "Trade Republic Sucursal ES (alta BOE-A-2025-5909, 24-mar-2025) "
        "retiene IRPF al 19 % y reporta vía Modelo 198 sobre dividendos e "
        "intereses cobrados a partir de la fecha de migración personal a "
        "IBAN ES. Lo pre-migración (TR Bank GmbH) NO se reporta — añadir "
        "manualmente. Ver post: trade-republic-iban-es-retencion-renta-2025.",
    ),
}


def aggregate_por_broker(
    dividendos_resumen: Optional[list[dict]],
    intereses: Optional[list[dict]],
) -> "OrderedDict[str, dict]":
    """Agrupa los totales de RCM (dividendos + intereses) por broker.

    Devuelve un OrderedDict {broker: {div_bruto, div_ret_org, div_cdi,
    div_ret_nac, int_bruto, int_ret}} con los brokers conocidos siempre
    presentes (a cero si no hay actividad) para garantizar orden estable.

    Reglas de imputación:
      - Dividendos: el motor agrega por ISIN; cada ISIN trae `eventos`
        con broker individual. El bruto se atribuye directamente al
        broker del evento. La retención origen, el CDI (0588) y la
        retención nacional (0591) se reparten proporcionalmente al
        bruto que cada broker aportó al ISIN (cubre el caso raro de
        mismo ISIN en dos brokers).
      - Intereses: cada fila ya trae `broker` ('IBKR' por defecto) y
        `retencion_es_eur` cuando viene de TR. Debit Interest se omite.

    Exportada (no `_`-prefijada) para que pdf_generator pueda reutilizar
    la misma lógica sin duplicar — el PDF y el XLSX deben mostrar valores
    idénticos.
    """
    from collections import OrderedDict
    agg: "OrderedDict[str, dict]" = OrderedDict()
    for b in ('DeGiro', 'IBKR', 'TR'):
        agg[b] = {
            'div_bruto':   Decimal('0'),
            'div_ret_org': Decimal('0'),
            'div_cdi':     Decimal('0'),
            'div_ret_nac': Decimal('0'),
            'int_bruto':   Decimal('0'),
            'int_ret':     Decimal('0'),
        }

    def _ensure(broker: str) -> dict:
        if broker not in agg:
            agg[broker] = {
                'div_bruto':   Decimal('0'),
                'div_ret_org': Decimal('0'),
                'div_cdi':     Decimal('0'),
                'div_ret_nac': Decimal('0'),
                'int_bruto':   Decimal('0'),
                'int_ret':     Decimal('0'),
            }
        return agg[broker]

    for d in dividendos_resumen or []:
        eventos = d.get('eventos') or []
        bruto_isin   = _to_dec(d.get('bruto', 0))
        ret_org_isin = _to_dec(d.get('ret_origen', d.get('retencion_origen', 0)))
        ret_es_isin  = _to_dec(d.get('retencion_es', 0))
        recup_isin   = _to_dec(d.get('recuperable', 0))
        es_nacional  = bool(d.get('es_nacional'))

        # Los `eventos` vienen YA consolidados por (fecha, broker) desde
        # calcular_resumen_dividendos, con los campos `bruto`,
        # `retencion_origen` y `retencion_es` por broker real (no `tipo`/
        # `importe_eur`). Sumamos esos campos directamente por broker — es
        # exacto, sin prorrateo. Antes el bucle filtraba por tipo='DIV' +
        # importe_eur (claves que el evento consolidado no tiene) → nunca
        # casaba → todo caía al fallback (100% al primer broker).
        brokers_bruto: dict[str, Decimal] = {}
        tiene_eventos = False
        for ev in eventos:
            b = ev.get('broker') or 'IBKR'
            bruto_b = _to_dec(ev.get('bruto', 0))
            if bruto_b == 0 and _to_dec(ev.get('retencion_origen', 0)) == 0 \
                    and _to_dec(ev.get('retencion_es', 0)) == 0:
                continue
            tiene_eventos = True
            a = _ensure(b)
            a['div_bruto']   += bruto_b
            a['div_ret_org'] += _to_dec(ev.get('retencion_origen', 0))
            a['div_ret_nac'] += _to_dec(ev.get('retencion_es', 0))
            brokers_bruto[b] = brokers_bruto.get(b, Decimal('0')) + bruto_b

        if not tiene_eventos and bruto_isin:
            # Fallback (resumen sin eventos): atribuir al primer broker.
            brokers_str = (d.get('brokers') or '').split(',')
            first = brokers_str[0].strip() if brokers_str else 'DeGiro'
            a = _ensure(first)
            a['div_bruto']   += bruto_isin
            a['div_ret_org'] += ret_org_isin
            a['div_ret_nac'] += ret_es_isin
            brokers_bruto[first] = bruto_isin

        # CDI recuperable (0588): se computa topado al CDI a nivel ISIN, así
        # que se prorratea por la cuota de bruto de cada broker.
        if not es_nacional and recup_isin and brokers_bruto:
            total_bruto = sum(brokers_bruto.values()) or Decimal('1')
            for broker, bruto_b in brokers_bruto.items():
                a = _ensure(broker)
                a['div_cdi'] += (recup_isin * bruto_b / total_bruto).quantize(
                    Decimal('0.01'), ROUND_HALF_UP)

    for it in intereses or []:
        if it.get('tipo') == 'debit':
            continue
        broker = it.get('broker') or 'IBKR'
        importe = _to_dec(it.get('importe_eur', 0))
        ret_es  = _to_dec(it.get('retencion_es_eur', 0))
        a = _ensure(broker)
        a['int_bruto'] += importe
        a['int_ret']   += ret_es

    return agg


def _build_por_broker(wb, ejercicio: int, fecha_gen: str, *,
                      dividendos_resumen: Optional[list[dict]],
                      intereses: Optional[list[dict]]) -> None:
    """Tabla agregada de dividendos + intereses por broker — para que el
    usuario pueda cuadrar las cifras de cada broker contra lo que ya está
    precargado en su borrador AEAT (Modelo 198 para sucursales españolas)
    y detectar errores antes de presentar.

    Estructura:

        Broker | Div bruto | Ret div (origen) | CDI 0588 | Ret nac. 0591 |
        Int bruto | Ret int. | Total RCM | Total reten. | Reporta AEAT

    Valores agregados desde:
      - `dividendos_resumen` (lista por ISIN del motor) — usa el campo
        `eventos` que sí tiene broker individual por pago.
      - `intereses` (lista combinada IBKR + TR del flujo principal) —
        cada fila tiene `broker` ('IBKR' o 'TR') y `retencion_es_eur`
        cuando viene de TR.

    Los valores se escriben como constantes (no SUMIFS) — más simple y
    cuadra al céntimo con los totales que ya muestra el Resumen. Si el
    usuario edita una celda amarilla en Dividendos o Intereses, esta
    hoja NO se auto-recalcula; ese caso requiere regenerar el informe.
    """
    ws = wb.create_sheet("Por_broker")
    next_row = _put_brand_header(
        ws, ejercicio,
        "Desglose por broker — auditoría vs borrador AEAT",
        fecha_gen, ncols=10,
    )

    # Leyenda
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=10)
    leg = ws.cell(
        row=next_row, column=1,
        value=(
            "Totales de dividendos e intereses agregados por broker para "
            "auditar contra lo que cada uno reporta a la AEAT. Los brokers "
            "extranjeros (DeGiro NL, Interactive Brokers IE) NO informan al "
            "Modelo 198 — sus dividendos e intereses NO aparecen "
            "precargados en tu borrador y hay que declararlos a mano. Trade "
            "Republic empezó a reportar vía Modelo 198 con la migración a "
            "IBAN ES (jun-2025+); el tramo pre-migración sigue sin reportar. "
            "Si tu borrador trae cifras de un broker, deberían cuadrar con "
            "esta tabla; si no cuadran, contrasta línea a línea con la hoja "
            "Dividendos o Intereses antes de aceptar el borrador."
        ),
    )
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 88
    next_row += 2

    headers = [
        "Broker",
        "Div. bruto",
        "Ret. div. origen",
        "CDI 0588 recup.",
        "Ret. nac. 0591",
        "Int. bruto",
        "Ret. int.",
        "Total RCM",
        "Total ret.",
        "Reporta a AEAT",
    ]
    widths  = [14, 13, 16, 16, 16, 13, 13, 14, 13, 32]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    # Agregación delegada al helper compartido — pdf_generator usa el mismo.
    agg = aggregate_por_broker(dividendos_resumen, intereses)

    # ── Escribir las filas ──────────────────────────────────────────────
    fila_inicio = next_row
    has_any_activity = False
    for broker, totals in agg.items():
        any_value = any(v != 0 for v in totals.values())
        if not any_value:
            continue
        has_any_activity = True
        ws.cell(row=next_row, column=1, value=broker).font = FONT_BOLD
        c2  = ws.cell(row=next_row, column=2, value=float(totals['div_bruto']))
        c3  = ws.cell(row=next_row, column=3, value=float(totals['div_ret_org']))
        c4  = ws.cell(row=next_row, column=4, value=float(totals['div_cdi']))
        c5  = ws.cell(row=next_row, column=5, value=float(totals['div_ret_nac']))
        c6  = ws.cell(row=next_row, column=6, value=float(totals['int_bruto']))
        c7  = ws.cell(row=next_row, column=7, value=float(totals['int_ret']))
        c8  = ws.cell(row=next_row, column=8,
                      value=float(totals['div_bruto'] + totals['int_bruto']))
        # Total retenciones del broker = origen (0588) + española 19% (0591,
        # div_ret_nac) + retención de intereses. Antes omitía div_ret_nac, que
        # es justo la retención que el Modelo 198 precarga en el borrador.
        c9  = ws.cell(row=next_row, column=9,
                      value=float(totals['div_ret_org'] + totals['div_ret_nac']
                                  + totals['int_ret']))
        c10 = ws.cell(row=next_row, column=10,
                      value=_BROKER_AEAT_INFO.get(broker, ("No",""))[0])
        for col in (2, 3, 4, 5, 6, 7, 8, 9):
            ws.cell(row=next_row, column=col).number_format = EUR_FMT
            ws.cell(row=next_row, column=col).alignment = ALIGN_RIGHT
        for col in range(1, 11):
            ws.cell(row=next_row, column=col).border = BORDER_ALL
            if col not in (2,3,4,5,6,7,8,9):
                ws.cell(row=next_row, column=col).font = (
                    FONT_BOLD if col == 1 else FONT_BODY
                )
        ws.cell(row=next_row, column=10).alignment = ALIGN_LEFT
        # Tooltip largo en la celda "Reporta a AEAT"
        from openpyxl.comments import Comment
        tooltip = _BROKER_AEAT_INFO.get(broker, ("","")).__getitem__(1)
        if tooltip:
            ws.cell(row=next_row, column=10).comment = Comment(tooltip, "Cuádrate")
        next_row += 1

    if not has_any_activity:
        ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=10)
        c = ws.cell(row=next_row, column=1,
                    value="(sin dividendos ni intereses en el ejercicio)")
        c.font = FONT_MUTED
        c.alignment = ALIGN_CENTER
        ws.freeze_panes = ws.cell(row=fila_inicio, column=1)
        return

    # Fila TOTAL
    fila_fin = next_row - 1
    ws.cell(row=next_row, column=1, value="TOTAL").font = FONT_TOTAL
    ws.cell(row=next_row, column=1).fill = FILL_TOTAL
    ws.cell(row=next_row, column=1).border = BORDER_BOTTOM_THICK
    for col in (2, 3, 4, 5, 6, 7, 8, 9):
        colL = get_column_letter(col)
        c = ws.cell(row=next_row, column=col,
                    value=f"=SUM({colL}{fila_inicio}:{colL}{fila_fin})")
        c.number_format = EUR_FMT
        c.font = FONT_TOTAL
        c.fill = FILL_TOTAL
        c.alignment = ALIGN_RIGHT
        c.border = BORDER_BOTTOM_THICK
    ws.cell(row=next_row, column=10).fill = FILL_TOTAL
    ws.cell(row=next_row, column=10).border = BORDER_BOTTOM_THICK

    ws.freeze_panes = ws.cell(row=fila_inicio, column=1)


# ─────────────────────────────────────────────────────────────────────────────
# Hoja: Resumen (casillas RentaWEB) — la más importante
# ─────────────────────────────────────────────────────────────────────────────

def _build_resumen(wb, ejercicio: int, fecha_gen: str,
                  *, gp_totales_por_casilla: dict, div_totales: dict,
                  perd_totales: dict, opt_totales: dict,
                  fx_total_ref=None, tbills_total_ref=None,
                  interest_credit_ref=None, interest_ret_es_ref=None,
                  staking_total_ref=None,
                  plataforma_total_ref=None,
                  futuros_totales: Optional[dict] = None,
                  fut_pl_ref=None):
    ws = wb.create_sheet("Resumen")
    next_row = _put_brand_header(ws, ejercicio,
                                 "Resumen por casilla — qué introducir en RentaWEB",
                                 fecha_gen, ncols=4)

    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    leg = ws.cell(row=next_row, column=1,
                  value="Esta es la hoja MAESTRA. Cada importe está enlazado por fórmula "
                        "a la hoja correspondiente, así que cualquier ajuste manual "
                        "(coste prorrateado tras un spin-off, dividendo añadido, etc.) "
                        "se refleja aquí automáticamente. Copia los valores finales a "
                        "RentaWEB en las casillas indicadas.")
    leg.font = FONT_MUTED
    leg.alignment = ALIGN_WRAP
    ws.row_dimensions[next_row].height = 56
    next_row += 2

    headers = ["Casilla", "Concepto", "Importe a declarar", "Hoja origen"]
    widths  = [14, 60, 22, 22]
    _put_table_header(ws, next_row, headers, widths)
    next_row += 1

    def fila(casilla, concepto, ref, hoja, *, bold=False, informativo=False):
        """`informativo=True` señaliza la fila como decisión del usuario:
        casilla en gris en vez de azul, valor sin negrita, sufijo aclaratorio.
        Útil para FX (Forex) y T-Bills, que NO suman automáticamente a las
        casillas oficiales — el usuario decide si los declara."""
        nonlocal next_row
        c1 = ws.cell(row=next_row, column=1, value=casilla)
        if informativo:
            concepto = f"{concepto} · INFORMATIVO — decides tú si lo declaras"
        c2 = ws.cell(row=next_row, column=2, value=_safe_cell(concepto))
        if ref is None:
            c3 = ws.cell(row=next_row, column=3, value=0)
        else:
            col_letter, fila_num = ref
            c3 = ws.cell(row=next_row, column=3,
                         value=f"='{hoja}'!{col_letter}{fila_num}")
        c4 = ws.cell(row=next_row, column=4, value=hoja)
        for col, c in enumerate([c1, c2, c3, c4], start=1):
            c.border = BORDER_ALL
            c.font = FONT_BOLD if bold else FONT_BODY
            if col == 1:
                c.alignment = ALIGN_CENTER
                if informativo:
                    c.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
                    c.fill = PatternFill("solid", fgColor="9CA3AF")  # gris medio
                else:
                    c.font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
                    c.fill = PatternFill("solid", fgColor=C_AZUL)
            elif col == 2 and informativo:
                c.alignment = ALIGN_LEFT
                c.font = FONT_MUTED
            elif col == 3:
                c.alignment = ALIGN_RIGHT
                c.number_format = EUR_FMT
                if bold:
                    c.font = FONT_TOTAL
                if informativo:
                    c.font = FONT_MUTED
            elif col == 4:
                c.alignment = ALIGN_CENTER
                c.font = FONT_MUTED
            else:
                c.alignment = ALIGN_LEFT
        next_row += 1

    # Bloque G/P
    if "0326-0340" in gp_totales_por_casilla:
        fila("0326-0340", "Ganancias/pérdidas — Acciones cotizadas (incluye REITs/SIIC y BDCs extranjeros)",
             gp_totales_por_casilla["0326-0340"], "G_P_por_valor", bold=True)
    if "0324/0325" in gp_totales_por_casilla:
        fila("0324/0325", "Ganancias/pérdidas — SOCIMI españolas (Ley 11/2009; sólo nacionales)",
             gp_totales_por_casilla["0324/0325"], "G_P_por_valor", bold=True)
    if "2224-2236" in gp_totales_por_casilla:
        fila("2224-2236", "Ganancias/pérdidas — ETFs / IIC sin retención (Renta 2025+, Art. 75.3.j RIRPF)",
             gp_totales_por_casilla["2224-2236"], "G_P_por_valor", bold=True)
    if "0341-0355" in gp_totales_por_casilla:
        fila("0341-0355", "Ganancias/pérdidas — Transmisión de derechos de suscripción",
             gp_totales_por_casilla["0341-0355"], "G_P_por_valor", bold=True)
    if "0031" in gp_totales_por_casilla:
        fila("0031", "Rendimientos por transmisión / amortización de bonos individuales (cupones cobrados → 0027)",
             gp_totales_por_casilla["0031"], "G_P_por_valor", bold=True)
    if "0031-ETC" in gp_totales_por_casilla:
        # ETC físico — misma casilla AEAT 0031 que los bonos, pero en el
        # Resumen se renderiza como fila separada para que el usuario sepa
        # qué parte del 0031 proviene de cada producto. RCM por cesión a
        # terceros (Art. 25.2.b LIRPF + DGT V0267-25). NO sujeto a regla 2M
        # (Art. 33.5.f LIRPF habla de pérdidas patrimoniales, no de RCM).
        fila("0031", "Rendimientos por transmisión de ETCs físicos (oro / plata / platino / paladio) — Art. 25.2.b LIRPF + DGT V0267-25",
             gp_totales_por_casilla["0031-ETC"], "G_P_por_valor", bold=True)
    if "1624-1654" in gp_totales_por_casilla:
        fila("1624-1654", "Otros elementos patrimoniales — derivados estructurados (Factor, Turbo, Mini, KO, ETN)",
             gp_totales_por_casilla["1624-1654"], "G_P_por_valor", bold=True)
    if "1800-1806" in gp_totales_por_casilla:
        fila("1800-1806", "Ganancias/pérdidas — Criptomonedas",
             gp_totales_por_casilla["1800-1806"], "G_P_por_valor", bold=True)

    # Dividendos
    if div_totales.get("bruto_total"):
        fila("0029", "Rendimientos del capital mobiliario — dividendos brutos "
             "(añade derechos residuales recomprados si aplica)",
             div_totales["bruto_total"], "Dividendos", bold=True)
    if div_totales.get("ret_es_total"):
        fila("0029 (popup)", "Retención IRPF de pagador español — campo \"Retenciones\" del popup individual de 0029",
             div_totales["ret_es_total"], "Dividendos")
    if div_totales.get("bruto_ext_con_ret"):
        fila("0588 (popup)", "Bruto extranjero con retención — campo \"Rendimientos netos reducidos del capital mobiliario\" del segundo popup de 0588 (base ahorro)",
             div_totales["bruto_ext_con_ret"], "Dividendos")
    if div_totales.get("cdi_total"):
        fila("0588", "Deducción doble imposición internacional (CDI) — campo \"Impuesto satisfecho en el extranjero\" del segundo popup",
             div_totales["cdi_total"], "Dividendos", bold=True)

    # Opciones
    if opt_totales.get("pl_total"):
        fila("1624-1654", "Otros elementos patrimoniales — primas opciones cerradas "
             "(tipo/clave en casilla 1626)",
             opt_totales["pl_total"], "Opciones", bold=True)

    # Futuros IBKR (Asset Category='Futures') — casilla 1626 clave 4.
    # Doctrina: Manual práctico AEAT cap. 11 §14. P&L viene del Realized
    # P/L que IBKR consolida por contrato (incluye multiplier y FX).
    # Imputación al ejercicio del cierre (cross-año automático).
    # La referencia apunta a la celda total de la hoja Futuros para que
    # el usuario pueda editar valores individuales si lo necesita.
    if fut_pl_ref is not None:
        fila("1624-1654",
             "Otros elementos patrimoniales — futuros financieros IBKR "
             "(P&L neto Realized P/L; casilla 1626 clave 4 · Manual AEAT cap 11 §14)",
             fut_pl_ref, "Futuros", bold=True)

    # FX P&L (IBKR) — informativo, casilla 1624-1654
    if fx_total_ref is not None:
        fila("1624-1654", "Otros elementos patrimoniales — G/P de divisa (Art. 33 LIRPF; "
             "tolerancia práctica AEAT para diferencias <~1.000 EUR)",
             fx_total_ref, "Forex", informativo=True)

    # Treasury Bills (IBKR) — Letras del Tesoro, casilla 0030 RCM
    # (Art. 25.2 LIRPF, Renta 2025).
    if tbills_total_ref is not None:
        fila("0030", "Rendimientos del capital mobiliario — Letras del Tesoro "
             "(transmisión/amortización)",
             tbills_total_ref, "Treasury_Bills", bold=True)

    # Intereses IBKR (Credit + Bond Interest) — DECLARABLE: RCM, casilla 0027.
    # Debit Interest se queda fuera (informativo, no deducible automaticamente).
    if interest_credit_ref is not None:
        fila("0027", "Rendimientos del capital mobiliario — intereses IBKR "
             "(Credit/Bond, sumar a otros intereses)",
             interest_credit_ref, "Intereses", bold=True)

    # Retención IRPF española sobre intereses (TR Sucursal ES, cuenta
    # remunerada post-migración) → campo "Retenciones" del popup individual de
    # la casilla 0027. 100% acreditable (mismo trato que la retención de 0029).
    if interest_ret_es_ref is not None:
        fila("0027 (popup)", "Retención IRPF de intereses — campo \"Retenciones\" del popup "
             "individual de 0027 (TR Sucursal ES, cuenta remunerada)",
             interest_ret_es_ref, "Intereses")

    # Staking de criptomonedas (Trade Republic) — RCM Art. 25.2 LIRPF, DGT
    # V1766-22 (26-7-2022). Casilla 0027 por analogía con intereses; alternativa
    # doctrinal 0031 (la consulta NO fija casilla, ver hoja Staking). Valoración
    # en EUR al precio de mercado de cada recepción (Art. 43.1 LIRPF, en especie).
    if staking_total_ref is not None:
        fila("0027", "Staking de criptomonedas — RCM por cesión a terceros (Art. 25.2 LIRPF, "
             "DGT V1766-22). Sumar a otros intereses. Alternativa doctrinal: casilla 0031 "
             "(ver hoja Staking; cuota idéntica en base ahorro)",
             staking_total_ref, "Staking", bold=True)

    # Gastos del broker (Art. 26.1.a LIRPF) — DEDUCIBLES del RCM neto.
    # NO existe casilla independiente para esto. Se introducen DENTRO del
    # popup individual de cualquier rendimiento del capital mobiliario
    # (típicamente el de dividendos 0029) en el campo "Gastos de
    # administración y depósito". RentaWEB los totaliza automáticamente
    # en la casilla 0037 del bloque B y los resta del rendimiento bruto
    # sin permitir saldo negativo.
    if plataforma_total_ref is not None:
        fila("0029 (popup) → 0037",
             "Gastos de administración y depósito — campo \"Gastos de administración y "
             "depósito\" del popup individual (típicamente al editar 0029). "
             "RentaWEB totaliza en 0037. Art. 26.1.a LIRPF.",
             plataforma_total_ref, "Gastos_plataforma", bold=True)

    # Pérdidas arrastradas
    if perd_totales.get("aplicable_total"):
        fila("1186+", "Saldos negativos de ejercicios anteriores aplicables este año "
             "(introducir manualmente en RentaWEB)",
             perd_totales["aplicable_total"], "Perdidas_arrastradas", bold=True)

    # Nota final
    next_row += 1
    ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=4)
    c = ws.cell(row=next_row, column=1,
                value="📌 Si cambias un coste en G_P_por_valor o un dividendo en "
                      "Dividendos, vuelve a abrir Excel para forzar el recálculo "
                      "(F9). Los importes de esta hoja se actualizan automáticamente.")
    c.font = FONT_MUTED
    c.alignment = ALIGN_WRAP
    c.fill = FILL_DISCLAIMER
    c.border = Border(left=Side("thin", color=C_AMARILLO_2),
                      right=Side("thin", color=C_AMARILLO_2),
                      top=Side("thin", color=C_AMARILLO_2),
                      bottom=Side("thin", color=C_AMARILLO_2))
    ws.row_dimensions[next_row].height = 38

    ws.column_dimensions['A'].width = widths[0]
    ws.column_dimensions['B'].width = widths[1]
    ws.column_dimensions['C'].width = widths[2]
    ws.column_dimensions['D'].width = widths[3]

    return ws
