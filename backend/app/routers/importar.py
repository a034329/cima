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
from app.services import storage_extractos
from app.services.opciones import reconciliar_opciones
from app.services.resultados import upsert_complejos, upsert_resultados_ibkr
from app.services.transacciones import reconciliar_extracto


# Kind del extracto principal por broker_tipo. DEGIRO tiene dos kinds (el
# CSV de Cuenta se persiste aparte como 'degiro_cuenta'); el resto un único
# kind = broker_tipo.
_KIND_PRINCIPAL = {
    "degiro": "degiro_transacciones",
    "ibkr":   "ibkr",
    "tr":     "tr",
}


router = APIRouter(prefix="/import", tags=["import"])


class ExtractoOut(BaseModel):
    id: str
    ejercicio: int
    kind: str
    filename_original: str
    size_bytes: int
    uploaded_at: str


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


@router.get("/extractos", response_model=list[ExtractoOut],
            summary="Lista los CSVs originales guardados (Roadmap 1.9)")
def listar_extractos_guardados(
    ejercicio: int | None = None,
    db: Session = Depends(get_db),
) -> list[ExtractoOut]:
    """Para que el frontend muestre qué tiene Cima guardado y permita generar
    la declaración solo cuando hay extractos para ese ejercicio."""
    items = storage_extractos.listar_extractos(db, _cartera(db).id, ejercicio)
    return [ExtractoOut(**i.__dict__) for i in items]


@router.delete("/extractos/{extracto_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Elimina un CSV guardado (fila + fichero)")
def eliminar_extracto_guardado(extracto_id: str, db: Session = Depends(get_db)) -> None:
    cartera_id = _cartera(db).id
    if not storage_extractos.eliminar_extracto(db, cartera_id, extracto_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Extracto no encontrado")
    db.commit()


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
    "/preview",
    summary="DIAGNÓSTICO: parsea el CSV y devuelve lo que el parser ve, SIN tocar BD",
)
async def preview_extracto(
    broker_tipo: Annotated[str, Form(description="ej: 'tr', 'degiro', 'ibkr'")],
    fichero: Annotated[UploadFile, File(description="CSV a inspeccionar")],
    fichero_cuenta: Annotated[UploadFile | None, File(
        description="(DEGIRO opcional) CSV de Cuenta")] = None,
    filtro: Annotated[str | None, Form(description="Subcadena opcional para filtrar")] = None,
) -> dict:
    """Endpoint de depuración: ejecuta los parsers (transacciones + opciones) y
    devuelve un volcado de lo que extrae, sin reconciliar ni persistir nada.
    Devuelve avisos y tracebacks completos para depurar sin acceso a logs."""
    import traceback
    avisos: list[str] = []
    cands_tx: list = []
    cands_opt: list = []
    err_tx: str | None = None
    err_opt: str | None = None

    try:
        parser = cuadrate.parser_para(broker_tipo)
        if parser is None:
            return {"error": f"broker_tipo '{broker_tipo}' no soportado",
                    "soportados": cuadrate.brokers_soportados()}

        suffix = Path(fichero.filename or "extracto.csv").suffix or ".csv"
        contenido = await fichero.read()
        if not contenido:
            return {"error": "Fichero vacío"}

        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=True, prefix="cima_prev_"
        ) as tmp:
            tmp.write(contenido); tmp.flush()
            cuenta_path: str | None = None
            tmp_cuenta = None
            if fichero_cuenta is not None and broker_tipo == "degiro":
                tmp_cuenta = tempfile.NamedTemporaryFile(
                    suffix=".csv", delete=True, prefix="cima_prev_cuenta_")
                cuenta_path = tmp_cuenta.name
                cnt = await fichero_cuenta.read()
                if cnt:
                    tmp_cuenta.write(cnt); tmp_cuenta.flush()

            try:
                if broker_tipo == "degiro":
                    cands_tx = parser(tmp.name, broker_id="preview",
                                      cuenta_path=cuenta_path, avisos=avisos)
                elif broker_tipo == "ibkr":
                    cands_tx = parser(tmp.name, broker_id="preview", avisos=avisos)
                else:
                    cands_tx = parser(tmp.name, broker_id="preview")
            except Exception:
                err_tx = traceback.format_exc()

            try:
                if broker_tipo == "degiro":
                    cands_opt = cuadrate.parse_degiro_opciones(
                        tmp.name, broker_id="preview", cuenta_path=cuenta_path)
                elif broker_tipo == "ibkr":
                    cands_opt = cuadrate.parse_ibkr_opciones(
                        tmp.name, broker_id="preview")
            except Exception:
                err_opt = traceback.format_exc()

            if tmp_cuenta is not None:
                tmp_cuenta.close()
    except Exception:
        return {"error_global": traceback.format_exc()}

    return {
        "broker_tipo": broker_tipo,
        "avisos_parser": avisos[:50],
        "transacciones": _resumen_tx(cands_tx, filtro),
        "opciones": _resumen_opt(cands_opt, filtro),
        "error_transacciones": err_tx,
        "error_opciones": err_opt,
    }


def _resumen_tx(cands: list, filtro: str | None) -> dict:
    f = (filtro or "").upper().strip()
    todas = [{
        "fecha": str(getattr(c, "fecha", "")),
        "tipo": getattr(c, "tipo", ""),
        "isin": getattr(c, "isin", ""),
        "nombre": getattr(c, "nombre", ""),
        "cantidad": str(getattr(c, "cantidad", "")),
        "precio_local": str(getattr(c, "precio_local", "")),
        "importe_eur": str(getattr(c, "importe_eur", "")),
        "external_id": getattr(c, "external_id", None),
    } for c in cands]
    if f:
        todas = [t for t in todas if f in (t["isin"] + " " + t["nombre"]).upper()]
    return {"total": len(cands), "mostradas": len(todas), "items": todas[:50]}


def _resumen_opt(cands: list, filtro: str | None) -> dict:
    f = (filtro or "").upper().strip()
    todas = [{
        "fecha": str(getattr(c, "fecha", "")),
        "simbolo": getattr(c, "simbolo", ""),
        "subyacente": getattr(c, "subyacente", ""),
        "isin": getattr(c, "isin", ""),
        "tipo_op": getattr(c, "tipo_op", ""),
        "strike": getattr(c, "strike", ""),
        "vencimiento": getattr(c, "vencimiento", ""),
        "accion": getattr(c, "accion", ""),
        "cantidad": str(getattr(c, "cantidad", "")),
        "importe_eur": str(getattr(c, "importe_eur", "")),
        "expirada": getattr(c, "expirada", False),
        "ejercida": getattr(c, "ejercida", False),
        "external_id": getattr(c, "external_id", None),
    } for c in cands]
    if f:
        todas = [t for t in todas if f in (
            t["simbolo"] + " " + t["subyacente"] + " " + t["isin"]).upper()]
    return {"total": len(cands), "mostradas": len(todas), "items": todas[:50]}


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
    ejercicio: Annotated[
        int | None,
        Form(description=(
            "Año fiscal del extracto (e.g. 2025). Si lo informas, Cima guarda "
            "el CSV original en disco para re-pasárselo al motor de Cuádrate "
            "al generar la declaración (Roadmap 1.9). Opcional."
        )),
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
        contenido_cuenta: bytes = b""
        if fichero_cuenta is not None and broker_tipo == "degiro":
            tmp_cuenta_obj = tempfile.NamedTemporaryFile(
                suffix=".csv", delete=True, prefix="cima_cuenta_"
            )
            contenido_cuenta = await fichero_cuenta.read()
            if contenido_cuenta:
                tmp_cuenta_obj.write(contenido_cuenta)
                tmp_cuenta_obj.flush()
                tmp_cuenta_path = tmp_cuenta_obj.name

        # ── Persistencia del CSV original (Roadmap 1.9) ─────────────────
        # Cima guarda el extracto tal cual para poder re-pasárselo a
        # `generar_irpf.main()` y entregar la declaración completa.
        # Si no se informa el ejercicio, lo omitimos sin romper la importación
        # (compatibilidad con el flujo previo).
        if ejercicio is not None:
            kind_principal = _KIND_PRINCIPAL.get(broker_tipo)
            if kind_principal:
                try:
                    storage_extractos.guardar_extracto(
                        db, cartera_id=cartera.id, ejercicio=ejercicio,
                        kind=kind_principal,
                        filename_original=fichero.filename or "extracto.csv",
                        contenido=contenido,
                    )
                except (OSError, ValueError) as e:
                    avisos_parser.append(
                        f"[STORAGE] No se pudo guardar el CSV original "
                        f"({kind_principal}/{ejercicio}): {e}. La importación "
                        f"a BD sigue, pero el motor IRPF de Cuádrate no podrá "
                        f"usar este extracto."
                    )
            if (broker_tipo == "degiro" and contenido_cuenta):
                try:
                    storage_extractos.guardar_extracto(
                        db, cartera_id=cartera.id, ejercicio=ejercicio,
                        kind="degiro_cuenta",
                        filename_original=(fichero_cuenta.filename
                                           if fichero_cuenta else "cuenta.csv"),
                        contenido=contenido_cuenta,
                    )
                except (OSError, ValueError) as e:
                    avisos_parser.append(
                        f"[STORAGE] No se pudo guardar el CSV de cuenta "
                        f"DEGIRO ({ejercicio}): {e}."
                    )

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
                    tmp_main.name, broker_id=broker.id, avisos=avisos_parser,
                )
            if opciones_cands:
                r_opt = reconciliar_opciones(db, cartera.id, opciones_cands)
                resultado.opciones_insertadas = r_opt.insertadas
                resultado.opciones_deduplicadas = r_opt.deduplicadas
                # Diagnóstico: si hay deduplicadas, listarlas para que el usuario
                # vea CUÁLES (descubre si el dedup fue correcto o si está
                # colapsando opciones legítimamente nuevas — caso típico:
                # mismo simbolo+fecha+importe+cantidad por colisión del
                # external_id sintético).
                if r_opt.duplicadas_detalle:
                    muestra = r_opt.duplicadas_detalle[:8]
                    extra = (f" (+{len(r_opt.duplicadas_detalle) - 8} más)"
                             if len(r_opt.duplicadas_detalle) > 8 else "")
                    avisos_parser.append(
                        f"[OPCIONES] {len(r_opt.duplicadas_detalle)} deduplicadas: "
                        + " · ".join(muestra) + extra
                    )
            elif broker_tipo == "tr":
                # TR todavía no tiene parser de opciones — si alguna vez el
                # extracto trae alguna, se ignoraría en silencio. Aviso preventivo.
                avisos_parser.append(
                    "[OPCIONES] Trade Republic no tiene parser de opciones en Cima — "
                    "si el extracto contiene alguna, no se importará. Reporta el caso si te ocurre."
                )
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
