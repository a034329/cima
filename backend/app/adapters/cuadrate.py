"""Adapter al motor fiscal de Cuádrate (/app/720/irpf/generar_irpf.py).

Aísla a Cima del path absoluto de Cuádrate y de la firma exacta de sus
parsers. Si en el futuro extraemos `wg-core/`, sólo este archivo cambia.

Conversión: cada parser de Cuádrate devuelve `list[dict]` con campos
específicos a Cuádrate. Aquí traducimos a `TxCandidata` que es lo que
`reconciliar_extracto` espera.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.services.opciones import OpcionCandidata
from app.services.transacciones import TxCandidata


@dataclass
class ResultadoCandidata:
    """Resultado de periodo IBKR (forex o T-Bill) listo para upsert en BD."""
    categoria: str               # 'FOREX' | 'TBILL'
    ejercicio: int
    clave: str                   # divisa (USD) o símbolo de la letra
    realized_eur: Decimal
    unrealized_eur: Decimal
    periodo_inicio: date | None
    periodo_fin: date | None
    external_id: str
    broker_id: str


@dataclass
class ComplejoCandidata:
    """Producto complejo detectado (CFD/futuro/warrant/...) — sólo detección."""
    ejercicio: int
    fecha: date | None
    simbolo: str
    isin: str | None
    nombre: str
    asset_category: str
    cantidad: Decimal
    importe_eur: Decimal
    external_id: str
    broker_id: str


# Motor de Cuádrate VENDORIZADO en backend/vendor/cuadrate (copia commiteada →
# disponible en el contenedor de producción). Se sincroniza con
# scripts/sync_cuadrate.py. CIMA_CUADRATE_IRPF_PATH puede apuntar al origen en dev.
from app.config import settings

_VENDOR_CUADRATE = Path(__file__).resolve().parents[2] / "vendor" / "cuadrate"
_VENDOR_CUADRATE_IRPF = _VENDOR_CUADRATE / "irpf"
_VENDOR_CUADRATE_WEBAPP = _VENDOR_CUADRATE / "webapp"
_CUADRATE_IRPF = Path(settings.cuadrate_irpf_path) if settings.cuadrate_irpf_path else _VENDOR_CUADRATE_IRPF
# webapp/ vive junto a irpf/ en el origen y en el vendor. Si el usuario
# override irpf con --source ./otro/, mantenemos webapp en el path paralelo.
_CUADRATE_WEBAPP = (_CUADRATE_IRPF.parent / "webapp") if settings.cuadrate_irpf_path else _VENDOR_CUADRATE_WEBAPP


def _ensure_cuadrate_importable() -> None:
    """Añade el path de Cuádrate a sys.path la primera vez."""
    if not _CUADRATE_IRPF.is_dir():
        raise RuntimeError(
            f"Motor de Cuádrate no encontrado en {_CUADRATE_IRPF}. "
            "Cima depende de Cuádrate para los parsers de broker."
        )
    p = str(_CUADRATE_IRPF)
    if p not in sys.path:
        sys.path.insert(0, p)
    # webapp/ contiene `excel_cartera` (generador XLSX) y `clasificacion_origen`
    # (etiqueta de origen ISIN). Es opcional: si falta, el motor sigue funcionando
    # — solo se rompe `generate_cartera_xlsx`.
    if _CUADRATE_WEBAPP.is_dir():
        w = str(_CUADRATE_WEBAPP)
        if w not in sys.path:
            sys.path.insert(0, w)


def get_excel_cartera():
    """Importa `excel_cartera` de Cuádrate (generador XLSX maestro).

    Requiere openpyxl en el entorno y el módulo `clasificacion_origen` también
    vendorizado. Lo invoca el servicio `cuadrate_irpf` desde Cima para
    materializar la declaración XLSX con la cartera del usuario.
    """
    _ensure_cuadrate_importable()
    import excel_cartera  # type: ignore[import-not-found]
    return excel_cartera


def get_pdf_generator():
    """Importa `pdf_generator` de Cuádrate (informe fiscal en PDF, weasyprint
    + jinja2). Recibe fifo_results del motor y dicts de dividendos/opciones
    parseados de los .txt para construir el HTML y rasterizar a PDF."""
    _ensure_cuadrate_importable()
    import pdf_generator  # type: ignore[import-not-found]
    return pdf_generator


def get_motor_fiscal():
    """Importa `motor_fiscal` de Cuádrate como librería pura.

    Es un módulo limpio (no toca filesystem, sin EJERCICIO global) que expone
    `FIFOTracker`, `FIFOMatch`, `FIFOResults`, etc. Cima lo invoca para el
    cálculo fiscal exacto (FIFO multi-año, regla 2M, pérdidas diferidas).
    """
    _ensure_cuadrate_importable()
    import motor_fiscal  # type: ignore[import-not-found]
    return motor_fiscal


def get_compensacion_perdidas():
    """Importa `compensacion_perdidas` de Cuádrate como librería pura.

    Expone `calcular_compensacion` (RCM↔patrimoniales 25% + bolsas 4 años),
    `auto_detectar_perdidas_anteriores`, `PerdidaPendiente`,
    `ResultadoCompensacion`.
    """
    _ensure_cuadrate_importable()
    import compensacion_perdidas  # type: ignore[import-not-found]
    return compensacion_perdidas


def _to_decimal(v: object) -> Decimal:
    """Normaliza cualquier numérico/Decimal/str a Decimal."""
    if isinstance(v, Decimal):
        return v
    if v is None or v == "":
        return Decimal("0")
    return Decimal(str(v))


def _to_date(v: object) -> "date":
    """Normaliza un valor de fecha. Acepta date, str ISO o str dd/mm/yyyy."""
    from datetime import date as _date, datetime as _dt
    if isinstance(v, _date):
        return v
    s = str(v)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"No se reconoce el formato de fecha: {v!r}")


# ── Trade Republic ─────────────────────────────────────────────────────────

def parse_tr_csv(csv_path: str | Path, broker_id: str) -> list[TxCandidata]:
    """Parsea un CSV de Trade Republic Transaction Export y devuelve
    `TxCandidata[]` listas para `reconciliar_extracto`.

    Incluye:
      - BUY / SELL como BUY / SELL.
      - DIVIDEND como DIVIDEND.
      - INTEREST_PAYMENT como INTEREST.
      - FREE_RECEIPT (staking) como STAKING_REWARD.

    Excluye: MIGRATION, CUSTOMER_INBOUND, TAX_OPTIMIZATION (sin impacto fiscal).
    """
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    path = str(csv_path)
    candidatas: list[TxCandidata] = []

    # ── BUY / SELL ─────────────────────────────────────────────────────────
    ops, _sp_ops, _desc = g.parse_tr(path)
    for op in ops:
        tipo_cuadrate = op["tipo"]   # 'A' o 'T' en Cuádrate
        tipo_cima = "BUY" if tipo_cuadrate == "A" else "SELL"
        candidatas.append(TxCandidata(
            fecha=_to_date(op["fecha"]),
            tipo=tipo_cima,
            isin=op["isin"],
            nombre=op.get("nombre"),
            cantidad=_to_decimal(op["cantidad"]),
            precio_local=_precio_desde_importe(
                _to_decimal(op["importe_eur"]),
                _to_decimal(op["cantidad"]),
            ),
            divisa_local="EUR",       # TR entrega siempre en EUR
            importe_local=_to_decimal(op["importe_eur"]),
            fx_rate=Decimal("1"),
            importe_eur=_to_decimal(op["importe_eur"]),
            gastos_eur=_to_decimal(op.get("gastos_eur", 0)),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=op.get("transaction_id"),
            broker_id=broker_id,
            notas=("Savings plan" if op.get("es_savings_plan") else None),
        ))

    # ── Dividendos (DIVIDEND + retencion ES) ───────────────────────────────
    divs = g.parse_tr_dividendos(path)
    # Cuádrate emite DIV y RET como filas separadas. Reagrupamos por ISIN+fecha
    # para producir UNA candidata por dividendo con su retención.
    div_index: dict[tuple, dict] = {}
    for d in divs:
        key = (d["isin"], _to_date(d["fecha"]))
        slot = div_index.setdefault(key, {"DIV": None, "RET": None, "nombre": d.get("nombre")})
        slot[d["tipo"]] = d
    for (isin, fecha), slot in div_index.items():
        div = slot.get("DIV")
        ret = slot.get("RET")
        if not div:
            continue
        importe = _to_decimal(div["importe_eur"])
        candidatas.append(TxCandidata(
            fecha=fecha,
            tipo="DIVIDEND",
            isin=isin,
            nombre=slot.get("nombre"),
            cantidad=Decimal("0"),       # dividendos no llevan unidades
            precio_local=Decimal("0"),
            divisa_local=div.get("divisa", "EUR"),
            importe_local=importe,
            fx_rate=Decimal("1"),
            importe_eur=importe,
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=_to_decimal(ret["importe_eur"]) if ret else Decimal("0"),
            retencion_pais="ES",
            # TR no expone tx_id de dividendo en parser → id sintético
            # determinista para que reimportar el mismo CSV no duplique.
            external_id=_synthetic_external_id(
                "tr-div", isin, fecha, "DIVIDEND", Decimal("0"), importe,
            ),
            broker_id=broker_id,
            notas=div.get("bruto_original"),
        ))

    # ── Intereses cuenta remunerada ────────────────────────────────────────
    for i in g.parse_tr_intereses(path):
        fecha_i = _to_date(i["fecha"])
        importe_i = _to_decimal(i["importe_eur"])
        candidatas.append(TxCandidata(
            fecha=fecha_i,
            tipo="INTEREST",
            isin="CASH-INTEREST",        # ISIN sintético para que tenga posicion
            nombre="Cuenta remunerada Trade Republic",
            cantidad=Decimal("0"),
            precio_local=Decimal("0"),
            divisa_local="EUR",
            importe_local=importe_i,
            fx_rate=Decimal("1"),
            importe_eur=importe_i,
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=_to_decimal(i.get("retencion_es_eur", 0)),
            retencion_pais="ES",
            external_id=_synthetic_external_id(
                "tr-int", "CASH-INTEREST", fecha_i, "INTEREST",
                Decimal("0"), importe_i,
            ),
            broker_id=broker_id,
            notas=i.get("fuente"),
        ))

    # ── Staking (FREE_RECEIPT crypto) ──────────────────────────────────────
    for s in g.parse_tr_staking(path):
        fecha_s = _to_date(s["fecha"])
        isin_s = s.get("isin") or f"CRYPTO-{s['asset']}"
        cantidad_s = _to_decimal(s["cantidad"])
        importe_s = _to_decimal(s["importe_eur"])
        candidatas.append(TxCandidata(
            fecha=fecha_s,
            tipo="STAKING_REWARD",
            isin=isin_s,
            nombre=s.get("fuente") or s["asset"],
            cantidad=cantidad_s,
            precio_local=_to_decimal(s.get("precio_unit_eur", 0)),
            divisa_local="EUR",
            importe_local=importe_s,
            fx_rate=Decimal("1"),
            importe_eur=importe_s,
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=_synthetic_external_id(
                "tr-stk", isin_s, fecha_s, "STAKING_REWARD",
                cantidad_s, importe_s,
            ),
            broker_id=broker_id,
            notas=s.get("fuente"),
        ))

    return candidatas


def _precio_desde_importe(importe_eur: Decimal, cantidad: Decimal) -> Decimal:
    """Recompone el precio unitario desde importe / cantidad cuando el parser
    no lo expone directamente (caso BUY/SELL de TR en la API de Cuádrate)."""
    if cantidad == 0:
        return Decimal("0")
    return (importe_eur / cantidad).quantize(Decimal("0.0000000001"))


def _synthetic_external_id(
    prefix: str,
    isin: str,
    fecha: "date",
    tipo: str,
    cantidad: Decimal,
    importe_eur: Decimal,
) -> str:
    """Genera un external_id estable y determinista para filas sin UUID nativo.

    Caso real DEGIRO: ~10% de las filas no tienen Order ID (corto forzado
    intra-broker §3.9, scrip dividends, ciertas M&A). Sin external_id la
    dedup `(broker_id, external_id)` falla y cada re-import del CSV vuelve a
    insertarlas, inflando la BD.

    Construimos un identificador con (isin + fecha + tipo + cantidad +
    importe). Es único en la práctica para esta combinación; el riesgo de
    colisión (mismo broker hace dos ops idénticas el mismo día) es bajo y,
    en el peor caso, la segunda quedaría deduplicada incorrectamente — al
    revés de duplicar es preferible.
    """
    return (
        f"{prefix}-{isin}-{fecha.isoformat()}-{tipo}-"
        f"{int(cantidad * 10000)}-{int(importe_eur * 100)}"
    )


# ── DEGIRO ─────────────────────────────────────────────────────────────────

def parse_degiro_csv(
    csv_path: str | Path,
    broker_id: str,
    cuenta_path: str | Path | None = None,
    avisos: list[str] | None = None,
) -> list[TxCandidata]:
    """Parsea `DeGiro_Transacciones_YYYY.csv` y devuelve `TxCandidata[]`.

    `cuenta_path` (opcional): si se proporciona el CSV de cuenta del mismo
    período, se extraen también las tasas externas (ITF España, UK/HK Stamp
    Duty, French FTT) y se suman a `tasas_externas_eur` por orden, igual que
    hace Cuádrate (Art. 35.1.b LIRPF + DGT V1989-21).

    `avisos` (opcional): lista mutable a la que se anexan avisos sobre
    eventos corporativos detectados que NO se auto-procesan (spin-offs,
    rights, complejos, etc.) — requieren acción manual del usuario.

    BUY/SELL → TxCandidata directas. SPLIT → TxCandidata tipo
    CORPORATE_SPLIT con qty_old/qty_new en `notas` (JSON). El resto de
    eventos corporativos se emiten como avisos descriptivos.
    """
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    path = str(csv_path)
    candidatas: list[TxCandidata] = []

    # Tasas externas (ITF, Stamp Duty, FTT) por order_id, si hay cuenta.
    external_fees = None
    if cuenta_path is not None:
        try:
            external_fees = g._build_degiro_external_fees(str(cuenta_path))
        except Exception:
            # No bloqueamos la importación si el cuenta falla — sólo perdemos
            # las tasas externas. El usuario lo verá en avisos del resultado.
            external_fees = None

    # `parse_degiro` no filtra por año; entrega todas las operaciones del CSV.
    # Devuelve (BUY/SELL, splits, descartadas, info_corporate). sp_ops llevan
    # tipo='SP' con qty_old/qty_new para que el motor FIFO aplique el split a
    # los lotes existentes. SIN procesar splits, posiciones que han hecho
    # split (NVDA 2021/2024, GOOGL 2022, AMZN 2022, etc.) tienen ventas
    # huérfanas porque la cantidad real es N veces la que Cima ve.
    operaciones, sp_ops, _descartadas, info_ca = g.parse_degiro(
        path, external_fees_by_order=external_fees,
    )

    if avisos is not None:
        _emitir_avisos_eventos_corporativos(info_ca, avisos)

    for op in operaciones:
        tipo_cuadrate = op["tipo"]              # 'A' (compra) o 'T' (venta)
        tipo_cima = "BUY" if tipo_cuadrate == "A" else "SELL"

        importe_eur = _to_decimal(op["importe_eur"])
        cantidad = _to_decimal(op["cantidad"])
        gastos_broker = _to_decimal(op.get("gastos_broker", 0))
        gastos_autofx = _to_decimal(op.get("gastos_autofx", 0))
        gastos_externos = _to_decimal(op.get("gastos_externos", 0))

        fecha = _to_date(op["fecha"])
        order_id = op.get("_order_id") or None
        # Fallback determinista para las ~10% de filas sin UUID nativo
        # (corto forzado §3.9, scrip, M&A). Sin esto se reinsertaban en
        # cada reimport del CSV.
        external_id = order_id or _synthetic_external_id(
            "dg-noid", op["isin"], fecha, tipo_cima, cantidad, importe_eur,
        )
        candidatas.append(TxCandidata(
            fecha=fecha,
            tipo=tipo_cima,
            isin=op["isin"],
            nombre=op.get("nombre"),
            cantidad=cantidad,
            precio_local=_precio_desde_importe(importe_eur, cantidad),
            # DEGIRO ya da el importe convertido a EUR; el detalle de divisa
            # nativa (USD/GBP/GBX) lo perdemos en esta v1 — pendiente cuando
            # añadamos PM por divisa para reporting.
            divisa_local="EUR",
            importe_local=importe_eur,
            fx_rate=Decimal("1"),
            importe_eur=importe_eur,
            gastos_eur=gastos_broker + gastos_autofx,
            tasas_externas_eur=gastos_externos,
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=external_id,
            broker_id=broker_id,
            notas=None if order_id else "Sin order_id DEGIRO — id sintético",
        ))

    # ── Dividendos del cuenta CSV (si se proporcionó) ───────────────────
    # Unificado en un único parser para que la UI suba ambos ficheros (T+C)
    # en un solo paso y produzca un único ImportResultado consolidado.
    if cuenta_path is not None:
        try:
            candidatas.extend(parse_degiro_cuenta_csv(cuenta_path, broker_id))
        except Exception as e:
            if avisos is not None:
                avisos.append(
                    f"[CUENTA] No se pudo parsear el CSV de cuenta: {e}. "
                    f"Las transacciones del fichero principal se han procesado igualmente."
                )

    # ── Splits / contrasplits ───────────────────────────────────────────
    import json as _json
    for sp in sp_ops:
        fecha_sp = _to_date(sp["fecha"])
        qty_old = _to_decimal(sp["cantidad"])       # estructura motor reusa fields
        qty_new = _to_decimal(sp["importe_eur"])
        nominal_old = _to_decimal(sp.get("gastos_eur", 1))
        isin_sp = sp["isin"]
        nombre_sp = (sp.get("nombre") or isin_sp)[:50]

        # En el modelo Cima guardamos `cantidad` = qty_new (post-split, lo
        # que el usuario tiene tras el evento) y serializamos qty_old +
        # nominal_old en `notas` como JSON para reconstruir el dict del motor.
        meta = {
            "split": {
                "qty_old": str(qty_old),
                "qty_new": str(qty_new),
                "nominal_old": str(nominal_old),
                "isin_old": sp.get("_isin_old", isin_sp),
            }
        }
        # external_id determinista por evento corporativo
        ext_id_sp = _synthetic_external_id(
            "dg-split", isin_sp, fecha_sp, "CORPORATE_SPLIT", qty_old, qty_new,
        )
        candidatas.append(TxCandidata(
            fecha=fecha_sp,
            tipo="CORPORATE_SPLIT",
            isin=isin_sp,
            nombre=nombre_sp,
            cantidad=qty_new,                    # post-split: las unidades que tendrás
            precio_local=Decimal("0"),
            divisa_local="EUR",
            importe_local=Decimal("0"),
            fx_rate=Decimal("1"),
            importe_eur=Decimal("0"),
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=ext_id_sp,
            broker_id=broker_id,
            notas=_json.dumps(meta, separators=(",", ":")),
        ))

    return candidatas


def _emitir_avisos_eventos_corporativos(
    info_ca: dict, avisos: list[str]
) -> None:
    """Genera avisos descriptivos para eventos corporativos detectados por
    `parse_degiro` que NO se auto-procesan (todo lo que no sea SPLIT).

    SPLITs sí se inyectan como TxCandidata `CORPORATE_SPLIT` y los aplica
    el FIFO. El resto requiere decisión fiscal o datos del folleto que el
    parser no tiene → mejor avisar y dejar al usuario aplicar la corrección.
    """
    # ── SPIN_OFFs (info_ca['name_changes']) ────────────────────────────
    for ev in info_ca.get("name_changes", []):
        if ev.get("tipo_ca") != "SPIN_OFF":
            continue
        avisos.append(
            f"[SPIN_OFF] {ev.get('fecha','?')} · {ev.get('nombre_matriz') or '?'}"
            f" ({ev.get('isin_old')}) → {ev.get('nombre','?')} ({ev.get('isin_new')}). "
            f"Cima NO reparte coste base automáticamente. Edita las dos posiciones "
            f"a mano según ratios del folleto (típicamente valor de mercado split-off date)."
        )

    # ── ISIN_CHANGE (info_ca['isin_chgs']) ─────────────────────────────
    for ev in info_ca.get("isin_chgs", []):
        avisos.append(
            f"[ISIN_CHANGE] {ev.get('fecha','?')} · {ev.get('isin_old','?')} → "
            f"{ev.get('isin_new','?')} ({ev.get('nombre','?')}). "
            f"Los lotes del ISIN antiguo NO se migran automáticamente al nuevo. "
            f"Migra manualmente para preservar coste y fecha de adquisición."
        )

    # ── RIGHTS asignados (info_ca['derechos']) ────────────────────────
    for ev in info_ca.get("derechos", []):
        avisos.append(
            f"[RIGHTS asignados] {ev.get('fecha','?')} · {ev.get('cantidad','?')} "
            f"derechos {ev.get('isin','?')} ({ev.get('nombre','?')}). "
            f"Coste 0 a efectos fiscales. Si los vendiste o ejerciste, la operación "
            f"debería estar registrada como BUY/SELL separada."
        )

    # ── RIGHTS_EXERCISED (info_ca['rights_exercised']) ────────────────
    for ev in info_ca.get("rights_exercised", []):
        avisos.append(
            f"[RIGHTS_EXERCISED] {ev.get('fecha','?')} · {ev.get('qty','?')} derechos de "
            f"{ev.get('isin','?')} ejercidos → acciones ordinarias. "
            f"Verifica que la compra de las acciones nuevas figura como BUY con su "
            f"precio de suscripción."
        )

    # ── COMPLEJOS — requieren revisión manual ──────────────────────────
    for ev in info_ca.get("complejos", []):
        avisos.append(
            f"[COMPLEX] {ev.get('fecha','?')} · {ev.get('isin','?')} ({ev.get('nombre','?')}): "
            f"{ev.get('descripcion','evento corporativo no clasificado')}. Revisión manual."
        )

    # ── POSIBLES_LIBERADAS — heurística sospechosa ─────────────────────
    for ev in info_ca.get("posibles_liberadas", []):
        avisos.append(
            f"[POSIBLE acción liberada] {ev.get('fecha','?')} · {ev.get('isin','?')} "
            f"({ev.get('nombre','?')}). Verifica en tu CSV si fue scrip o acción gratuita."
        )

    # ── MARKET_TRANSFERS (cortos forzados, cambios de mercado) ────────
    for ev in info_ca.get("market_transfers", []):
        tipo_ca = ev.get("tipo_ca", "MARKET_TRANSFER")
        avisos.append(
            f"[{tipo_ca}] {ev.get('fecha','?')} · {ev.get('isin','?')} "
            f"({ev.get('nombre','?')}): {ev.get('descripcion','transferencia intra-broker')}. "
            f"Sin alteración patrimonial (Art. 33 LIRPF)."
        )


# ── DEGIRO — CSV de Cuenta (dividendos + retenciones) ─────────────────────


def parse_degiro_cuenta_csv(
    csv_path: str | Path,
    broker_id: str,
) -> list[TxCandidata]:
    """Parsea `DeGiro_Cuenta_YYYY.csv` y devuelve `TxCandidata[]` con
    dividendos (`DIVIDEND`) consolidados con sus retenciones ES.

    El parser de Cuádrate (`parse_degiro_cuenta`) filtra por `EJERCICIO`
    global. Para Cima (tracker multi-año) iteramos ese global por todos
    los años razonables y concatenamos. Es un hack pragmático válido en
    modo Owner / beta single-user; en SaaS multi-usuario hay race condition
    sobre el global y habrá que refactorizar Cuádrate (parámetro explícito)
    cuando llegue ese momento.

    Cuádrate emite DIV y RET como filas separadas; las reagrupamos por
    (isin, fecha) para producir un único `DIVIDEND` con `retencion_eur`.

    Idempotencia: como las filas no traen `transaction_id`, construimos un
    `external_id` determinista desde (isin, fecha, importe). Al reimportar
    el mismo cuenta CSV se deduplica vía `(broker_id, external_id)`.
    """
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    path = str(csv_path)

    # Iterar EJERCICIO para capturar dividendos de cualquier año del fichero.
    all_dividendos: list[dict] = []
    original_ej = g.EJERCICIO
    try:
        for year in range(2010, 2030):
            g.EJERCICIO = str(year)
            try:
                resultados, _ejercidas, _gastos = g.parse_degiro_cuenta(path)
            except Exception:
                # Año sin filas → la propia función ya devuelve [], pero
                # blindamos contra cualquier fallo de parsing inesperado.
                continue
            all_dividendos.extend(resultados)
    finally:
        g.EJERCICIO = original_ej

    return _consolidar_dividendos(
        all_dividendos, broker_id, prefix="dg-cuenta-div", notas="DEGIRO cuenta",
    )


def _consolidar_dividendos(
    dividendos: list[dict],
    broker_id: str,
    prefix: str,
    notas: str,
) -> list[TxCandidata]:
    """Reagrupa filas DIV+RET (formato Cuádrate) por (isin, fecha) en una
    única `TxCandidata` DIVIDEND con su retención. Usado por DEGIRO cuenta
    e IBKR (ambos parsers devuelven el mismo shape de dicts).

    `external_id` determinista (`{prefix}-{isin}-{fecha}-{centimos}`) para
    que reimportar deduplique.
    """
    div_index: dict[tuple, dict] = {}
    for d in dividendos:
        key = (d["isin"], _to_date(d["fecha"]))
        slot = div_index.setdefault(
            key, {"DIV": None, "RET": None, "nombre": d.get("nombre")}
        )
        slot[d["tipo"]] = d

    candidatas: list[TxCandidata] = []
    for (isin, fecha), slot in div_index.items():
        div = slot.get("DIV")
        ret = slot.get("RET")
        if not div:
            continue   # retención huérfana sin dividendo → ignorar
        if not isin:
            continue   # sin ISIN no podemos crear posición

        importe_bruto = _to_decimal(div["importe_eur"])
        retencion = _to_decimal(ret["importe_eur"]) if ret else Decimal("0")
        divisa_local = div.get("divisa", "EUR") or "EUR"

        ext_id = f"{prefix}-{isin}-{fecha.isoformat()}-{int(importe_bruto * 100)}"

        # Retención del país emisor del valor (Cuádrate etiqueta `pais`).
        retencion_pais = div.get("pais") or None
        if retencion == 0:
            retencion_pais = None

        candidatas.append(TxCandidata(
            fecha=fecha,
            tipo="DIVIDEND",
            isin=isin,
            nombre=slot.get("nombre") or isin,
            cantidad=Decimal("0"),
            precio_local=Decimal("0"),
            divisa_local=divisa_local,
            importe_local=importe_bruto,
            fx_rate=Decimal("1"),
            importe_eur=importe_bruto,
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=retencion,
            retencion_pais=retencion_pais,
            external_id=ext_id,
            broker_id=broker_id,
            notas=notas,
        ))
    return candidatas


# ── IBKR — Activity Statement (un solo fichero: trades + div + corp) ──────


def parse_ibkr_csv(
    csv_path: str | Path,
    broker_id: str,
    avisos: list[str] | None = None,
) -> list[TxCandidata]:
    """Parsea un IBKR Activity Statement (CSV) y devuelve `TxCandidata[]`.

    A diferencia de DEGIRO (Transacciones + Cuenta en dos ficheros), el
    Activity Statement de IBKR contiene TODO en un único CSV: Trades,
    Corporate Actions, Dividends, Withholding Tax, Interest. Por eso este
    parser no toma `cuenta_path`.

    Genera:
      - BUY/SELL desde la sección Trades.
      - CORPORATE_SPLIT desde Corporate Actions (igual que DEGIRO).
      - DIVIDEND consolidando Dividends + Withholding Tax.
      - INTEREST desde la sección de intereses.
      - Avisos para eventos corporativos no auto-procesables.

    Exportar desde IBKR con Base Currency = EUR y secciones Trades +
    Corporate Actions + Dividends + Withholding Tax.
    """
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    import json as _json

    path = str(csv_path)
    candidatas: list[TxCandidata] = []

    operaciones, sp_ops, _descartadas, info_ca = g.parse_ibkr(path)
    if avisos is not None:
        _emitir_avisos_eventos_corporativos(info_ca, avisos)

    # ── BUY / SELL ──────────────────────────────────────────────────────
    for op in operaciones:
        tipo_cima = "BUY" if op["tipo"] == "A" else "SELL"
        importe_eur = _to_decimal(op["importe_eur"])
        cantidad = _to_decimal(op["cantidad"])
        gastos_externos = _to_decimal(op.get("gastos_externos", 0))
        gastos_total = _to_decimal(op.get("gastos_eur", 0))
        gastos_broker = gastos_total - gastos_externos
        isin = op.get("isin") or ""
        if not isin:
            if avisos is not None:
                avisos.append(
                    f"[IBKR] Trade sin ISIN resoluble ({op.get('nombre','?')} "
                    f"{op.get('fecha','?')}) — omitido."
                )
            continue
        fecha = _to_date(op["fecha"])
        # IBKR no expone un trade-id estable en la sección Trades → sintético.
        ext_id = _synthetic_external_id(
            "ibkr", isin, fecha, tipo_cima, cantidad, importe_eur,
        )
        candidatas.append(TxCandidata(
            fecha=fecha,
            tipo=tipo_cima,
            isin=isin,
            nombre=op.get("nombre"),
            cantidad=cantidad,
            precio_local=_precio_desde_importe(importe_eur, cantidad),
            divisa_local="EUR",          # IBKR Activity Statement en EUR
            importe_local=importe_eur,
            fx_rate=Decimal("1"),
            importe_eur=importe_eur,
            gastos_eur=gastos_broker if gastos_broker > 0 else Decimal("0"),
            tasas_externas_eur=gastos_externos,
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=ext_id,
            broker_id=broker_id,
            notas=None,
        ))

    # ── Splits ──────────────────────────────────────────────────────────
    for sp in sp_ops:
        fecha_sp = _to_date(sp["fecha"])
        qty_old = _to_decimal(sp["cantidad"])
        qty_new = _to_decimal(sp["importe_eur"])
        nominal_old = _to_decimal(sp.get("gastos_eur", 1))
        isin_sp = sp["isin"]
        meta = {
            "split": {
                "qty_old": str(qty_old),
                "qty_new": str(qty_new),
                "nominal_old": str(nominal_old),
                "isin_old": sp.get("_isin_old", isin_sp),
            }
        }
        candidatas.append(TxCandidata(
            fecha=fecha_sp,
            tipo="CORPORATE_SPLIT",
            isin=isin_sp,
            nombre=(sp.get("nombre") or isin_sp)[:50],
            cantidad=qty_new,
            precio_local=Decimal("0"),
            divisa_local="EUR",
            importe_local=Decimal("0"),
            fx_rate=Decimal("1"),
            importe_eur=Decimal("0"),
            gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"),
            retencion_eur=Decimal("0"),
            retencion_pais=None,
            external_id=_synthetic_external_id(
                "ibkr-split", isin_sp, fecha_sp, "CORPORATE_SPLIT", qty_old, qty_new,
            ),
            broker_id=broker_id,
            notas=_json.dumps(meta, separators=(",", ":")),
        ))

    # ── Dividendos (Dividends + Withholding Tax) ───────────────────────
    try:
        divs = g.parse_ibkr_dividendos(path)
        candidatas.extend(
            _consolidar_dividendos(divs, broker_id, prefix="ibkr-div", notas="IBKR")
        )
    except Exception as e:
        if avisos is not None:
            avisos.append(f"[IBKR] No se pudieron parsear dividendos: {e}")

    # ── Intereses ───────────────────────────────────────────────────────
    # El tipo (credit/debit/bond_interest) y la casilla los guardamos en
    # `notas` como JSON para que la pestaña de Intereses pueda agruparlos sin
    # re-parsear el CSV.
    try:
        for i in g.parse_ibkr_interest(path):
            fecha_i = _to_date(i["fecha"])
            importe_i = _to_decimal(i["importe_eur"])
            meta_int = {
                "interes": {
                    "tipo": i.get("tipo"),
                    "casilla": i.get("casilla"),
                    "descripcion": i.get("descripcion"),
                    "divisa": i.get("divisa"),
                }
            }
            candidatas.append(TxCandidata(
                fecha=fecha_i,
                tipo="INTEREST",
                isin="CASH-INTEREST-IBKR",
                nombre="Intereses IBKR",
                cantidad=Decimal("0"),
                precio_local=Decimal("0"),
                divisa_local="EUR",
                importe_local=importe_i,
                fx_rate=Decimal("1"),
                importe_eur=importe_i,
                gastos_eur=Decimal("0"),
                tasas_externas_eur=Decimal("0"),
                retencion_eur=Decimal("0"),
                retencion_pais=None,
                external_id=_synthetic_external_id(
                    "ibkr-int", "CASH-INTEREST-IBKR", fecha_i, "INTEREST",
                    Decimal("0"), importe_i,
                ),
                broker_id=broker_id,
                notas=_json.dumps(meta_int, separators=(",", ":")),
            ))
    except Exception as e:
        if avisos is not None:
            avisos.append(f"[IBKR] No se pudieron parsear intereses: {e}")

    return candidatas


# ── IBKR — Resultados de periodo (forex + letras del tesoro) ──────────────


def _ejercicio_de_periodo(path: str) -> tuple[int, "date | None", "date | None"]:
    """Deriva (ejercicio, inicio, fin) del periodo del statement IBKR.
    El ejercicio es el año de la fecha fin (las cifras realized son del
    periodo cubierto; el usuario exporta normalmente un año natural)."""
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    inicio, fin = g._parse_ibkr_statement_period(path)
    ref = fin or inicio
    ejercicio = ref.year if ref else date.today().year
    return ejercicio, inicio, fin


def parse_ibkr_resultados(
    csv_path: str | Path, broker_id: str,
) -> list[ResultadoCandidata]:
    """Forex + T-Bills de la 'Realized & Unrealized Performance Summary' IBKR.

    Forex → Art. 33.5.e (G/P patrimonial). T-Bills → RCM. Cifras en EUR.
    El ejercicio se deriva del periodo del statement.
    """
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    path = str(csv_path)
    ejercicio, inicio, fin = _ejercicio_de_periodo(path)
    fx = g.parse_ibkr_fx_pl(path)
    out: list[ResultadoCandidata] = []

    for f in fx.get("fx", []):
        divisa = str(f.get("divisa") or "?")
        out.append(ResultadoCandidata(
            categoria="FOREX",
            ejercicio=ejercicio,
            clave=divisa,
            realized_eur=_to_decimal(f.get("realized")),
            unrealized_eur=_to_decimal(f.get("unrealized")),
            periodo_inicio=inicio,
            periodo_fin=fin,
            external_id=f"ibkr-fx-{ejercicio}-{divisa}",
            broker_id=broker_id,
        ))
    for t in fx.get("tbills", []):
        sym = str(t.get("symbol") or "?")[:120]
        out.append(ResultadoCandidata(
            categoria="TBILL",
            ejercicio=ejercicio,
            clave=sym,
            realized_eur=_to_decimal(t.get("realized")),
            unrealized_eur=Decimal("0"),
            periodo_inicio=inicio,
            periodo_fin=fin,
            external_id=f"ibkr-tbill-{ejercicio}-{sym[:60]}",
            broker_id=broker_id,
        ))
    return out


def parse_ibkr_complejos(
    csv_path: str | Path, broker_id: str,
) -> list[ComplejoCandidata]:
    """Productos NO soportados por el motor (CFD/futuro/warrant/estructurado/
    fondo/cripto según Asset Category IBKR). Sólo detección, sin fiscalidad."""
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    path = str(csv_path)
    ejercicio, _inicio, _fin = _ejercicio_de_periodo(path)
    _ops, _sp, descartadas, _ca = g.parse_ibkr(path)
    out: list[ComplejoCandidata] = []
    for d in descartadas.get("categoria_no_soportada", []):
        fecha_d = None
        try:
            fecha_d = _to_date(d.get("fecha")) if d.get("fecha") else None
        except ValueError:
            fecha_d = None
        simbolo = str(d.get("symbol") or "?")[:120]
        importe = _to_decimal(d.get("importe_eur"))
        cantidad = _to_decimal(d.get("cantidad"))
        out.append(ComplejoCandidata(
            ejercicio=fecha_d.year if fecha_d else ejercicio,
            fecha=fecha_d,
            simbolo=simbolo,
            isin=(d.get("isin") or None),
            nombre=str(d.get("nombre") or simbolo)[:120],
            asset_category=str(d.get("asset_category") or ""),
            cantidad=cantidad,
            importe_eur=importe,
            external_id=_synthetic_external_id(
                "ibkr-cplx", simbolo, fecha_d or date(ejercicio, 1, 1),
                "COMPLEJO", cantidad, importe,
            ),
            broker_id=broker_id,
        ))
    return out


# ── Opciones (derivados) ────────────────────────────────────────────────────


def _opcion_dict_a_candidata(op: dict, broker_id: str) -> OpcionCandidata:
    """Convierte el dict de Cuádrate (parse_opciones_*) a OpcionCandidata."""
    fecha = _to_date(op["fecha"])
    cantidad = _to_decimal(op["cantidad"])
    importe = _to_decimal(op["importe_eur"])
    accion = op["accion"]
    simbolo = op.get("simbolo") or op.get("subyacente") or "?"
    # external_id determinista: símbolo + fecha + acción + importe en céntimos.
    # Si el parser propaga `order_id`/`TransactionID` (DEGIRO/IBKR), lo añadimos
    # como sufijo para distinguir dos órdenes del mismo contrato/día/importe
    # (caso real: rolling intra-día, lotes idénticos). NO se incluye en el
    # core para preservar compatibilidad con opciones ya importadas con la
    # versión vieja del parser — su external_id sigue siendo el mismo y el
    # re-import las deduplica correctamente.
    ext_id = (
        f"opt-{broker_id[:6]}-{simbolo}-{fecha.isoformat()}-{accion}-"
        f"{int(importe * 100)}-{int(cantidad)}"
    )
    order_id = op.get("order_id")
    if order_id:
        ext_id = f"{ext_id}-ord{order_id}"
    return OpcionCandidata(
        fecha=fecha,
        simbolo=simbolo,
        isin=(op.get("isin") or None),
        tipo_op=op.get("tipo_op") or "?",
        subyacente=op.get("subyacente") or "",
        strike=str(op.get("strike") or ""),
        vencimiento=op.get("vencimiento") or "",
        accion=accion,
        cantidad=cantidad,
        prima_unitaria=_to_decimal(op.get("prima_unitaria", 0)),
        importe_eur=importe,
        gastos_eur=_to_decimal(op.get("gastos_eur", 0)),
        expirada=bool(op.get("expirada")),
        ejercida=bool(op.get("ejercida")),
        external_id=ext_id,
        broker_id=broker_id,
    )


def _build_degiro_ejercicio_subyacente_map(cuenta_path: str | Path) -> dict[str, str]:
    """Mapa option_isin → underlying_isin desde los eventos "OPCIÓN EJERCIDA"
    del cuenta DEGIRO. Cada ejercicio comparte (fecha, hora) entre la línea
    del subyacente (ISIN real) y la de la opción (ISIN tipo NLEX/ES0A...)."""
    import csv as _csv

    grupos: dict[tuple, list[tuple[str, str]]] = {}
    try:
        with open(str(cuenta_path), encoding="utf-8") as f:
            reader = _csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 6:
                    continue
                desc = (row[5] or "").upper()
                if "OPCIÓN EJERCIDA" not in desc and "OPCION EJERCIDA" not in desc:
                    continue
                isin = (row[4] or "").strip()
                if not isin:
                    continue
                key = (row[0].strip(), row[1].strip())  # fecha, hora
                grupos.setdefault(key, []).append((isin, (row[3] or "").strip()))
    except Exception:
        return {}

    mapa: dict[str, str] = {}
    for _key, items in grupos.items():
        # En cada grupo: la opción tiene ISIN de opción; el subyacente, ISIN real.
        opciones = [(i, n) for i, n in items if _es_isin_opcion(i, n)]
        subyacentes = [(i, n) for i, n in items if not _es_isin_opcion(i, n)]
        if opciones and subyacentes:
            sub_isin = subyacentes[0][0]
            for op_isin, _n in opciones:
                mapa[op_isin] = sub_isin
    return mapa


def _es_isin_opcion(isin: str, nombre: str) -> bool:
    """Heurística: ISIN de opción DEGIRO (NLEX*, ES0A*, DE000F*, FREX*...) o
    nombre con patrón C/P + strike + vencimiento."""
    import re as _re
    if _re.match(r"^(NLEX|FREX|ES0A|DE000[A-Z]|GB00B[A-Z0-9]*)", isin or ""):
        # Aproximación: los subyacentes de acciones no usan estos prefijos de
        # warrant/opción. Reforzamos con el patrón de nombre.
        pass
    return bool(_re.search(r"\s[CP]\s?\d+[.,]?\d*\s+\d{2}[A-Z]{3}\d{2}", nombre or ""))


# UUID v4 al estilo que emite DEGIRO en la columna ID Orden (lo replicamos
# aquí en vez de importarlo del parser vendorizado: Cima no toca vendor/).
import re as _re
_RE_DEGIRO_OID = _re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.I,
)


def _degiro_order_id(row: list[str]) -> str:
    """Localiza el OrderID en cualquier celda de la fila (DEGIRO inserta
    columnas vacías cuando la orden se ejecuta partida en varios mercados)."""
    for cell in reversed(row):
        s = (cell or "").strip()
        if s and _RE_DEGIRO_OID.match(s):
            return s
    return ""


def _anotar_order_ids_degiro(transacciones_path: str | Path, raw_rows: list[list[str]],
                              opciones: list[dict]) -> None:
    """Anota cada opción con el OrderID del DEGIRO. El parser vendorizado lo usa
    internamente para agrupar splits pero NO lo propaga al dict de salida; sin
    él, dos órdenes del mismo contrato+día+cantidad+importe colapsan al mismo
    `external_id` sintético (bug real: rolling de puts el mismo día).

    Construimos un mapa (ISIN_opcion, fecha) → primer OrderID encontrado y lo
    inyectamos. Las filas sin OrderID (~10%, p.ej. opciones expiradas con
    precio 0) usan el fallback `noorder-fecha-isin`.

    Vive en el adaptador (no en el parser vendorizado): Cima nunca toca
    `vendor/cuadrate/`."""
    mapa: dict[tuple[str, str], str] = {}
    for row in raw_rows:
        if len(row) < 16:
            continue
        oid = _degiro_order_id(row)
        if not oid:
            continue
        isin_row = (row[3] or "").strip()
        fecha_row = (row[0] or "").strip()
        key = (isin_row, fecha_row)
        mapa.setdefault(key, oid)
    for o in opciones:
        isin = (o.get("isin") or "").strip()
        f = o.get("fecha")
        # `fecha` en el dict ya es `date` parseado por el parser; recuperamos
        # el formato original del CSV (dd-mm-YYYY) para el lookup.
        fecha_str = f.strftime("%d-%m-%Y") if hasattr(f, "strftime") else ""
        oid = mapa.get((isin, fecha_str))
        o["order_id"] = oid or f"noorder-{fecha_str}-{isin}"


def _anotar_order_ids_ibkr(csv_path: str | Path, opciones: list[dict]) -> None:
    """Anota cada opción con el TransactionID/TradeID/IBOrderID del Activity
    Statement de IBKR. Sin él, dos trades del mismo contrato/día/cantidad/
    importe colapsan al mismo `external_id` (caso real Angel: vender el mismo
    strike de OWL en días distintos con vencimientos distintos cuando además
    la prima coincide al céntimo).

    Estrategia: segunda pasada al CSV indexando trades de opciones por su
    firma `(symbol, fecha, qty, proceeds)` → TransactionID. Es el mismo
    matching que hace el parser de Cuádrate, en paralelo. Fallback al
    `Date/Time` completo si IBKR no expone TransactionID/TradeID en este
    statement (incluye hora:minuto:segundo, suficiente intra-día)."""
    import csv as _csv

    mapa: dict[tuple[str, str, str, str], str] = {}
    try:
        with open(str(csv_path), encoding="utf-8") as f:
            reader = _csv.reader(f)
            header: list[str] | None = None
            for row in reader:
                if not row or len(row) < 5:
                    continue
                if row[0] == "Trades" and row[1] == "Header":
                    header = row
                    continue
                if row[0] == "Trades" and row[1] == "Data" and header:
                    def col(name: str) -> str:
                        return row[header.index(name)].strip() if name in header else ""
                    if "Option" not in col("Asset Category"):
                        continue
                    qty = col("Quantity")
                    if not qty or qty in ("Quantity", "Total"):
                        continue
                    sig = (col("Symbol"), col("Date/Time"), qty, col("Proceeds"))
                    tid = (col("TransactionID") or col("TradeID")
                           or col("IBOrderID") or col("Date/Time"))
                    if tid:
                        # Si el mismo trade aparece más de una vez (no debería),
                        # nos quedamos con el primero — el resto se trata como
                        # entradas distintas si llegasen a las candidatas.
                        mapa.setdefault(sig, tid)
    except Exception:
        return

    for o in opciones:
        # El parser ya truncó symbol a 40 chars; aquí necesitamos la firma
        # ORIGINAL para el lookup. Como no la tenemos, usamos los campos del
        # parser (simbolo, cantidad, importe) y buscamos el ID con la firma
        # de IBKR recompuesta a partir de ellos. Si no encuentra, deja sin
        # order_id y el external_id se mantiene como legacy (compatibilidad).
        cantidad = o.get("cantidad")
        importe = o.get("importe_eur")
        simbolo = (o.get("simbolo") or "")[:40]
        # En IBKR, el `proceeds` en el CSV puede llevar signo; los importes en
        # el dict del parser ya son absolutos. Buscamos por una firma flexible:
        # primer match cuyo Symbol[:40] coincide y qty (absoluto) coincide.
        oid: str | None = None
        for (sym, dt, q, pr), tid in mapa.items():
            if sym[:40] != simbolo:
                continue
            try:
                if abs(_to_decimal(q)) != _to_decimal(cantidad):
                    continue
                if abs(_to_decimal(pr)) != _to_decimal(importe):
                    continue
            except Exception:
                continue
            oid = tid
            break
        if oid:
            o["order_id"] = oid


# Divisas IBKR comunes (las que Cuádrate sabe traer del BCE). Antes de invocar
# al parser, le pedimos `fetch_ecb_rates(..., min_fecha=hoy)` para que refresque
# si está stale parcial (caso real Angel: cache hasta 8-may, opciones del 28-may
# descartadas por falta de FX). El parámetro `min_fecha` lo añadió el dev de
# Cuádrate justo para este caso.
_IBKR_DIVISAS_FX = {"USD", "GBP", "DKK", "HKD", "CHF", "PLN", "AED", "SEK",
                    "NOK", "JPY", "CAD", "AUD", "CNY", "SGD", "KRW"}


def parse_degiro_opciones(
    transacciones_path: str | Path,
    broker_id: str,
    cuenta_path: str | Path | None = None,
) -> list[OpcionCandidata]:
    """Extrae operaciones de opciones del CSV de Transacciones DEGIRO.

    Necesita el CSV de cuenta para `ejercidas_isin` (qué opciones se
    ejercieron vs expiraron) y para mapear opción→subyacente en ejercicios.
    Sin cuenta, todas se tratan como expiradas/no-ejercidas.
    """
    _ensure_cuadrate_importable()
    import csv as _csv
    import generar_irpf as g  # type: ignore[import-not-found]

    raw_rows: list[list[str]] = []
    with open(str(transacciones_path), encoding="utf-8") as f:
        reader = _csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 15:
                raw_rows.append(row)

    ejercidas_isin: set = set()
    sub_map: dict[str, str] = {}
    if cuenta_path is not None:
        try:
            _res, ejercidas_isin, _gastos = g.parse_degiro_cuenta(str(cuenta_path))
        except Exception:
            ejercidas_isin = set()
        sub_map = _build_degiro_ejercicio_subyacente_map(cuenta_path)

    opciones = g.parse_opciones_degiro(raw_rows, ejercidas_isin)
    _anotar_order_ids_degiro(transacciones_path, raw_rows, opciones)
    cands = []
    for o in opciones:
        c = _opcion_dict_a_candidata(o, broker_id)
        if c.ejercida and c.isin in sub_map:
            c.subyacente_isin = sub_map[c.isin]
        cands.append(c)
    return cands


def parse_ibkr_opciones(
    csv_path: str | Path,
    broker_id: str,
    avisos: list[str] | None = None,
) -> list[OpcionCandidata]:
    """Extrae operaciones de opciones del Activity Statement de IBKR.

    Pre-refresca el cache BCE con `min_fecha=hoy` para que cubra hasta el día
    en curso (sin esto, una caché stale parcial dejaba descartadas las opciones
    de los últimos días por falta de tipo USD/EUR — caso real Angel 28-may).
    Cuádrate devuelve `(opciones, descartadas)`: propagamos `descartadas['sin_fx']`
    via `avisos` para que el import muestre exactamente qué se quedó fuera."""
    from datetime import date as _date

    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    ano = _date.today().year
    try:
        g.fetch_ecb_rates(_IBKR_DIVISAS_FX, str(ano), min_fecha=_date.today().isoformat())
    except Exception:
        pass  # Sin red: degradar; el parser usará el fallback del CSV o descartará.

    opciones, descartadas = g.parse_ibkr_opciones(str(csv_path))
    if avisos is not None:
        sin_fx = (descartadas or {}).get("sin_fx") or []
        for d in sin_fx:
            avisos.append(
                f"[OPCIONES] Descartada por falta de tipo BCE: "
                f"{d.get('symbol', '?')} en {d.get('currency', '?')} ({d.get('fecha', '?')})"
            )
    _anotar_order_ids_ibkr(csv_path, opciones)
    return [_opcion_dict_a_candidata(o, broker_id) for o in opciones]


# ── Mapeo broker_tipo → función parser ─────────────────────────────────────

# Algunos brokers exportan formatos distintos en ficheros separados (DEGIRO
# tiene Transacciones + Cuenta). Para no inventar tipos de Broker artificiales,
# distinguimos el "parser kind" del "broker_tipo" del modelo: el endpoint
# acepta cualquier parser kind como `broker_tipo` y mapea con `broker_tipo_db`
# al tipo del Broker en BD.
_PARSERS: dict[str, callable] = {
    "tr": parse_tr_csv,
    "degiro": parse_degiro_csv,
    "degiro_cuenta": parse_degiro_cuenta_csv,
    "ibkr": parse_ibkr_csv,
}


def broker_tipo_db(parser_kind: str) -> str:
    """Mapa parser_kind (que recibe el endpoint) → broker_tipo (en BD).

    `degiro_cuenta` → `degiro` (mismo Broker, formato distinto del fichero).
    Cualquier otro: identidad.
    """
    return parser_kind.removesuffix("_cuenta")


def parser_para(broker_tipo: str):
    """Devuelve la función de parseo para un broker_tipo, o None si no hay
    adapter implementado todavía."""
    return _PARSERS.get(broker_tipo)


def brokers_soportados() -> list[str]:
    return sorted(_PARSERS.keys())
