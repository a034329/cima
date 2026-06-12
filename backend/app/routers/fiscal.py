"""Endpoint del cálculo fiscal — invoca el motor de Cuádrate.

GET /api/fiscal/{ejercicio} → FiscalResumenOut con plusvalías reales,
matches FIFO con flag 2M, pérdidas diferidas latentes, RCM neto y la
aplicación de las reglas de compensación (RCM↔patrimoniales 25%, bolsas
4 años).

Sin auth todavía: opera sobre la primera cartera disponible.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.schemas.fiscal import CompensacionOut, FiscalResumenOut
from app.services.fiscal import calcular_fiscal
from app.services.fiscal_resumen import calcular_resumen


router = APIRouter(prefix="/fiscal", tags=["fiscal"])


def _q2(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


# ── Resumen del ejercicio (cuadro IRPF integrado) ──────────────────────────

class ResumenFiscalOut(BaseModel):
    ejercicio: int
    fecha_calculo: date
    # G/P patrimoniales (base del ahorro) — repartido en casillas como Cuádrate
    gp_acciones: Decimal = Field(decimal_places=2)        # 0326-0340
    gp_derechos: Decimal = Field(decimal_places=2)        # 0341-0355
    gp_estructurados: Decimal = Field(decimal_places=2)   # 1624-1654
    perdidas_afloradas: Decimal = Field(decimal_places=2)  # las declara el usuario
    gp_no_deducible_2m: Decimal = Field(decimal_places=2)
    forex_realized: Decimal = Field(decimal_places=2)
    opciones_pl: Decimal = Field(decimal_places=2)        # casilla 1626
    # RCM (base del ahorro)
    dividendos_bruto: Decimal = Field(decimal_places=2)   # casilla 0029
    dividendos_ret_es: Decimal = Field(decimal_places=2)
    intereses_rcm: Decimal = Field(decimal_places=2)      # casilla 0027
    letras_rcm: Decimal = Field(decimal_places=2)
    intereses_debit: Decimal = Field(decimal_places=2)    # informativo
    # Deducción de cuota
    cdi_recuperable: Decimal = Field(decimal_places=2)    # casilla 0588
    # Totales a tributar
    base_ahorro_gp: Decimal = Field(decimal_places=2)
    base_ahorro_rcm: Decimal = Field(decimal_places=2)
    base_ahorro_total: Decimal = Field(decimal_places=2)
    compensacion: CompensacionOut


def _serializar_resumen(r) -> ResumenFiscalOut:  # type: ignore[no-untyped-def]
    return ResumenFiscalOut(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        gp_acciones=_q2(r.gp_acciones),
        gp_derechos=_q2(r.gp_derechos),
        gp_estructurados=_q2(r.gp_estructurados),
        perdidas_afloradas=_q2(r.perdidas_afloradas),
        gp_no_deducible_2m=_q2(r.gp_no_deducible_2m),
        forex_realized=_q2(r.forex_realized),
        opciones_pl=_q2(r.opciones_pl),
        dividendos_bruto=_q2(r.dividendos_bruto),
        dividendos_ret_es=_q2(r.dividendos_ret_es),
        intereses_rcm=_q2(r.intereses_rcm),
        letras_rcm=_q2(r.letras_rcm),
        intereses_debit=_q2(r.intereses_debit),
        cdi_recuperable=_q2(r.cdi_recuperable),
        base_ahorro_gp=_q2(r.base_ahorro_gp),
        base_ahorro_rcm=_q2(r.base_ahorro_rcm),
        base_ahorro_total=_q2(r.base_ahorro_total),
        compensacion=r.compensacion,
    )


def _cartera_o_404(db: Session) -> models.Cartera:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return cartera


@router.get("/resumen/acumulado", response_model=ResumenFiscalOut,
            summary="Cuadro IRPF integrado — acumulado")
def get_resumen_acumulado(db: Session = Depends(get_db)) -> ResumenFiscalOut:
    return _serializar_resumen(calcular_resumen(db, _cartera_o_404(db).id, None))


@router.get("/resumen/{ejercicio}", response_model=ResumenFiscalOut,
            summary="Cuadro IRPF integrado del ejercicio (base del ahorro completa)")
def get_resumen(ejercicio: int, db: Session = Depends(get_db)) -> ResumenFiscalOut:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar_resumen(calcular_resumen(db, _cartera_o_404(db).id, ejercicio))


# Rango razonable. Antes de 2015 cambió la regla 2M; después de 2030 es
# improbable que alguien consulte. Si se necesita más, ampliar.
_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


def _serializar(r) -> FiscalResumenOut:  # type: ignore[no-untyped-def]
    return FiscalResumenOut(
        ejercicio=r.ejercicio,
        cartera_id=r.cartera_id,
        fecha_corte=r.fecha_corte,
        fecha_calculo=r.fecha_calculo,
        gp_bruto=r.gp_bruto,
        gp_no_deducible_2m=r.gp_no_deducible_2m,
        total_perdida_aflorada=r.total_perdida_aflorada,
        rcm_neto=r.rcm_neto,
        n_matches=r.n_matches,
        matches=r.matches,                         # type: ignore[arg-type]
        positions=r.positions,                     # type: ignore[arg-type]
        perdidas_diferidas_latentes=r.perdidas_diferidas_latentes,  # type: ignore[arg-type]
        orphan_sales=r.orphan_sales,               # type: ignore[arg-type]
        warnings=r.warnings,
        compensacion=r.resultado_compensacion,     # type: ignore[arg-type]
    )


@router.get(
    "/acumulado",
    response_model=FiscalResumenOut,
    summary="Cálculo fiscal acumulado (todos los años en BD)",
)
def get_fiscal_acumulado(limit_matches: int = 1000,
                         db: Session = Depends(get_db)) -> FiscalResumenOut:
    """Vista histórica: todos los matches FIFO de cualquier año + todos los
    dividendos del histórico. La compensación se devuelve referida al año
    siguiente al último match (informativo, no es la cifra que va a RentaWEB).

    Útil para auditar el patrimonio realizado total y los flujos de RCM
    sin tener que pivotar año por año.
    """
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    r = calcular_fiscal(db, cartera.id, None)
    out = _serializar(r)
    # Cota de payload: un daytrader acumula miles de matches históricos —
    # el endpoint servía TODOS (auditoría). n_matches conserva el total real;
    # se devuelven los `limit_matches` más recientes.
    if limit_matches and len(out.matches) > limit_matches:
        out.matches = sorted(out.matches, key=lambda m: m.fecha_venta)[-limit_matches:]
    return out


# ── Optimizador fiscal de cierre de año (tax-loss harvesting) ──────────────

class LatenteOut(BaseModel):
    isin: str
    nombre: str
    cantidad: Decimal = Field(decimal_places=4)
    pm_real_eur: Decimal = Field(decimal_places=4)
    precio_actual_eur: Decimal | None = None
    valor_actual_eur: Decimal | None = None
    gp_latente_eur: Decimal | None = None
    es_perdida: bool
    bloqueo_2m: bool
    precio_manual: bool
    sin_precio: bool


class OptimizadorOut(BaseModel):
    ejercicio: int
    fecha_calculo: date
    gp_realizada_ytd: Decimal = Field(decimal_places=2)
    rcm_ytd: Decimal = Field(decimal_places=2)
    bolsas_pendientes: Decimal = Field(decimal_places=2)
    perdida_a_arrastrar_anio: Decimal = Field(decimal_places=2)
    diferidas_2m: Decimal = Field(decimal_places=2)
    perdida_latente_cosechable: Decimal = Field(decimal_places=2)
    ganancia_latente_total: Decimal = Field(decimal_places=2)
    compensable_ahora: Decimal = Field(decimal_places=2)
    latentes: list[LatenteOut]
    no_resueltos: list[str]


class PrecioManualIn(BaseModel):
    isin: str
    precio_eur: Decimal | None = None   # None → quita el override (vuelve al feed)


def _q4n(x):  # type: ignore[no-untyped-def]
    return None if x is None else Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP)


@router.get("/optimizador/{ejercicio}", response_model=OptimizadorOut,
            summary="Optimizador fiscal de cierre de año (harvesting)")
def get_optimizador(ejercicio: int, db: Session = Depends(get_db)) -> OptimizadorOut:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ejercicio fuera de rango")
    from app.services.fiscal_optimizador import calcular_optimizador
    r = calcular_optimizador(db, _cartera_o_404(db).id, ejercicio)
    return OptimizadorOut(
        ejercicio=r.ejercicio, fecha_calculo=r.fecha_calculo,
        gp_realizada_ytd=_q2(r.gp_realizada_ytd), rcm_ytd=_q2(r.rcm_ytd),
        bolsas_pendientes=_q2(r.bolsas_pendientes),
        perdida_a_arrastrar_anio=_q2(r.perdida_a_arrastrar_anio),
        diferidas_2m=_q2(r.diferidas_2m),
        perdida_latente_cosechable=_q2(r.perdida_latente_cosechable),
        ganancia_latente_total=_q2(r.ganancia_latente_total),
        compensable_ahora=_q2(r.compensable_ahora),
        latentes=[
            LatenteOut(
                isin=l.isin, nombre=l.nombre, cantidad=_q4n(l.cantidad),
                pm_real_eur=_q4n(l.pm_real_eur), precio_actual_eur=_q4n(l.precio_actual_eur),
                valor_actual_eur=(_q2(l.valor_actual_eur) if l.valor_actual_eur is not None else None),
                gp_latente_eur=(_q2(l.gp_latente_eur) if l.gp_latente_eur is not None else None),
                es_perdida=l.es_perdida, bloqueo_2m=l.bloqueo_2m,
                precio_manual=l.precio_manual, sin_precio=l.sin_precio,
            )
            for l in r.latentes
        ],
        no_resueltos=r.no_resueltos,
    )


class PerdidaManualOut(BaseModel):
    ejercicio_origen: int
    importe_eur: Decimal = Field(decimal_places=2)
    expira: int


class PerdidaManualIn(BaseModel):
    ejercicio_origen: int
    importe_eur: Decimal | None = None   # None o <=0 → elimina


@router.get("/perdidas-pendientes", response_model=list[PerdidaManualOut],
            summary="Pérdidas pendientes de años anteriores (entrada manual)")
def get_perdidas_pendientes(db: Session = Depends(get_db)) -> list[PerdidaManualOut]:
    from app.services import perdidas as svc
    return [
        PerdidaManualOut(ejercicio_origen=p.ejercicio_origen,
                         importe_eur=_q2(p.importe_eur), expira=p.expira)
        for p in svc.listar(db, _cartera_o_404(db).id)
    ]


@router.put("/perdidas-pendientes", status_code=status.HTTP_204_NO_CONTENT,
            summary="Fijar/quitar una pérdida pendiente de un año anterior")
def set_perdida_pendiente(payload: PerdidaManualIn, db: Session = Depends(get_db)) -> None:
    from app.services import perdidas as svc
    svc.set_perdida(db, _cartera_o_404(db).id, payload.ejercicio_origen, payload.importe_eur)


@router.put("/optimizador/precio", status_code=status.HTTP_204_NO_CONTENT,
            summary="Fijar/quitar el precio actual manual de una posición")
def set_precio_manual(payload: PrecioManualIn, db: Session = Depends(get_db)) -> None:
    cartera = _cartera_o_404(db)
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera.id)
        .where(models.Posicion.isin == payload.isin)
    ).scalars().first()
    if pos is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Posición {payload.isin} no existe")
    if payload.precio_eur is not None and payload.precio_eur < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El precio no puede ser negativo")
    pos.precio_manual_eur = payload.precio_eur
    db.commit()


# ── Filtro fiscal de rotación (umbrales R-U del modelo WG) ─────────────────

class RotacionItemOut(BaseModel):
    isin: str
    nombre: str
    valor_eur: Decimal = Field(decimal_places=2)
    gp_latente_eur: Decimal = Field(decimal_places=2)
    coste_fiscal_eur: Decimal = Field(decimal_places=2)
    tipo_efectivo_pct: Decimal | None = None
    cagr4_div_origen_pct: Decimal | None = None
    umbral_1y_pct: Decimal | None = None
    umbral_2y_pct: Decimal | None = None
    umbral_3y_pct: Decimal | None = None
    umbral_4y_pct: Decimal | None = None
    delta_anios_if: Decimal | None = None   # años de IF que retrasa el coste fiscal (V2)


class RotacionOut(BaseModel):
    ejercicio: int
    fecha_calculo: date
    base_ahorro_actual_eur: Decimal = Field(decimal_places=2)
    items: list[RotacionItemOut]
    sin_estimacion: list[str]


@router.get("/rotacion/{ejercicio}", response_model=RotacionOut,
            summary="Filtro fiscal de rotación: umbral CAGR4+Div que debe batir el destino")
def get_rotacion(ejercicio: int, db: Session = Depends(get_db)) -> RotacionOut:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ejercicio fuera de rango")
    from app.services.fiscal_rotacion import calcular_rotacion
    r = calcular_rotacion(db, _cartera_o_404(db).id, ejercicio)
    return RotacionOut(
        ejercicio=r.ejercicio, fecha_calculo=r.fecha_calculo,
        base_ahorro_actual_eur=_q2(r.base_ahorro_actual_eur),
        items=[
            RotacionItemOut(
                isin=it.isin, nombre=it.nombre,
                valor_eur=_q2(it.valor_eur),
                gp_latente_eur=_q2(it.gp_latente_eur),
                coste_fiscal_eur=_q2(it.coste_fiscal_eur),
                tipo_efectivo_pct=_q4n(it.tipo_efectivo_pct),
                cagr4_div_origen_pct=_q4n(it.cagr4_div_origen_pct),
                umbral_1y_pct=_q4n(it.umbral_1y_pct),
                umbral_2y_pct=_q4n(it.umbral_2y_pct),
                umbral_3y_pct=_q4n(it.umbral_3y_pct),
                umbral_4y_pct=_q4n(it.umbral_4y_pct),
                delta_anios_if=it.delta_anios_if,
            )
            for it in r.items
        ],
        sin_estimacion=r.sin_estimacion,
    )


@router.get(
    "/{ejercicio}",
    response_model=FiscalResumenOut,
    summary="Cálculo fiscal del ejercicio (FIFO + regla 2M + compensación)",
)
def get_fiscal(
    ejercicio: int,
    db: Session = Depends(get_db),
) -> FiscalResumenOut:
    """Devuelve el cálculo fiscal completo para un ejercicio: matches FIFO,
    G/P bruto y deducible, pérdidas diferidas latentes y compensación final.

    El motor se invoca en cada llamada — no se persiste. Si el rendimiento
    se degrada con carteras grandes, cachear por (cartera_id, ejercicio,
    hash_tx) con TTL.
    """
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )

    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )

    r = calcular_fiscal(db, cartera.id, ejercicio)
    return _serializar(r)


# ── Fugas fiscales: retención de origen no recuperable vía 0588 ────────────

class FugaPosicionOut(BaseModel):
    isin: str
    nombre: str
    pais: str
    exceso_pct: Decimal = Field(decimal_places=4)
    div_anual_estimado_eur: Decimal | None = None
    fuga_anual_estimada_eur: Decimal | None = None
    exceso_real_ytd_eur: Decimal = Field(decimal_places=2)


class FugaPaisOut(BaseModel):
    pais: str
    exceso_pct: Decimal = Field(decimal_places=4)
    fuga_anual_estimada_eur: Decimal = Field(decimal_places=2)
    exceso_real_ytd_eur: Decimal = Field(decimal_places=2)
    mecanismo: str
    posiciones: list[FugaPosicionOut]


class FugasOut(BaseModel):
    ejercicio: int
    total_fuga_anual_estimada_eur: Decimal = Field(decimal_places=2)
    total_exceso_real_ytd_eur: Decimal = Field(decimal_places=2)
    por_pais: list[FugaPaisOut]


@router.get("/fugas", response_model=FugasOut,
            summary="Fugas fiscales: retención de origen NO recuperable vía 0588")
def get_fugas(db: Session = Depends(get_db)) -> FugasOut:
    """Cuantifica el dinero que se queda en el fisco extranjero por encima
    del tope del CDI (solo recuperable reclamando al país de origen):
    exceso REAL del ejercicio sobre dividendos ya cobrados + PROYECCIÓN
    anual sobre el yield estimado de cada posición."""
    from app.services.fugas_fiscales import calcular_fugas

    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    r = calcular_fugas(db, cartera.id)
    return FugasOut(
        ejercicio=r.ejercicio,
        total_fuga_anual_estimada_eur=r.total_fuga_anual_estimada_eur,
        total_exceso_real_ytd_eur=r.total_exceso_real_ytd_eur,
        por_pais=[FugaPaisOut(
            pais=p.pais, exceso_pct=p.exceso_pct,
            fuga_anual_estimada_eur=p.fuga_anual_estimada_eur,
            exceso_real_ytd_eur=p.exceso_real_ytd_eur,
            mecanismo=p.mecanismo,
            posiciones=[FugaPosicionOut(
                isin=x.isin, nombre=x.nombre, pais=x.pais,
                exceso_pct=x.exceso_pct,
                div_anual_estimado_eur=x.div_anual_estimado_eur,
                fuga_anual_estimada_eur=x.fuga_anual_estimada_eur,
                exceso_real_ytd_eur=x.exceso_real_ytd_eur,
            ) for x in p.posiciones],
        ) for p in r.por_pais],
    )
