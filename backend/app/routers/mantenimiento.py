"""Endpoints de mantenimiento — operaciones one-off de limpieza.

Pensados para resolver legados de bugs previos sin tener que vaciar la BD.
NO se exponen en `/docs` en producción; sólo en modo Owner / dev.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import fifo


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


class DedupResultado(BaseModel):
    cartera_id: str
    grupos_duplicados: int           # nº de combinaciones (broker, isin, fecha, tipo, cantidad, importe) con duplicados
    descartadas: int                 # nº de transacciones marcadas descartada
    conservadas: int                 # nº de transacciones que quedan vivas
    posiciones_rebuild: int          # nº de posiciones rebuildeadas
    detalle: list[str]               # primer/algunos grupos para inspección


@router.post(
    "/dedup-sin-external-id",
    response_model=DedupResultado,
    summary="Dedup retroactiva de tx sin external_id (bug previo a id sintético)",
)
def deduplicar_sin_external_id(db: Session = Depends(get_db)) -> DedupResultado:
    """Encuentra transacciones que comparten firma natural
    `(broker_id, posicion_id, fecha, tipo, cantidad, importe_eur)` y conserva
    sólo la más antigua. El resto pasa a `estado='descartada'`.

    Útil después de varios re-imports del mismo CSV de DEGIRO antes de que
    el adapter generara external_id sintético para filas sin Order ID.

    Tras descartar duplicados, rebuildea el FIFO de las posiciones tocadas.

    Es idempotente: una segunda invocación no encontrará duplicados nuevos.
    """
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )

    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera.id)
        .where(models.Transaccion.estado == "confirmada")
        .order_by(models.Transaccion.created_at)
    ).scalars())

    # Agrupar por firma natural
    grupos: dict[tuple, list[models.Transaccion]] = defaultdict(list)
    for tx in txs:
        key = (
            tx.broker_id,
            tx.posicion_id,
            tx.fecha,
            tx.tipo,
            # Decimal a string para que comparaciones de igualdad sean robustas
            str(Decimal(str(tx.cantidad))),
            str(Decimal(str(tx.importe_eur))),
        )
        grupos[key].append(tx)

    posiciones_tocadas: set[str] = set()
    descartadas = 0
    conservadas = 0
    grupos_dup = 0
    detalle: list[str] = []

    for key, lista in grupos.items():
        if len(lista) == 1:
            conservadas += 1
            continue
        grupos_dup += 1
        # Conservar la más antigua (orden por created_at por la query)
        original = lista[0]
        conservadas += 1
        duplicados = lista[1:]
        for d in duplicados:
            d.estado = "descartada"
            d.notas = (
                (d.notas + " · " if d.notas else "")
                + f"Descartada por dedup retroactiva (firma duplicada con tx {original.id[:8]})"
            )
            posiciones_tocadas.add(d.posicion_id)
            descartadas += 1
        if len(detalle) < 10:
            detalle.append(
                f"{original.posicion_id[:8]} {original.fecha} {original.tipo} "
                f"qty={original.cantidad} eur={original.importe_eur}: "
                f"{len(duplicados)} duplicados marcados descartada"
            )

    db.flush()

    # Rebuild FIFO de las posiciones tocadas — el inventario cambia ahora.
    rebuilds = fifo.rebuild_for_posiciones(db, posiciones_tocadas)

    db.commit()

    return DedupResultado(
        cartera_id=cartera.id,
        grupos_duplicados=grupos_dup,
        descartadas=descartadas,
        conservadas=conservadas,
        posiciones_rebuild=len(rebuilds),
        detalle=detalle,
    )
