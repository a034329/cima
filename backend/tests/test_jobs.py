"""Tests del mecanismo de jobs en segundo plano (estado + worker síncrono)."""
from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.services import jobs


def test_set_y_estado(db: Session, cartera) -> None:
    assert jobs.estado(db, cartera.id, "US_X", "one_pager") is None
    jobs._set(db, cartera.id, "US_X", "one_pager", jobs.EN_CURSO)
    assert jobs.estado(db, cartera.id, "US_X", "one_pager").estado == jobs.EN_CURSO
    jobs._set(db, cartera.id, "US_X", "one_pager", jobs.OK)
    j = jobs.estado(db, cartera.id, "US_X", "one_pager")
    assert j.estado == jobs.OK and j.error is None


def test_ejecutar_ok(db: Session, cartera) -> None:
    SM = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)
    ran: list[str] = []
    jobs._ejecutar(cartera.id, "US_X", "one_pager",
                   lambda s, c, i: ran.append(i), session_factory=SM)
    assert ran == ["US_X"]
    assert jobs.estado(db, cartera.id, "US_X", "one_pager").estado == jobs.OK


def test_ejecutar_error_captura_mensaje(db: Session, cartera) -> None:
    SM = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)

    def _boom(s, c, i):  # type: ignore[no-untyped-def]
        raise ValueError("solo para PER")

    jobs._ejecutar(cartera.id, "US_Y", "valoracion", _boom, session_factory=SM)
    j = jobs.estado(db, cartera.id, "US_Y", "valoracion")
    assert j.estado == jobs.ERROR and "PER" in (j.error or "")
