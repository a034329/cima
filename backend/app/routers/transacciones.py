"""Endpoints de transacciones manuales."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.schemas.transaccion import (
    EstadoTransaccion,
    TransaccionIn,
    TransaccionOut,
)
from app.services.transacciones import TxCandidata, crear_manual


router = APIRouter(prefix="/transacciones", tags=["transacciones"])


def _resolver_cartera_por_defecto(db: Session) -> models.Cartera:
    """Devuelve la primera cartera disponible.

    Stub mientras no haya auth: en producción será `current_user.cartera_id`.
    """
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera creada. Crea una primero o inicializa BD.",
        )
    return cartera


@router.post(
    "",
    response_model=TransaccionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar operación manual",
)
def crear_transaccion(
    payload: TransaccionIn,
    db: Session = Depends(get_db),
) -> models.Transaccion:
    """Registra una transacción manual. Por defecto se aplica AL INSTANTE
    (`confirmar_directo=true`): queda `confirmada` y dispara el rebuild FIFO
    → la posición se actualiza (cantidad, coste, G/P realizada). Si pasas
    `false`, queda en `pendiente_confirmar` esperando al extracto del broker.

    Si pasas `posicion_id` (selector de la cartera), `isin`/`nombre`/`divisa`
    se autocompletan desde esa posición."""
    cartera = _resolver_cartera_por_defecto(db)

    isin = payload.isin
    nombre = payload.nombre
    divisa_local = payload.divisa_local
    if payload.posicion_id:
        pos = db.execute(
            select(models.Posicion)
            .where(models.Posicion.id == payload.posicion_id)
            .where(models.Posicion.cartera_id == cartera.id)
        ).scalars().first()
        if pos is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"Posición {payload.posicion_id} no existe en tu cartera")
        isin = isin or pos.isin
        nombre = nombre or pos.nombre
        divisa_local = divisa_local or pos.divisa_local
    if not isin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Falta isin (o posicion_id para autocompletarlo)")

    candidata = TxCandidata(
        fecha=payload.fecha,
        tipo=payload.tipo,
        isin=isin,
        nombre=nombre,
        cantidad=payload.cantidad,
        precio_local=payload.precio_local,
        divisa_local=divisa_local or "EUR",
        importe_local=payload.importe_local,
        fx_rate=payload.fx_rate,
        importe_eur=payload.importe_eur,
        gastos_eur=payload.gastos_eur,
        tasas_externas_eur=payload.tasas_externas_eur,
        retencion_eur=payload.retencion_eur,
        retencion_pais=payload.retencion_pais,
        external_id=None,            # nunca para manuales
        broker_id=payload.broker_id or "",
        notas=payload.notas,
    )
    return crear_manual(db, cartera.id, candidata,
                        confirmar_directo=payload.confirmar_directo)


@router.get(
    "",
    response_model=list[TransaccionOut],
    summary="Listar transacciones (con filtros)",
)
def listar_transacciones(
    estado: EstadoTransaccion | None = Query(default=None),
    isin: str | None = Query(default=None, min_length=12, max_length=12),
    desde: date | None = Query(default=None),
    hasta: date | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[models.Transaccion]:
    cartera = _resolver_cartera_por_defecto(db)
    q = (
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera.id)
        .order_by(models.Transaccion.fecha.desc())
    )
    if estado is not None:
        q = q.where(models.Transaccion.estado == estado)
    if isin is not None:
        q = q.join(models.Posicion).where(models.Posicion.isin == isin)
    if desde is not None:
        q = q.where(models.Transaccion.fecha >= desde)
    if hasta is not None:
        q = q.where(models.Transaccion.fecha <= hasta)
    q = q.limit(limit).offset(offset)
    return list(db.execute(q).scalars())


@router.get(
    "/{tx_id}",
    response_model=TransaccionOut,
    summary="Obtener una transacción por ID",
)
def obtener_transaccion(tx_id: str, db: Session = Depends(get_db)) -> models.Transaccion:
    tx = db.get(models.Transaccion, tx_id)
    if tx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transacción {tx_id} no encontrada",
        )
    return tx


@router.delete(
    "/{tx_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Marcar una transacción como descartada",
)
def descartar_transaccion(tx_id: str, db: Session = Depends(get_db)) -> None:
    """No elimina físicamente — marca `estado=descartada` para preservar
    auditoría, y reconstruye el FIFO de la posición (los lotes derivan de
    las transacciones confirmadas: sin rebuild, la posición seguía mostrando
    cantidad/coste de la transacción descartada en dashboard, fiscal y
    optimizador — auditoría Cima 2026-06-11, C3; mantenimiento.py ya lo
    hacía bien)."""
    tx = db.get(models.Transaccion, tx_id)
    if tx is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transacción {tx_id} no encontrada",
        )
    tx.estado = "descartada"
    db.flush()
    if tx.posicion_id:
        from app.services import fifo
        fifo.rebuild_for_posicion(db, tx.posicion_id)
    db.commit()
