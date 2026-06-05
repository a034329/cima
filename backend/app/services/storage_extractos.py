"""Persistencia de CSVs originales de broker en disco (Roadmap 1.9 CSV approach).

Cima guarda CADA extracto subido por el usuario tal cual le llega para poder
re-pasarlo a `generar_irpf.main()` cuando toque generar la declaración. El
fichero vive en `{storage_dir}/extractos/{cartera_id}/{ejercicio}/{kind}.csv`
y la fila de BD (`ExtractoArchivo`) guarda el sha256 + tamaño + ruta relativa.

Política de reemplazo: subir el mismo (cartera, ejercicio, kind) **sobreescribe**
el fichero anterior y elimina la fila vieja. El usuario maneja sólo el extracto
vigente — sin versionado por ahora.

Kinds soportados (= los que `generar_irpf.main()` mapea a CSVs concretos):
    degiro_transacciones  → DeGiro_Transacciones_{ej}.csv
    degiro_cuenta         → DeGiro_Cuenta_{ej}.csv
    ibkr                  → IBKR_Trades_{ej}.csv
    tr                    → TR_Transacciones_{ej}.csv
"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models


KIND_VALIDOS = ("degiro_transacciones", "degiro_cuenta", "ibkr", "tr")

# Nombre exacto del CSV que cada kind tiene que llevar al pasarse al motor
# de Cuádrate. El orquestador (services/cuadrate_irpf.py) usa este mapa para
# copiar/renombrar al tempdir de la sesión.
NOMBRES_CUADRATE: dict[str, str] = {
    "degiro_transacciones": "DeGiro_Transacciones_{ejercicio}.csv",
    "degiro_cuenta":        "DeGiro_Cuenta_{ejercicio}.csv",
    "ibkr":                 "IBKR_Trades_{ejercicio}.csv",
    "tr":                   "TR_Transacciones_{ejercicio}.csv",
}

# Mapeo kind ←→ broker_tipo (el broker_tipo del modelo Broker). Sirve para
# resolver el broker del usuario si existe; si no, dejamos broker_id=None y
# Cuádrate sigue funcionando (el broker se infiere del propio CSV).
KIND_A_BROKER_TIPO: dict[str, str] = {
    "degiro_transacciones": "degiro",
    "degiro_cuenta":        "degiro",
    "ibkr":                 "ibkr",
    "tr":                   "tr",
}


@dataclass
class ExtractoInfo:
    id: str
    ejercicio: int
    kind: str
    filename_original: str
    size_bytes: int
    uploaded_at: str


def _storage_root() -> Path:
    """Raíz del storage. `settings.storage_dir` puede ser absoluto o relativo
    al directorio de trabajo (backend/). Sin override → `backend/storage/`."""
    root = Path(settings.storage_dir) if settings.storage_dir else Path("storage")
    return (root / "extractos").resolve()


def _ruta_relativa(cartera_id: str, ejercicio: int, kind: str) -> str:
    """Ruta canónica del fichero dentro del storage (relativa)."""
    return f"{cartera_id}/{ejercicio}/{kind}.csv"


def ruta_absoluta(rel: str) -> Path:
    """Resuelve una `ruta_storage` (relativa, como guardada en BD) al disco."""
    return _storage_root() / rel


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolver_broker(db: Session, cartera_id: str, kind: str) -> str | None:
    """Devuelve el broker_id del Broker del user para ese kind, si existe.

    NO crea el Broker si falta — la importación normal de Cima ya lo hace.
    Es solo una conveniencia para la fila `ExtractoArchivo` (informativa).
    """
    cartera = db.get(models.Cartera, cartera_id)
    if cartera is None:
        return None
    broker_tipo = KIND_A_BROKER_TIPO.get(kind)
    if not broker_tipo:
        return None
    b = db.execute(
        select(models.Broker)
        .where(models.Broker.user_id == cartera.user_id)
        .where(models.Broker.broker_tipo == broker_tipo)
    ).scalars().first()
    return b.id if b else None


def guardar_extracto(
    db: Session, *,
    cartera_id: str,
    ejercicio: int,
    kind: str,
    filename_original: str,
    contenido: bytes,
) -> ExtractoInfo:
    """Guarda el CSV en disco y registra la fila ExtractoArchivo.

    Si ya existe un extracto para (cartera, ejercicio, kind), lo sobreescribe
    en disco y reemplaza la fila previa. Solo hay UN fichero vigente por
    combinación.

    Devuelve un ExtractoInfo con los metadatos guardados.
    """
    if kind not in KIND_VALIDOS:
        raise ValueError(
            f"kind {kind!r} no soportado. Válidos: {', '.join(KIND_VALIDOS)}"
        )
    if not (2000 <= ejercicio <= 2100):
        raise ValueError(f"ejercicio fuera de rango: {ejercicio}")
    if not contenido:
        raise ValueError("Contenido vacío")

    rel = _ruta_relativa(cartera_id, ejercicio, kind)
    destino = ruta_absoluta(rel)
    destino.parent.mkdir(parents=True, exist_ok=True)

    # Escritura atómica: a .tmp y luego rename. Si el rename falla, no
    # dejamos un fichero corrupto en la ruta canónica.
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    tmp.write_bytes(contenido)
    tmp.replace(destino)

    sha = _sha256_file(destino)
    size = destino.stat().st_size

    # Reemplazo: elimina filas previas con la misma combinación.
    db.execute(
        models.ExtractoArchivo.__table__.delete().where(
            (models.ExtractoArchivo.cartera_id == cartera_id) &
            (models.ExtractoArchivo.ejercicio == ejercicio) &
            (models.ExtractoArchivo.kind == kind)
        )
    )
    broker_id = _resolver_broker(db, cartera_id, kind)
    extracto = models.ExtractoArchivo(
        cartera_id=cartera_id,
        broker_id=broker_id,
        ejercicio=ejercicio,
        kind=kind,
        filename_original=filename_original[:255],
        ruta_storage=rel,
        sha256=sha,
        size_bytes=size,
    )
    db.add(extracto)
    db.flush()
    return ExtractoInfo(
        id=extracto.id,
        ejercicio=extracto.ejercicio,
        kind=extracto.kind,
        filename_original=extracto.filename_original,
        size_bytes=extracto.size_bytes,
        uploaded_at=extracto.uploaded_at.isoformat(),
    )


def listar_extractos(db: Session, cartera_id: str,
                     ejercicio: int | None = None) -> list[ExtractoInfo]:
    """Extractos vigentes de la cartera, opcionalmente filtrados por ejercicio."""
    q = (select(models.ExtractoArchivo)
         .where(models.ExtractoArchivo.cartera_id == cartera_id))
    if ejercicio is not None:
        q = q.where(models.ExtractoArchivo.ejercicio == ejercicio)
    q = q.order_by(models.ExtractoArchivo.ejercicio.desc(),
                   models.ExtractoArchivo.kind)
    return [
        ExtractoInfo(
            id=e.id, ejercicio=e.ejercicio, kind=e.kind,
            filename_original=e.filename_original, size_bytes=e.size_bytes,
            uploaded_at=e.uploaded_at.isoformat(),
        )
        for e in db.execute(q).scalars().all()
    ]


def eliminar_extracto(db: Session, cartera_id: str, extracto_id: str) -> bool:
    """Borra el fichero del disco y la fila. True si existía y se borró."""
    row = db.execute(
        select(models.ExtractoArchivo)
        .where(models.ExtractoArchivo.id == extracto_id)
        .where(models.ExtractoArchivo.cartera_id == cartera_id)
    ).scalars().first()
    if row is None:
        return False
    fpath = ruta_absoluta(row.ruta_storage)
    if fpath.exists():
        try:
            fpath.unlink()
        except OSError:
            pass
    db.delete(row)
    db.flush()
    return True


def materializar_para_ejercicio(
    db: Session, cartera_id: str, ejercicio: int, destino: Path,
) -> dict[str, str]:
    """Copia todos los extractos vigentes del ejercicio al directorio
    `destino` con los nombres EXACTOS que `generar_irpf.main()` busca.

    Devuelve un dict `{kind: nombre_destino}` con lo que efectivamente se
    materializó (útil para logs/avisos). El llamador (orquestador IRPF)
    pasa después `destino` como `--base-path` al subprocess.
    """
    destino.mkdir(parents=True, exist_ok=True)
    extractos = listar_extractos(db, cartera_id, ejercicio)
    materializados: dict[str, str] = {}
    for info in extractos:
        # `listar_extractos` devuelve ExtractoInfo (sin ruta) → recargamos
        # la fila para acceder a `ruta_storage`.
        row = db.get(models.ExtractoArchivo, info.id)
        if row is None:
            continue
        src = ruta_absoluta(row.ruta_storage)
        if not src.exists():
            continue
        nombre_dst = NOMBRES_CUADRATE[info.kind].format(ejercicio=ejercicio)
        shutil.copy2(src, destino / nombre_dst)
        materializados[info.kind] = nombre_dst
    return materializados
