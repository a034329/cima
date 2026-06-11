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

from fastapi import HTTPException, status

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.schemas.transaccion import ImportResultado
from app.services import fifo


# Tolerancias para matching tolerante entre manual y extracto
TOL_DIAS = 2                            # ±2 días naturales
TOL_PRECIO_PCT = Decimal("0.005")       # ±0.5% precio (secundario)
TOL_IMPORTE_PCT = Decimal("0.02")       # ±2% importe_eur (firma fiscal real)
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

    `es_match_exacto=True` → reconciliar sin preguntar (extracto reemplaza).
    `es_match_exacto=False` con diferencias → conflicto: pedir decisión.

    Firma de match: **tipo + cantidad + fecha (±2d) + importe_eur (±2%)**.
    El `importe_eur` es la verdad fiscal (lo que entra/sale de la cuenta tras FX);
    el `precio_local` puede diferir por unidades del broker (caso real Angel: la
    misma venta de Zegona con manual a 1834 GBX vs extracto a 21,17 GBP — son la
    misma operación porque el importe_eur casa al 0,28%). Si el precio_local
    diverge mucho pero el resto casa, lo dejamos como aviso informativo, no
    rompe el match.
    """
    diferencias: list[str] = []

    if candidata.tipo != manual.tipo:
        diferencias.append(f"tipo difiere ({manual.tipo} vs {candidata.tipo})")
        return False, diferencias

    if abs(candidata.cantidad - manual.cantidad) > TOL_CANTIDAD:
        diferencias.append(
            f"cantidad difiere ({manual.cantidad} vs {candidata.cantidad})"
        )
        return False, diferencias

    delta_dias = abs((candidata.fecha - manual.fecha).days)
    if delta_dias > TOL_DIAS:
        diferencias.append(
            f"fecha difiere {delta_dias} días ({manual.fecha} vs {candidata.fecha})"
        )
        return False, diferencias

    # Importe EUR: la firma fiscal. Si casa, son la misma operación aunque el
    # precio_local difiera por unidades del broker (GBX vs GBP, ADR ratio…).
    if manual.importe_eur > 0:
        ratio_eur = abs(candidata.importe_eur - manual.importe_eur) / manual.importe_eur
        if ratio_eur > TOL_IMPORTE_PCT:
            diferencias.append(
                f"importe_eur difiere {ratio_eur * 100:.2f}% "
                f"({manual.importe_eur} vs {candidata.importe_eur})"
            )
            return False, diferencias

    # Precio local: chequeo informativo, no determina el match. Solo se reporta
    # como diferencia si difiere mucho — útil para auditar unidades raras.
    if manual.precio_local > 0:
        ratio_px = abs(candidata.precio_local - manual.precio_local) / manual.precio_local
        if ratio_px > TOL_PRECIO_PCT:
            diferencias.append(
                f"precio_local difiere {ratio_px * 100:.2f}% "
                f"({manual.precio_local} vs {candidata.precio_local}) — "
                f"posible diferencia de unidades (GBX/GBP, ADR, etc.)"
            )

    # Tipo + cantidad + fecha + importe_eur casaron → match exacto, aunque haya
    # avisos informativos sobre precio_local.
    es_match_exacto = all(
        not d.startswith(("tipo ", "cantidad ", "fecha ", "importe_eur "))
        for d in diferencias
    )
    return es_match_exacto, diferencias


def _candidatos_manual(
    db: Session, candidata: TxCandidata, cartera_id: str
) -> list[models.Transaccion]:
    """Manuales candidatas a reconciliarse con esta fila del extracto.

    Doctrina: **toda operación manual es provisional hasta que un extracto la
    confirme**. No basta con buscar `pendiente_confirmar`: las manuales que
    se introdujeron con `confirmar_directo=True` también son candidatas
    mientras `origen='manual'` y `external_id IS NULL` (= aún no las ha tocado
    ningún extracto). Filtros: mismo ISIN, mismo tipo, ±2 días.
    """
    fecha_min = candidata.fecha - timedelta(days=TOL_DIAS)
    fecha_max = candidata.fecha + timedelta(days=TOL_DIAS)
    return list(
        db.execute(
            select(models.Transaccion)
            .join(models.Posicion)
            .where(models.Posicion.cartera_id == cartera_id)
            .where(models.Posicion.isin == candidata.isin)
            .where(models.Transaccion.origen == "manual")
            .where(models.Transaccion.external_id.is_(None))
            .where(models.Transaccion.estado.in_(("pendiente_confirmar", "confirmada")))
            .where(models.Transaccion.tipo == candidata.tipo)
            .where(models.Transaccion.fecha >= fecha_min)
            .where(models.Transaccion.fecha <= fecha_max)
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
    confirmar_directo: bool = False,
) -> models.Transaccion:
    """Crea una transacción manual.

    Por defecto queda en `pendiente_confirmar` (esperando confirmación cruzada
    con el extracto del broker). Con `confirmar_directo=True` queda CONFIRMADA
    y se ejecuta el rebuild FIFO inmediatamente → la posición se actualiza al
    instante (caso típico: el usuario acaba de operar y registra la venta sin
    esperar al CSV del broker)."""
    pos = _get_or_create_posicion(
        db, cartera_id, candidata.isin, candidata.nombre, candidata.divisa_local
    )
    estado = "confirmada" if confirmar_directo else "pendiente_confirmar"
    tx = _insertar_tx(db, cartera_id, pos.id, candidata, origen="manual", estado=estado)
    if not confirmar_directo:
        db.commit()
        return tx

    # ATÓMICO (auditoría Cima 2026-06-11, J2): tx + rebuild + validación de
    # inventario se commitean JUNTOS. Antes la tx confirmada se commiteaba
    # primero y el rebuild después en un segundo commit: un crash o una
    # excepción entre ambos dejaba una transacción confirmada SIN lotes
    # (estado inconsistente que fifo.py reconoce como "no detectable").
    from app.services.fifo import rebuild_for_posicion
    from app.services.plan import aplicar_transaccion as aplicar_a_plan
    try:
        # Aplica el FIFO sobre los lotes: BUY añade lote, SELL consume FIFO y
        # genera matches. Sin esto, la posición no refleja el cambio.
        rb = rebuild_for_posicion(db, pos.id)
        # Validación crítica: si el rebuild generó un aviso de inventario
        # insuficiente para ESTA tx (vender más de lo disponible), revertir
        # TODO (la tx incluida) y devolver 4xx al cliente. Caso real Angel:
        # vendió 17 acciones de ACS con 16 disponibles, el frontend mostró
        # "venta aplicada" y la BD nunca se modificó.
        if candidata.tipo == "SELL" and any("sin inventario" in a for a in rb.avisos):
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Inventario insuficiente: estás vendiendo {candidata.cantidad} "
                    f"acciones pero no hay tantas disponibles. Comprueba la posición "
                    f"o importa el extracto que falte antes."
                ),
            )
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise
    # Avanza/cierra los pasos del plan activos para este ISIN.
    aplicar_a_plan(db, cartera_id, pos.isin)
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
    # Desambiguar external_id REPETIDOS dentro del mismo extracto (auditoría
    # Cima 2026-06-11, A3): dos compras limit idénticas el mismo día (IBKR
    # sin trade-id, sintético sin hora) o un rolling de opciones intradía
    # con el mismo OrderID colapsaban al mismo external_id y la segunda
    # operación LEGÍTIMA se "deduplicaba" (dinero desaparecido). Sufijo por
    # orden de aparición (-2, -3…): determinista entre reimports del mismo
    # extracto (el orden del CSV es estable) y compatible con BD existentes
    # (la primera aparición conserva el id sin sufijo).
    candidatas = list(candidatas)
    _vistos: dict[str, int] = {}
    for c in candidatas:
        if not c.external_id:
            continue
        n = _vistos.get(c.external_id, 0) + 1
        _vistos[c.external_id] = n
        if n > 1:
            c.external_id = f"{c.external_id}-{n}"
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
            # El extracto es la fuente de verdad: sobreescribimos los campos
            # numéricos y de procedencia, manteniendo el `id` (los lots/FIFO
            # ya referencian a este registro). Si el usuario introdujo la
            # manual como `confirmada` (confirmar_directo=True), aquí se
            # promueve a confirmada con origen del extracto y external_id real.
            era_manual_confirmada = (
                match_exacto.origen == "manual" and match_exacto.estado == "confirmada"
            )
            match_exacto.estado = "confirmada"
            match_exacto.origen = f"extracto_{broker_tipo}"
            match_exacto.external_id = candidata.external_id
            match_exacto.broker_id = candidata.broker_id
            match_exacto.fecha = candidata.fecha
            match_exacto.cantidad = candidata.cantidad
            match_exacto.precio_local = candidata.precio_local
            match_exacto.divisa_local = candidata.divisa_local
            match_exacto.importe_local = candidata.importe_local
            match_exacto.fx_rate = candidata.fx_rate
            match_exacto.importe_eur = candidata.importe_eur
            match_exacto.gastos_eur = candidata.gastos_eur
            match_exacto.tasas_externas_eur = candidata.tasas_externas_eur
            match_exacto.retencion_eur = candidata.retencion_eur
            match_exacto.retencion_pais = candidata.retencion_pais
            db.flush()
            posiciones_tocadas.add(match_exacto.posicion_id)
            resultado.reconciliadas += 1
            if era_manual_confirmada:
                # Caso real (Angel, Zegona): el usuario registró la venta a mano
                # con `confirmar_directo=True`; al llegar el extracto, antes
                # quedaba huérfana y se insertaba otra fila duplicando. Ahora
                # la reconciliamos in-place y avisamos para que el usuario sepa.
                resultado.avisos.append(
                    f"[RECONCILIADA] Manual confirmada del {match_exacto.fecha} "
                    f"({candidata.isin} {candidata.tipo}) reemplazada por la fila "
                    f"del extracto {broker_tipo} (misma operación)."
                )
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

    # Manuales huérfanas: cualquier manual sin contrapartida del extracto del
    # mismo broker (origen='manual', external_id NULL) registrada hace más de
    # `DIAS_HUERFANA` días que no se haya cazado en esta importación. Cubre
    # tanto `pendiente_confirmar` como `confirmada` (`confirmar_directo=True`):
    # ambas son provisionales hasta que el broker las refleje.
    limite = date.today() - timedelta(days=DIAS_HUERFANA)
    huerfanas = list(
        db.execute(
            select(models.Transaccion)
            .join(models.Posicion)
            .where(models.Posicion.cartera_id == cartera_id)
            .where(models.Transaccion.origen == "manual")
            .where(models.Transaccion.external_id.is_(None))
            .where(models.Transaccion.estado.in_(("pendiente_confirmar", "confirmada")))
            .where(models.Transaccion.fecha <= limite)
        ).scalars()
    )
    for h in huerfanas:
        resultado.huerfanas_manuales += 1
        estado_lbl = ("confirmada manualmente" if h.estado == "confirmada"
                      else "pendiente de confirmar")
        resultado.avisos.append(
            f"Manual huérfana id={h.id[:8]} ({h.fecha}, {estado_lbl}): registrada "
            f"hace >{DIAS_HUERFANA} días pero no aparece en el extracto. Verifica "
            f"si fue cancelada o si necesitas un extracto del broker correcto."
        )

    # Rebuild FIFO por posición tocada — el orden cronológico vence al
    # orden de importación, garantizando FIFO cross-broker correcto
    # aunque DEGIRO se importe antes que IBKR, etc.
    for rb in fifo.rebuild_for_posiciones(db, posiciones_tocadas):
        for aviso in rb.avisos:
            resultado.avisos.append(f"[FIFO] {aviso}")

    db.commit()
    # Avanza los pasos del plan por cada ISIN tocado tras el extracto.
    from app.services.plan import aplicar_transaccion as aplicar_a_plan
    isines = {
        p.isin for p in db.execute(
            select(models.Posicion).where(models.Posicion.id.in_(posiciones_tocadas))
        ).scalars()
    }
    for isin in isines:
        aplicar_a_plan(db, cartera_id, isin)
    return resultado
