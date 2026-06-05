"""Tests del servicio de persistencia de CSVs originales (Roadmap 1.9)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models
from app.services import storage_extractos as st


@pytest.fixture(autouse=True)
def storage_temporal(tmp_path: Path, monkeypatch):
    """Cada test usa su propio storage_dir aislado bajo tmp_path."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    yield tmp_path


def test_guardar_extracto_escribe_fichero_y_fila(
    db: Session, cartera: models.Cartera,
) -> None:
    info = st.guardar_extracto(
        db, cartera_id=cartera.id, ejercicio=2025,
        kind="degiro_transacciones",
        filename_original="DeGiro.csv",
        contenido=b"col1,col2\n1,2\n",
    )
    db.commit()
    assert info.kind == "degiro_transacciones"
    assert info.size_bytes == len(b"col1,col2\n1,2\n")
    # El fichero existe en disco con la ruta esperada
    fila = db.get(models.ExtractoArchivo, info.id)
    assert fila is not None
    assert st.ruta_absoluta(fila.ruta_storage).exists()


def test_guardar_extracto_reemplaza_el_existente(
    db: Session, cartera: models.Cartera,
) -> None:
    """Subir el mismo (cartera, ejercicio, kind) sobrescribe el anterior."""
    primero = st.guardar_extracto(
        db, cartera_id=cartera.id, ejercicio=2025,
        kind="ibkr", filename_original="ibkr_v1.csv",
        contenido=b"v1\n",
    )
    db.commit()

    segundo = st.guardar_extracto(
        db, cartera_id=cartera.id, ejercicio=2025,
        kind="ibkr", filename_original="ibkr_v2.csv",
        contenido=b"v2_mucho_mas_largo\n",
    )
    db.commit()

    # La fila vieja se borró
    assert db.get(models.ExtractoArchivo, primero.id) is None
    # La fila nueva tiene el nuevo contenido
    fila_nueva = db.get(models.ExtractoArchivo, segundo.id)
    assert fila_nueva is not None
    assert fila_nueva.size_bytes > 5
    assert st.ruta_absoluta(fila_nueva.ruta_storage).read_bytes() == b"v2_mucho_mas_largo\n"


def test_listar_extractos_filtra_por_ejercicio(
    db: Session, cartera: models.Cartera,
) -> None:
    st.guardar_extracto(db, cartera_id=cartera.id, ejercicio=2024,
                        kind="ibkr", filename_original="a.csv", contenido=b"a\n")
    st.guardar_extracto(db, cartera_id=cartera.id, ejercicio=2025,
                        kind="ibkr", filename_original="b.csv", contenido=b"b\n")
    st.guardar_extracto(db, cartera_id=cartera.id, ejercicio=2025,
                        kind="tr", filename_original="c.csv", contenido=b"c\n")
    db.commit()

    todos = st.listar_extractos(db, cartera.id)
    assert len(todos) == 3

    solo_2025 = st.listar_extractos(db, cartera.id, ejercicio=2025)
    assert len(solo_2025) == 2
    assert {e.kind for e in solo_2025} == {"ibkr", "tr"}


def test_eliminar_extracto_borra_disco_y_fila(
    db: Session, cartera: models.Cartera,
) -> None:
    info = st.guardar_extracto(
        db, cartera_id=cartera.id, ejercicio=2025,
        kind="tr", filename_original="tr.csv", contenido=b"x\n",
    )
    db.commit()
    fila = db.get(models.ExtractoArchivo, info.id)
    fpath = st.ruta_absoluta(fila.ruta_storage)
    assert fpath.exists()

    assert st.eliminar_extracto(db, cartera.id, info.id) is True
    db.commit()
    assert db.get(models.ExtractoArchivo, info.id) is None
    assert not fpath.exists()


def test_eliminar_inexistente_devuelve_false(
    db: Session, cartera: models.Cartera,
) -> None:
    assert st.eliminar_extracto(db, cartera.id, "00000000-0000-0000-0000-000000000000") is False


def test_kind_invalido_rechazado(db: Session, cartera: models.Cartera) -> None:
    with pytest.raises(ValueError, match="kind"):
        st.guardar_extracto(
            db, cartera_id=cartera.id, ejercicio=2025,
            kind="inventado", filename_original="x.csv", contenido=b"x\n",
        )


def test_ejercicio_fuera_de_rango_rechazado(
    db: Session, cartera: models.Cartera,
) -> None:
    with pytest.raises(ValueError, match="ejercicio"):
        st.guardar_extracto(
            db, cartera_id=cartera.id, ejercicio=1900,
            kind="ibkr", filename_original="x.csv", contenido=b"x\n",
        )


def test_materializar_renombra_a_nombres_cuadrate(
    db: Session, cartera: models.Cartera, tmp_path: Path,
) -> None:
    """`materializar_para_ejercicio` copia los CSVs al tempdir con los nombres
    EXACTOS que `generar_irpf.main()` busca."""
    st.guardar_extracto(db, cartera_id=cartera.id, ejercicio=2025,
                        kind="degiro_transacciones",
                        filename_original="cualquier_nombre_user.csv",
                        contenido=b"linea_dg\n")
    st.guardar_extracto(db, cartera_id=cartera.id, ejercicio=2025,
                        kind="ibkr", filename_original="otro_nombre.csv",
                        contenido=b"linea_ibkr\n")
    db.commit()

    destino = tmp_path / "sesion_irpf"
    materializados = st.materializar_para_ejercicio(db, cartera.id, 2025, destino)

    assert materializados == {
        "degiro_transacciones": "DeGiro_Transacciones_2025.csv",
        "ibkr": "IBKR_Trades_2025.csv",
    }
    # Los ficheros existen físicamente con el nombre correcto
    assert (destino / "DeGiro_Transacciones_2025.csv").read_bytes() == b"linea_dg\n"
    assert (destino / "IBKR_Trades_2025.csv").read_bytes() == b"linea_ibkr\n"


def test_resolver_broker_enlaza_si_existe(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker,
) -> None:
    """Si el user tiene Broker tipo 'tr', el ExtractoArchivo de kind='tr'
    debe enlazar con su broker_id."""
    info = st.guardar_extracto(
        db, cartera_id=cartera.id, ejercicio=2025,
        kind="tr", filename_original="tr.csv", contenido=b"x\n",
    )
    db.commit()
    fila = db.get(models.ExtractoArchivo, info.id)
    assert fila.broker_id == broker_tr.id
