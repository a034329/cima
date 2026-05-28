"""Endpoints del plan por valor (decisión por posición)."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import plan as svc


router = APIRouter(prefix="/plan", tags=["plan"])


def _q2(x) -> Decimal | None:  # type: ignore[no-untyped-def]
    if x is None:
        return None
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _q4(x) -> Decimal | None:  # type: ignore[no-untyped-def]
    if x is None:
        return None
    return Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP)


# ── Schemas ────────────────────────────────────────────────────────────────

class PasoOut(BaseModel):
    id: str
    isin: str
    decision: str
    prioridad: str
    estado: str
    capital_objetivo_eur: Decimal | None = None
    razon: str | None = None
    fecha_objetivo: date | None = None
    notas: str | None = None
    orden: int


class PosicionPlanOut(BaseModel):
    isin: str
    nombre: str
    valor_eur: Decimal = Field(decimal_places=2)
    bloque_id: str | None
    bloque_nombre: str | None
    decision: str
    capital_objetivo_eur: Decimal | None = None
    razon: str | None = None
    prioridad: str | None = None
    paso_id: str | None = None
    en_cartera: bool = True


class HuecoBloqueOut(BaseModel):
    bloque_id: str
    nombre: str
    categoria_base: str
    objetivo_pct: Decimal | None = Field(default=None, decimal_places=4)
    actual_pct: Decimal = Field(decimal_places=4)
    planeado_pct: Decimal = Field(decimal_places=4)
    proyectado_pct: Decimal = Field(decimal_places=4)
    deficit_pct: Decimal | None = Field(default=None, decimal_places=4)
    valor_actual_eur: Decimal = Field(decimal_places=2)
    planeado_eur: Decimal = Field(decimal_places=2)
    deficit_eur: Decimal | None = Field(default=None, decimal_places=2)
    criterios: str = ""


class HuecoOut(BaseModel):
    total_actual_eur: Decimal = Field(decimal_places=2)
    total_planeado_eur: Decimal = Field(decimal_places=2)
    total_proyectado_eur: Decimal = Field(decimal_places=2)
    sin_clasificar_planeado_eur: Decimal = Field(decimal_places=2)
    bloques: list[HuecoBloqueOut]


class CrearPasoIn(BaseModel):
    isin: str
    decision: str
    prioridad: str = "MEDIA"
    razon: str | None = None
    capital_objetivo_eur: Decimal | None = None
    fecha_objetivo: date | None = None
    notas: str | None = None
    # Fricción: si el paso se crea tras rebatir un aviso, se registra el override.
    friccion_severidad: str | None = None
    friccion_motivo: str | None = None


class FriccionIn(BaseModel):
    isin: str
    decision: str


class FriccionOut(BaseModel):
    severidad: str
    titulo: str
    rebate1: str
    rebate2: str
    etiquetas: list[str]


class EditarPasoIn(BaseModel):
    decision: str | None = None
    prioridad: str | None = None
    estado: str | None = None
    razon: str | None = None
    capital_objetivo_eur: Decimal | None = None
    fecha_objetivo: date | None = None
    notas: str | None = None


def _cartera(db: Session) -> models.Cartera:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return cartera


def _to_paso_out(p: models.PlanPaso) -> PasoOut:
    return PasoOut(
        id=p.id, isin=p.isin, decision=p.decision, prioridad=p.prioridad,
        estado=p.estado, capital_objetivo_eur=_q2(p.capital_objetivo_eur),
        razon=p.razon, fecha_objetivo=p.fecha_objetivo, notas=p.notas, orden=p.orden,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("", response_model=list[PasoOut], summary="Cola de pasos del plan")
def get_pasos(estado: str | None = None, db: Session = Depends(get_db)) -> list[PasoOut]:
    return [_to_paso_out(p) for p in svc.listar_pasos(db, _cartera(db).id, estado)]


@router.get("/posiciones", response_model=list[PosicionPlanOut],
            summary="Posiciones abiertas con su decisión activa + bloque")
def get_posiciones(db: Session = Depends(get_db)) -> list[PosicionPlanOut]:
    return [
        PosicionPlanOut(
            isin=p.isin, nombre=p.nombre, valor_eur=_q2(p.valor_eur),  # type: ignore[arg-type]
            bloque_id=p.bloque_id, bloque_nombre=p.bloque_nombre,
            decision=p.decision, capital_objetivo_eur=_q2(p.capital_objetivo_eur),
            razon=p.razon, prioridad=p.prioridad, paso_id=p.paso_id,
            en_cartera=p.en_cartera,
        )
        for p in svc.posiciones_con_plan(db, _cartera(db).id)
    ]


@router.get("/hueco", response_model=HuecoOut,
            summary="Hueco de asignación: cuánto falta comprar por bloque (top-down)")
def get_hueco(db: Session = Depends(get_db)) -> HuecoOut:
    r = svc.hueco_asignacion(db, _cartera(db).id)
    return HuecoOut(
        total_actual_eur=_q2(r.total_actual_eur),                # type: ignore[arg-type]
        total_planeado_eur=_q2(r.total_planeado_eur),            # type: ignore[arg-type]
        total_proyectado_eur=_q2(r.total_proyectado_eur),        # type: ignore[arg-type]
        sin_clasificar_planeado_eur=_q2(r.sin_clasificar_planeado_eur),  # type: ignore[arg-type]
        bloques=[
            HuecoBloqueOut(
                bloque_id=b.bloque_id, nombre=b.nombre, categoria_base=b.categoria_base,
                objetivo_pct=_q4(b.objetivo_pct), actual_pct=_q4(b.actual_pct),
                planeado_pct=_q4(b.planeado_pct), proyectado_pct=_q4(b.proyectado_pct),
                deficit_pct=_q4(b.deficit_pct), valor_actual_eur=_q2(b.valor_actual_eur),
                planeado_eur=_q2(b.planeado_eur), deficit_eur=_q2(b.deficit_eur),
                criterios=b.criterios,
            )
            for b in r.bloques
        ],
    )


@router.post("/evaluar-friccion", response_model=FriccionOut | None,
             summary="¿Esta decisión (VENDER/RECORTAR) merece fricción? null = deja pasar")
def evaluar_friccion(payload: FriccionIn, db: Session = Depends(get_db)) -> FriccionOut | None:
    from app.services import friccion
    r = friccion.evaluar_friccion(db, _cartera(db).id, payload.isin, payload.decision)
    if r is None:
        return None
    return FriccionOut(severidad=r.severidad, titulo=r.titulo, rebate1=r.rebate1,
                       rebate2=r.rebate2, etiquetas=r.etiquetas)


@router.post("", response_model=PasoOut, status_code=status.HTTP_201_CREATED,
             summary="Crear un paso del plan para una posición")
def crear(payload: CrearPasoIn, db: Session = Depends(get_db)) -> PasoOut:
    cid = _cartera(db).id
    p = svc.crear_paso(
        db, cid, payload.isin, payload.decision, payload.prioridad,
        razon=payload.razon, capital_objetivo_eur=payload.capital_objetivo_eur,
        fecha_objetivo=payload.fecha_objetivo, notas=payload.notas,
    )
    # Si el paso se creó tras rebatir una fricción, registrar el override.
    if payload.friccion_severidad:
        from app.services import friccion
        friccion.registrar_evento(db, cid, payload.isin, payload.decision,
                                  payload.friccion_severidad, payload.friccion_motivo)
        db.commit()
    return _to_paso_out(p)


@router.patch("/{paso_id}", response_model=PasoOut, summary="Editar un paso")
def editar(paso_id: str, payload: EditarPasoIn,
           db: Session = Depends(get_db)) -> PasoOut:
    campos = payload.model_dump(exclude_unset=True)
    p = svc.actualizar_paso(db, _cartera(db).id, paso_id, **campos)
    return _to_paso_out(p)


@router.delete("/{paso_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Eliminar un paso")
def eliminar(paso_id: str, db: Session = Depends(get_db)) -> None:
    svc.eliminar_paso(db, _cartera(db).id, paso_id)
