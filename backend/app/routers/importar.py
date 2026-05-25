"""Endpoint para importar extractos de broker.

Flujo:
  1. Cliente sube uno o dos CSVs (multipart/form-data).
  2. Se persisten a tempfiles.
  3. El adapter de Cuádrate parsea según broker_tipo.
  4. `reconciliar_extracto` dedupea, reconcilia con manuales pendientes,
     marca conflictos y dispara rebuild FIFO.
  5. Devuelve `ImportResultado` con resumen + avisos.

DEGIRO: dos CSVs por diseño. `fichero` = Transacciones (BUY/SELL + splits).
`fichero_cuenta` = Cuenta (dividendos + retenciones + tasas externas). Ambos
opcionales menos `fichero`. Un único broker_tipo `degiro` los procesa los
dos en un solo flow.

Otros brokers (tr, ibkr): un único CSV en `fichero`; `fichero_cuenta`
ignorado.

Nota: los tempfiles se borran al terminar. NO persistimos CSVs con PII.
"""
from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters import cuadrate
from app.db import get_db, models
from app.schemas.transaccion import ImportResultado
from app.services.aportaciones import (
    parse_ibkr_aportaciones,
    parse_tr_aportaciones,
    reconciliar_aportaciones,
    saldo_degiro_cuenta,
    saldo_ibkr_ending_cash,
)
from app.services.opciones import reconciliar_opciones
from app.services.resultados import upsert_complejos, upsert_resultados_ibkr
from app.services.transacciones import reconciliar_extracto


router = APIRouter(prefix="/import", tags=["import"])


@router.get("/brokers")
def brokers_soportados() -> dict[str, list[str]]:
    """Lista de broker_tipo soportados por el adapter (sólo los que se
    exponen al usuario en la UI; `degiro_cuenta` queda interno)."""
    publicos = [b for b in cuadrate.brokers_soportados() if not b.endswith("_cuenta")]
    return {"brokers": publicos}


class BrokerEstado(BaseModel):
    broker_tipo: str
    ultima_fecha: date | None          # último registro importado (max de todas las tablas)
    num_registros: int                 # transacciones + opciones + aportaciones del broker
    saldo_reportado_eur: Decimal | None
    saldo_fecha: date | None


@router.get("/estado", response_model=list[BrokerEstado],
            summary="Fecha del último registro importado por broker (para reanudar desde ahí)")
def estado_brokers(db: Session = Depends(get_db)) -> list[BrokerEstado]:
    """Por cada broker del usuario: la fecha del registro más reciente ya
    importado (máximo entre transacciones, opciones y aportaciones). Permite
    saber desde qué fecha pedir el siguiente extracto sin solaparse."""
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        return []
    brokers = db.execute(
        select(models.Broker).where(models.Broker.user_id == cartera.user_id)
    ).scalars().all()

    def _max_fecha(modelo, broker_id: str):  # type: ignore[no-untyped-def]
        return db.execute(
            select(func.max(modelo.fecha)).where(modelo.broker_id == broker_id)
        ).scalar()

    def _count(modelo, broker_id: str) -> int:  # type: ignore[no-untyped-def]
        return db.execute(
            select(func.count()).select_from(modelo).where(modelo.broker_id == broker_id)
        ).scalar() or 0

    out: list[BrokerEstado] = []
    for b in brokers:
        fechas = [
            f for f in (
                _max_fecha(models.Transaccion, b.id),
                _max_fecha(models.Opcion, b.id),
                _max_fecha(models.Aportacion, b.id),
            ) if f is not None
        ]
        n = (
            _count(models.Transaccion, b.id)
            + _count(models.Opcion, b.id)
            + _count(models.Aportacion, b.id)
        )
        out.append(BrokerEstado(
            broker_tipo=b.broker_tipo,
            ultima_fecha=max(fechas) if fechas else None,
            num_registros=n,
            saldo_reportado_eur=b.saldo_reportado_eur,
            saldo_fecha=b.saldo_fecha,
        ))
    out.sort(key=lambda x: x.broker_tipo)
    return out


def _resolver_broker(
    db: Session, broker_tipo: str
) -> tuple[models.Cartera, models.Broker]:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    broker_tipo_bd = cuadrate.broker_tipo_db(broker_tipo)
    broker = db.execute(
        select(models.Broker)
        .where(models.Broker.user_id == cartera.user_id)
        .where(models.Broker.broker_tipo == broker_tipo_bd)
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No hay broker '{broker_tipo_bd}' creado en BD. "
                f"Llama a /api/bootstrap"
            ),
        )
    return cartera, broker


@router.post(
    "",
    response_model=ImportResultado,
    summary="Importar extracto(s) CSV de un broker",
)
async def importar_extracto(
    db: Annotated[Session, Depends(get_db)],
    broker_tipo: Annotated[str, Form(description="ej: 'tr', 'degiro', 'ibkr'")],
    fichero: Annotated[
        UploadFile,
        File(description="CSV principal (DEGIRO: Transacciones; TR/IBKR: extracto único)"),
    ],
    fichero_cuenta: Annotated[
        UploadFile | None,
        File(description="(DEGIRO opcional) CSV de Cuenta para dividendos + tasas externas"),
    ] = None,
) -> ImportResultado:
    if broker_tipo.endswith("_cuenta"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"'{broker_tipo}' es un parser_kind interno. Usa "
                f"broker_tipo='{broker_tipo.removesuffix('_cuenta')}' y sube el "
                f"CSV de cuenta en el campo 'fichero_cuenta'."
            ),
        )

    parser = cuadrate.parser_para(broker_tipo)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"broker_tipo '{broker_tipo}' no soportado todavía. "
                f"Soportados: {cuadrate.brokers_soportados()}"
            ),
        )

    cartera, broker = _resolver_broker(db, broker_tipo)

    suffix_main = Path(fichero.filename or "extracto.csv").suffix or ".csv"
    avisos_parser: list[str] = []
    with tempfile.NamedTemporaryFile(
        suffix=suffix_main, delete=True, prefix="cima_extracto_"
    ) as tmp_main:
        contenido = await fichero.read()
        if not contenido:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El fichero principal está vacío",
            )
        tmp_main.write(contenido)
        tmp_main.flush()

        # Si hay fichero_cuenta y broker es DEGIRO, también lo escribimos en tempfile.
        tmp_cuenta_path: str | None = None
        tmp_cuenta_obj = None
        if fichero_cuenta is not None and broker_tipo == "degiro":
            tmp_cuenta_obj = tempfile.NamedTemporaryFile(
                suffix=".csv", delete=True, prefix="cima_cuenta_"
            )
            contenido_cuenta = await fichero_cuenta.read()
            if contenido_cuenta:
                tmp_cuenta_obj.write(contenido_cuenta)
                tmp_cuenta_obj.flush()
                tmp_cuenta_path = tmp_cuenta_obj.name

        try:
            # parse_degiro_csv acepta cuenta_path + avisos; el resto de parsers
            # aceptan sólo (path, broker_id) — los llamamos sin kwargs extra.
            if broker_tipo == "degiro":
                candidatas = parser(
                    tmp_main.name,
                    broker_id=broker.id,
                    cuenta_path=tmp_cuenta_path,
                    avisos=avisos_parser,
                )
            elif broker_tipo == "ibkr":
                # IBKR Activity Statement = un solo fichero con todo.
                candidatas = parser(
                    tmp_main.name, broker_id=broker.id, avisos=avisos_parser,
                )
                if fichero_cuenta is not None:
                    avisos_parser.append(
                        "IBKR usa un único Activity Statement — 'fichero_cuenta' ignorado."
                    )
            else:
                candidatas = parser(tmp_main.name, broker_id=broker.id)
                if fichero_cuenta is not None:
                    avisos_parser.append(
                        f"'fichero_cuenta' sólo se procesa para broker_tipo='degiro' "
                        f"(recibido para '{broker_tipo}' → ignorado)."
                    )
        except Exception as e:
            if tmp_cuenta_obj is not None:
                tmp_cuenta_obj.close()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Error parseando extracto de {broker_tipo}: {e}",
            ) from e

        resultado = reconciliar_extracto(db, cartera.id, broker_tipo, candidatas)

        # ── Opciones (derivados) ────────────────────────────────────────
        # DEGIRO: del CSV de Transacciones + cuenta (para ejercidas_isin).
        # IBKR: del propio Activity Statement.
        try:
            opciones_cands = []
            if broker_tipo == "degiro":
                opciones_cands = cuadrate.parse_degiro_opciones(
                    tmp_main.name, broker_id=broker.id, cuenta_path=tmp_cuenta_path,
                )
            elif broker_tipo == "ibkr":
                opciones_cands = cuadrate.parse_ibkr_opciones(
                    tmp_main.name, broker_id=broker.id,
                )
            if opciones_cands:
                r_opt = reconciliar_opciones(db, cartera.id, opciones_cands)
                resultado.opciones_insertadas = r_opt.insertadas
                resultado.opciones_deduplicadas = r_opt.deduplicadas
        except Exception as e:
            avisos_parser.append(f"[OPCIONES] No se pudieron procesar: {e}")

        # ── Aportaciones (depósitos/retiradas externos) ─────────────────
        # IBKR: Deposits & Withdrawals. TR: inbound/outbound. DEGIRO: no
        # disponible en sus CSV → entrada manual.
        try:
            ap_cands = []
            if broker_tipo == "ibkr":
                ap_cands = parse_ibkr_aportaciones(tmp_main.name, broker.id)
            elif broker_tipo == "tr":
                ap_cands = parse_tr_aportaciones(tmp_main.name, broker.id)
            if ap_cands:
                r_ap = reconciliar_aportaciones(db, cartera.id, ap_cands)
                if r_ap.insertadas:
                    avisos_parser.append(
                        f"[APORTACIONES] {r_ap.insertadas} depósitos/retiradas "
                        f"externos registrados."
                    )
            elif broker_tipo == "degiro":
                avisos_parser.append(
                    "[APORTACIONES] DEGIRO no expone aportaciones externas en sus "
                    "CSV — regístralas manualmente."
                )
        except Exception as e:
            avisos_parser.append(f"[APORTACIONES] No se pudieron procesar: {e}")

        # ── Resultados de periodo IBKR: forex (Art. 33.5.e) + T-Bills (RCM)
        #    + detección de productos complejos. Sólo IBKR los expone. ──────
        if broker_tipo == "ibkr":
            try:
                res_cands = cuadrate.parse_ibkr_resultados(tmp_main.name, broker.id)
                if res_cands:
                    r_res = upsert_resultados_ibkr(db, cartera.id, res_cands)
                    n_fx = sum(1 for c in res_cands if c.categoria == "FOREX")
                    n_tb = sum(1 for c in res_cands if c.categoria == "TBILL")
                    avisos_parser.append(
                        f"[IBKR] Forex/T-Bills: {n_fx} divisas + {n_tb} letras "
                        f"({r_res.insertadas} nuevas, {r_res.actualizadas} actualizadas)."
                    )
            except Exception as e:
                avisos_parser.append(f"[IBKR FOREX/TBILL] No se pudieron procesar: {e}")
            try:
                cplx_cands = cuadrate.parse_ibkr_complejos(tmp_main.name, broker.id)
                if cplx_cands:
                    r_cplx = upsert_complejos(db, cartera.id, cplx_cands)
                    avisos_parser.append(
                        f"[IBKR] Productos complejos detectados (sin cálculo fiscal): "
                        f"{r_cplx.insertadas} nuevos."
                    )
            except Exception as e:
                avisos_parser.append(f"[IBKR COMPLEJOS] No se pudieron procesar: {e}")

        # ── Saldo reportado del broker (para validar liquidez) ──────────
        try:
            if broker_tipo == "degiro" and tmp_cuenta_path:
                s = saldo_degiro_cuenta(tmp_cuenta_path)
                if s:
                    broker.saldo_reportado_eur, broker.saldo_fecha = s
            elif broker_tipo == "ibkr":
                s_ibkr = saldo_ibkr_ending_cash(tmp_main.name)
                if s_ibkr is not None:
                    broker.saldo_reportado_eur = s_ibkr
            db.commit()
        except Exception as e:
            avisos_parser.append(f"[SALDO] No se pudo capturar el saldo del broker: {e}")
        finally:
            # El tempfile del Cuenta debe sobrevivir hasta aquí: lo usan tanto
            # el parser de opciones (pairing ejercidas→ISIN) como la captura de
            # saldo. Cerrarlo antes (delete=True) lo borra y ambos fallan.
            if tmp_cuenta_obj is not None:
                tmp_cuenta_obj.close()

    # Inyectar avisos del parser (eventos corporativos no auto-procesados)
    for a in avisos_parser:
        resultado.avisos.append(a)

    return resultado
