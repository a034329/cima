"""Endpoints de bloques de estrategia (distribución, asignación, CRUD)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError
from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import bloques as svc
from app.services import clasificador as clasif_svc


router = APIRouter(prefix="/bloques", tags=["bloques"])


def _q2(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _q4(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP)


# ── Schemas ────────────────────────────────────────────────────────────────

class BloqueDistOut(BaseModel):
    id: str
    nombre: str
    categoria_base: str
    orden: int
    es_base: bool
    en_estrategia: bool
    valor_eur: Decimal = Field(decimal_places=2)
    peso_actual: Decimal = Field(decimal_places=4)
    n_posiciones: int
    liquidez_asignada_eur: Decimal = Field(decimal_places=2)
    rendimiento_pct: Decimal | None = None
    peso_objetivo: Decimal | None = None
    tolerancia: Decimal = Field(decimal_places=4)
    desviacion: Decimal | None = None
    fuera_tolerancia: bool
    cagr4_div_pct: Decimal | None = None        # CAGR4+Div proyectado ponderado del bloque
    cobertura_estimacion: Decimal | None = None  # fracción del valor del bloque con estimación
    n_con_estimacion: int = 0                    # posiciones del bloque con CAGR estimado


class DistribucionOut(BaseModel):
    total_eur: Decimal = Field(decimal_places=2)
    liquidez_disponible_eur: Decimal = Field(decimal_places=2)
    bloques: list[BloqueDistOut]


class PosicionBloqueOut(BaseModel):
    isin: str
    nombre: str
    valor_eur: Decimal = Field(decimal_places=2)
    bloque_id: str | None


class AsignarIn(BaseModel):
    isin: str
    bloque_id: str | None = None     # None o 'sin_clasificar' → saco
    # Opcionales: lo que sugirió la IA + por qué el usuario decide distinto.
    # Si categoria_sugerida ≠ la del bloque elegido → se registra override.
    categoria_sugerida: str | None = None
    confianza_ia: float | None = None
    razon: str | None = None


class CrearBloqueIn(BaseModel):
    nombre: str
    categoria_base: str


class EditarBloqueIn(BaseModel):
    nombre: str | None = None
    categoria_base: str | None = None
    liquidez_asignada_eur: Decimal | None = None
    rendimiento_pct: Decimal | None = None
    peso_objetivo: Decimal | None = None
    tolerancia: Decimal | None = None
    en_estrategia: bool | None = None


class BloqueOut(BaseModel):
    id: str
    nombre: str
    categoria_base: str
    orden: int
    es_base: bool


class SugerirIn(BaseModel):
    isin: str


class SugerenciaBloqueOut(BaseModel):
    """Sugerencia de la IA. Solo informa; el usuario aplica con PUT /asignar."""
    isin: str | None = None
    categoria_base: str
    bloque_id: str | None
    razonamiento: str
    confianza: float
    modelo: str
    proveedor: str
    distribucion: list[dict] | None = None


class AutoclasificarIn(BaseModel):
    solo_sin_clasificar: bool = True
    isines: list[str] | None = None     # si se da, clasifica solo ese batch


class CriterioCheckOut(BaseModel):
    etiqueta: str
    valor: float | None = None
    valor_txt: str
    objetivo_txt: str
    cumple: bool | None = None          # None = dato no disponible


class EvaluacionOut(BaseModel):
    """Encaje de un candidato: en qué bloque cae (IA) y si cumple sus criterios."""
    isin: str
    nombre: str
    categoria_sugerida: str
    confianza: float
    razonamiento: str
    criterios_texto: str
    checks: list[CriterioCheckOut]
    n_cumplidos: int
    n_medibles: int
    veredicto: str
    cualitativo: str
    target_categoria: str | None = None
    cubre_target: bool | None = None


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("", response_model=DistribucionOut,
            summary="Distribución actual de la cartera por bloque")
def get_distribucion(db: Session = Depends(get_db),
                     cartera: models.Cartera = Depends(get_current_cartera)) -> DistribucionOut:
    from app.services.estimaciones import agregado_por_bloque

    cid = cartera.id
    r = svc.calcular_distribucion(db, cid)
    # CAGR4+Div proyectado por bloque (posiciones sin clasificar → clave None).
    aggs = agregado_por_bloque(db, cid)

    def _agg(bloque_id: str):  # type: ignore[no-untyped-def]
        return aggs.get(None if bloque_id == svc.SIN_CLASIFICAR_ID else bloque_id)

    return DistribucionOut(
        total_eur=_q2(r.total_eur),
        liquidez_disponible_eur=_q2(r.liquidez_disponible_eur),
        bloques=[
            BloqueDistOut(
                id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
                orden=b.orden, es_base=b.es_base, en_estrategia=b.en_estrategia,
                valor_eur=_q2(b.valor_eur),
                peso_actual=_q4(b.peso_actual), n_posiciones=b.n_posiciones,
                liquidez_asignada_eur=_q2(b.liquidez_asignada_eur),
                rendimiento_pct=(
                    _q4(b.rendimiento_pct) if b.rendimiento_pct is not None else None
                ),
                peso_objetivo=(
                    _q4(b.peso_objetivo) if b.peso_objetivo is not None else None
                ),
                tolerancia=_q4(b.tolerancia),
                desviacion=(_q4(b.desviacion) if b.desviacion is not None else None),
                fuera_tolerancia=b.fuera_tolerancia,
                cagr4_div_pct=(_q4(a.cagr4_div_pct) if (a := _agg(b.id)) and a.cagr4_div_pct is not None else None),
                cobertura_estimacion=(_q4(a.cobertura) if (a := _agg(b.id)) and a.cobertura is not None else None),
                n_con_estimacion=((a := _agg(b.id)) and a.n_con_estimacion) or 0,
            )
            for b in r.bloques
        ],
    )


@router.get("/posiciones", response_model=list[PosicionBloqueOut],
            summary="Posiciones abiertas con su bloque asignado (para asignar)")
def get_posiciones(db: Session = Depends(get_db),
                   cartera: models.Cartera = Depends(get_current_cartera)) -> list[PosicionBloqueOut]:
    return [
        PosicionBloqueOut(
            isin=p.isin, nombre=p.nombre, valor_eur=_q2(p.valor_eur),
            bloque_id=p.bloque_id,
        )
        for p in svc.listar_posiciones(db, cartera.id)
    ]


@router.put("/asignar", status_code=status.HTTP_204_NO_CONTENT,
            summary="Asignar una posición a un bloque (o a 'Sin clasificar')")
def asignar(payload: AsignarIn, db: Session = Depends(get_db),
            cartera: models.Cartera = Depends(get_current_cartera)) -> None:
    svc.asignar_bloque(
        db, cartera.id, payload.isin, payload.bloque_id,
        categoria_sugerida=payload.categoria_sugerida,
        confianza_ia=payload.confianza_ia, razon=payload.razon,
    )


def _sug_out(s) -> SugerenciaBloqueOut:  # type: ignore[no-untyped-def]
    return SugerenciaBloqueOut(
        isin=s.isin, categoria_base=s.categoria_base, bloque_id=s.bloque_id,
        razonamiento=s.razonamiento, confianza=s.confianza,
        modelo=s.modelo, proveedor=s.proveedor, distribucion=s.distribucion,
    )


@router.post("/sugerir", response_model=SugerenciaBloqueOut,
             summary="Sugerir bloque para una posición (IA). El usuario decide.")
def sugerir(payload: SugerirIn, db: Session = Depends(get_db),
            cartera: models.Cartera = Depends(get_current_cartera)) -> SugerenciaBloqueOut:
    try:
        s = clasif_svc.sugerir(db, cartera.id, payload.isin)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Clasificador IA: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return _sug_out(s)


@router.get("/evaluar/{isin}", response_model=EvaluacionOut,
            summary="Evaluar un candidato: ¿en qué bloque encaja y cumple sus criterios? "
                    "(target opcional = el bloque cuyo déficit quieres llenar)")
def evaluar(isin: str, target: str | None = None,
            db: Session = Depends(get_db),
            cartera: models.Cartera = Depends(get_current_cartera)) -> EvaluacionOut:
    try:
        ev = clasif_svc.evaluar_candidato(db, cartera.id, isin, target)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Clasificador IA: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return EvaluacionOut(
        isin=ev.isin, nombre=ev.nombre, categoria_sugerida=ev.categoria_sugerida,
        confianza=ev.confianza, razonamiento=ev.razonamiento,
        criterios_texto=ev.criterios_texto,
        checks=[
            CriterioCheckOut(etiqueta=c.etiqueta, valor=c.valor, valor_txt=c.valor_txt,
                             objetivo_txt=c.objetivo_txt, cumple=c.cumple)
            for c in ev.checks
        ],
        n_cumplidos=ev.n_cumplidos, n_medibles=ev.n_medibles, veredicto=ev.veredicto,
        cualitativo=ev.cualitativo, target_categoria=ev.target_categoria,
        cubre_target=ev.cubre_target,
    )


@router.post("/autoclasificar", response_model=list[SugerenciaBloqueOut],
             summary="Autoclasificar en lote las posiciones (IA). Menos preciso, "
                     "más barato. El usuario revisa y aplica.")
def autoclasificar(payload: AutoclasificarIn | None = None,
                   db: Session = Depends(get_db),
                   cartera: models.Cartera = Depends(get_current_cartera)) -> list[SugerenciaBloqueOut]:
    solo = payload.solo_sin_clasificar if payload else True
    isines = payload.isines if payload else None
    try:
        sugs = clasif_svc.autoclasificar(db, cartera.id, solo, isines)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Clasificador IA: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return [_sug_out(s) for s in sugs]


@router.post("", response_model=BloqueOut, status_code=status.HTTP_201_CREATED,
             summary="Crear un bloque personalizado (tope 8)")
def crear(payload: CrearBloqueIn, db: Session = Depends(get_db),
          cartera: models.Cartera = Depends(get_current_cartera)) -> BloqueOut:
    b = svc.crear_bloque(db, cartera.id, payload.nombre, payload.categoria_base)
    return BloqueOut(id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
                     orden=b.orden, es_base=b.es_base)


@router.patch("/{bloque_id}", response_model=BloqueOut,
              summary="Renombrar o recategorizar un bloque")
def editar(bloque_id: str, payload: EditarBloqueIn,
           db: Session = Depends(get_db),
           cartera: models.Cartera = Depends(get_current_cartera)) -> BloqueOut:
    enviados = payload.model_dump(exclude_unset=True)
    b = svc.editar_bloque(
        db, cartera.id, bloque_id,
        payload.nombre, payload.categoria_base,
        liquidez_asignada_eur=payload.liquidez_asignada_eur,
        rendimiento_pct=payload.rendimiento_pct,
        set_liquidez="liquidez_asignada_eur" in enviados,
        set_rendimiento="rendimiento_pct" in enviados,
        peso_objetivo=payload.peso_objetivo,
        set_peso="peso_objetivo" in enviados,
        tolerancia=payload.tolerancia,
        en_estrategia=payload.en_estrategia,
    )
    return BloqueOut(id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
                     orden=b.orden, es_base=b.es_base)


@router.delete("/{bloque_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Eliminar un bloque (sus posiciones → Sin clasificar)")
def eliminar(bloque_id: str, db: Session = Depends(get_db),
             cartera: models.Cartera = Depends(get_current_cartera)) -> None:
    svc.eliminar_bloque(db, cartera.id, bloque_id)
