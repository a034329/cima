"""Endpoint de Forex — G/P de divisa (Art. 33.5.e LIRPF). Sólo IBKR."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services.fiscal_forex import calcular_forex


router = APIRouter(prefix="/forex", tags=["forex"])

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


class ForexLineaOut(BaseModel):
    divisa: str
    realized_eur: Decimal = Field(decimal_places=2)
    unrealized_eur: Decimal = Field(decimal_places=2)


class ForexResumen(BaseModel):
    ejercicio: int
    fecha_calculo: date
    realized_total: Decimal = Field(decimal_places=2)      # declarable
    unrealized_total: Decimal = Field(decimal_places=2)    # latente, informativo
    periodo_inicio: date | None
    periodo_fin: date | None
    lineas: list[ForexLineaOut]


def _q2(x: Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _serializar(r) -> ForexResumen:  # type: ignore[no-untyped-def]
    return ForexResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        realized_total=_q2(r.realized_total),
        unrealized_total=_q2(r.unrealized_total),
        periodo_inicio=r.periodo_inicio,
        periodo_fin=r.periodo_fin,
        lineas=[
            ForexLineaOut(divisa=l.divisa, realized_eur=_q2(l.realized_eur),
                          unrealized_eur=_q2(l.unrealized_eur))
            for l in r.lineas
        ],
    )


@router.get("/acumulado", response_model=ForexResumen,
            summary="Forex acumulado (todos los años)")
def get_forex_acumulado(db: Session = Depends(get_db),
                        cartera: models.Cartera = Depends(get_current_cartera)) -> ForexResumen:
    return _serializar(calcular_forex(db, cartera.id, None))


@router.get("/{ejercicio}", response_model=ForexResumen,
            summary="G/P de divisa del ejercicio (realized declarable Art. 33.5.e)")
def get_forex(ejercicio: int, db: Session = Depends(get_db),
              cartera: models.Cartera = Depends(get_current_cartera)) -> ForexResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar(calcular_forex(db, cartera.id, ejercicio))
