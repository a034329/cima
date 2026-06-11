"""Schemas Pydantic para transacciones."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TipoTransaccion = Literal[
    "BUY", "SELL", "DIVIDEND", "INTEREST", "STAKING_REWARD",
    "CORPORATE_SPLIT", "CORPORATE_ISIN_CHANGE", "CORPORATE_SCRIP",
    "CORPORATE_RIGHTS", "CORPORATE_MERGER", "CORPORATE_OPA", "OTRO",
]

EstadoTransaccion = Literal["pendiente_confirmar", "confirmada", "descartada"]


class TransaccionIn(BaseModel):
    """Payload para POST /api/transacciones (operación manual)."""

    # Para flexibilizar el alta: si pasas `posicion_id` (selector de cartera),
    # `isin`/`nombre`/`divisa_local` se autocompletan desde esa posición y son
    # opcionales en el payload. Si no, `isin` es obligatorio (alta nueva).
    posicion_id: str | None = None
    isin: str | None = Field(default=None, min_length=12, max_length=12)
    ticker: str | None = None
    nombre: str | None = None
    broker_id: str | None = None
    fecha: date
    tipo: TipoTransaccion
    cantidad: Decimal = Field(gt=0)
    precio_local: Decimal = Field(ge=0)
    divisa_local: str | None = Field(default=None, min_length=3, max_length=3)
    importe_local: Decimal = Field(ge=0)
    fx_rate: Decimal = Field(default=Decimal("1"), gt=0)
    importe_eur: Decimal = Field(ge=0)
    gastos_eur: Decimal = Field(default=Decimal("0"), ge=0)
    tasas_externas_eur: Decimal = Field(default=Decimal("0"), ge=0)
    retencion_eur: Decimal = Field(default=Decimal("0"), ge=0)
    retencion_pais: str | None = Field(default=None, max_length=2)
    notas: str | None = None
    # Default `True` = el alta manual se aplica AL INSTANTE (estado confirmada +
    # rebuild FIFO). Pasa `false` si quieres dejarla como borrador esperando al
    # extracto del broker.
    confirmar_directo: bool = True


class TransaccionOut(BaseModel):
    """Respuesta de la API. Datos completos de la transacción persistida."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    cartera_id: str
    broker_id: str | None
    posicion_id: str
    isin: str | None = None              # de la posición (columna ISIN del frontend)
    posicion_nombre: str | None = None
    fecha: date
    tipo: TipoTransaccion
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
    estado: EstadoTransaccion
    origen: str
    external_id: str | None
    notas: str | None
    created_at: datetime
    updated_at: datetime


class ImportResultado(BaseModel):
    """Resumen del resultado tras importar un extracto."""

    broker: str
    insertadas: int
    deduplicadas: int           # ya existían por external_id
    reconciliadas: int          # casadas con una manual pendiente_confirmar
    conflictos: int             # match parcial — requieren decisión del usuario
    huerfanas_manuales: int     # manuales antiguas sin contrapartida en el extracto
    opciones_insertadas: int = 0
    opciones_deduplicadas: int = 0
    avisos: list[str] = Field(default_factory=list)


class TransaccionConflicto(BaseModel):
    """Discrepancia entre operación manual previa y fila del extracto."""

    transaccion_manual_id: str
    extracto_fecha: date
    extracto_cantidad: Decimal
    extracto_precio_local: Decimal
    extracto_importe_eur: Decimal
    diferencias: list[str]      # ej. ["precio difiere 1.2% (75.50 vs 73.24)"]
