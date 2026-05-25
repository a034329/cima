"""Configuración base de SQLAlchemy 2.

Single SessionLocal por proceso, Base declarativa común para todos los
modelos, helpers para FastAPI dependency injection (`get_db`) y bootstrap
del schema en startup (`init_db`).

En dev: SQLite con archivo local `cima.db`. En producción: Postgres.
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


# `check_same_thread=False` sólo aplica a SQLite — permite usar la misma
# conexión en hilos distintos (FastAPI con multi-worker). Para Postgres
# este kwarg se ignora.
_connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    # echo=settings.debug,   # imprime todas las queries — útil para depurar
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,  # objetos siguen utilizables tras commit
)


class Base(DeclarativeBase):
    """Base declarativa de SQLAlchemy 2. Heredan todos los modelos."""


def get_db() -> Generator[Session, None, None]:
    """Dependency injection para FastAPI. Cierra la sesión al terminar."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Crea las tablas si no existen + micro-migraciones de columnas nuevas.

    Idempotente. Mientras no haya Alembic, aplicamos ALTER TABLE ADD COLUMN
    para columnas añadidas a tablas ya existentes (SQLite no las crea con
    `create_all` si la tabla ya existe). En cuanto haya datos productivos,
    sustituir por Alembic.
    """
    # Import diferido para evitar ciclos en arranque
    from app.db import models  # noqa: F401 — registra todas las clases ORM
    from sqlalchemy import inspect, text

    Base.metadata.create_all(bind=engine)

    # ── Micro-migraciones (solo SQLite/dev) ────────────────────────────
    # (tabla, columna, definición SQL). Se aplican si la columna falta.
    _migraciones = [
        ("opciones", "subyacente_isin", "VARCHAR(12)"),
        ("brokers", "saldo_reportado_eur", "NUMERIC(18,4)"),
        ("brokers", "saldo_fecha", "DATE"),
        ("posiciones", "bloque_id", "VARCHAR(36)"),
        ("seguimientos", "bloque_id", "VARCHAR(36)"),
        ("bloques", "liquidez_asignada_eur", "NUMERIC(18,4)"),
        ("bloques", "rendimiento_pct", "NUMERIC(7,4)"),
        ("bloques", "en_estrategia", "BOOLEAN DEFAULT 1"),
        ("posiciones", "precio_manual_eur", "NUMERIC(18,6)"),
        ("estimaciones", "consenso_json", "TEXT"),
        ("carteras", "objetivo_if_eur", "NUMERIC(18,2) DEFAULT 300000"),
        ("carteras", "aportacion_mensual_eur", "NUMERIC(18,2) DEFAULT 0"),
    ]
    if engine.dialect.name == "sqlite":
        insp = inspect(engine)
        tablas = set(insp.get_table_names())
        with engine.begin() as conn:
            for tabla, col, definicion in _migraciones:
                if tabla not in tablas:
                    continue
                cols = {c["name"] for c in insp.get_columns(tabla)}
                if col not in cols:
                    conn.execute(text(
                        f"ALTER TABLE {tabla} ADD COLUMN {col} {definicion}"
                    ))
