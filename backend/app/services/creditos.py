"""Seam de créditos de IA — PUNTO ÚNICO de contabilización (Roadmap 3.5).

Hoy es un no-op: en desarrollo la IA corre sobre Claude Max (CLI), sin coste por
token que descontar. Cuando llegue el proveedor de API key, implementar aquí el
descuento de saldo y el registro de consumo (tabla `consumo_ia`), sin tocar el
resto del flujo. Todas las operaciones de IA pasan por `registrar_uso_ia`.

Tarifa prevista (cuando haya créditos):
  - 'puntual' (clasificar): 1 crédito por empresa (preciso).
  - 'lote' (autoclasificar): coste reducido por el conjunto (1 llamada, salida
    tersa) — menos preciso, más barato.
"""
from __future__ import annotations

from sqlalchemy.orm import Session


def registrar_uso_ia(
    db: Session, cartera_id: str, operacion: str, n_empresas: int
) -> None:
    """Registra (y en el futuro cobra) un uso de IA.

    operacion: 'puntual' | 'lote'. No-op mientras el proveedor sea claude_cli/mock
    (Max no consume créditos). Implementar el ledger al activar el proveedor API.
    """
    # TODO(creditos, Roadmap 3.5): cuando settings.ia_provider == 'anthropic':
    #   1. comprobar saldo de la cartera/usuario; si insuficiente → HTTP 402.
    #   2. descontar según tarifa (puntual: n_empresas; lote: tarifa de lote).
    #   3. insertar fila en consumo_ia (operacion, n_empresas, coste, ts).
    return None
