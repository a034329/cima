"""Métricas enriquecidas por posición para la tabla de Posiciones.

Columnas (todas opcionales salvo PM real):
  - pm_real                : (coste adq. + comisiones + tasas) / nº acciones.
  - pm_fiscal_es           : PM real ajustado por primas de opciones EJERCIDAS
                             sobre ese subyacente (cobradas reducen coste,
                             pagadas lo aumentan). DGT V2172-21.
  - opciones_ejercidas_anio: prima neta (cobradas - pagadas) de opciones
                             ejercidas sobre la posición en el año en curso.
  - opciones_ejercidas_hist: ídem, histórico acumulado.
  - dividendos_anio        : dividendos brutos cobrados (ISIN) en año en curso.
  - dividendos_hist        : dividendos brutos históricos.
  - pm_desc                : PM descontando dividendos + primas ejercidas netas.
  - importe_diferido_2m    : pérdida latente bloqueada por regla 2M (Art.33.5.f).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import estado_posicion
from app.services.fiscal import calcular_fiscal


@dataclass
class PosicionMetricas:
    isin: str
    nombre: str
    cantidad: Decimal
    pm_real: Decimal
    precio_actual_eur: Decimal
    gp_no_realizada_eur: Decimal
    gp_no_realizada_pct: Decimal
    rentab_total_pct: Decimal     # incl. dividendos + opciones cobradas (holding actual)
    pm_fiscal_es: Decimal
    opciones_ejercidas_anio: Decimal
    opciones_ejercidas_hist: Decimal
    dividendos_anio: Decimal
    dividendos_hist: Decimal
    pm_desc: Decimal
    importe_diferido_2m: Decimal
    gp_realizada_anio: Decimal
    decision: str                  # decisión vigente del plan (default MANTENER)
    tipo_activo: str = "STOCK"     # STOCK / ETF / CRYPTO (para agrupar en la UI)
    # Umbral de rotación (R-U del modelo WG): CAGR4+Div que el destino debe batir
    # para que rotar compense el coste fiscal de aflorar la plusvalía. None si no
    # hay plusvalía+estimación o el cálculo no está disponible.
    umbral_rotacion_1y_pct: Decimal | None = None
    umbral_rotacion_2y_pct: Decimal | None = None
    umbral_rotacion_3y_pct: Decimal | None = None
    umbral_rotacion_4y_pct: Decimal | None = None
    # CAGR4+Div proyectado a 4 años (retorno total esperado de Estimaciones).
    cagr4_div_pct: Decimal | None = None
    # Rentab. histórica del ISIN: + G/P realizada en cierres anteriores, sobre Σ
    # todas las compras. Para posiciones nunca cerradas = rentab_total_pct.
    rentab_total_hist_pct: Decimal = Decimal("0")
    # Primas NETAS de TODAS las opciones (venta cobrada − compra pagada), no solo
    # ejercidas — refleja lo que ganas/pierdes haciendo wheel/income sobre el ISIN.
    primas_opc_anio: Decimal = Decimal("0")
    primas_opc_hist: Decimal = Decimal("0")
    # Precio de mercado en DIVISA LOCAL (la del broker), como referencia.
    precio_actual_local: Decimal | None = None
    divisa_cotizacion: str | None = None


def _tipo_activo(isin: str | None, nombre: str | None) -> str:
    """STOCK / ETF / CRYPTO. Cripto sintética (Trade Republic) usa ISIN 'XF...'
    o 'CRYPTO...' que classify_isin no reconoce → heurístico de prefijo primero;
    el resto vía classify_isin de Cuádrate (whitelist ETF + señales)."""
    iu = (isin or "").upper()
    if iu.startswith("XF") or iu.startswith("CRYPTO"):
        return "CRYPTO"
    try:
        from app.adapters.cuadrate import _ensure_cuadrate_importable
        _ensure_cuadrate_importable()
        from instrument_classifier import classify_isin
        return classify_isin(isin or "", nombre or "", "")[0]
    except Exception:
        return "STOCK"


def _coste_medio_ponderado(txs: list[models.Transaccion]) -> Decimal:
    """Coste de adquisición del holding actual por MEDIA PONDERADA MÓVIL (como
    los brokers y el Excel), incluyendo gastos y comisiones de compra. En cada
    compra recalcula el coste medio; en cada venta reduce el coste al medio
    vigente sin alterarlo. Difiere del coste FIFO cuando hay ventas parciales.
    NO incorpora primas de opciones ni dividendos (eso lo aplican pm_fiscal_es y
    pm_desc sobre esta misma base). `txs` ordenado por fecha ascendente."""
    coste = Decimal("0")
    acciones = Decimal("0")
    for t in txs:
        q = Decimal(str(t.cantidad))
        if t.tipo == "BUY":
            coste += (Decimal(str(t.importe_eur)) + Decimal(str(t.gastos_eur))
                      + Decimal(str(t.tasas_externas_eur)))
            acciones += q
        elif t.tipo == "SELL" and acciones > 0:
            vendidas = min(q, acciones)
            coste -= (coste / acciones) * vendidas
            acciones -= vendidas
            if acciones <= 0:
                acciones = Decimal("0")
                coste = Decimal("0")
    return coste if coste > 0 else Decimal("0")


def calcular_metricas_posiciones(
    db: Session, cartera_id: str, anio: int | None = None
) -> list[PosicionMetricas]:
    """Calcula las métricas por posición abierta. `anio` = año en curso para
    las columnas "año en curso" (default: hoy)."""
    anio = anio or date.today().year

    posiciones = list(db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars())

    # ── Dividendos por ISIN (año y total) ──────────────────────────────
    div_anio: dict[str, Decimal] = {}
    div_hist: dict[str, Decimal] = {}
    divs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).scalars()
    for d in divs:
        isin = d.posicion.isin
        bruto = Decimal(str(d.importe_eur))
        div_hist[isin] = div_hist.get(isin, Decimal("0")) + bruto
        if d.fecha.year == anio:
            div_anio[isin] = div_anio.get(isin, Decimal("0")) + bruto

    # ── Transacciones BUY/SELL por posición (para coste medio ponderado) ─
    tx_por_pos: dict[str, list[models.Transaccion]] = {}
    for t in db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo.in_(["BUY", "SELL"]))
        .order_by(models.Transaccion.fecha, models.Transaccion.id)
    ).scalars():
        tx_por_pos.setdefault(t.posicion_id, []).append(t)

    # ── Primas de opciones EJERCIDAS por subyacente_isin ───────────────
    # neta = cobradas (venta) - pagadas (compra). Para opciones que vienen del
    # importer sin `subyacente_isin` poblado, intenta un FALLBACK por ticker:
    # busca en la caché FIGI un ISIN cuyo ticker coincida con `subyacente` y, si
    # ese ISIN está en cartera, lo asocia. Si hay ambigüedad o no se encuentra,
    # la opción se ignora (mejor omitir que mapearla a la posición incorrecta).
    isines_cartera = {p.isin for p in posiciones}
    ticker_a_isin: dict[str, str] = {}
    try:
        from app.services.precios import _leer_cache       # type: ignore[attr-defined]
        cache = _leer_cache()
        candidatos: dict[str, list[str]] = {}
        for k, v in cache.items():
            if not k.startswith("figi:"):
                continue
            isin_k = k[5:]
            if isin_k not in isines_cartera:
                continue
            tkr = (v or {}).get("ticker")
            if not tkr:
                continue
            candidatos.setdefault(tkr.upper(), []).append(isin_k)
        # Solo mapea cuando el ticker apunta a UN único ISIN en cartera.
        ticker_a_isin = {t: ls[0] for t, ls in candidatos.items() if len(ls) == 1}
    except Exception:
        ticker_a_isin = {}

    opt_anio: dict[str, Decimal] = {}
    opt_hist: dict[str, Decimal] = {}
    # Primas NETAS de TODAS las opciones (no solo ejercidas): ventas cobradas
    # menos compras pagadas. Es lo que has ganado/perdido vendiendo opciones
    # sobre el subyacente. Para PM fiscal sigue valiendo solo lo ejercido (arriba).
    primas_anio: dict[str, Decimal] = {}
    primas_hist: dict[str, Decimal] = {}
    opts_todas = list(db.execute(
        select(models.Opcion)
        .where(models.Opcion.cartera_id == cartera_id)
        .where(models.Opcion.estado == "confirmada")
    ).scalars())
    for o in opts_todas:
        sub = o.subyacente_isin
        if not sub and o.subyacente:
            sub = ticker_a_isin.get(o.subyacente.upper())   # fallback por ticker
        if not sub:
            continue
        prima = Decimal(str(o.importe_eur))
        signed = prima if o.accion == "venta" else -prima
        primas_hist[sub] = primas_hist.get(sub, Decimal("0")) + signed
        if o.fecha.year == anio:
            primas_anio[sub] = primas_anio.get(sub, Decimal("0")) + signed
        # Sub-conjunto solo ejercidas (semántica fiscal, alimenta pm_fiscal_es).
        if o.ejercida:
            opt_hist[sub] = opt_hist.get(sub, Decimal("0")) + signed
            if o.fecha.year == anio:
                opt_anio[sub] = opt_anio.get(sub, Decimal("0")) + signed

    # ── Pérdidas diferidas 2M por ISIN (acumulado) + G/P realizada histórica
    # por ISIN (acumulado: todas las matches FIFO de todos los años; entra en
    # la "Rentab. total %" para reflejar también lo ganado/perdido en cierres
    # anteriores, no solo el holding actual) + G/P realizada (año) ─────────
    diferido_2m: dict[str, Decimal] = {}
    gp_real_hist: dict[str, Decimal] = {}
    gp_real_anio: dict[str, Decimal] = {}
    try:
        fiscal = calcular_fiscal(db, cartera_id, None)  # acumulado
        for pd in fiscal.perdidas_diferidas_latentes:
            diferido_2m[pd.isin] = diferido_2m.get(pd.isin, Decimal("0")) + Decimal(
                str(pd.importe_eur)
            )
        for m in fiscal.matches:
            gp_real_hist[m.isin] = gp_real_hist.get(m.isin, Decimal("0")) + Decimal(
                str(m.ganancia_perdida)
            )
    except Exception:
        diferido_2m = {}
        gp_real_hist = {}
    try:
        fiscal_anio = calcular_fiscal(db, cartera_id, anio)
        for m in fiscal_anio.matches:
            gp_real_anio[m.isin] = gp_real_anio.get(m.isin, Decimal("0")) + Decimal(
                str(m.ganancia_perdida)
            )
    except Exception:
        gp_real_anio = {}

    # ── Decisión vigente del plan por ISIN (default MANTENER) ──────────
    from app.services.plan import DECISION_DEFECTO, decisiones_activas
    try:
        activas = decisiones_activas(db, cartera_id)
    except Exception:
        activas = {}

    # ── Precios actuales (cacheados) para G/P no realizada ─────────────
    from app.services.precios import obtener_precios_eur, precios_nativos
    try:
        precios, _ = obtener_precios_eur(db, cartera_id)
    except Exception:
        precios = {}
    # Precio en divisa local (la del broker), como referencia opcional.
    try:
        nativos = precios_nativos(db, cartera_id)   # {isin: (precio, divisa)}
    except Exception:
        nativos = {}

    # ── Umbrales de rotación (R-U de WG) por ISIN ──────────────────────
    # Reutiliza el feed/fiscal/estimaciones. Guarded: si falla (sin red, sin
    # estimaciones), la tabla se sirve igual sin esas columnas.
    rot: dict[str, object] = {}
    try:
        from app.services.fiscal_rotacion import calcular_rotacion
        rot = {it.isin: it for it in calcular_rotacion(db, cartera_id, anio).items}
    except Exception:
        rot = {}

    resultado: list[PosicionMetricas] = []
    for pos in posiciones:
        est = estado_posicion(db, pos.id)
        cantidad = est["cantidad"]
        if cantidad <= 0:
            continue   # posición cerrada
        # Coste por MEDIA PONDERADA MÓVIL (como brokers/Excel), no FIFO. El
        # coste FIFO de los lotes restantes solo importa para el motor fiscal
        # (Cuádrate), que calcula la ganancia real de cada venta aparte.
        coste_total = _coste_medio_ponderado(tx_por_pos.get(pos.id, []))
        pm_real = ((coste_total / cantidad).quantize(Decimal("0.0001"))
                   if cantidad > 0 else Decimal("0"))

        isin = pos.isin
        d_anio = div_anio.get(isin, Decimal("0"))
        d_hist = div_hist.get(isin, Decimal("0"))
        o_anio = opt_anio.get(isin, Decimal("0"))
        o_hist = opt_hist.get(isin, Decimal("0"))
        dif2m = diferido_2m.get(isin, Decimal("0"))

        # Precio actual + G/P no realizada (si no hay precio → usa PM, G/P 0).
        precio_actual = precios.get(isin)
        if precio_actual is None:
            precio_actual = Decimal(str(pm_real))
        else:
            precio_actual = Decimal(str(precio_actual))
        gp_raw = (precio_actual - Decimal(str(pm_real))) * cantidad
        gp_no_real_pct = (gp_raw / coste_total) if coste_total > 0 else Decimal("0")
        # Rentabilidad total del HOLDING ACTUAL = G/P latente + dividendos cobrados
        # (hist del ISIN) + primas de opciones ejercidas (hist del ISIN), sobre el
        # coste del holding actual. No incluye G/P realizadas de cierres anteriores
        # (eso lo hace `rentab_total_hist_pct`, ver más abajo).
        rentab_total_pct = (
            ((gp_raw + d_hist + o_hist) / coste_total) if coste_total > 0 else Decimal("0")
        )
        # Rentabilidad total HISTÓRICA del valor: TODO lo ganado/perdido con este
        # ISIN, incluyendo cierres anteriores. Numerador = lo de arriba + G/P
        # realizada histórica. Denominador = capital TOTAL desplegado (Σ compras).
        # Para posiciones nunca cerradas: idéntico a `rentab_total_pct`.
        total_invertido = Decimal("0")
        for t in tx_por_pos.get(pos.id, []):
            if t.tipo == "BUY":
                total_invertido += (Decimal(str(t.importe_eur))
                                    + Decimal(str(t.gastos_eur))
                                    + Decimal(str(t.tasas_externas_eur)))
        if total_invertido <= 0:
            total_invertido = coste_total                          # respaldo defensivo
        gp_real_hist_isin = gp_real_hist.get(isin, Decimal("0"))
        rentab_total_hist_pct = (
            ((gp_raw + d_hist + o_hist + gp_real_hist_isin) / total_invertido)
            if total_invertido > 0 else Decimal("0")
        )
        rentab_total_hist_pct = rentab_total_hist_pct.quantize(Decimal("0.0001"))
        precio_actual = precio_actual.quantize(Decimal("0.0001"))
        gp_no_real = gp_raw.quantize(Decimal("0.01"))
        gp_no_real_pct = gp_no_real_pct.quantize(Decimal("0.0001"))
        rentab_total_pct = rentab_total_pct.quantize(Decimal("0.0001"))

        # PM fiscal ES: coste ajustado por primas ejercidas históricas.
        # Prima neta cobrada (>0) reduce el coste; pagada (<0) lo aumenta.
        coste_fiscal = coste_total - o_hist
        pm_fiscal = (coste_fiscal / cantidad).quantize(Decimal("0.0001")) \
            if cantidad > 0 else Decimal("0")

        # PM descontando dividendos históricos + primas ejercidas netas.
        coste_desc = coste_total - d_hist - o_hist
        pm_desc = (coste_desc / cantidad).quantize(Decimal("0.0001")) \
            if cantidad > 0 else Decimal("0")

        resultado.append(PosicionMetricas(
            isin=isin,
            nombre=pos.nombre or isin,
            cantidad=cantidad,
            pm_real=pm_real,
            precio_actual_eur=precio_actual,
            gp_no_realizada_eur=gp_no_real,
            gp_no_realizada_pct=gp_no_real_pct,
            rentab_total_pct=rentab_total_pct,
            rentab_total_hist_pct=rentab_total_hist_pct,
            primas_opc_anio=primas_anio.get(isin, Decimal("0")).quantize(Decimal("0.01")),
            primas_opc_hist=primas_hist.get(isin, Decimal("0")).quantize(Decimal("0.01")),
            pm_fiscal_es=pm_fiscal,
            opciones_ejercidas_anio=o_anio,
            opciones_ejercidas_hist=o_hist,
            dividendos_anio=d_anio,
            dividendos_hist=d_hist,
            pm_desc=pm_desc,
            importe_diferido_2m=dif2m,
            gp_realizada_anio=gp_real_anio.get(isin, Decimal("0")),
            decision=(activas[isin].decision if isin in activas else DECISION_DEFECTO),
            tipo_activo=_tipo_activo(isin, pos.nombre),
            precio_actual_local=(
                Decimal(str(nativos[isin][0])).quantize(Decimal("0.0001"))
                if isin in nativos and nativos[isin][0] is not None else None
            ),
            divisa_cotizacion=(nativos[isin][1] if isin in nativos else None),
            umbral_rotacion_1y_pct=getattr(rot.get(isin), "umbral_1y_pct", None),
            umbral_rotacion_2y_pct=getattr(rot.get(isin), "umbral_2y_pct", None),
            umbral_rotacion_3y_pct=getattr(rot.get(isin), "umbral_3y_pct", None),
            umbral_rotacion_4y_pct=getattr(rot.get(isin), "umbral_4y_pct", None),
            cagr4_div_pct=getattr(rot.get(isin), "cagr4_div_origen_pct", None),
        ))

    resultado.sort(key=lambda p: -float(p.pm_real * p.cantidad))
    return resultado
