"""Fugas fiscales: retención de origen NO recuperable vía IRPF (0588).

El dinero que el país de origen retiene POR ENCIMA del tope del CDI no se
recupera en la declaración española — solo reclamándolo al fisco extranjero
(formulario 85 suizo, BZSt alemán…). La mayoría de inversores ni sabe que
lo pierde. Este servicio lo cuantifica:

  - REAL (ejercicio en curso): sobre los dividendos ya cobrados, el exceso
    efectivo = max(0, retención_origen − bruto × tope_CDI) por país.
  - PROYECCIÓN anual: yield estimado × valor de la posición × exceso del
    país (estatutaria − tope CDI), para anticipar la fuga del año completo.

Es además la validación de demanda del add-on de Recuperación CDI
(Fase 4 del roadmap): primero se muestra la fuga, luego se ofrece
recuperarla.

Tablas de topes CDI y retenciones estatutarias: vendor de Cuádrate,
verificadas contra BOE/fuentes primarias (auditoría 2026-06-11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.dividendo_neto import exceso_no_recuperable_pct, pais_de_isin
from app.services.fifo import estado_posicion

_CERO = Decimal("0")
_C2 = Decimal("0.01")

# Cómo se reclama el exceso en cada país (verificado en Cuádrate contra las
# guías oficiales al montar la tabla CDI del blog — 2026-05/06).
MECANISMO_RECUPERACION: dict[str, str] = {
    "CH": "Formulario 85 al ESTV suizo (plazo típico 6-18 meses).",
    "DE": "Solicitud de devolución al BZSt (Bundeszentralamt für Steuern), 4-12 meses.",
    "FR": "Formularios 5000-FR + 5001-FR vía banco/agente fiscal, 6-12 meses.",
    "BE": "Formulario 276 Div al SPF Finances (trámite anual).",
    "IT": "Solicitud a la Agenzia delle Entrate (12-24 meses).",
    "DK": "Solicitud de devolución a la Skattestyrelsen danesa.",
    "NL": "Sin exceso típico (estatutaria 15% == tope CDI).",
    "US": "Sin exceso con W-8BEN en vigor (15% == tope CDI).",
}


@dataclass
class FugaPosicion:
    isin: str
    nombre: str
    pais: str
    exceso_pct: Decimal                 # puntos no recuperables (fracción)
    div_anual_estimado_eur: Decimal | None
    fuga_anual_estimada_eur: Decimal | None
    exceso_real_ytd_eur: Decimal


@dataclass
class FugaPais:
    pais: str
    exceso_pct: Decimal
    fuga_anual_estimada_eur: Decimal
    exceso_real_ytd_eur: Decimal
    mecanismo: str
    posiciones: list[FugaPosicion] = field(default_factory=list)


@dataclass
class FugasResultado:
    ejercicio: int
    total_fuga_anual_estimada_eur: Decimal
    total_exceso_real_ytd_eur: Decimal
    por_pais: list[FugaPais] = field(default_factory=list)


def _exceso_real_por_isin(
    db: Session, cartera_id: str, ejercicio: int,
) -> dict[str, Decimal]:
    """Exceso efectivo YTD por ISIN sobre dividendos COBRADOS: la parte de la
    retención de origen que supera bruto × tope_CDI. La retención española
    (retencion_pais='ES') es crédito 0591 y no cuenta como fuga."""
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    out: dict[str, Decimal] = {}
    txs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).scalars()
    for t in txs:
        if t.fecha.year != ejercicio:
            continue
        ret = Decimal(str(t.retencion_eur or 0))
        if ret <= 0 or t.retencion_pais == "ES":
            continue
        isin = t.posicion.isin if t.posicion else None
        if not isin:
            continue
        pais = pais_de_isin(isin, t.posicion.nombre)
        tope = g.DTA_SOURCE_MAX.get((pais or "").upper())
        if tope is None:
            continue   # sin CDI conocido → no podemos separar el exceso
        bruto = Decimal(str(t.importe_eur or 0))
        exceso = ret - bruto * Decimal(str(tope))
        if exceso > 0:
            out[isin] = out.get(isin, _CERO) + exceso
    return out


def calcular_fugas(db: Session, cartera_id: str) -> FugasResultado:
    from app.services.estimaciones import calcular_estimaciones
    from app.services.precios import obtener_precios_eur

    ejercicio = date.today().year
    precios_eur, _ = obtener_precios_eur(db, cartera_id)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}
    real_por_isin = _exceso_real_por_isin(db, cartera_id, ejercicio)

    por_pais: dict[str, FugaPais] = {}
    posiciones = db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars()
    for pos in posiciones:
        cant = estado_posicion(db, pos.id)["cantidad"]
        real = real_por_isin.get(pos.isin, _CERO)
        if cant <= 0 and real <= 0:
            continue
        pais = (pais_de_isin(pos.isin, pos.nombre) or "").upper()
        exceso = exceso_no_recuperable_pct(pais)
        if exceso <= 0 and real <= 0:
            continue

        div_anual = None
        fuga_anual = None
        c = calcs.get(pos.isin)
        px = precios_eur.get(pos.isin)
        if (c is not None and c.div_yield_pct is not None
                and px is not None and cant > 0):
            valor = Decimal(str(px)) * cant
            div_anual = (valor * c.div_yield_pct).quantize(_C2)
            fuga_anual = (div_anual * exceso).quantize(_C2)

        fp = por_pais.setdefault(pais, FugaPais(
            pais=pais, exceso_pct=exceso,
            fuga_anual_estimada_eur=_CERO, exceso_real_ytd_eur=_CERO,
            mecanismo=MECANISMO_RECUPERACION.get(
                pais, "Procedimiento de devolución del país de origen."),
        ))
        fp.posiciones.append(FugaPosicion(
            isin=pos.isin, nombre=pos.nombre or pos.isin, pais=pais,
            exceso_pct=exceso, div_anual_estimado_eur=div_anual,
            fuga_anual_estimada_eur=fuga_anual,
            exceso_real_ytd_eur=real.quantize(_C2),
        ))
        if fuga_anual:
            fp.fuga_anual_estimada_eur += fuga_anual
        fp.exceso_real_ytd_eur += real

    paises = [p for p in por_pais.values()
              if p.fuga_anual_estimada_eur > 0 or p.exceso_real_ytd_eur > 0]
    for p in paises:
        p.exceso_real_ytd_eur = p.exceso_real_ytd_eur.quantize(_C2)
        p.posiciones.sort(key=lambda x: -(x.fuga_anual_estimada_eur or _CERO))
    paises.sort(key=lambda p: -(p.fuga_anual_estimada_eur + p.exceso_real_ytd_eur))

    return FugasResultado(
        ejercicio=ejercicio,
        total_fuga_anual_estimada_eur=sum(
            (p.fuga_anual_estimada_eur for p in paises), _CERO).quantize(_C2),
        total_exceso_real_ytd_eur=sum(
            (p.exceso_real_ytd_eur for p in paises), _CERO).quantize(_C2),
        por_pais=paises,
    )
