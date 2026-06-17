"""Endpoint de cartera.

Lee posiciones + lots de BD. Si la BD está vacía, devuelve un resumen mock
de demostración para que el frontend no se rompa.

Cuando llegue auth: el `cartera_id` saldrá del JWT del usuario.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services.fifo import estado_posicion


# ── Modelos Pydantic (esquema final, no mock-specific) ─────────────────

class Bloque(BaseModel):
    nombre: str
    categoria_base: Literal[
        "defensivo", "income", "growth", "aggressive", "colchon", "sin_clasificar"
    ]
    peso_objetivo: Decimal = Field(decimal_places=4)
    peso_actual: Decimal = Field(decimal_places=4)
    desviacion: Decimal = Field(decimal_places=4)
    valor_eur: Decimal = Field(decimal_places=2)


class PosicionResumen(BaseModel):
    isin: str
    ticker: str
    nombre: str
    divisa_local: str
    cantidad: Decimal = Field(decimal_places=10)
    pm_real_eur: Decimal = Field(decimal_places=4)
    pm_fiscal_es_eur: Decimal = Field(decimal_places=4)
    pm_opciones_total_eur: Decimal = Field(decimal_places=4)
    precio_actual_local: Decimal = Field(decimal_places=4)
    valor_eur: Decimal = Field(decimal_places=2)
    plusvalia_latente_eur: Decimal = Field(decimal_places=2)
    bloque: str | None
    fecha_actualizacion: date


class CarteraResumen(BaseModel):
    cartera_id: str
    nombre: str
    capital_total_eur: Decimal = Field(decimal_places=2)
    progreso_if_pct: Decimal = Field(decimal_places=4)
    anos_estimados_if: Decimal = Field(decimal_places=2)
    yield_actual_pct: Decimal = Field(decimal_places=4)
    # Agregados del año en curso (marcadores de resumen)
    anio: int
    dividendos_bruto_anio: Decimal = Field(decimal_places=2)
    opciones_neto_anio: Decimal = Field(decimal_places=2)   # primas cobradas − pagadas (declarables)
    gp_realizada_anio: Decimal = Field(decimal_places=2)    # plusvalías/minusvalías realizadas (FIFO)
    aportacion_neta_anio: Decimal = Field(decimal_places=2)  # capital aportado de bolsillo este año
    liquidez_eur: Decimal = Field(decimal_places=2)          # efectivo disponible (cash flows)
    bloques: list[Bloque]
    posiciones: list[PosicionResumen]
    fecha_snapshot: date


# ── Endpoints ──────────────────────────────────────────────────────────

router = APIRouter(tags=["cartera"])


@router.get("/cartera", response_model=CarteraResumen)
def get_cartera(db: Session = Depends(get_db),
                cartera: models.Cartera = Depends(get_current_cartera)) -> CarteraResumen:
    """Devuelve resumen de la cartera del usuario autenticado."""
    posiciones_db = list(db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera.id)
    ).scalars())

    pos_resumen: list[PosicionResumen] = []
    capital_total = Decimal("0")
    for pos in posiciones_db:
        est = estado_posicion(db, pos.id)
        if est["cantidad"] <= 0:
            continue   # posición cerrada, no la mostramos
        # Precio actual no lo tenemos todavía (sin feed) → usamos PM real.
        valor = est["coste_total_eur"]
        capital_total += valor
        pos_resumen.append(PosicionResumen(
            isin=pos.isin,
            ticker=pos.ticker or "",
            nombre=pos.nombre or pos.isin,
            divisa_local=pos.divisa_local,
            cantidad=est["cantidad"],
            pm_real_eur=est["pm_real_eur"],
            pm_fiscal_es_eur=est["pm_real_eur"],       # idéntico hasta opciones
            pm_opciones_total_eur=est["pm_real_eur"],
            precio_actual_local=est["pm_real_eur"],     # placeholder sin feed
            valor_eur=valor,
            plusvalia_latente_eur=Decimal("0"),
            bloque=None,
            fecha_actualizacion=date.today(),
        ))

    # ── Agregados del año en curso ──────────────────────────────────────
    from app.services.fiscal import calcular_fiscal
    from app.services.fiscal_dividendos import calcular_dividendos
    from app.services.fiscal_opciones import calcular_opciones

    anio = date.today().year
    div_anio = Decimal("0")
    div_neto = Decimal("0")
    try:
        div_res = calcular_dividendos(db, cartera.id, anio)
        div_anio = div_res.bruto_total
        # Neto recibido = bruto − retención en origen (lo que entra a la cuenta).
        div_neto = div_res.bruto_total - div_res.ret_origen_total
    except Exception:
        pass
    try:
        opt = calcular_opciones(db, cartera.id, anio)
        opciones_neto = opt.totales.get("primas_cobradas", Decimal("0")) - \
            opt.totales.get("primas_pagadas", Decimal("0"))
    except Exception:
        opciones_neto = Decimal("0")
    try:
        gp_realizada = calcular_fiscal(db, cartera.id, anio).gp_bruto
    except Exception:
        gp_realizada = Decimal("0")
    try:
        from app.services.aportaciones import aportaciones_por_anio
        aportacion_neta = aportaciones_por_anio(db, cartera.id).get(anio, Decimal("0"))
    except Exception:
        aportacion_neta = Decimal("0")
    try:
        from app.services.liquidez import liquidez_para_invertir
        # Disponible para invertir = total − liquidez de bloques fuera de
        # estrategia (colchón etc.). Refleja lo que de verdad puede desplegar.
        liquidez_eur, _, _ = liquidez_para_invertir(db, cartera.id)
    except Exception:
        liquidez_eur = Decimal("0")

    # Yield actual = dividendos netos del año / capital (YTD).
    yield_pct = (
        (div_neto / capital_total) if capital_total > 0 else Decimal("0")
    )

    return CarteraResumen(
        cartera_id=cartera.id,
        nombre=cartera.nombre,
        capital_total_eur=capital_total.quantize(Decimal("0.01")),
        progreso_if_pct=Decimal("0"),
        anos_estimados_if=Decimal("0"),
        yield_actual_pct=Decimal(str(yield_pct)).quantize(Decimal("0.0001")),
        anio=anio,
        dividendos_bruto_anio=Decimal(str(div_anio)).quantize(Decimal("0.01")),
        opciones_neto_anio=Decimal(str(opciones_neto)).quantize(Decimal("0.01")),
        gp_realizada_anio=Decimal(str(gp_realizada)).quantize(Decimal("0.01")),
        aportacion_neta_anio=Decimal(str(aportacion_neta)).quantize(Decimal("0.01")),
        liquidez_eur=Decimal(str(liquidez_eur)).quantize(Decimal("0.01")),
        bloques=[],                                       # bloques en fase posterior
        posiciones=pos_resumen,
        fecha_snapshot=date.today(),
    )


def _mock_resumen() -> CarteraResumen:
    """Resumen mock para frontend cuando la BD está vacía."""
    return CarteraResumen(
        cartera_id="00000000-0000-0000-0000-000000000001",
        nombre="Cartera IF principal (mock)",
        capital_total_eur=Decimal("170493.34"),
        progreso_if_pct=Decimal("0.5683"),
        anos_estimados_if=Decimal("2.58"),
        yield_actual_pct=Decimal("0.0223"),
        anio=date.today().year,
        dividendos_bruto_anio=Decimal("3120.00"),
        opciones_neto_anio=Decimal("1915.85"),
        gp_realizada_anio=Decimal("8430.00"),
        aportacion_neta_anio=Decimal("12000.00"),
        liquidez_eur=Decimal("8214.49"),
        bloques=[
            Bloque(
                nombre="Compounders",
                categoria_base="growth",
                peso_objetivo=Decimal("0.4000"),
                peso_actual=Decimal("0.4250"),
                desviacion=Decimal("0.0250"),
                valor_eur=Decimal("72400.00"),
            ),
            Bloque(
                nombre="Dividend Growth",
                categoria_base="income",
                peso_objetivo=Decimal("0.2500"),
                peso_actual=Decimal("0.2410"),
                desviacion=Decimal("-0.0090"),
                valor_eur=Decimal("41100.00"),
            ),
            Bloque(
                nombre="Seguro de Vida",
                categoria_base="defensivo",
                peso_objetivo=Decimal("0.1500"),
                peso_actual=Decimal("0.1670"),
                desviacion=Decimal("0.0170"),
                valor_eur=Decimal("28500.00"),
            ),
        ],
        posiciones=[
            PosicionResumen(
                isin="US5949181045",
                ticker="MSFT",
                nombre="Microsoft Corp",
                divisa_local="USD",
                cantidad=Decimal("40.0000000000"),
                pm_real_eur=Decimal("322.5000"),
                pm_fiscal_es_eur=Decimal("322.5000"),
                pm_opciones_total_eur=Decimal("322.5000"),
                precio_actual_local=Decimal("407.6000"),
                valor_eur=Decimal("14936.00"),
                plusvalia_latente_eur=Decimal("2036.00"),
                bloque="Compounders",
                fecha_actualizacion=date.today(),
            ),
            PosicionResumen(
                isin="IE000U9J8HX9",
                ticker="JEQP",
                nombre="JPM Nasdaq Equity Premium Income ETF",
                divisa_local="GBX",
                cantidad=Decimal("725.7827550000"),
                pm_real_eur=Decimal("24.4256"),
                pm_fiscal_es_eur=Decimal("24.4256"),
                pm_opciones_total_eur=Decimal("24.4256"),
                precio_actual_local=Decimal("2734.0000"),
                valor_eur=Decimal("16853.00"),
                plusvalia_latente_eur=Decimal("-783.00"),
                bloque="Colchón Psicológico",
                fecha_actualizacion=date.today(),
            ),
        ],
        fecha_snapshot=date.today(),
    )
