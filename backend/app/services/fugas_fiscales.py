"""Fugas fiscales: retención de origen NO recuperable vía IRPF (0588).

El dinero que el país de origen retiene POR ENCIMA del tope del CDI no se
recupera en la declaración española — solo reclamándolo al fisco extranjero
(formulario 85 suizo, BZSt alemán…). La mayoría de inversores ni sabe que
lo pierde. Este servicio lo cuantifica POR PAÍS Y AÑO sobre la ventana de
reclamación de cada país (2-5 años según jurisdicción): el exceso de un solo
año puede parecer poco, pero el acumulado reclamable suele justificar el
trámite.

  - EXCESO REAL por (país, ejercicio): sobre los dividendos cobrados,
    max(0, retención_origen − bruto × tope_CDI), para todos los ejercicios
    aún dentro del plazo de reclamación del país.
  - PROYECCIÓN anual: yield estimado × valor de la posición × exceso sobre
    el tope CDI con la retención OBSERVADA en los propios dividendos
    (respaldo: estatutaria del vendor), para anticipar la fuga del año.
  - RECLAMADO: el usuario marca (país, ejercicio) como ya reclamado
    (tabla `reclamaciones_cdi`) y el panel muestra solo lo pendiente.

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
from app.services.dividendo_neto import (
    exceso_no_recuperable_pct, exceso_observado_pct, pais_de_isin,
    tasa_origen_observada,
)
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

# Plazo de reclamación en AÑOS tras el fin del año natural del dividendo
# (aprox. conservadora "hasta el 31-dic del año N+plazo"). Verificado por web
# 2026-06-12:
#   CH 3 — ESTV (estv.admin.ch): hasta 31-dic del 3er año siguiente.
#   DE 4 — BZSt: 4 años desde el fin del año natural del cobro.
#   FR 2 — prescripción general 2 años (PwC tax summaries).
#   IT 4 — 48 meses desde el pago (Agenzia Entrate / Clearstream).
#   BE 5 — 5 años desde el 1-ene del año del pago (KPMG/EY 2025-26).
#   DK 5 — Tribunal fiscal danés 2021: 5 años (antes la Skattestyrelsen
#          aplicaba 3 — si hay duda, reclamar pronto).
# Países sin verificar → 2 años (mínimo observado), marcado orientativo.
PLAZO_RECLAMACION_ANIOS: dict[str, int] = {
    "CH": 3, "DE": 4, "FR": 2, "IT": 4, "BE": 5, "DK": 5,
}
_PLAZO_DEFECTO = 2
_PLAZO_MAX = max(PLAZO_RECLAMACION_ANIOS.values())


def plazo_reclamacion(pais: str) -> tuple[int, bool]:
    """(años de plazo, verificado). No verificado → defecto conservador."""
    p = PLAZO_RECLAMACION_ANIOS.get(pais)
    return (p, True) if p is not None else (_PLAZO_DEFECTO, False)


@dataclass
class FugaAnio:
    ejercicio: int
    exceso_eur: Decimal              # exceso real cobrado ese año
    dentro_plazo: bool               # aún reclamable según el plazo del país
    limite: date | None              # fecha límite estimada de reclamación
    reclamado: bool                  # marcado por el usuario


@dataclass
class FugaPosicion:
    isin: str
    nombre: str
    pais: str
    exceso_pct: Decimal                 # puntos no recuperables (fracción)
    div_anual_estimado_eur: Decimal | None
    fuga_anual_estimada_eur: Decimal | None
    exceso_real_total_eur: Decimal      # suma de la ventana reclamable


@dataclass
class FugaPais:
    pais: str
    exceso_pct: Decimal
    fuga_anual_estimada_eur: Decimal
    reclamable_pendiente_eur: Decimal   # dentro de plazo y NO reclamado
    reclamado_eur: Decimal              # dentro de plazo y ya reclamado
    fuera_plazo_eur: Decimal            # prescrito (informativo)
    plazo_anios: int
    plazo_verificado: bool
    mecanismo: str
    anios: list[FugaAnio] = field(default_factory=list)
    posiciones: list[FugaPosicion] = field(default_factory=list)


@dataclass
class FugasResultado:
    ejercicio: int                      # año en curso (ancla de la ventana)
    ventana_anios: int                  # años mirados hacia atrás
    total_fuga_anual_estimada_eur: Decimal
    total_reclamable_pendiente_eur: Decimal
    por_pais: list[FugaPais] = field(default_factory=list)


def _exceso_real(db: Session, cartera_id: str, desde_anio: int) -> dict[tuple[str, int], Decimal]:
    """{(isin, ejercicio): exceso} sobre dividendos COBRADOS desde `desde_anio`:
    la parte de la retención de origen que supera bruto × tope_CDI. La
    retención española (retencion_pais='ES') es crédito 0591 y no es fuga."""
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    out: dict[tuple[str, int], Decimal] = {}
    txs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).scalars()
    for t in txs:
        if t.fecha.year < desde_anio:
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
            k = (isin, t.fecha.year)
            out[k] = out.get(k, _CERO) + exceso
    return out


def _reclamados(db: Session, cartera_id: str) -> set[tuple[str, int]]:
    return {(r.pais, r.ejercicio) for r in db.execute(
        select(models.ReclamacionCDI)
        .where(models.ReclamacionCDI.cartera_id == cartera_id)
    ).scalars()}


def calcular_fugas(db: Session, cartera_id: str) -> FugasResultado:
    from app.services.estimaciones import calcular_estimaciones
    from app.services.precios import obtener_precios_eur

    hoy = date.today()
    anio_actual = hoy.year
    desde = anio_actual - _PLAZO_MAX
    precios_eur, _ = obtener_precios_eur(db, cartera_id)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}
    real = _exceso_real(db, cartera_id, desde)
    reclamados = _reclamados(db, cartera_id)
    # Tasas de origen OBSERVADAS en los propios dividendos: la proyección usa
    # lo que el broker retiene de verdad (DeGiro: FR 25%), no la estatutaria
    # (12,8% persona física, que es lo que aplica TR). Sin historial → vendor.
    observadas = tasa_origen_observada(db, cartera_id)

    # Exceso real total por ISIN (para el detalle por posición)
    real_por_isin: dict[str, Decimal] = {}
    for (isin, _a), v in real.items():
        real_por_isin[isin] = real_por_isin.get(isin, _CERO) + v

    por_pais: dict[str, FugaPais] = {}

    def _pais_entry(pais: str) -> FugaPais:
        plazo, verificado = plazo_reclamacion(pais)
        return por_pais.setdefault(pais, FugaPais(
            pais=pais,
            exceso_pct=exceso_observado_pct(pais, None, observadas),
            fuga_anual_estimada_eur=_CERO,
            reclamable_pendiente_eur=_CERO, reclamado_eur=_CERO,
            fuera_plazo_eur=_CERO,
            plazo_anios=plazo, plazo_verificado=verificado,
            mecanismo=MECANISMO_RECUPERACION.get(
                pais, "Procedimiento de devolución del país de origen."),
        ))

    # ── Posiciones: proyección anual + detalle ──
    isin_a_pais: dict[str, str] = {}
    posiciones = db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars()
    for pos in posiciones:
        cant = estado_posicion(db, pos.id)["cantidad"]
        real_isin = real_por_isin.get(pos.isin, _CERO)
        pais = (pais_de_isin(pos.isin, pos.nombre) or "").upper()
        isin_a_pais[pos.isin] = pais
        exceso = exceso_observado_pct(pais, pos.isin, observadas)
        if (cant <= 0 and real_isin <= 0) or (exceso <= 0 and real_isin <= 0):
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

        fp = _pais_entry(pais)
        fp.posiciones.append(FugaPosicion(
            isin=pos.isin, nombre=pos.nombre or pos.isin, pais=pais,
            exceso_pct=exceso, div_anual_estimado_eur=div_anual,
            fuga_anual_estimada_eur=fuga_anual,
            exceso_real_total_eur=real_isin.quantize(_C2),
        ))
        if fuga_anual:
            fp.fuga_anual_estimada_eur += fuga_anual

    # ── Años: exceso real por (país, ejercicio) con plazo y marca ──
    por_pais_anio: dict[tuple[str, int], Decimal] = {}
    for (isin, anio), v in real.items():
        pais = isin_a_pais.get(isin)
        if pais is None:
            # Posición cerrada/borrada: resolver país por ISIN igualmente.
            pais = (pais_de_isin(isin, "") or "").upper()
        if not pais:
            continue
        k = (pais, anio)
        por_pais_anio[k] = por_pais_anio.get(k, _CERO) + v

    for (pais, anio), v in por_pais_anio.items():
        fp = _pais_entry(pais)
        plazo = fp.plazo_anios
        limite = date(anio + plazo, 12, 31)
        dentro = hoy <= limite
        marcado = (pais, anio) in reclamados
        fp.anios.append(FugaAnio(
            ejercicio=anio, exceso_eur=v.quantize(_C2),
            dentro_plazo=dentro, limite=limite, reclamado=marcado,
        ))
        if not dentro:
            fp.fuera_plazo_eur += v
        elif marcado:
            fp.reclamado_eur += v
        else:
            fp.reclamable_pendiente_eur += v

    paises = []
    for p in por_pais.values():
        if (p.fuga_anual_estimada_eur <= 0 and p.reclamable_pendiente_eur <= 0
                and p.reclamado_eur <= 0 and p.fuera_plazo_eur <= 0):
            continue
        p.reclamable_pendiente_eur = p.reclamable_pendiente_eur.quantize(_C2)
        p.reclamado_eur = p.reclamado_eur.quantize(_C2)
        p.fuera_plazo_eur = p.fuera_plazo_eur.quantize(_C2)
        p.anios.sort(key=lambda a: -a.ejercicio)
        p.posiciones.sort(key=lambda x: -(x.fuga_anual_estimada_eur or _CERO))
        paises.append(p)
    paises.sort(key=lambda p: -(p.reclamable_pendiente_eur + p.fuga_anual_estimada_eur))

    return FugasResultado(
        ejercicio=anio_actual,
        ventana_anios=_PLAZO_MAX,
        total_fuga_anual_estimada_eur=sum(
            (p.fuga_anual_estimada_eur for p in paises), _CERO).quantize(_C2),
        total_reclamable_pendiente_eur=sum(
            (p.reclamable_pendiente_eur for p in paises), _CERO).quantize(_C2),
        por_pais=paises,
    )


def marcar_reclamado(db: Session, cartera_id: str, pais: str, ejercicio: int,
                     reclamado: bool, notas: str | None = None) -> None:
    """Marca/desmarca un (país, ejercicio) como ya reclamado. Idempotente."""
    pais = pais.upper()
    existente = db.execute(
        select(models.ReclamacionCDI)
        .where(models.ReclamacionCDI.cartera_id == cartera_id)
        .where(models.ReclamacionCDI.pais == pais)
        .where(models.ReclamacionCDI.ejercicio == ejercicio)
    ).scalars().first()
    if reclamado and existente is None:
        db.add(models.ReclamacionCDI(
            cartera_id=cartera_id, pais=pais, ejercicio=ejercicio, notas=notas))
    elif not reclamado and existente is not None:
        db.delete(existente)
    db.commit()
