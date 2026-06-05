"""
Generador de PDF de resumen de cartera — posiciones abiertas, PM real y rendimiento.
"""
import base64
import csv as csv_mod
import io
import os
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "irpf"))


# ── Métricas anuales y gráfica ───────────────────────────────────────────────

def _compute_annual_metrics(csv_paths: list[str], fifo_results) -> list[dict]:
    """Computa métricas anuales desde los ficheros de cartera (CSV o XLSX)
    y el motor FIFO.

    Retorna lista ordenada por año:
        [{year, compras, ventas, aportacion_neta, capital_acum, gp_realizada}]
    """
    # Reutilizamos el parser del motor: acepta CSV legacy y XLSX maestro,
    # y normaliza códigos AEAT (AD/AL/VD/TR) a internos (A/T/SP) con flags
    # es_scrip/es_derecho. Nos da la fecha como `date` real.
    from motor_fiscal import parse_csv_irpf

    annual = defaultdict(lambda: {"compras": Decimal("0"), "ventas": Decimal("0")})

    for path in csv_paths:
        if not os.path.exists(path):
            continue
        try:
            ops = parse_csv_irpf(path)
        except Exception:
            continue
        for op in ops:
            fecha = op.get("fecha")
            if not fecha:
                continue
            year = str(fecha.year)
            tipo = op.get("tipo", "")
            importe = op.get("importe_eur", Decimal("0"))
            gastos = op.get("gastos_eur", Decimal("0"))
            if tipo == "A":
                annual[year]["compras"] += importe + gastos
            elif tipo == "T":
                annual[year]["ventas"] += importe - gastos

    gp_by_year = fifo_results.total_gp_por_ejercicio() if fifo_results else {}

    metrics = []
    capital_acum = Decimal("0")
    for year in sorted(annual.keys()):
        d = annual[year]
        aportacion = d["compras"] - d["ventas"]
        capital_acum += aportacion
        gp = gp_by_year.get(int(year), Decimal("0"))
        metrics.append({
            "year": year,
            "compras": d["compras"],
            "ventas": d["ventas"],
            "aportacion_neta": aportacion,
            "capital_acum": capital_acum,
            "gp_realizada": gp,
        })

    return metrics


def _generate_chart(metrics: list[dict]) -> str:
    """Genera gráfica de evolución del capital con matplotlib.

    Retorna string base64 del PNG (para embeber en HTML como data URI).
    """
    if not metrics or len(metrics) < 2:
        return ""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
    except ImportError:
        return ""

    years = [m["year"] for m in metrics]
    aportaciones = [float(m["aportacion_neta"]) for m in metrics]
    capital = [float(m["capital_acum"]) for m in metrics]
    gp = [float(m["gp_realizada"]) for m in metrics]

    fig, ax1 = plt.subplots(figsize=(10, 4.5), dpi=150)
    fig.patch.set_facecolor("#fafbfc")
    ax1.set_facecolor("#fafbfc")

    x = range(len(years))
    bar_width = 0.35

    # Barras: aportaciones netas (azul) y G/P realizada (verde/rojo)
    colors_aport = ["#3b82f6" if v >= 0 else "#ef4444" for v in aportaciones]
    colors_gp = ["#10b981" if v >= 0 else "#ef4444" for v in gp]

    bars1 = ax1.bar([i - bar_width / 2 for i in x], aportaciones, bar_width,
                    color=colors_aport, alpha=0.8, label="Aportación neta", zorder=2)
    bars2 = ax1.bar([i + bar_width / 2 for i in x], gp, bar_width,
                    color=colors_gp, alpha=0.7, label="G/P realizada", zorder=2)

    ax1.set_ylabel("EUR", fontsize=9, color="#4a5568")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(years, fontsize=8)
    ax1.tick_params(axis="y", labelsize=8)
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.axhline(y=0, color="#cbd5e0", linewidth=0.5, zorder=1)
    ax1.grid(axis="y", alpha=0.3, zorder=0)

    # Línea: capital acumulado (eje derecho)
    ax2 = ax1.twinx()
    ax2.plot(list(x), capital, color="#1e40af", linewidth=2.5, marker="o",
             markersize=5, label="Capital acumulado", zorder=3)
    ax2.set_ylabel("Capital acumulado (EUR)", fontsize=9, color="#1e40af")
    ax2.tick_params(axis="y", labelsize=8, labelcolor="#1e40af")
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))

    # Leyenda combinada
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left",
               fontsize=7, framealpha=0.9)

    plt.title("Evolución del Capital Invertido", fontsize=12, fontweight="bold",
              color="#1a365d", pad=12)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


def format_eur(d) -> str:
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


def generate_portfolio_pdf(
    fifo_results,
    ejercicio: int,
    output_path: str,
    dividendos_bruto: Decimal | None = None,
    dividendos_by_isin: dict | None = None,
    primas_by_isin: dict | None = None,
    csv_paths: list[str] | None = None,
    multi_anio: bool = False,
    anios_analizados: str = "",
) -> str:
    """Genera el PDF de resumen de cartera enriquecido.

    Args:
        fifo_results: FIFOResults del motor_fiscal.
        ejercicio: Año fiscal.
        output_path: Ruta del PDF de salida.
        dividendos_bruto: Total de dividendos brutos (opcional).
        dividendos_by_isin: Dividendo bruto por ISIN del año target.
        primas_by_isin: Primas netas por subyacente del año target.
        multi_anio: Si es análisis multi-año.
        anios_analizados: String descriptivo.

    Returns:
        Ruta del PDF generado.
    """
    import weasyprint

    div_by_isin = dividendos_by_isin or {}
    prim_by_isin = primas_by_isin or {}

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=False)
    env.globals["format_eur"] = format_eur
    template = env.get_template("portfolio_resumen.html")

    positions = sorted(fifo_results.positions, key=lambda p: p.nombre)
    total_coste = sum(p.coste_total_eur for p in positions)
    positions_scrip = [p for p in positions if p.es_mixta]

    # G/P realizadas por ISIN (del año target)
    year_matches = [m for m in fifo_results.matches if m.ejercicio_fiscal == ejercicio]
    gp_by_isin_dict = defaultdict(lambda: {"ganancia": Decimal("0"), "perdida": Decimal("0")})
    nombres = {}
    for m in year_matches:
        if m.ganancia_perdida >= 0:
            gp_by_isin_dict[m.isin]["ganancia"] += m.ganancia_perdida
        else:
            gp_by_isin_dict[m.isin]["perdida"] += m.ganancia_perdida
        nombres[m.isin] = m.nombre

    gp_by_isin = []
    for isin in sorted(gp_by_isin_dict.keys(), key=lambda i: nombres.get(i, "")):
        d = gp_by_isin_dict[isin]
        gp_by_isin.append({
            "isin": isin,
            "nombre": nombres[isin],
            "ganancia": d["ganancia"],
            "perdida": d["perdida"],
            "neto": d["ganancia"] + d["perdida"],
        })

    total_ganancias = sum(d["ganancia"] for d in gp_by_isin)
    total_perdidas = sum(d["perdida"] for d in gp_by_isin)
    total_gp_realizada = total_ganancias + total_perdidas if year_matches else None

    # Enrich positions with dividends, primas, and returns
    enriched_positions = []
    total_div_pos = Decimal("0")
    total_primas_pos = Decimal("0")

    for p in positions:
        div_eur = div_by_isin.get(p.isin, Decimal("0"))
        prima_eur = prim_by_isin.get(p.isin, Decimal("0"))
        # G/P realizada para este ISIN
        gp_isin = gp_by_isin_dict.get(p.isin, {})
        gp_neto = gp_isin.get("ganancia", Decimal("0")) + gp_isin.get("perdida", Decimal("0"))

        # Rentabilidad acumulada = (div + G/P) / coste
        if p.coste_total_eur > 0:
            rent_acum = ((div_eur + gp_neto) / p.coste_total_eur * 100).quantize(
                Decimal("0.1")
            )
            rent_acum_primas = ((div_eur + prima_eur + gp_neto) / p.coste_total_eur * 100).quantize(
                Decimal("0.1")
            )
        else:
            rent_acum = Decimal("0")
            rent_acum_primas = Decimal("0")

        enriched_positions.append({
            "pos": p,
            "div_eur": div_eur,
            "prima_eur": prima_eur,
            "rent_acum": rent_acum,
            "rent_acum_primas": rent_acum_primas,
        })
        total_div_pos += div_eur
        total_primas_pos += prima_eur

    # Total returns
    total_rent_acum = None
    total_rent_acum_primas = None
    if total_coste > 0:
        total_gp = total_gp_realizada or Decimal("0")
        total_rent_acum = ((total_div_pos + total_gp) / total_coste * 100).quantize(Decimal("0.1"))
        total_rent_acum_primas = ((total_div_pos + total_primas_pos + total_gp) / total_coste * 100).quantize(Decimal("0.1"))

    # Annual metrics and chart
    annual_metrics = []
    chart_base64 = ""
    if csv_paths:
        annual_metrics = _compute_annual_metrics(csv_paths, fifo_results)
        chart_base64 = _generate_chart(annual_metrics)

    context = {
        "ejercicio": ejercicio,
        "fecha_generacion": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "multi_anio": multi_anio,
        "anios_analizados": anios_analizados,
        "positions": positions,
        "enriched_positions": enriched_positions,
        "total_coste": total_coste,
        "total_div_pos": total_div_pos,
        "total_primas_pos": total_primas_pos,
        "total_rent_acum": total_rent_acum,
        "total_rent_acum_primas": total_rent_acum_primas,
        "positions_scrip": positions_scrip,
        "gp_by_isin": gp_by_isin if year_matches else [],
        "total_ganancias": total_ganancias,
        "total_perdidas": total_perdidas,
        "total_gp_realizada": total_gp_realizada,
        "total_dividendos": dividendos_bruto,
        "annual_metrics": annual_metrics,
        "chart_base64": chart_base64,
        "format_eur": format_eur,
    }

    html_content = template.render(**context)
    doc = weasyprint.HTML(string=html_content)
    doc.write_pdf(output_path)
    return output_path
