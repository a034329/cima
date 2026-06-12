"""Filtro fiscal de rotación (umbrales R-U del modelo WG).

Pregunta que responde: si vendo una posición con plusvalía latente G para
rotar a otra empresa, ¿qué rentabilidad anual (CAGR4+Div) tiene que ofrecer el
destino para que la rotación compense, dado que aflorar G cuesta impuestos?

Modelo de break-even (horizonte N años):

    mantener:  V · (1 + r_origen)^N           (capital íntegro, impuesto diferido)
    rotar:     (V − t) · (1 + r_destino)^N     (capital tras pagar t de impuesto)

La rotación compensa en N años cuando rotar ≥ mantener, es decir:

    r_destino ≥ (1 + r_origen) · ( V / (V − t) )^(1/N) − 1   ← umbral del origen

donde:
  · V = valor de mercado actual de la posición (EUR)
  · G = plusvalía latente (EUR); t = coste fiscal de aflorarla
  · r_origen = CAGR4+Div esperado de la posición (de Estimaciones)

El **efecto fiscal es incremental por tramos y CON SIGNO**: la variación de
cuota al añadir G sobre la base ya acumulada del ejercicio
(`cuota(base + G) − cuota(base)`), no un tipo plano. Las primeras plusvalías
tributan al 19% y las que caen en tramos altos al 23-28%.

  · G > 0 (ganancia): efecto > 0 = COSTE. Capital reinvertible = V − coste →
    multiplicador V/(V−coste) > 1 → umbral POR ENCIMA de r_origen, decreciendo
    hacia r al alargar el horizonte (el coste se amortiza en más años).
  · G < 0 (pérdida): efecto < 0 = CRÉDITO fiscal (la pérdida compensa otras
    ganancias / arrastra 4 años). Capital reinvertible = V + crédito →
    multiplicador < 1 → umbral POR DEBAJO de r_origen, subiendo hacia r con N.
    Vender en pérdida ADELANTA un ahorro fiscal, así que rotar exige menos
    retorno al destino. (Es el comportamiento de las columnas R-U del Excel.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.services.estimaciones import calcular_estimaciones
from app.services.fiscal import calcular_fiscal
from app.services.fiscal_optimizador import calcular_optimizador


# Escala de la base del ahorro, Art. 66 LIRPF (vigente desde 2023). Tuplas
# (límite SUPERIOR acumulado del tramo, tipo). El último tramo es abierto.
ESCALA_AHORRO: list[tuple[Decimal | None, Decimal]] = [
    (Decimal("6000"),   Decimal("0.19")),
    (Decimal("50000"),  Decimal("0.21")),
    (Decimal("200000"), Decimal("0.23")),
    (Decimal("300000"), Decimal("0.27")),
    (None,              Decimal("0.28")),
]

_CENT = Decimal("0.01")
_PCT = Decimal("0.0001")


def cuota_ahorro(base: Decimal) -> Decimal:
    """Cuota íntegra de la base del ahorro aplicando la escala progresiva."""
    base = base if base > 0 else Decimal("0")
    cuota = Decimal("0")
    prev = Decimal("0")
    for limite, tipo in ESCALA_AHORRO:
        tope = base if limite is None else min(base, limite)
        tramo = tope - prev
        if tramo > 0:
            cuota += tramo * tipo
        prev = tope
        if limite is None or base <= limite:
            break
    return cuota.quantize(_CENT, ROUND_HALF_UP)


def efecto_fiscal_incremental(base_actual: Decimal, gp: Decimal) -> Decimal:
    """Variación marginal de la cuota de la base del ahorro al aflorar `gp`
    (con SU SIGNO), por tramos:
      · gp > 0 (ganancia) → COSTE positivo: el impuesto a pagar al vender.
      · gp < 0 (pérdida)  → CRÉDITO (negativo): la pérdida baja la cuota
        (compensa otras ganancias del año o arrastra 4 años) → libera capital
        para reinvertir, así que rotar exige MENOS retorno al destino.
      · gp = 0 → 0.
    Simétrico: el mismo `cuota(base+gp) − cuota(base)` sirve para ambos signos."""
    return cuota_ahorro(base_actual + gp) - cuota_ahorro(base_actual)


@dataclass
class RotacionItem:
    isin: str
    nombre: str
    valor_eur: Decimal                  # V
    gp_latente_eur: Decimal             # G
    coste_fiscal_eur: Decimal           # t
    tipo_efectivo_pct: Decimal | None   # t / G (carga fiscal efectiva de la venta)
    cagr4_div_origen_pct: Decimal | None  # r_origen
    umbral_1y_pct: Decimal | None
    umbral_2y_pct: Decimal | None
    umbral_3y_pct: Decimal | None
    umbral_4y_pct: Decimal | None
    # Años de IF que retrasa (+) o adelanta (−) pagar el efecto fiscal HOY,
    # con la misma proyección del dashboard. None si no converge (V2).
    delta_anios_if: Decimal | None = None


@dataclass
class RotacionResultado:
    ejercicio: int
    fecha_calculo: date
    base_ahorro_actual_eur: Decimal     # base del ahorro YTD sobre la que se aplica el marginal
    buffer_perdidas_eur: Decimal = Decimal("0")  # pérdidas pendientes que absorben la plusvalía (compartido)
    items: list[RotacionItem] = field(default_factory=list)
    sin_estimacion: list[str] = field(default_factory=list)


def _umbral(r_origen: Decimal, multiplicador: float, n: int) -> Decimal:
    """(1 + r_origen) · multiplicador^(1/N) − 1, en %."""
    factor = (1.0 + float(r_origen)) * (multiplicador ** (1.0 / n)) - 1.0
    return Decimal(str(factor)).quantize(_PCT, ROUND_HALF_UP)


def calcular_rotacion(
    db: Session,
    cartera_id: str,
    ejercicio: int | None = None,
    precios: dict[str, Decimal] | None = None,
) -> RotacionResultado:
    if ejercicio is None:
        ejercicio = date.today().year

    opt = calcular_optimizador(db, cartera_id, ejercicio, precios=precios)
    f = calcular_fiscal(db, cartera_id, ejercicio)
    comp = f.resultado_compensacion
    base_actual = (
        Decimal(str(comp.base_ahorro_gp)) + Decimal(str(comp.base_ahorro_rcm))
    )
    # Buffer de pérdidas que absorbe la PRÓXIMA plusvalía realizada (sin coste
    # fiscal hasta ese importe): pendientes de años anteriores + arrastre del año.
    # Es COMPARTIDO entre posiciones; aquí se aplica por-posición como "¿y si
    # vendo SOLO esta?" — se expone para señalar el límite común.
    # OJO (auditoría Cima 2026-06-11, A6): `perdidas_actualizadas` YA incluye
    # la pérdida nueva de ESTE año (origen == ejercicio); sumarle además
    # `nuevo_saldo_negativo` la contaba DOS veces y presentaba como "coste
    # fiscal ~0" rotaciones que tributan. Mismo filtro que el optimizador.
    buffer = (
        sum((Decimal(str(p.pendiente_eur)) for p in comp.perdidas_actualizadas
             if p.ejercicio_origen < ejercicio), Decimal("0"))
        + Decimal(str(abs(comp.nuevo_saldo_negativo)))
    )

    est = {e.isin: e for e in calcular_estimaciones(db, cartera_id)}

    # Parámetros de la proyección IF, una vez para todo el bucle (V2: el coste
    # fiscal de cada rotación se traduce a años de retraso de la IF).
    from app.services.impacto_if import delta_anios_if, parametros_proyeccion_if
    try:
        params_if = parametros_proyeccion_if(db, cartera_id)
    except Exception:
        params_if = None

    items: list[RotacionItem] = []
    sin_est: list[str] = []
    for lat in opt.latentes:
        if lat.sin_precio or lat.valor_actual_eur is None or lat.gp_latente_eur is None:
            continue
        v = Decimal(str(lat.valor_actual_eur))
        g = Decimal(str(lat.gp_latente_eur))
        # Efecto fiscal CON SIGNO: ganancia → coste (+, resta capital); pérdida
        # → crédito (−, suma capital reinvertible). Una ganancia se absorbe primero
        # con el buffer de pérdidas pendientes (tributa solo el exceso) → si el
        # buffer la cubre, el coste fiscal es 0 y el switching cost desaparece.
        g_imponible = max(Decimal("0"), g - buffer) if g > 0 else g
        efecto = efecto_fiscal_incremental(base_actual, g_imponible)

        e = est.get(lat.isin)
        r_o = (
            Decimal(str(e.cagr4_div_pct))
            if e is not None and e.cagr4_div_pct is not None
            else None
        )
        if r_o is None:
            sin_est.append(lat.isin)

        # Capital reinvertible tras el efecto fiscal: V − efecto. Ganancia →
        # V − coste (mult > 1 → umbral por encima de r). Pérdida → V + crédito
        # (mult < 1 → umbral por debajo de r, subiendo hacia r con N). Si el
        # coste ≥ V (extremo patológico) no hay umbral significativo.
        reinvertible = v - efecto
        mult = float(v / reinvertible) if reinvertible > 0 else None
        umbrales: list[Decimal | None] = [None, None, None, None]
        if r_o is not None and mult is not None:
            umbrales = [_umbral(r_o, mult, n) for n in (1, 2, 3, 4)]

        tipo_efectivo = (
            (efecto / g).quantize(_PCT, ROUND_HALF_UP) if g != 0 else None
        )

        items.append(RotacionItem(
            isin=lat.isin, nombre=lat.nombre,
            valor_eur=v.quantize(_CENT, ROUND_HALF_UP),
            gp_latente_eur=g.quantize(_CENT, ROUND_HALF_UP),
            coste_fiscal_eur=efecto.quantize(_CENT, ROUND_HALF_UP),
            tipo_efectivo_pct=tipo_efectivo,
            cagr4_div_origen_pct=(
                r_o.quantize(_PCT, ROUND_HALF_UP) if r_o is not None else None
            ),
            umbral_1y_pct=umbrales[0], umbral_2y_pct=umbrales[1],
            umbral_3y_pct=umbrales[2], umbral_4y_pct=umbrales[3],
            delta_anios_if=(
                delta_anios_if(db, cartera_id, -efecto, params=params_if)
                if params_if is not None and efecto != 0 else
                (Decimal("0.0") if params_if is not None else None)
            ),
        ))

    # Orden: mayor ancla fiscal primero (umbral 4Y desc), los sin umbral al final.
    items.sort(key=lambda x: (x.umbral_4y_pct is None,
                              -(x.umbral_4y_pct or Decimal("0"))))
    return RotacionResultado(
        ejercicio=ejercicio,
        fecha_calculo=date.today(),
        base_ahorro_actual_eur=base_actual.quantize(_CENT, ROUND_HALF_UP),
        buffer_perdidas_eur=buffer.quantize(_CENT, ROUND_HALF_UP),
        items=items,
        sin_estimacion=sin_est,
    )
