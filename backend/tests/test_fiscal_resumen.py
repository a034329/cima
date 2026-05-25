"""Test del Resumen del ejercicio — el cuadro IRPF integrado.

Verifica que une G/P patrimoniales (acciones + forex + opciones) y RCM
(dividendos + intereses + letras) en UNA compensación, con datos reales de
IBKR.csv.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base

IBKR_CSV = Path("/app/cima/test_data/IBKR.csv")


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="IBKR.csv no presente")
def test_resumen_integra_patrimoniales_y_rcm() -> None:
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng)

    def override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            c.post("/api/bootstrap")
            with open(IBKR_CSV, "rb") as f:
                r = c.post("/api/import", data={"broker_tipo": "ibkr"},
                           files={"fichero": ("IBKR.csv", f, "text/csv")})
            assert r.status_code == 200, r.text

            d = c.get("/api/fiscal/resumen/2026").json()

            # Componentes integrados de distintas fuentes
            assert d["forex_realized"] == "-90.50"       # forex (Art. 33.5.e)
            assert d["letras_rcm"] == "7.24"             # T-Bills → RCM
            assert Decimal(d["opciones_pl"]) != 0        # casilla 1626
            assert Decimal(d["dividendos_bruto"]) > 0    # 0029
            # El interés de débito NO entra en RCM (informativo, negativo)
            assert Decimal(d["intereses_debit"]) < 0
            assert d["intereses_rcm"] == "0.00"

            # La compensación cruzada aplica pérdidas patrimoniales contra RCM:
            # con G/P muy negativa, se compensa el 25 % del RCM.
            c_ = d["compensacion"]
            assert Decimal(c_["cruce_gp_a_rcm"]) > 0
            # base RCM = RCM neto − cruce (tolerancia de céntimo: las cifras de
            # `compensacion` no van cuantizadas a 2 decimales, la base sí)
            esperado_rcm = Decimal(c_["rcm_neto"]) - Decimal(c_["cruce_gp_a_rcm"])
            assert abs(Decimal(d["base_ahorro_rcm"]) - esperado_rcm) < Decimal("0.02")
            # base G/P a 0 (pérdidas se arrastran)
            assert Decimal(d["base_ahorro_gp"]) == 0
            # total = gp + rcm
            assert Decimal(d["base_ahorro_total"]) == (
                Decimal(d["base_ahorro_gp"]) + Decimal(d["base_ahorro_rcm"])
            )
            # CDI casilla 0588 presente
            assert Decimal(d["cdi_recuperable"]) > 0
    finally:
        app.dependency_overrides.clear()
