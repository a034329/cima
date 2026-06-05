"""Endpoint de generación del XLSX maestro IRPF (Roadmap 1.9).

Cima genera 'in-process' la declaración usando el motor + generador XLSX
vendorizado desde Cuádrate. No depende de los CSVs originales del broker:
reconstruye las operaciones desde la BD de Cima vía `cuadrate_irpf`.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import cuadrate_irpf as svc


router = APIRouter(prefix="/cuadrate", tags=["cuadrate"])


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


@router.get(
    "/irpf/{ejercicio}.xlsx",
    summary="Genera y descarga el XLSX maestro IRPF (estilo Cuádrate) del ejercicio",
    response_class=FileResponse,
)
def generar_irpf_xlsx(
    ejercicio: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Devuelve `cartera_valores_irpf_{ejercicio}.xlsx`. Tras la entrega, el
    fichero temporal se limpia en background.

    MVP: cubre operaciones BUY/SELL/SP + FIFO + Resumen. Las hojas opcionales
    (dividendos por país con CDI, opciones por contrato, forex, T-Bills,
    intereses, staking, gastos plataforma) se rellenarán en iteraciones
    siguientes; mientras tanto aparecen vacías o se omiten.
    """
    ahora = date.today().year
    if ejercicio < 2000 or ejercicio > ahora:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Ejercicio fuera de rango (esperado 2000..{ahora}): {ejercicio}",
        )

    cartera_id = _cartera(db).id     # fuera del try: deja propagar el 404
    try:
        out_path = svc.generar_xlsx(db, cartera_id, ejercicio)
    except RuntimeError as e:
        # Motor vendorizado no disponible (despliegue sin vendor/).
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except HTTPException:
        raise
    except Exception as e:   # pragma: no cover — defensivo
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"Error generando XLSX IRPF: {e}")

    # Limpieza diferida: el directorio temporal se borra tras enviar el fichero.
    background.add_task(_cleanup, out_path)
    return FileResponse(
        path=str(out_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=out_path.name,
    )


def _cleanup(path) -> None:
    """Borra el fichero y su directorio temporal padre. Best-effort."""
    import shutil
    try:
        if path.exists():
            shutil.rmtree(path.parent, ignore_errors=True)
    except Exception:
        pass
