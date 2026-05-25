"""Servicio de transacciones — CRUD + reconciliación.

Reconciliación es la pieza crítica del producto. Cubre tres caminos de
entrada para una operación:

  1. Operación manual (UI / API).
  2. Importación de extracto (primera vez o reimport mensual).
  3. Casado de extracto contra una manual previa pendiente_confirmar.

Reglas (ver `Cima ROADMAP` y `ADR-002`):

  | Caso                                                              | Acción                                              |
  |-------------------------------------------------------------------|-----------------------------------------------------|
  | Extracto trae fila con `external_id` que YA existe en BD          | Ignorar (dedup idempotente)                         |
  | Extracto trae fila nueva sin match manual                         | Insertar como `confirmada`                          |
  | Extracto trae fila que casa con manual `pendiente_confirmar`      | Promover manual a `confirmada`, copiar external_id  |
  | Manual existe pero el extracto no la trae en 30 días              | Avisar al usuario                                   |
  | Match parcial (precio difiere >0.5%)                              | Marcar como conflicto, no auto-resolver             |
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.schemas.transaccion import ImportResultado
from app.services import fifo


# Tolerancias para matching tolerante entre manual y extracto
TOL_DIAS = 2                            # ±2 días naturales
TOL_PRECIO_PCT = Decimal("0.005")       # ±0.5% precio
TOL_CANTIDAD = Decimal("0.000001")      # cantidad ha de ser exacta (margen redondeo)
DIAS_HUERFANA = 30                      # avisar si manual sin confirmar >30 días


@dataclass
class TxCandidata:
    """Una fila normalizada de extracto, lista para reconciliar/insertar.

    Mismo shape que `models.Transaccion` pero sin id, sin estado y con
    `broker_id` / `posicion_id` opcionales — el servicio los resuelve.
    """
    fecha: date
    tipo: str
    isin: str
    nombre: str | None
    cantidad: Decimal
    precio_local: Decimal
    divisa_local: str
    importe_local: Decimal
    fx_rate: Decimal
    importe_eur: Decimal
    gastos_eur: Decimal
    tasas_externas_eur: Decimal
    retencion_eur: Decimal
    retencion_pais: str | None
    external_id: str | None
    broker_id: str
    notas: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_or_create_posicion(
    db: Session, cartera_id: str, isin: str, nombre: str | None, divisa: str
) -> models.Posicion:
    """Crea la posición si no existe, devuelve la existente si sí."""
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalar_one_or_none()
    if pos is not None:
        # Actualiza nombre si no lo tenía
        if nombre and not pos.nombre:
            pos.nombre = nombre
        return pos
    pos = models.Posicion(
        cartera_id=cartera_id,
        isin=isin,
        nombre=nombre,
        divisa_local=divisa,
    )
    db.add(pos)
    db.flush()
    return pos


def _existe_por_external_id(
    db: Session, broker_id: str, external_id: str
) -> models.Transaccion | None:
    return db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.broker_id == broker_id)
        .where(models.Transaccion.external_id == external_id)
    ).scalar_one_or_none()


def _matching_tolerante(
    candidata: TxCandidata, manual: models.Transaccion
) -> tuple[bool, list[str]]:
    """¿Casa la candidata del extracto con la transacción manual pendiente?

    Devuelve (es_match_exacto, lista_de_diferencias).

    `es_match_exacto=True` significa que el match es lo suficientemente
    cercano para promover la manual a confirmada sin pedir al usuario.

    `es_match_exacto=False` con diferencias → conflicto: pedir decisión.
    """
    diferencias: list[str] = []

    # Tipo: tiene que coincidir (BUY/SELL/DIVIDEND/...)
    if candidata.tipo != manual.tipo:
        diferencias.append(f"tipo difiere ({manual.tipo} vs {candidata.tipo})")
        return False, diferencias

    # Cantidad: exacta (ya tolerada por TOL_CANTIDAD redondeo)
    if abs(candidata.cantidad - manual.cantidad) > TOL_CANTIDAD:
        diferencias.append(
            f"cantidad difiere ({manual.cantidad} vs {candidata.cantidad})"
        )
        return False, diferencias

    # Precio: ±0.5%
    if manual.precio_local > 0:
        ratio = abs(candidata.precio_local - manual.precio_local) / manual.precio_local
        if ratio > TOL_PRECIO_PCT:
            diferencias.append(
                f"precio difiere {ratio * 100:.2f}% "
                f"({manual.precio_local} vs {candidata.precio_local})"
            )

    # Fecha: ±2 días
    delta_dias = abs((candidata.fecha - manual.fecha).days)
    if delta_dias > TOL_DIAS:
        diferencias.append(
            f"fecha difiere {delta_dias} días ({manual.fecha} vs {candidata.fecha})"
        )
        return False, diferencias

    # Si llegamos aquí: misma posición, misma cantidad, misma fecha aproximada,
    # tipo coincide. Si el precio cae dentro del 0.5% es match exacto.
    es_match_exacto = not diferencias
    return es_match_exacto, diferencias


def _candidatos_manual(
    db: Session, candidata: TxCandidata, cartera_id: str
) -> list[models.Transaccion]:
    """Manuales pendientes que podrían casar: misma posición, ±2 días, sin external_id."""
    fecha_min = candidata.fecha - timedelta(days=TOL_DIAS)
    fecha_max = candidata.fecha + timedelta(days=TOL_DIAS)
    return list(
        db.execute(
            select(models.Transaccion)
            .join(models.Posicion)
            .where(models.Posicion.cartera_id == cartera_id)
            .where(models.Posicion.isin == candidata.isin)
            .where(models.Transaccion.estado == "pendiente_confirmar")
            .where(models.Transaccion.tipo == candidata.tipo)
            .where(models.Transaccion.fecha >= fecha_min)
            .where(models.Transaccion.fecha <= fecha_max)
            .where(models.Transaccion.external_id.is_(None))
        ).scalars()
    )


def _insertar_tx(
    db: Session,
    cartera_id: str,
    posicion_id: str,
    candidata: TxCandidata,
    origen: str,
    estado: str,
) -> models.Transaccion:
    tx = models.Transaccion(
        cartera_id=cartera_id,
        broker_id=candidata.broker_id,
        posicion_id=posicion_id,
        fecha=candidata.fecha,
        tipo=candidata.tipo,
        cantidad=candidata.cantidad,
        precio_local=candidata.precio_local,
        divisa_local=candidata.divisa_local,
        importe_local=candidata.importe_local,
        fx_rate=candidata.fx_rate,
        importe_eur=candidata.importe_eur,
        gastos_eur=candidata.gastos_eur,
        tasas_externas_eur=candidata.tasas_externas_eur,
        retencion_eur=candidata.retencion_eur,
        retencion_pais=candidata.retencion_pais,
        estado=estado,
        origen=origen,
        external_id=candidata.external_id,
        notas=candidata.notas,
    )
    db.add(tx)
    db.flush()
    return tx


# ── API pública del servicio ───────────────────────────────────────────────

def crear_manual(
    db: Session,
    cartera_id: str,
    candidata: TxCandidata,
) -> models.Transaccion:
    """Crea una transacción manual (estado `pendiente_confirmar`)."""
    pos = _get_or_create_posicion(
        db, cartera_id, candidata.isin, candidata.nombre, candidata.divisa_local
    )
    tx = _insertar_tx(
        db, cartera_id, pos.id, candidata,
        origen="manual", estado="pendiente_confirmar",
    )
    db.commit()
    return tx


def reconciliar_extracto(
    db: Session,
    cartera_id: str,
    broker_tipo: str,
    candidatas: Iterable[TxCandidata],
) -> ImportResultado:
    """Aplica el algoritmo de dedup+reconciliación a las filas de un extracto.

    Para cada `TxCandidata`:
      1. Si `external_id` ya existe (por broker_id) → dedup, skip.
      2. Si casa con manual `pendiente_confirmar` (tolerancia exacta) →
         promover a `confirmada`, copiar external_id.
      3. Si match parcial → conflicto (manual queda pendiente_confirmar, se
         registra el aviso para revisión del usuario).
      4. Sin match previo → insertar como `confirmada`.

    Tras procesar todas, marca avisos para manuales >30 días sin confirmar
    en la misma cartera y el mismo broker_tipo.
    """
    resultado = ImportResultado(
        broker=broker_tipo,
        insertadas=0, deduplicadas=0, reconciliadas=0,
        conflictos=0, huerfanas_manuales=0,
    )
    # Recolectamos las posiciones tocadas para hacer UN rebuild FIFO al
    # final en vez de aplicar FIFO incrementalmente. Crítico cuando se
    # importa un broker después de otro y el orden cronológico no coincide
    # con el orden de importación (ver doc en `services/fifo.py`).
    posiciones_tocadas: set[str] = set()

    for candidata in candidatas:
        # 1. Dedup por external_id
        if candidata.external_id:
            existente = _existe_por_external_id(
                db, candidata.broker_id, candidata.external_id
            )
            if existente is not None:
                resultado.deduplicadas += 1
                continue

        # 2/3. Buscar candidatos manuales que casen
        manuales = _candidatos_manual(db, candidata, cartera_id)
        match_exacto = None
        conflictos_locales: list[tuple[models.Transaccion, list[str]]] = []
        for m in manuales:
            es_exacto, diferencias = _matching_tolerante(candidata, m)
            if es_exacto:
                match_exacto = m
                break
            if diferencias:
                conflictos_locales.append((m, diferencias))

        if match_exacto is not None:
            # Promover manual a confirmada, heredar metadatos del extracto
            match_exacto.estado = "confirmada"
            match_exacto.origen = f"extracto_{broker_tipo}"
            match_exacto.external_id = candidata.external_id
            match_exacto.broker_id = candidata.broker_id
            match_exacto.gastos_eur = candidata.gastos_eur
            match_exacto.tasas_externas_eur = candidata.tasas_externas_eur
            match_exacto.retencion_eur = candidata.retencion_eur
            match_exacto.fx_rate = candidata.fx_rate
            match_exacto.importe_eur = candidata.importe_eur
            db.flush()
            posiciones_tocadas.add(match_exacto.posicion_id)
            resultado.reconciliadas += 1
            continue

        if conflictos_locales:
            # Hay manuales que casan parcialmente pero no exacto.
            # Insertamos la del extracto como confirmada (fuente fiscal de
            # verdad), incrementamos `insertadas`, y dejamos avisos para
            # que el usuario decida qué hacer con la manual pendiente.
            pos = _get_or_create_posicion(
                db, cartera_id, candidata.isin, candidata.nombre, candidata.divisa_local
            )
            tx_nueva = _insertar_tx(
                db, cartera_id, pos.id, candidata,
                origen=f"extracto_{broker_tipo}", estado="confirmada",
            )
            posiciones_tocadas.add(pos.id)
            resultado.insertadas += 1
            for manual, difs in conflictos_locales:
                resultado.conflictos += 1
                resultado.avisos.append(
                    f"Conflicto con manual id={manual.id[:8]} ({candidata.isin} "
                    f"{candidata.fecha}): {'; '.join(difs)}"
                )
            continue

        # 4. Sin match — insertar como confirmada
        pos = _get_or_create_posicion(
            db, cartera_id, candidata.isin, candidata.nombre, candidata.divisa_local
        )
        tx_nueva = _insertar_tx(
            db, cartera_id, pos.id, candidata,
            origen=f"extracto_{broker_tipo}", estado="confirmada",
        )
        posiciones_tocadas.add(pos.id)
        resultado.insertadas += 1

    # Manuales huérfanas (>DIAS_HUERFANA sin confirmar)
    limite = date.today() - timedelta(days=DIAS_HUERFANA)
    huerfanas = list(
        db.execute(
            select(models.Transaccion)
            .join(models.Posicion)
            .where(models.Posicion.cartera_id == cartera_id)
            .where(models.Transaccion.estado == "pendiente_confirmar")
            .where(models.Transaccion.fecha <= limite)
        ).scalars()
    )
    for h in huerfanas:
        resultado.huerfanas_manuales += 1
        resultado.avisos.append(
            f"Manual huérfana id={h.id[:8]} ({h.fecha}): registrada hace >{DIAS_HUERFANA} "
            f"días pero no aparece en el extracto. Verifica si fue cancelada."
        )

    # Rebuild FIFO por posición tocada — el orden cronológico vence al
    # orden de importación, garantizando FIFO cross-broker correcto
    # aunque DEGIRO se importe antes que IBKR, etc.
    for rb in fifo.rebuild_for_posiciones(db, posiciones_tocadas):
        for aviso in rb.avisos:
            resultado.avisos.append(f"[FIFO] {aviso}")

    db.commit()
    return resultado
