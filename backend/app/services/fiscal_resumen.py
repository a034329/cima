"""Resumen del ejercicio — el cuadro IRPF integrado ("la joya").

Une en UNA vista todo lo que va a la base imponible del ahorro:

  Base del ahorro — Ganancias/pérdidas patrimoniales:
    · Acciones/ETFs (FIFO, ya deducido el bloqueo 2M)
    · Forex realizado (Art. 33.5.e)
    · Opciones cerradas/expiradas (casilla 1626)
  Base del ahorro — Rendimientos del capital mobiliario (RCM):
    · Dividendos netos (bruto 0029 − retención ES)
    · Intereses RCM crédito/cupón (0023)
    · Letras del Tesoro

Todo pasa por UNA sola compensación (RCM↔patrimoniales 25% + bolsas 4 años)
reutilizando el motor de Cuádrate vía `calcular_fiscal(..., extras=...)`.

El interés de débito (no deducible) se reporta aparte, informativo.
La deducción CDI (casilla 0588) es deducción de cuota, fuera de la base.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.cuadrate import _ensure_cuadrate_importable
from app.services.fiscal import FiscalExtras, calcular_fiscal
from app.services.fiscal_bills import calcular_bills
from app.services.fiscal_dividendos import calcular_dividendos
from app.services.fiscal_forex import calcular_forex
from app.services.fiscal_intereses import calcular_intereses
from app.services.fiscal_opciones import calcular_opciones


@dataclass
class ResumenFiscalResultado:
    ejercicio: int                       # 0 = acumulado
    fecha_calculo: date

    # Componentes — base del ahorro · G/P patrimoniales (BRUTO, sin restar
    # afloradas; el FIFO se reparte en las casillas como Cuádrate)
    gp_acciones: Decimal                 # acciones/ETFs cotizados → 0326-0340
    gp_derechos: Decimal                 # derechos de suscripción → 0341-0355
    gp_estructurados: Decimal            # turbos/factor/derivados → 1624-1654
    perdidas_afloradas: Decimal          # pérdidas diferidas que afloran (positivo);
                                         # el usuario DEBE declararlas (Hacienda no
                                         # las aplica sola)
    gp_no_deducible_2m: Decimal          # pérdidas bloqueadas regla 2M (positivo)
    forex_realized: Decimal              # Art. 33.5.e
    opciones_pl: Decimal                 # casilla 1626

    # Componentes — base del ahorro · RCM
    dividendos_bruto: Decimal            # casilla 0029
    dividendos_ret_es: Decimal           # retención de pagadores ES
    intereses_rcm: Decimal               # casilla 0023 (crédito + cupón)
    letras_rcm: Decimal                  # T-Bills
    intereses_debit: Decimal             # informativo, NO deducible (negativo)

    # Compensación integrada (Cuádrate ResultadoCompensacion)
    compensacion: Any

    # Deducción de cuota
    cdi_recuperable: Decimal             # casilla 0588

    # Totales a tributar
    base_ahorro_gp: Decimal
    base_ahorro_rcm: Decimal
    base_ahorro_total: Decimal


def calcular_resumen(
    db: Session, cartera_id: str, ejercicio: int | None
) -> ResumenFiscalResultado:
    fx = calcular_forex(db, cartera_id, ejercicio)
    bl = calcular_bills(db, cartera_id, ejercicio)
    dv = calcular_dividendos(db, cartera_id, ejercicio)
    it = calcular_intereses(db, cartera_id, ejercicio)
    op = calcular_opciones(db, cartera_id, ejercicio)
    opciones_pl = Decimal(str(op.totales.get("pl_neto", 0)))

    # RCM completo y correcto: dividendos neto (− retención ES) + intereses RCM
    # (crédito/cupón) + letras. El interés de débito NO entra (no deducible).
    rcm_completo = (
        (Decimal(str(dv.bruto_total)) - Decimal(str(dv.ret_es_total)))
        + Decimal(str(it.rcm_total))
        + Decimal(str(bl.realized_total))
    )

    extras = FiscalExtras(
        gp_patrimonial_extra=Decimal(str(fx.realized_total)),
        opciones_pl=opciones_pl,
        rcm_neto_override=rcm_completo,
    )
    f = calcular_fiscal(db, cartera_id, ejercicio, extras)
    comp = f.resultado_compensacion

    base_gp = Decimal(str(comp.base_ahorro_gp))
    base_rcm = Decimal(str(comp.base_ahorro_rcm))

    # G/P de acciones BRUTO: deshacemos el neteo de afloradas que hace
    # `calcular_fiscal` (gp_bruto = Σ(g/p − aflorada)). Las afloradas se
    # muestran en su propia línea para que el usuario las declare.
    afloradas = Decimal(str(f.total_perdida_aflorada))

    # Repartir el FIFO en las casillas como Cuádrate: derechos de suscripción
    # (0341-0355, `is_rts`), derivados estructurados turbos/factor (1624-1654,
    # classify_isin → DERIVATIVE) y el resto acciones/ETFs (0326-0340).
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    gp_derechos = Decimal("0")
    gp_estructurados = Decimal("0")
    gp_acciones = Decimal("0")
    for m in f.matches:
        gp = Decimal(str(m.ganancia_perdida))
        nombre = m.nombre or ""
        isin = m.isin or ""
        if g.is_rts(nombre):
            gp_derechos += gp
        elif g.classify_isin(isin, nombre)[0] == "DERIVATIVE":
            gp_estructurados += gp
        else:
            gp_acciones += gp

    return ResumenFiscalResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        fecha_calculo=date.today(),
        gp_acciones=gp_acciones,                       # 0326-0340 (bruto)
        gp_derechos=gp_derechos,                       # 0341-0355
        gp_estructurados=gp_estructurados,             # 1624-1654
        perdidas_afloradas=afloradas,
        gp_no_deducible_2m=Decimal(str(f.gp_no_deducible_2m)),
        forex_realized=Decimal(str(fx.realized_total)),
        opciones_pl=opciones_pl,
        dividendos_bruto=Decimal(str(dv.bruto_total)),
        dividendos_ret_es=Decimal(str(dv.ret_es_total)),
        intereses_rcm=Decimal(str(it.rcm_total)),
        letras_rcm=Decimal(str(bl.realized_total)),
        intereses_debit=Decimal(str(it.debit_total)),
        compensacion=comp,
        cdi_recuperable=Decimal(str(dv.cdi_recuperable_total)),
        base_ahorro_gp=base_gp,
        base_ahorro_rcm=base_rcm,
        base_ahorro_total=base_gp + base_rcm,
    )
