"""Endpoint de generación de la declaración IRPF estilo Cuádrate (Roadmap 1.9).

Cima invoca el `generar_irpf.py` vendorizado como subprocess sobre los CSVs
ORIGINALES guardados por el usuario (storage_extractos) y devuelve un ZIP con
todos los entregables (XLSX maestro + 4 informes + sidecars JSON).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import cuadrate_irpf as svc


router = APIRouter(prefix="/cuadrate", tags=["cuadrate"])


@router.get(
    "/irpf/{ejercicio}.zip",
    summary="Genera y descarga la declaración IRPF completa (XLSX + informes) del ejercicio",
    response_class=FileResponse,
)
def generar_irpf_zip(
    ejercicio: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    cartera: models.Cartera = Depends(get_current_cartera),
) -> FileResponse:
    """Devuelve `cartera_irpf_{ejercicio}.zip` con todos los entregables que
    produce el motor de Cuádrate. Requiere haber subido previamente los CSVs
    del broker para ese ejercicio en POST /api/import indicando `ejercicio`.

    Tras la entrega, el tempdir (CSVs + outputs) se limpia en background.
    """
    ahora = date.today().year
    if ejercicio < 2000 or ejercicio > ahora:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Ejercicio fuera de rango (esperado 2000..{ahora}): {ejercicio}",
        )

    cartera_id = cartera.id     # fuera del try: deja propagar el 404
    try:
        resultado = svc.generar_irpf_zip(db, cartera_id, ejercicio)
    except svc.DependenciasFaltantesError as e:
        # 503: el SERVIDOR está mal aprovisionado — no es culpa del request.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    except svc.SinExtractosError as e:
        # 422: el cliente debe subir los CSVs primero — request bien formado
        # pero estado incompatible.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except svc.GenerarIRPFError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
    except HTTPException:
        raise

    background.add_task(svc.limpiar, resultado)
    return FileResponse(
        path=str(resultado.zip_path),
        media_type="application/zip",
        filename=resultado.zip_path.name,
        headers={
            # Diagnóstico útil para el frontend (qué entró, qué salió).
            "X-Cima-Irpf-Kinds":     ",".join(resultado.kinds_usados),
            "X-Cima-Irpf-Ficheros":  ",".join(resultado.ficheros_generados),
        },
    )
