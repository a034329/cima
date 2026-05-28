"""Jobs de análisis IA en segundo plano (one-pager/valoración: búsqueda web de minutos).

La UI hace polling de `estado`; el RESULTADO lo persiste el generador en AnalisisGuardado.
Mecanismo simple por hilo — suficiente para dev/owner (1 proceso). En SaaS multi-worker se
sustituiría por una cola real (Celery/RQ). Uno por (cartera, isin, tipo).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.db.base import SessionLocal

EN_CURSO, OK, ERROR = "en_curso", "ok", "error"

Generador = Callable[[Session, str, str], object]   # (db, cartera_id, isin) → persiste el resultado


def estado(db: Session, cartera_id: str, isin: str, tipo: str) -> models.AnalisisJob | None:
    return db.execute(
        select(models.AnalisisJob)
        .where(models.AnalisisJob.cartera_id == cartera_id)
        .where(models.AnalisisJob.isin == isin)
        .where(models.AnalisisJob.tipo == tipo)
    ).scalars().first()


def _set(db: Session, cartera_id: str, isin: str, tipo: str,
         est: str, error: str | None = None) -> None:
    row = estado(db, cartera_id, isin, tipo)
    if row is None:
        db.add(models.AnalisisJob(cartera_id=cartera_id, isin=isin, tipo=tipo,
                                  estado=est, error=error))
    else:
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


def lanzar(db: Session, cartera_id: str, isin: str, tipo: str, fn: Generador) -> None:
    """Marca el job en_curso y ejecuta `fn` en un hilo con su propia sesión."""
    _set(db, cartera_id, isin, tipo, EN_CURSO)
    threading.Thread(target=_ejecutar, args=(cartera_id, isin, tipo, fn), daemon=True).start()
