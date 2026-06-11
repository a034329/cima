"""Modelos ORM SQLAlchemy 2.

Subconjunto mínimo de ADR-002 para el backbone de transacciones. Las
entidades faltantes (`planes`, `bloques`, `opciones`, `corporate_events`,
`bolsas_fiscales`, `snapshots`) se añadirán cuando las necesite el flujo.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, UTC
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid() -> str:
    """UUID4 como string para portabilidad SQLite/Postgres."""
    return str(uuid.uuid4())


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ── users ──────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    nif: Mapped[str | None] = mapped_column(String(20), nullable=True)
    modo: Mapped[str] = mapped_column(String(10), nullable=False, default="saas")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    carteras: Mapped[list[Cartera]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    brokers: Mapped[list[Broker]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("modo IN ('saas', 'owner')", name="ck_users_modo"),
    )


# ── carteras ───────────────────────────────────────────────────────────────

class Cartera(Base):
    __tablename__ = "carteras"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    # Objetivo de capital para la Independencia Financiera (progreso IF).
    objetivo_if_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("300000")
    )
    # Aportación periódica PREVISTA (mensual) para la proyección de años a IF.
    # 0 = usar las aportaciones reales del año en curso como ritmo.
    aportacion_mensual_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    # Régimen macro (4 indicadores VERDE/AMARILLA/ROJA + fecha) como JSON. Lo fija
    # el usuario; calibra el tamaño/ritmo del tramo de compra. None = sin definir.
    regimen_macro_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    user: Mapped[User] = relationship(back_populates="carteras")
    posiciones: Mapped[list[Posicion]] = relationship(
        back_populates="cartera", cascade="all, delete-orphan"
    )
    transacciones: Mapped[list[Transaccion]] = relationship(
        back_populates="cartera", cascade="all, delete-orphan"
    )


# ── brokers ────────────────────────────────────────────────────────────────

class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker_tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(120), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    divisa_base: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    # Saldo de caja reportado por el broker en el último extracto importado
    # (DEGIRO: última fila Saldo; IBKR: Ending Cash). Para validar la liquidez
    # calculada de cash flows.
    saldo_reportado_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    saldo_fecha: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    user: Mapped[User] = relationship(back_populates="brokers")
    transacciones: Mapped[list[Transaccion]] = relationship(back_populates="broker")

    __table_args__ = (
        CheckConstraint(
            "broker_tipo IN ('degiro','ibkr','tr','trading212','ing','myinvestor','otro')",
            name="ck_brokers_tipo",
        ),
    )


# ── posiciones ─────────────────────────────────────────────────────────────

class Posicion(Base):
    __tablename__ = "posiciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(20), nullable=True)
    nombre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    divisa_local: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    bloque_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Precio actual en EUR fijado manualmente por el usuario (override del feed
    # automático, que es best-effort y puede fallar). Si está, manda sobre el feed.
    precio_manual_eur: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)

    cartera: Mapped[Cartera] = relationship(back_populates="posiciones")
    transacciones: Mapped[list[Transaccion]] = relationship(back_populates="posicion")
    lots: Mapped[list[Lot]] = relationship(
        back_populates="posicion", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", name="uq_posiciones_cartera_isin"),
    )


# ── transacciones ──────────────────────────────────────────────────────────

class Transaccion(Base):
    """Entidad central. Cada operación que afecta a la cartera vive aquí.

    Estados:
      - `pendiente_confirmar`: registrada manualmente por el usuario, todavía
        no aparece en un extracto del broker. Esperando reconciliación.
      - `confirmada`: vino de un extracto o se reconcilió con uno.
      - `descartada`: el usuario la marcó como errónea.

    Origen:
      - `manual`: usuario via UI/API.
      - `extracto_<broker>`: vino de un extracto importado.
    """

    __tablename__ = "transacciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )
    posicion_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posiciones.id", ondelete="CASCADE"), nullable=False
    )

    # Datos de la operación
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    cantidad: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    precio_local: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    divisa_local: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    importe_local: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal("1")
    )
    importe_eur: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    gastos_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    tasas_externas_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    retencion_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    retencion_pais: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Procedencia / dedup / reconciliación
    estado: Mapped[str] = mapped_column(String(30), nullable=False, default="confirmada")
    origen: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    cartera: Mapped[Cartera] = relationship(back_populates="transacciones")
    broker: Mapped[Broker | None] = relationship(back_populates="transacciones")
    posicion: Mapped[Posicion] = relationship(back_populates="transacciones")

    __table_args__ = (
        # Dedup hard: (broker, external_id) único cuando external_id no es null.
        # SQLite no soporta UNIQUE parcial; el repo aplica la verificación
        # antes de insertar y aquí dejamos sólo el índice para query rápida.
        Index("ix_tx_broker_external", "broker_id", "external_id"),
        Index("ix_tx_cartera_fecha", "cartera_id", "fecha"),
        Index("ix_tx_posicion_fecha", "posicion_id", "fecha"),
        Index("ix_tx_estado", "estado"),
        CheckConstraint(
            "tipo IN ("
            "'BUY','SELL','DIVIDEND','INTEREST','STAKING_REWARD',"
            "'CORPORATE_SPLIT','CORPORATE_ISIN_CHANGE','CORPORATE_SCRIP',"
            "'CORPORATE_RIGHTS','CORPORATE_MERGER','CORPORATE_OPA','OTRO'"
            ")",
            name="ck_tx_tipo",
        ),
        CheckConstraint(
            "estado IN ('pendiente_confirmar','confirmada','descartada')",
            name="ck_tx_estado",
        ),
    )


# ── lots (inventario FIFO) ─────────────────────────────────────────────────

    @property
    def isin(self) -> str | None:
        """ISIN de la posición (para la API/tabla de movimientos — la columna
        'ISIN' del frontend mostraba un prefijo del UUID interno, F6)."""
        return self.posicion.isin if self.posicion is not None else None

    @property
    def posicion_nombre(self) -> str | None:
        return self.posicion.nombre if self.posicion is not None else None

class Lot(Base):
    """Lote FIFO. Cada BUY crea un lote; cada SELL consume lotes por orden."""

    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    posicion_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posiciones.id", ondelete="CASCADE"), nullable=False
    )
    transaccion_origen_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transacciones.id", ondelete="SET NULL"), nullable=True
    )
    fecha_compra: Mapped[date] = mapped_column(Date, nullable=False)
    cantidad_inicial: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    cantidad_restante: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    coste_unit_eur: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    coste_total_eur: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    gastos_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    posicion: Mapped[Posicion] = relationship(back_populates="lots")

    __table_args__ = (
        Index("ix_lots_posicion_fecha", "posicion_id", "fecha_compra"),
        CheckConstraint("cantidad_restante >= 0", name="ck_lots_restante_no_neg"),
    )


# ── opciones (derivados) ────────────────────────────────────────────────────

class Opcion(Base):
    """Operación de opción/derivado. No encaja en `Transaccion` (orientada a
    acciones por ISIN) porque tiene su propia identidad: subyacente, strike,
    vencimiento, tipo C/P. El cálculo fiscal (casilla 1626, DGT V2172-21) lo
    hace `calcular_resumen_opciones` de Cuádrate agrupando por contrato.

    `fecha` es la fecha del trade. La clasificación fiscal (normal / ejercida
    / mixta / long-abierta / short-abierta / roll) se computa al vuelo.
    """

    __tablename__ = "opciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    simbolo: Mapped[str] = mapped_column(String(120), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # ISIN del subyacente (solo para opciones ejercidas: la prima ajusta el
    # coste/precio de ESA posición). Se extrae del registro de ejercicio.
    subyacente_isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    tipo_op: Mapped[str] = mapped_column(String(1), nullable=False, default="?")  # C/P/?
    subyacente: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    strike: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    vencimiento: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    accion: Mapped[str] = mapped_column(String(10), nullable=False)  # compra/venta

    cantidad: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    prima_unitaria: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal("0")
    )
    importe_eur: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    gastos_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    expirada: Mapped[bool] = mapped_column(default=False, nullable=False)
    ejercida: Mapped[bool] = mapped_column(default=False, nullable=False)

    estado: Mapped[str] = mapped_column(String(30), nullable=False, default="confirmada")
    origen: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_opciones_cartera_fecha", "cartera_id", "fecha"),
        Index("ix_opciones_broker_external", "broker_id", "external_id"),
        CheckConstraint("accion IN ('compra','venta')", name="ck_opciones_accion"),
        CheckConstraint("estado IN ('confirmada','descartada')", name="ck_opciones_estado"),
    )


# ── preferencias de UI (por cartera) ────────────────────────────────────────

class Preferencias(Base):
    """Preferencias de presentación por cartera. Sin auth todavía: una fila
    por cartera. `columnas_posiciones` es un JSON array con los IDs de columna
    seleccionados para la tabla de Posiciones."""

    __tablename__ = "preferencias"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    columnas_posiciones: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )


# ── aportaciones (dinero del bolsillo del usuario) ───────────────────────────

class Aportacion(Base):
    """Aportación/retirada de capital externo (transferencia desde/hacia el
    banco del usuario). `importe_eur` positivo = aportación, negativo =
    retirada. Permite saber cuánto se aporta de bolsillo propio cada año.

    Origen: `extracto_<broker>` (IBKR Deposits&Withdrawals, TR inbound) o
    `manual` (DEGIRO no expone el dato en sus CSV)."""

    __tablename__ = "aportaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    importe_eur: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origen: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_aportaciones_cartera_fecha", "cartera_id", "fecha"),
        Index("ix_aportaciones_broker_external", "broker_id", "external_id"),
    )


# ── resultados de periodo IBKR (forex + letras del tesoro) ──────────────────

class ResultadoIbkr(Base):
    """Resultado realizado de la 'Realized & Unrealized Performance Summary'
    del Activity Statement IBKR. Son cifras AGREGADAS de periodo (no operación
    a operación), por eso no encajan en `Transaccion`.

    Dos categorías, fiscalmente distintas:
      - `FOREX`: G/P por divisa (Art. 33.5.e LIRPF) → ganancia/pérdida
        patrimonial, base del ahorro. Sólo el `realized_eur` es declarable;
        `unrealized_eur` es latente (informativo).
      - `TBILL`: Letras del Tesoro / Treasury Bills → RCM (rendimiento del
        capital mobiliario). `unrealized_eur` siempre 0.

    El `ejercicio` se deriva del periodo del statement (año de la fecha fin).
    DEGIRO no proporciona esta sección — sólo IBKR.
    """

    __tablename__ = "resultados_ibkr"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    categoria: Mapped[str] = mapped_column(String(10), nullable=False)  # FOREX/TBILL
    ejercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    clave: Mapped[str] = mapped_column(String(120), nullable=False)  # divisa o símbolo
    realized_eur: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unrealized_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    periodo_inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    periodo_fin: Mapped[date | None] = mapped_column(Date, nullable=True)

    estado: Mapped[str] = mapped_column(String(30), nullable=False, default="confirmada")
    origen: Mapped[str] = mapped_column(String(30), nullable=False, default="extracto")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_resultados_ibkr_cartera", "cartera_id", "categoria", "ejercicio"),
        UniqueConstraint("cartera_id", "external_id", name="uq_resultados_ibkr_ext"),
        CheckConstraint("categoria IN ('FOREX','TBILL')", name="ck_resultados_ibkr_cat"),
    )


# ── productos complejos (detección, sin cálculo fiscal) ─────────────────────

class ProductoComplejo(Base):
    """Instrumento detectado en el extracto pero NO soportado por el motor
    fiscal (CFD, futuro, warrant, producto estructurado, fondo, cripto IBKR
    según Asset Category). Se persiste sólo para mostrarlo al usuario con un
    aviso honesto: 'detectado, no calculado'. Sin tratamiento fiscal."""

    __tablename__ = "productos_complejos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True
    )

    ejercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    fecha: Mapped[date | None] = mapped_column(Date, nullable=True)
    simbolo: Mapped[str] = mapped_column(String(120), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    asset_category: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    cantidad: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, default=Decimal("0")
    )
    importe_eur: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )

    origen: Mapped[str] = mapped_column(String(30), nullable=False, default="extracto")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_productos_complejos_cartera", "cartera_id", "ejercicio"),
        UniqueConstraint("cartera_id", "external_id", name="uq_productos_complejos_ext"),
    )


# ── bloques de estrategia ───────────────────────────────────────────────────

# Categorías base del catálogo (ADR-002). Mapeo con la doctrina WG:
# defensivo=A (Estable/Seguro de Vida) · income=B (Dividend Growth/Subidas de
# sueldo) · growth=C (Compounders) · aggressive=D (High Yield/Aceleradores) ·
# satelite=E (Satélite/Alternativos) · colchon=F (Escudo psicológico).
# indice y renta_fija son OPCIONALES (no se siembran; el usuario los añade).
# Las fichas (descripción/criterios/no_es) viven en adapters/ia/prompt.py.
CATEGORIAS_BASE = (
    "defensivo", "income", "growth", "aggressive", "satelite",
    "indice", "renta_fija", "cripto", "materias_primas",
    "colchon", "sin_clasificar",
)


class Bloque(Base):
    """Bloque de estrategia de una cartera. El usuario reparte sus posiciones
    en bloques (vía `Posicion.bloque_id`); las no asignadas caen en el saco
    'Sin clasificar' (bloque_id NULL, no es una fila aquí).

    Catálogo base sembrado en bootstrap (5 bloques) + personalizados. Tope 8
    por cartera. `peso_objetivo`/`tolerancia` quedan para cuando llegue el Plan
    (de momento la UI solo muestra distribución actual)."""

    __tablename__ = "bloques"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    categoria_base: Mapped[str] = mapped_column(String(20), nullable=False)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    es_base: Mapped[bool] = mapped_column(default=False, nullable=False)
    # ¿El bloque cuenta para el objetivo de IF? El Colchón nace en False (paz
    # mental, fuera de estrategia); el usuario puede sacar otros (p.ej. cripto a
    # largo). Generaliza el antiguo hardcode de 'colchon'.
    en_estrategia: Mapped[bool] = mapped_column(
        default=True, server_default="1", nullable=False
    )
    peso_objetivo: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    tolerancia: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.05")
    )
    # Colchón especial: efectivo (liquidez) asignado a este bloque y el
    # rendimiento que da (p.ej. letras del tesoro / cuenta remunerada).
    # Conceptualmente solo para categoria_base='colchon', pero general en BD.
    # `rendimiento_pct` como fracción (0.0325 = 3,25%), igual que los pesos.
    liquidez_asignada_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    rendimiento_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    # Sin CheckConstraint sobre categoria_base a propósito: el catálogo de
    # categorías evoluciona y SQLite no permite ALTER de un CHECK (obligaría a
    # reconstruir la tabla en cada categoría nueva). La validación vive en el
    # servicio (crear_bloque/editar_bloque contra CATEGORIAS_BASE).
    __table_args__ = (
        UniqueConstraint("cartera_id", "nombre", name="uq_bloques_cartera_nombre"),
    )


# ── plan por valor (pasos del plan de inversión) ────────────────────────────

# Taxonomía simplificada de decisiones (vs la completa de WG).
DECISIONES_PLAN = (
    "COMPRAR", "REFORZAR", "MANTENER", "MONITORIZAR", "RECORTAR", "VENDER", "ESPERAR",
)
PRIORIDADES_PLAN = ("CRITICA", "ALTA", "MEDIA", "BAJA")
ESTADOS_PLAN = ("PENDIENTE", "EN_CURSO", "COMPLETADO", "CANCELADO")


class PlanPaso(Base):
    """Paso del plan de inversión, asociado a un valor (ISIN) de la cartera.

    Es una cola: cada posición puede tener varios pasos. La 'decisión vigente'
    de una posición es la del paso activo (PENDIENTE/EN_CURSO) de mayor
    prioridad; sin paso activo → MANTENER. Replica la hoja Plan → columna
    Decision del Excel de Wealth Guardian. Objetivo numérico = capital € a
    invertir/desinvertir."""

    __tablename__ = "plan_pasos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    razon: Mapped[str | None] = mapped_column(Text, nullable=True)
    capital_objetivo_eur: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    prioridad: Mapped[str] = mapped_column(String(20), nullable=False, default="MEDIA")
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDIENTE")
    fecha_objetivo: Mapped[date | None] = mapped_column(Date, nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        Index("ix_plan_pasos_cartera_isin", "cartera_id", "isin"),
        CheckConstraint(
            "decision IN "
            "('COMPRAR','REFORZAR','MANTENER','MONITORIZAR','RECORTAR','VENDER','ESPERAR')",
            name="ck_plan_pasos_decision",
        ),
        CheckConstraint(
            "prioridad IN ('CRITICA','ALTA','MEDIA','BAJA')",
            name="ck_plan_pasos_prioridad",
        ),
        CheckConstraint(
            "estado IN ('PENDIENTE','EN_CURSO','COMPLETADO','CANCELADO')",
            name="ck_plan_pasos_estado",
        ),
    )


# ── pérdidas pendientes de años anteriores (entrada manual) ─────────────────

class PerdidaPendienteManual(Base):
    """Pérdida patrimonial pendiente de compensar de un ejercicio anterior,
    introducida MANUALMENTE por el usuario desde sus declaraciones previas.

    La auto-detección desde los matches FIFO no puede saber qué se compensó en
    declaraciones pasadas, así que estas entradas, cuando existen, son la
    fuente de verdad para la compensación (sustituyen al auto-detect).
    Caduca a los 4 años (`ejercicio_origen + 4`). Importe positivo = pendiente.
    """

    __tablename__ = "perdidas_pendientes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    ejercicio_origen: Mapped[int] = mapped_column(Integer, nullable=False)
    importe_eur: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "ejercicio_origen", name="uq_perdidas_cartera_ano"),
    )


# ── estimaciones de valoración (Fase 2, multi-método WG) ────────────────────

TIPOS_VAL = ("PER", "P_FCF", "P_BV", "P_FRE", "SOTP")

# Etiqueta legible de cada método: (múltiplo, métrica por acción que multiplica).
# Fuente única para prompts, one-pager y frontend (este último la replica).
ETIQUETAS_TIPO_VAL: dict[str, tuple[str, str]] = {
    "PER": ("PER", "EPS (beneficio por acción)"),
    "P_FCF": ("P/FCF", "FCF por acción"),
    "P_BV": ("P/BV", "valor contable (NAV) por acción"),
    "P_FRE": ("P/FRE", "FRE (fee-related earnings) por acción"),
    # Suma de partes: precio objetivo = (P/NAV objetivo) × (NAV/acción). Para
    # conglomerados/holdings donde el PER no captura el valor (CK Hutchison…).
    "SOTP": ("P/NAV", "NAV por acción (suma de partes)"),
}


def etiquetas_tipo_val(tipo_val: str | None) -> tuple[str, str]:
    return ETIQUETAS_TIPO_VAL.get(tipo_val or "PER", ETIQUETAS_TIPO_VAL["PER"])


class Estimacion(Base):
    """Estimación de valoración por posición (per-holding), modelo WG:
    precio objetivo = `multiplo_objetivo` (N) × `metrica_base_4y` (O), según
    `tipo_val` (PER=EPS, P_FCF=FCF/acción, P_BV=NAV/acción, P_FRE=FRE/acción,
    SOTP=P/NAV × NAV/acción suma de partes).
    De ahí se derivan CAGR4 y CAGR4+Div. Híbrido: campos auto-rellenados desde
    el feed que el usuario puede ajustar. Cifras en divisa nativa (los ratios
    CAGR/yield son agnósticos a divisa)."""

    __tablename__ = "estimaciones"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    tipo_val: Mapped[str] = mapped_column(String(8), nullable=False, default="PER")
    eps_actual: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    multiplo_objetivo: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    metrica_base_4y: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    dividendo_share: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    # Crecimiento estimado del dividendo a 4 años (fracción, p.ej. 0.10 = 10%).
    # Editable; si es NULL se deriva del crecimiento implícito de la métrica
    # base capado a [−5%, +20%] (ETFs sin estimación → 0). Alimenta la
    # componente Div del horizonte en CAGR4+Div neto (decisión Angel 2026-06-11).
    crecimiento_div_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Consenso de analistas (referencia NO editable) cacheado como JSON:
    #   eps_forward, eps_consenso_4y, eps_high, eps_low, num_analistas_eps,
    #   anio_consenso_4y, precio_obj_consenso, target_high, target_low,
    #   per_hist_medio, per_hist_mediano. Refrescado en cada prefill.
    consenso_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", name="uq_estimaciones_cartera_isin"),
        CheckConstraint(
            "tipo_val IN ('PER','P_FCF','P_BV','P_FRE','SOTP')", name="ck_estimaciones_tipo",
        ),
    )


class Seguimiento(Base):
    """Watchlist: empresas que el usuario SIGUE sin tener en cartera, para
    estudiarlas antes de comprar. Identificadas por `ticker` (lo que el usuario
    conoce) + ISIN/nombre/divisa resueltos al añadir. La valoración reutiliza la
    tabla `estimaciones` (keyed por isin), que NO está atada a tener posición."""

    __tablename__ = "seguimientos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    nombre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    divisa: Mapped[str | None] = mapped_column(String(8), nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Intención de bloque del candidato (la sugiere el clasificador, la confirma
    # el usuario). Permite que el plan top-down sepa dónde caería la compra.
    bloque_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("bloques.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", name="uq_seguimientos_cartera_isin"),
    )


# ── overrides de bloque (semilla del aprendizaje few-shot) ──────────────────

class OverrideBloque(Base):
    """Registro de cuando el usuario asigna una posición a un bloque DISTINTO al
    que sugirió la IA. Es el sesgo emocional del usuario hecho dato: alimenta el
    few-shot del clasificador puntual (ver `bloques.overrides_recientes`). La IA
    sugiere por criterio objetivo; el usuario decide; el desacuerdo se aprende."""

    __tablename__ = "overrides_bloque"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    nombre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(80), nullable=True)
    categoria_sugerida: Mapped[str] = mapped_column(String(20), nullable=False)
    categoria_elegida: Mapped[str] = mapped_column(String(20), nullable=False)
    confianza_ia: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    razon: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_overrides_bloque_cartera", "cartera_id", "created_at"),
    )


# ── fricción conductual (avisa, rebate 2 veces, te deja, captura) ───────────

class EventoFriccion(Base):
    """Registro de cuando el usuario procede con una decisión peligrosa (vender en
    pánico, romper tesis, tocar el colchón) a pesar de la fricción. Es el override
    conductual hecho dato: auditoría + semilla del 'tu historial' para futuros
    rebates. Ver `services/friccion.py` y la memoria del pilar psicológico."""

    __tablename__ = "eventos_friccion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    severidad: Mapped[str] = mapped_column(String(10), nullable=False)  # ALTA | MEDIA
    rebatido: Mapped[bool] = mapped_column(default=True, nullable=False)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_eventos_friccion_cartera", "cartera_id", "created_at"),
    )


# ── plan firmado (onboarding: el contrato de Ulises) ────────────────────────

class PlanFirmado(Base):
    """Estrategia que el usuario co-construye con la IA y FIRMA en frío. Snapshot
    del perfil + objetivos por bloque + resumen, versionado por cartera (re-
    onboarding = nueva versión). Es el contrato de Ulises que la fricción
    referencia ('tu propio Plan dice…'). Al firmar se aplican los peso_objetivo."""

    __tablename__ = "planes_firmados"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    perfil_json: Mapped[str] = mapped_column(Text, nullable=False)       # objetivo, horizonte, tolerancia, fase
    objetivos_json: Mapped[str] = mapped_column(Text, nullable=False)    # {categoria_base: peso_objetivo}
    resumen: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_planes_firmados_cartera", "cartera_id", "version"),
    )


class AnalisisGuardado(Base):
    """Análisis IA persistido por empresa (p.ej. one-pager) para no re-generarlo
    en cada visita; el usuario regenera explícitamente. `payload_json` = el dataclass
    serializado; `tipo` permite varios análisis por ISIN ('one_pager', 'contexto'…)."""

    __tablename__ = "analisis_guardados"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    tipo: Mapped[str] = mapped_column(String(24), nullable=False)        # one_pager | contexto
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", "tipo", name="uq_analisis_cartera_isin_tipo"),
    )


class AnalisisJob(Base):
    """Estado de un análisis IA lanzado en segundo plano (one-pager/valoración son
    lentos: búsqueda web de minutos). La UI hace polling de `estado`; el resultado
    se persiste en AnalisisGuardado. Uno por (cartera, isin, tipo)."""

    __tablename__ = "analisis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    tipo: Mapped[str] = mapped_column(String(24), nullable=False)        # one_pager | valoracion
    estado: Mapped[str] = mapped_column(String(12), nullable=False)      # en_curso | ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", "tipo", name="uq_jobs_cartera_isin_tipo"),
    )


class MensajeAsesor(Base):
    """Hilo de conversación con el asesor IA (uno por cartera). Persistido para que
    sobreviva recargas; el contexto (cartera/estrategia/plan) se reensambla en cada turno."""

    __tablename__ = "mensajes_asesor"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    rol: Mapped[str] = mapped_column(String(12), nullable=False)         # user | assistant
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        Index("ix_mensajes_asesor_cartera", "cartera_id", "created_at"),
    )


class PropuestaRegimen(Base):
    """Última propuesta de auto-clasificación del régimen macro (4 indicadores
    + razón + fuentes + datos objetivos). Se persiste sin tocar el régimen
    vigente (`Cartera.regimen_macro_json`) hasta que el usuario la firme. Una
    fila por cartera; upsert al regenerar."""

    __tablename__ = "propuestas_regimen"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", name="uq_propuestas_regimen_cartera"),
    )


class SnapshotPrecio(Base):
    """Precio base por posición = la última vez que el usuario dio por 'visto' la
    vigilancia. La alerta es el cambio del precio actual frente a este baseline."""

    __tablename__ = "snapshots_precio"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    isin: Mapped[str] = mapped_column(String(12), nullable=False)
    precio_eur: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc, onupdate=_now_utc
    )

    __table_args__ = (
        UniqueConstraint("cartera_id", "isin", name="uq_snapshot_cartera_isin"),
    )


class ExtractoArchivo(Base):
    """Extracto bruto del broker guardado tal cual lo subió el usuario.

    Roadmap 1.9 (CSV approach): Cima persiste los CSVs originales para poder
    re-pasarlos al `generar_irpf.main()` de Cuádrate y entregar la declaración
    completa (XLSX maestro + informes corporativas/dividendos/opciones/fx +
    sidecars). La BD de Cima es una transformación; el CSV original es la
    source of truth fiscal.

    `kind` clasifica qué CSV es para que el orquestador lo renombre al
    nombre exacto que Cuádrate espera en su BASE_DIR:
      - 'degiro_transacciones' → DeGiro_Transacciones_{ejercicio}.csv
      - 'degiro_cuenta'        → DeGiro_Cuenta_{ejercicio}.csv
      - 'ibkr'                 → IBKR_Trades_{ejercicio}.csv
      - 'tr'                   → TR_Transacciones_{ejercicio}.csv
    """

    __tablename__ = "extractos_archivos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    cartera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("carteras.id", ondelete="CASCADE"), nullable=False
    )
    broker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("brokers.id", ondelete="SET NULL"), nullable=True,
    )
    ejercicio: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    filename_original: Mapped[str] = mapped_column(String(255), nullable=False)
    # Ruta RELATIVA al storage_dir configurado (no absoluta — facilita migrar
    # entre hosts/contenedores sin reescribir la BD).
    ruta_storage: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now_utc,
    )

    __table_args__ = (
        # Un único fichero "vigente" por (cartera, ejercicio, kind). Subir
        # de nuevo el mismo kind+ejercicio sobreescribe el anterior (lógica
        # de reemplazo en el servicio, NO en BD: para una fila por subida).
        Index("ix_extractos_cartera_ejercicio_kind",
              "cartera_id", "ejercicio", "kind"),
        Index("ix_extractos_sha256", "sha256"),
        CheckConstraint(
            "kind IN ('degiro_transacciones','degiro_cuenta','ibkr','tr')",
            name="ck_extracto_kind",
        ),
    )
