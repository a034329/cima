"""Jobs de análisis IA en segundo plano (one-pager/valoración: búsqueda web de minutos).

La UI hace polling de `estado`; el RESULTADO lo persiste el generador en AnalisisGuardado.
Mecanismo simple por hilo — suficiente para dev/owner (1 proceso). En SaaS multi-worker se
sustituiría por una cola real (Celery/RQ). Uno por (cartera, isin, tipo).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import models
from app.db.base import SessionLocal

EN_CURSO, OK, ERROR = "en_curso", "ok", "error"

# Un job en_curso más viejo que esto se considera ZOMBI (el hilo murió o el
# proceso se reinició): el timeout máximo de la IA con web es 600s, con margen.
STALE_S = 20 * 60

Generador = Callable[[Session, str, str], object]   # (db, cartera_id, isin) → persiste el resultado


def estado(db: Session, cartera_id: str, isin: str, tipo: str) -> models.AnalisisJob | None:
    return db.execute(
        select(models.AnalisisJob)
        .where(models.AnalisisJob.cartera_id == cartera_id)
        .where(models.AnalisisJob.isin == isin)
        .where(models.AnalisisJob.tipo == tipo)
    ).scalars().first()


def _es_zombi(row: models.AnalisisJob) -> bool:
    """en_curso sin actualización en STALE_S → el hilo murió o hubo restart."""
    if row.estado != EN_CURSO:
        return False
    ts = row.updated_at or row.created_at
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts) > timedelta(seconds=STALE_S)


def limpiar_zombis(db: Session) -> int:
    """Marca como error los jobs en_curso huérfanos. Se invoca al ARRANCAR el
    proceso: con un único proceso de jobs, nada puede seguir corriendo tras un
    restart — sin esto, el polling de la UI devolvía en_curso PARA SIEMPRE
    (auditoría Cima 2026-06-11, J1)."""
    res = db.execute(
        update(models.AnalisisJob)
        .where(models.AnalisisJob.estado == EN_CURSO)
        .values(estado=ERROR,
                error="Interrumpido por reinicio del servidor — relanza el análisis.",
                updated_at=datetime.now(UTC))
    )
    db.commit()
    return res.rowcount or 0


def _set(db: Session, cartera_id: str, isin: str, tipo: str,
         est: str, error: str | None = None) -> None:
    row = estado(db, cartera_id, isin, tipo)
    if row is None:
        db.add(models.AnalisisJob(cartera_id=cartera_id, isin=isin, tipo=tipo,
                                  estado=est, error=error))
        try:
            db.commit()
        except IntegrityError:
            # Carrera read-then-insert entre el router y el hilo (sesiones
            # distintas, UniqueConstraint cartera+isin+tipo): reintentar como
            # UPDATE — antes el IntegrityError mataba el hilo y el job quedaba
            # en_curso para siempre (auditoría J1).
            db.rollback()
            row = estado(db, cartera_id, isin, tipo)
            if row is not None:
                row.estado = est
                row.error = error
                row.updated_at = datetime.now(UTC)
                db.commit()
        return
    row.estado = est
    row.error = error
    row.updated_at = datetime.now(UTC)
    db.commit()


def _ejecutar(cartera_id: str, isin: str, tipo: str, fn: Generador,
              session_factory=SessionLocal) -> None:
    """Cuerpo del worker — testeable en síncrono (inyecta `session_factory`)."""
    s = session_factory()
    try:
        fn(s, cartera_id, isin)                  # genera + persiste el resultado
        _set(s, cartera_id, isin, tipo, OK)
    except Exception as e:                        # noqa: BLE001 — cualquier fallo → estado error
        s.rollback()
        _set(s, cartera_id, isin, tipo, ERROR, str(e)[:300])
    finally:
        s.close()


def lanzar(db: Session, cartera_id: str, isin: str, tipo: str, fn: Generador) -> bool:
    """Marca el job en_curso y ejecuta `fn` en un hilo con su propia sesión.

    Devuelve False (sin lanzar nada) si ya hay un job en_curso FRESCO para la
    misma (cartera, isin, tipo): un doble clic o un retry del frontend
    lanzaba un segundo hilo con otra llamada IA de minutos (coste real) que
    además escribía sobre el mismo AnalisisGuardado (auditoría J1). Los
    en_curso rancios (zombis) sí se relanzan."""
    actual = estado(db, cartera_id, isin, tipo)
    if actual is not None and actual.estado == EN_CURSO and not _es_zombi(actual):
        return False
    _set(db, cartera_id, isin, tipo, EN_CURSO)
    threading.Thread(target=_ejecutar, args=(cartera_id, isin, tipo, fn), daemon=True).start()
    return True
