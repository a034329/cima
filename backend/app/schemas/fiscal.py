"""Schemas Pydantic para el endpoint fiscal.

Mapean las dataclasses del motor de Cuádrate (`FIFOMatch`, `PositionSummary`,
`PerdidaDiferida`, `OrphanSale`, `PerdidaPendiente`, `ResultadoCompensacion`)
a JSON serializable. Los `Decimal` se serializan como string para preservar
precisión en el frontend.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class FifoMatchOut(BaseModel):
    """Una venta casada con uno o más lotes FIFO."""
    model_config = ConfigDict(from_attributes=True)

    isin: str
    nombre: str
    fecha_compra: date
    fecha_venta: date
    cantidad: Decimal = Field(decimal_places=10)
    coste_adquisicion: Decimal = Field(decimal_places=4)
    importe_transmision: Decimal = Field(decimal_places=4)
    gastos_venta: Decimal = Field(decimal_places=4)
    gastos_compra: Decimal = Field(decimal_places=4)
    ganancia_perdida: Decimal = Field(decimal_places=4)
    ejercicio_fiscal: int
    regla_2_meses: bool
    regla_2_meses_detalle: str
    es_scrip: bool
    es_corto: bool
    broker_compra: str
    broker_venta: str
    instrument_type: str
    lote_id: int
    perdida_diferida_aflorada_eur: Decimal = Field(decimal_places=4)


class PositionSummaryOut(BaseModel):
    """Posición abierta tras procesar todos los matches."""
    model_config = ConfigDict(from_attributes=True)

    isin: str
    nombre: str
    cantidad_total: Decimal = Field(decimal_places=10)
    coste_total_eur: Decimal = Field(decimal_places=4)
    pm_ponderado_eur: Decimal = Field(decimal_places=10)
    num_lotes: int
    lote_mas_antiguo: date
    lote_mas_reciente: date
    es_mixta: bool


class PerdidaDiferidaOut(BaseModel):
    """Pérdida bloqueada por regla 2M, latente hasta que se transmita el lote
    recomprado que la disparó."""
    model_config = ConfigDict(from_attributes=True)

    isin: str
    nombre: str
    importe_eur: Decimal = Field(decimal_places=4)
    cantidad_pendiente: Decimal = Field(decimal_places=10)
    fecha_venta_origen: date
    ejercicio_origen: int
    lote_id_recompra: int


class OrphanSaleOut(BaseModel):
    """Venta sin lotes (CSV incompleto, anterior al primer extracto, etc.)."""
    model_config = ConfigDict(from_attributes=True)

    isin: str
    nombre: str
    fecha: date
    cantidad: Decimal = Field(decimal_places=10)
    importe_eur: Decimal = Field(decimal_places=4)
    broker: str
    parcial: bool
    cantidad_faltante: Decimal = Field(decimal_places=10)


class PerdidaPendienteOut(BaseModel):
    """Bolsa de pérdida pendiente de compensar (arrastre 4 años)."""
    model_config = ConfigDict(from_attributes=True)

    ejercicio_origen: int
    importe_original_eur: Decimal = Field(decimal_places=4)
    compensado_eur: Decimal = Field(decimal_places=4)
    pendiente_eur: Decimal = Field(decimal_places=4)
    expira: int
    detalle: str


class CompensacionOut(BaseModel):
    """Resultado de aplicar las reglas de compensación a G/P + RCM."""
    model_config = ConfigDict(from_attributes=True)

    ejercicio: int
    gp_bruto: Decimal = Field(decimal_places=4)
    gp_no_deducible_2m: Decimal = Field(decimal_places=4)
    gp_deducible: Decimal = Field(decimal_places=4)
    rcm_neto: Decimal = Field(decimal_places=4)
    opciones_pl: Decimal = Field(decimal_places=4)
    gp_total: Decimal = Field(decimal_places=4)
    saldo_gp_tras_intra: Decimal = Field(decimal_places=4)
    cruce_gp_a_rcm: Decimal = Field(decimal_places=4)
    cruce_rcm_a_gp: Decimal = Field(decimal_places=4)
    saldo_gp_tras_cruce: Decimal = Field(decimal_places=4)
    saldo_rcm_tras_cruce: Decimal = Field(decimal_places=4)
    perdidas_anteriores: list[PerdidaPendienteOut]
    aplicadas_de_anteriores: Decimal = Field(decimal_places=4)
    saldo_gp_final: Decimal = Field(decimal_places=4)
    nuevo_saldo_negativo: Decimal = Field(decimal_places=4)
    perdidas_actualizadas: list[PerdidaPendienteOut]
    perdidas_expiradas: list[PerdidaPendienteOut]
    perdidas_proximas_expirar: list[PerdidaPendienteOut]
    base_ahorro_gp: Decimal = Field(decimal_places=4)
    base_ahorro_rcm: Decimal = Field(decimal_places=4)


class FiscalResumenOut(BaseModel):
    """Respuesta del endpoint `/api/fiscal/{ejercicio}`."""

    ejercicio: int
    cartera_id: str
    fecha_corte: date
    fecha_calculo: date

    # Síntesis numérica
    gp_bruto: Decimal = Field(decimal_places=4)
    gp_no_deducible_2m: Decimal = Field(decimal_places=4)
    total_perdida_aflorada: Decimal = Field(decimal_places=4)
    rcm_neto: Decimal = Field(decimal_places=4)
    n_matches: int

    # Detalle del motor
    matches: list[FifoMatchOut]
    positions: list[PositionSummaryOut]
    perdidas_diferidas_latentes: list[PerdidaDiferidaOut]
    orphan_sales: list[OrphanSaleOut]
    warnings: list[str]

    compensacion: CompensacionOut
