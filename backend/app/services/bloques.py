"""Bloques de estrategia: distribución actual, asignación y CRUD.

Iteración 1: solo distribución actual (sin pesos objetivo — llegan con el
Plan). El valor de cada posición se toma del coste FIFO (no hay feed de
precios todavía), igual que el endpoint de cartera. Las posiciones sin bloque
(bloque_id NULL) caen en el saco 'Sin clasificar'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import estado_posicion


TOPE_BLOQUES = 12
SIN_CLASIFICAR_ID = "sin_clasificar"


@dataclass
class BloqueDist:
    id: str
    nombre: str
    categoria_base: str
    orden: int
    es_base: bool
    en_estrategia: bool                      # ¿cuenta para el objetivo de IF?
    valor_eur: Decimal                       # posiciones + efectivo asignado
    peso_actual: Decimal
    n_posiciones: int
    liquidez_asignada_eur: Decimal           # efectivo (colchón)
    rendimiento_pct: Decimal | None          # fracción (0.0325 = 3,25%)
    peso_objetivo: Decimal | None            # fracción; None = sin objetivo
    tolerancia: Decimal                      # fracción (default 0.05 = ±5%)
    desviacion: Decimal | None               # peso_actual − peso_objetivo
    fuera_tolerancia: bool                   # |desviacion| > tolerancia


@dataclass
class DistribucionResultado:
    total_eur: Decimal
    liquidez_disponible_eur: Decimal         # de calcular_liquidez (referencia UI)
    bloques: list[BloqueDist] = field(default_factory=list)


@dataclass
class PosicionBloque:
    isin: str
    nombre: str
    valor_eur: Decimal
    bloque_id: str | None


def _valor_posiciones_abiertas(
    db: Session, cartera_id: str
) -> list[tuple[models.Posicion, Decimal]]:
    """Posiciones con cantidad > 0 y su valor (coste FIFO en EUR)."""
    posiciones = list(db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars())
    out: list[tuple[models.Posicion, Decimal]] = []
    for pos in posiciones:
        est = estado_posicion(db, pos.id)
        if est["cantidad"] <= 0:
            continue
        out.append((pos, Decimal(str(est["coste_total_eur"]))))
    return out


def calcular_distribucion(db: Session, cartera_id: str) -> DistribucionResultado:
    bloques = list(db.execute(
        select(models.Bloque)
        .where(models.Bloque.cartera_id == cartera_id)
        .order_by(models.Bloque.orden)
    ).scalars())

    valor: dict[str | None, Decimal] = {}
    n_pos: dict[str | None, int] = {}
    total = Decimal("0")
    bloque_ids = {b.id for b in bloques}
    for pos, v in _valor_posiciones_abiertas(db, cartera_id):
        # Si el bloque fue borrado, tratar como sin clasificar.
        clave = pos.bloque_id if pos.bloque_id in bloque_ids else None
        valor[clave] = valor.get(clave, Decimal("0")) + v
        n_pos[clave] = n_pos.get(clave, 0) + 1
        total += v

    # El efectivo asignado (colchón) cuenta en el valor del bloque y en el total.
    liquidez_bloque = {
        b.id: Decimal(str(b.liquidez_asignada_eur))
        for b in bloques if b.liquidez_asignada_eur
    }
    total += sum(liquidez_bloque.values(), Decimal("0"))

    def peso(v: Decimal) -> Decimal:
        return (v / total) if total > 0 else Decimal("0")

    out: list[BloqueDist] = []
    for b in bloques:
        liq = liquidez_bloque.get(b.id, Decimal("0"))
        v = valor.get(b.id, Decimal("0")) + liq
        pa = peso(v)
        objetivo = Decimal(str(b.peso_objetivo)) if b.peso_objetivo is not None else None
        tol = Decimal(str(b.tolerancia)) if b.tolerancia is not None else Decimal("0.05")
        desv = (pa - objetivo) if objetivo is not None else None
        fuera = bool(desv is not None and abs(desv) > tol)
        out.append(BloqueDist(
            id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
            orden=b.orden, es_base=b.es_base, en_estrategia=b.en_estrategia, valor_eur=v,
            peso_actual=pa, n_posiciones=n_pos.get(b.id, 0),
            liquidez_asignada_eur=liq,
            rendimiento_pct=(
                Decimal(str(b.rendimiento_pct)) if b.rendimiento_pct is not None else None
            ),
            peso_objetivo=objetivo, tolerancia=tol,
            desviacion=desv, fuera_tolerancia=fuera,
        ))
    # Saco 'Sin clasificar' (siempre al final, solo si tiene algo).
    v_sin = valor.get(None, Decimal("0"))
    if n_pos.get(None, 0) > 0:
        out.append(BloqueDist(
            id=SIN_CLASIFICAR_ID, nombre="Sin clasificar",
            categoria_base="sin_clasificar", orden=999, es_base=False,
            en_estrategia=True,
            valor_eur=v_sin, peso_actual=peso(v_sin),
            n_posiciones=n_pos.get(None, 0),
            liquidez_asignada_eur=Decimal("0"), rendimiento_pct=None,
            peso_objetivo=None, tolerancia=Decimal("0.05"),
            desviacion=None, fuera_tolerancia=False,
        ))

    from app.services.liquidez import calcular_liquidez
    try:
        liq_disp = calcular_liquidez(db, cartera_id).total_disponible
    except Exception:
        liq_disp = Decimal("0")
    return DistribucionResultado(
        total_eur=total, liquidez_disponible_eur=liq_disp, bloques=out,
    )


def listar_posiciones(db: Session, cartera_id: str) -> list[PosicionBloque]:
    bloque_ids = {
        b.id for b in db.execute(
            select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
        ).scalars()
    }
    out: list[PosicionBloque] = []
    for pos, v in _valor_posiciones_abiertas(db, cartera_id):
        bid = pos.bloque_id if pos.bloque_id in bloque_ids else None
        out.append(PosicionBloque(
            isin=pos.isin, nombre=pos.nombre or pos.isin, valor_eur=v, bloque_id=bid,
        ))
    out.sort(key=lambda p: p.valor_eur, reverse=True)
    return out


def asignar_bloque(
    db: Session, cartera_id: str, isin: str, bloque_id: str | None,
    categoria_sugerida: str | None = None, confianza_ia: float | None = None,
    razon: str | None = None,
) -> None:
    """Asigna una posición a un bloque (o al saco). Si `categoria_sugerida` viene
    (lo que sugirió la IA) y difiere de la categoría finalmente elegida, registra
    un override: el sesgo del usuario hecho dato para el few-shot."""
    # El destino del bloque puede ser una posición (cartera) o una empresa del
    # watchlist (Seguimiento): ambas tienen bloque_id. Una posición CERRADA
    # (cantidad 0) NO debe tapar al seguimiento — la tuviste pero ya no es
    # tenencia; el candidato del watchlist es lo que el usuario quiere clasificar.
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    seg = db.execute(
        select(models.Seguimiento)
        .where(models.Seguimiento.cartera_id == cartera_id)
        .where(models.Seguimiento.isin == isin)
    ).scalars().first()
    if pos is not None and estado_posicion(db, pos.id)["cantidad"] > 0:
        entidad = pos                      # tenencia abierta
    elif seg is not None:
        entidad = seg                      # watchlist (o posición cerrada + seguimiento)
    elif pos is not None:
        entidad = pos                      # posición cerrada sin seguimiento (borde)
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"{isin} no es posición ni seguimiento")

    if bloque_id is not None and bloque_id != SIN_CLASIFICAR_ID:
        b = db.get(models.Bloque, bloque_id)
        if b is None or b.cartera_id != cartera_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Bloque no existe")
        entidad.bloque_id = bloque_id
        categoria_elegida = b.categoria_base
    else:
        entidad.bloque_id = None   # Sin clasificar
        categoria_elegida = "sin_clasificar"

    if categoria_sugerida and categoria_sugerida != categoria_elegida:
        registrar_override(
            db, cartera_id, isin, entidad.nombre, categoria_sugerida,
            categoria_elegida, confianza_ia, razon,
        )
    db.commit()


def registrar_override(
    db: Session, cartera_id: str, isin: str, nombre: str | None,
    categoria_sugerida: str, categoria_elegida: str,
    confianza_ia: float | None = None, razon: str | None = None,
    sector: str | None = None,
) -> None:
    """Persiste un override (no hace commit; lo hace el llamante)."""
    db.add(models.OverrideBloque(
        cartera_id=cartera_id, isin=isin, nombre=nombre, sector=sector,
        categoria_sugerida=categoria_sugerida, categoria_elegida=categoria_elegida,
        confianza_ia=Decimal(str(confianza_ia)) if confianza_ia is not None else None,
        razon=razon,
    ))


def overrides_recientes(
    db: Session, cartera_id: str, n: int = 8
) -> list[dict]:
    """Últimos overrides donde la IA y el usuario DISCREPARON, como ejemplos
    few-shot para el clasificador puntual (el sesgo personal del usuario)."""
    rows = db.execute(
        select(models.OverrideBloque)
        .where(models.OverrideBloque.cartera_id == cartera_id)
        .where(models.OverrideBloque.categoria_sugerida
               != models.OverrideBloque.categoria_elegida)
        .order_by(models.OverrideBloque.created_at.desc())
        .limit(n)
    ).scalars().all()
    return [
        {
            "isin": r.isin, "nombre": r.nombre, "sector": r.sector,
            "categoria_sugerida": r.categoria_sugerida,
            "categoria_elegida": r.categoria_elegida, "razon": r.razon,
        }
        for r in rows
    ]


def crear_bloque(
    db: Session, cartera_id: str, nombre: str, categoria_base: str
) -> models.Bloque:
    nombre = nombre.strip()
    if not nombre:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El nombre no puede estar vacío")
    if categoria_base not in models.CATEGORIAS_BASE or categoria_base == "sin_clasificar":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "categoria_base inválida")
    bloques = list(db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
    ).scalars())
    if len(bloques) >= TOPE_BLOQUES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Máximo {TOPE_BLOQUES} bloques por cartera",
        )
    if any(b.nombre.lower() == nombre.lower() for b in bloques):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un bloque '{nombre}'")
    orden = max((b.orden for b in bloques), default=0) + 1
    # Default de en_estrategia desde la ficha de la categoría (Colchón → fuera).
    from app.adapters.ia.prompt import FICHAS
    ficha = FICHAS.get(categoria_base)
    bloque = models.Bloque(
        cartera_id=cartera_id, nombre=nombre, categoria_base=categoria_base,
        orden=orden, es_base=False,
        en_estrategia=ficha.en_estrategia if ficha else True,
    )
    db.add(bloque)
    db.commit()
    return bloque


def editar_bloque(
    db: Session, cartera_id: str, bloque_id: str,
    nombre: str | None = None, categoria_base: str | None = None,
    *, liquidez_asignada_eur: Decimal | None = None,
    rendimiento_pct: Decimal | None = None, set_liquidez: bool = False,
    set_rendimiento: bool = False,
    peso_objetivo: Decimal | None = None, set_peso: bool = False,
    tolerancia: Decimal | None = None,
    en_estrategia: bool | None = None,
) -> models.Bloque:
    b = db.get(models.Bloque, bloque_id)
    if b is None or b.cartera_id != cartera_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bloque no existe")
    if nombre is not None:
        nombre = nombre.strip()
        if not nombre:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nombre vacío")
        b.nombre = nombre
    if categoria_base is not None:
        if categoria_base not in models.CATEGORIAS_BASE or categoria_base == "sin_clasificar":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "categoria_base inválida")
        b.categoria_base = categoria_base
    # Flags set_* permiten poner el campo a NULL explícitamente.
    if set_liquidez:
        if liquidez_asignada_eur is not None and liquidez_asignada_eur < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Efectivo no puede ser negativo")
        b.liquidez_asignada_eur = liquidez_asignada_eur
    if set_rendimiento:
        b.rendimiento_pct = rendimiento_pct
    if set_peso:
        if peso_objetivo is not None and not (Decimal("0") <= peso_objetivo <= Decimal("1")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "peso_objetivo debe estar entre 0 y 1")
        b.peso_objetivo = peso_objetivo
    if tolerancia is not None:
        if not (Decimal("0") <= tolerancia <= Decimal("1")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "tolerancia debe estar entre 0 y 1")
        b.tolerancia = tolerancia
    if en_estrategia is not None:
        b.en_estrategia = en_estrategia
    db.commit()
    return b


def eliminar_bloque(db: Session, cartera_id: str, bloque_id: str) -> None:
    b = db.get(models.Bloque, bloque_id)
    if b is None or b.cartera_id != cartera_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bloque no existe")
    # Las posiciones de ese bloque vuelven a 'Sin clasificar'.
    for pos in db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.bloque_id == bloque_id)
    ).scalars():
        pos.bloque_id = None
    db.delete(b)
    db.commit()
