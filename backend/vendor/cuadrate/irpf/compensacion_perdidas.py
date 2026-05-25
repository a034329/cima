"""
Compensación de pérdidas patrimoniales — Art. 49 LIRPF.

Gestiona:
  - Lectura/escritura de perdidas_pendientes.json
  - Compensación intra-año (G/P vs RCM, cruce del 25%)
  - Aplicación de pérdidas de ejercicios anteriores (arrastre 4 años)
  - Cálculo del nuevo saldo pendiente

Uso como módulo:
    from compensacion_perdidas import calcular_compensacion, cargar_perdidas, guardar_perdidas

Uso standalone:
    python compensacion_perdidas.py --ejercicio 2025 --gp-neto -3000 --rcm-neto 5000
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

ZERO = Decimal("0")
CENT = Decimal("0.01")
CROSS_LIMIT = Decimal("0.25")  # 25% compensación cruzada


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class PerdidaPendiente:
    """Saldo negativo de un ejercicio anterior pendiente de compensar."""
    ejercicio_origen: int
    importe_original_eur: Decimal
    compensado_eur: Decimal
    pendiente_eur: Decimal
    expira: int                   # último año en que se puede aplicar
    detalle: str = ""

    def to_dict(self) -> dict:
        return {
            "ejercicio_origen": self.ejercicio_origen,
            "importe_original_eur": float(self.importe_original_eur),
            "compensado_eur": float(self.compensado_eur),
            "pendiente_eur": float(self.pendiente_eur),
            "expira": self.expira,
            "detalle": self.detalle,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PerdidaPendiente":
        return cls(
            ejercicio_origen=int(d["ejercicio_origen"]),
            importe_original_eur=Decimal(str(d["importe_original_eur"])),
            compensado_eur=Decimal(str(d["compensado_eur"])),
            pendiente_eur=Decimal(str(d["pendiente_eur"])),
            expira=int(d["expira"]),
            detalle=d.get("detalle", ""),
        )


@dataclass
class ResultadoCompensacion:
    """Resultado completo del cálculo de compensación para un ejercicio."""
    ejercicio: int

    # Saldos de entrada
    gp_bruto: Decimal              # G/P patrimoniales brutas del ejercicio
    gp_no_deducible_2m: Decimal    # Pérdidas regla 2 meses (no computan)
    gp_deducible: Decimal          # G/P netas deducibles
    rcm_neto: Decimal              # Rendimientos capital mobiliario netos
    opciones_pl: Decimal           # P&L opciones (casillas 1624-1654)

    # Paso 1: compensación intra-compartimento
    gp_total: Decimal              # gp_deducible + opciones_pl
    saldo_gp_tras_intra: Decimal

    # Paso 2: compensación cruzada (25%)
    cruce_gp_a_rcm: Decimal        # pérdidas G/P aplicadas contra RCM
    cruce_rcm_a_gp: Decimal        # RCM- aplicado contra G/P+
    saldo_gp_tras_cruce: Decimal
    saldo_rcm_tras_cruce: Decimal

    # Paso 3: aplicación de pérdidas anteriores
    perdidas_anteriores: list[PerdidaPendiente] = field(default_factory=list)
    aplicadas_de_anteriores: Decimal = ZERO
    detalle_aplicacion: list[dict] = field(default_factory=list)
    saldo_gp_final: Decimal = ZERO

    # Paso 4: nuevo saldo a arrastrar
    nuevo_saldo_negativo: Decimal = ZERO
    perdidas_actualizadas: list[PerdidaPendiente] = field(default_factory=list)
    perdidas_expiradas: list[PerdidaPendiente] = field(default_factory=list)
    perdidas_proximas_expirar: list[PerdidaPendiente] = field(default_factory=list)

    # Totales base del ahorro para declaración
    base_ahorro_gp: Decimal = ZERO
    base_ahorro_rcm: Decimal = ZERO


# ── Carga/guardado del JSON ──────────────────────────────────────────────────

def _find_json_path(base_dir: str | None = None) -> str:
    """Busca perdidas_pendientes.json en las ubicaciones posibles."""
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, "perdidas_pendientes.json"))
    # Ubicación principal: /app/720/irpf/ (junto al módulo)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "perdidas_pendientes.json"))

    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)

    # Devolver primera ubicación escribible
    for path in candidates:
        parent = os.path.dirname(os.path.abspath(path))
        if os.access(parent, os.W_OK):
            return os.path.abspath(path)

    return os.path.abspath(candidates[0])


def cargar_perdidas(base_dir: str | None = None) -> list[PerdidaPendiente]:
    """Carga pérdidas pendientes del JSON."""
    path = _find_json_path(base_dir)
    if not os.path.exists(path):
        return []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    return [PerdidaPendiente.from_dict(p) for p in data.get("perdidas_pendientes", [])]


def guardar_perdidas(
    perdidas: list[PerdidaPendiente],
    base_dir: str | None = None,
) -> str:
    """Guarda pérdidas pendientes al JSON. Devuelve la ruta usada."""
    path = _find_json_path(base_dir)
    data = {
        "perdidas_pendientes": [p.to_dict() for p in perdidas if p.pendiente_eur > ZERO],
        "_nota": (
            "Fichero de control de saldos negativos de ejercicios anteriores "
            "pendientes de compensar (Art. 49.1.b LIRPF). "
            "Actualizado automaticamente por generar_irpf.py y el servicio web."
        ),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except PermissionError:
        # Fallback: intentar en /tmp y avisar
        alt_path = os.path.join("/tmp", "perdidas_pendientes.json")
        with open(alt_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  *** No se pudo escribir en {path} — guardado en {alt_path}")
        return alt_path
    return path


# ── Auto-detección de pérdidas de ejercicios anteriores ─────────────────────

def auto_detectar_perdidas_anteriores(
    fifo_matches: list,
    ejercicio_actual: int,
    opciones_pl_por_ano: dict | None = None,
    rcm_neto_por_ano: dict | None = None,
    max_anios: int = 4,
) -> list[PerdidaPendiente]:
    """Construye la lista de pérdidas pendientes de los últimos `max_anios`
    ejercicios a partir de los matches FIFO multi-año.

    Consideraciones:
      · Sólo años estrictamente anteriores al actual y dentro del plazo de 4
        años de arrastre (Art. 49.1.b LIRPF).
      · Para cada año se calcula el saldo G/P patrimonial neto (sumando
        ganancias/pérdidas FIFO y excluyendo las pérdidas no deducibles por
        regla 2 meses) + P&L opciones + cruce 25% con RCM.
      · Si el saldo resultante es negativo, se emite una `PerdidaPendiente`
        con `compensado_eur=0`. El usuario debe ajustarlo en
        `perdidas_pendientes.json` si ya compensó algo en declaraciones
        anteriores a la actual.

    Args:
        fifo_matches: Lista de FIFOMatch de motor_fiscal.
        ejercicio_actual: Año que se está declarando.
        opciones_pl_por_ano: dict {año: P&L opciones neto}. Opcional.
        rcm_neto_por_ano: dict {año: RCM neto (dividendos)}. Opcional.
        max_anios: Ventana de arrastre (default 4).

    Returns:
        Lista de `PerdidaPendiente` para los años con saldo negativo.
    """
    opciones_pl_por_ano = opciones_pl_por_ano or {}
    rcm_neto_por_ano = rcm_neto_por_ano or {}

    # Agrupar matches por año
    gp_por_ano: dict[int, Decimal] = {}
    gp_2m_por_ano: dict[int, Decimal] = {}
    for m in fifo_matches:
        y = m.ejercicio_fiscal
        if y >= ejercicio_actual:
            continue  # este año se calcula aparte
        gp_por_ano[y] = gp_por_ano.get(y, ZERO) + m.ganancia_perdida
        if m.regla_2_meses and m.ganancia_perdida < ZERO:
            gp_2m_por_ano[y] = gp_2m_por_ano.get(y, ZERO) + m.ganancia_perdida

    resultados: list[PerdidaPendiente] = []
    ventana_inicio = ejercicio_actual - max_anios
    for y in sorted(gp_por_ano):
        if y < ventana_inicio:
            continue  # ya expirada
        gp_bruto = gp_por_ano[y]
        # Excluir las pérdidas no deducibles por regla 2M
        gp_deducible = gp_bruto - gp_2m_por_ano.get(y, ZERO)
        opciones_pl = Decimal(str(opciones_pl_por_ano.get(y, 0) or 0))
        rcm_neto = Decimal(str(rcm_neto_por_ano.get(y, 0) or 0))

        gp_total = gp_deducible + opciones_pl
        saldo_gp = gp_total
        saldo_rcm = rcm_neto

        # Compensación cruzada 25%
        if saldo_gp < ZERO and saldo_rcm > ZERO:
            max_cruce = (saldo_rcm * CROSS_LIMIT).quantize(CENT, ROUND_HALF_UP)
            cruce = min(abs(saldo_gp), max_cruce)
            saldo_gp += cruce
        elif saldo_rcm < ZERO and saldo_gp > ZERO:
            max_cruce = (saldo_gp * CROSS_LIMIT).quantize(CENT, ROUND_HALF_UP)
            cruce = min(abs(saldo_rcm), max_cruce)
            saldo_gp -= cruce

        if saldo_gp < ZERO:
            importe = abs(saldo_gp).quantize(CENT, ROUND_HALF_UP)
            resultados.append(PerdidaPendiente(
                ejercicio_origen=y,
                importe_original_eur=importe,
                compensado_eur=ZERO,
                pendiente_eur=importe,
                expira=y + max_anios,
                detalle=(
                    f"Saldo negativo G/P patrimoniales {y} "
                    f"(auto-detectado desde FIFO multi-año — verificar si "
                    f"ya compensaste parte en declaraciones anteriores)"
                ),
            ))
    return resultados


# ── Cálculo principal ────────────────────────────────────────────────────────

def calcular_compensacion(
    ejercicio: int,
    gp_bruto: Decimal,
    gp_no_deducible_2m: Decimal = ZERO,
    rcm_neto: Decimal = ZERO,
    opciones_pl: Decimal = ZERO,
    perdidas_previas: list[PerdidaPendiente] | None = None,
    auto_guardar: bool = False,
    base_dir: str | None = None,
) -> ResultadoCompensacion:
    """Calcula la compensación completa de un ejercicio.

    Args:
        ejercicio: Año fiscal (ej: 2025).
        gp_bruto: G/P patrimoniales brutas (acciones/ETFs).
        gp_no_deducible_2m: Pérdidas no deducibles por regla 2M (valor negativo o 0).
        rcm_neto: RCM neto (dividendos brutos - retenciones España).
        opciones_pl: P&L opciones cerradas/expiradas (casillas 1624-1654).
        perdidas_previas: Pérdidas anteriores. Si None, carga del JSON.
        auto_guardar: Si True, guarda el JSON actualizado.
        base_dir: Directorio base para el JSON.
    """
    if perdidas_previas is None:
        perdidas_previas = cargar_perdidas(base_dir)

    # Clonar las pérdidas previas — sus campos `compensado_eur` y `pendiente_eur`
    # representan el estado ANTES de este ejercicio (lo que el usuario cargó o
    # lo que auto-detectamos con compensado_eur=0). Trabajamos sobre copias
    # para no mutar la lista original y poder mostrar en el informe el estado
    # inicial frente al resultado tras aplicar la compensación de este año.
    _snapshot_inicial = [
        PerdidaPendiente.from_dict(p.to_dict()) for p in perdidas_previas
    ]
    perdidas_previas = [
        PerdidaPendiente.from_dict(p.to_dict()) for p in perdidas_previas
    ]

    # Asegurar que gp_no_deducible_2m es negativo o cero
    gp_no_deducible_2m = min(gp_no_deducible_2m, ZERO)

    gp_deducible = gp_bruto - gp_no_deducible_2m  # restar negativo = sumar

    # G/P total = acciones/ETFs + opciones
    gp_total = gp_deducible + opciones_pl

    # ── Paso 1: ya netado en gp_total ───────────────────────────────────
    saldo_gp = gp_total
    saldo_rcm = rcm_neto

    # ── Paso 2: Compensación cruzada (25%) ──────────────────────────────
    cruce_gp_a_rcm = ZERO
    cruce_rcm_a_gp = ZERO

    if saldo_gp < ZERO and saldo_rcm > ZERO:
        max_cruce = (saldo_rcm * CROSS_LIMIT).quantize(CENT, ROUND_HALF_UP)
        cruce_gp_a_rcm = min(abs(saldo_gp), max_cruce)
        saldo_gp += cruce_gp_a_rcm
        saldo_rcm -= cruce_gp_a_rcm

    elif saldo_rcm < ZERO and saldo_gp > ZERO:
        max_cruce = (saldo_gp * CROSS_LIMIT).quantize(CENT, ROUND_HALF_UP)
        cruce_rcm_a_gp = min(abs(saldo_rcm), max_cruce)
        saldo_rcm += cruce_rcm_a_gp
        saldo_gp -= cruce_rcm_a_gp

    saldo_gp_tras_cruce = saldo_gp
    saldo_rcm_tras_cruce = saldo_rcm

    # ── Paso 3: Aplicar pérdidas de ejercicios anteriores ───────────────
    aplicadas_total = ZERO
    detalle_aplicacion = []
    expiradas = []
    proximas_expirar = []

    perdidas_activas = sorted(
        [p for p in perdidas_previas if p.pendiente_eur > ZERO],
        key=lambda p: p.ejercicio_origen,
    )

    perdidas_validas = []
    for p in perdidas_activas:
        if p.expira < ejercicio:
            expiradas.append(p)
        else:
            perdidas_validas.append(p)

    # Aplicar contra saldo positivo de G/P
    gp_disponible = max(saldo_gp, ZERO)
    for p in perdidas_validas:
        if gp_disponible <= ZERO:
            break
        aplicar = min(p.pendiente_eur, gp_disponible)
        if aplicar > ZERO:
            detalle_aplicacion.append({
                "ejercicio_origen": p.ejercicio_origen,
                "pendiente_antes": p.pendiente_eur,
                "aplicado": aplicar,
                "pendiente_despues": p.pendiente_eur - aplicar,
            })
            p.compensado_eur += aplicar
            p.pendiente_eur -= aplicar
            gp_disponible -= aplicar
            aplicadas_total += aplicar

    saldo_gp_final = saldo_gp - aplicadas_total

    for p in perdidas_validas:
        if p.pendiente_eur > ZERO and p.expira == ejercicio + 1:
            proximas_expirar.append(p)

    # ── Paso 4: Nuevo saldo a arrastrar ─────────────────────────────────
    nuevo_saldo = ZERO
    nuevas_perdidas = list(perdidas_validas)

    if saldo_gp_final < ZERO:
        nuevo_saldo = abs(saldo_gp_final)
        nuevas_perdidas.append(PerdidaPendiente(
            ejercicio_origen=ejercicio,
            importe_original_eur=nuevo_saldo,
            compensado_eur=ZERO,
            pendiente_eur=nuevo_saldo,
            expira=ejercicio + 4,
            detalle=f"Saldo negativo G/P patrimoniales neto {ejercicio}",
        ))

    resultado = ResultadoCompensacion(
        ejercicio=ejercicio,
        gp_bruto=gp_bruto,
        gp_no_deducible_2m=gp_no_deducible_2m,
        gp_deducible=gp_deducible,
        rcm_neto=rcm_neto,
        opciones_pl=opciones_pl,
        gp_total=gp_total,
        saldo_gp_tras_intra=gp_total,
        cruce_gp_a_rcm=cruce_gp_a_rcm,
        cruce_rcm_a_gp=cruce_rcm_a_gp,
        saldo_gp_tras_cruce=saldo_gp_tras_cruce,
        saldo_rcm_tras_cruce=saldo_rcm_tras_cruce,
        # `perdidas_anteriores` refleja el ESTADO INICIAL del ejercicio:
        # importe_original, compensado_eur (en años previos al actual) y
        # pendiente_eur (lo que queda por compensar al empezar este año).
        # El `detalle_aplicacion` contiene lo aplicado en el ejercicio actual.
        perdidas_anteriores=_snapshot_inicial,
        aplicadas_de_anteriores=aplicadas_total,
        detalle_aplicacion=detalle_aplicacion,
        saldo_gp_final=saldo_gp_final,
        nuevo_saldo_negativo=nuevo_saldo,
        perdidas_actualizadas=nuevas_perdidas,
        perdidas_expiradas=expiradas,
        perdidas_proximas_expirar=proximas_expirar,
        base_ahorro_gp=max(saldo_gp_final, ZERO),
        base_ahorro_rcm=max(saldo_rcm_tras_cruce, ZERO),
    )

    if auto_guardar:
        guardar_perdidas(nuevas_perdidas, base_dir)

    return resultado


# ── Formateo para consola ────────────────────────────────────────────────────

def _fmt(d: Decimal) -> str:
    """Formatea Decimal como EUR."""
    sign = "-" if d < 0 else ""
    abs_d = abs(d).quantize(CENT, ROUND_HALF_UP)
    int_part, dec_part = str(abs_d).split(".")
    groups = []
    for i, c in enumerate(reversed(int_part)):
        if i > 0 and i % 3 == 0:
            groups.append(".")
        groups.append(c)
    formatted_int = "".join(reversed(groups))
    return f"{sign}{formatted_int},{dec_part} EUR"


def imprimir_resumen(r: ResultadoCompensacion) -> None:
    """Imprime resumen de compensación por consola."""
    print()
    print("  " + "=" * 60)
    print("  COMPENSACION DE PERDIDAS — Art. 49 LIRPF")
    print("  " + "=" * 60)

    print(f"\n  Ejercicio fiscal: {r.ejercicio}")
    print(f"  {'─' * 55}")
    print(f"  G/P patrimoniales (acciones/ETFs)  : {_fmt(r.gp_deducible)}")
    if r.gp_no_deducible_2m != ZERO:
        print(f"    (excl. regla 2M no deducibles   : {_fmt(r.gp_no_deducible_2m)})")
    if r.opciones_pl != ZERO:
        print(f"  P&L opciones (casillas 1624-1654)        : {_fmt(r.opciones_pl)}")
    print(f"  G/P total base del ahorro          : {_fmt(r.gp_total)}")
    print(f"  RCM neto (dividendos)              : {_fmt(r.rcm_neto)}")

    if r.cruce_gp_a_rcm > ZERO:
        print(f"\n  Compensacion cruzada (G/P- -> 25% RCM+):")
        print(f"    Perdidas G/P aplicadas vs RCM   : {_fmt(r.cruce_gp_a_rcm)}")
        print(f"    Saldo G/P tras cruce            : {_fmt(r.saldo_gp_tras_cruce)}")
        print(f"    Saldo RCM tras cruce            : {_fmt(r.saldo_rcm_tras_cruce)}")
    elif r.cruce_rcm_a_gp > ZERO:
        print(f"\n  Compensacion cruzada (RCM- -> 25% G/P+):")
        print(f"    RCM negativo aplicado vs G/P    : {_fmt(r.cruce_rcm_a_gp)}")
        print(f"    Saldo G/P tras cruce            : {_fmt(r.saldo_gp_tras_cruce)}")
        print(f"    Saldo RCM tras cruce            : {_fmt(r.saldo_rcm_tras_cruce)}")

    if r.perdidas_anteriores:
        print(f"\n  {'─' * 55}")
        print(f"  SALDOS NEGATIVOS DE EJERCICIOS ANTERIORES:")
        for p in sorted(r.perdidas_anteriores, key=lambda x: x.ejercicio_origen):
            aplicado = next(
                (d for d in r.detalle_aplicacion if d["ejercicio_origen"] == p.ejercicio_origen),
                None,
            )
            if aplicado:
                print(f"    {p.ejercicio_origen}: pendiente {_fmt(aplicado['pendiente_antes'])}"
                      f" -> compensado {_fmt(aplicado['aplicado'])}"
                      f" -> queda {_fmt(aplicado['pendiente_despues'])}"
                      f" | expira {p.expira}")
            elif p.pendiente_eur > ZERO:
                print(f"    {p.ejercicio_origen}: pendiente {_fmt(p.pendiente_eur)}"
                      f" (sin ganancias para compensar) | expira {p.expira}")

        if r.aplicadas_de_anteriores > ZERO:
            print(f"    TOTAL aplicado de anos anteriores: {_fmt(r.aplicadas_de_anteriores)}")

    if r.perdidas_expiradas:
        print(f"\n  PERDIDAS EXPIRADAS (no aplicables):")
        for p in r.perdidas_expiradas:
            print(f"    {p.ejercicio_origen}: {_fmt(p.importe_original_eur)} — "
                  f"expiro en {p.expira} sin compensar")

    if r.perdidas_proximas_expirar:
        print(f"\n  *** ALERTA: Perdidas que EXPIRAN en {r.ejercicio + 1}:")
        for p in r.perdidas_proximas_expirar:
            print(f"    {p.ejercicio_origen}: {_fmt(p.pendiente_eur)} pendiente — "
                  f"ULTIMA OPORTUNIDAD para compensar")

    print(f"\n  {'─' * 55}")
    print(f"  RESULTADO FINAL:")
    print(f"    Saldo G/P final                 : {_fmt(r.saldo_gp_final)}")
    if r.nuevo_saldo_negativo > ZERO:
        print(f"    Nuevo saldo negativo a arrastrar: {_fmt(r.nuevo_saldo_negativo)} "
              f"(expira {r.ejercicio + 4})")
    print(f"    Base ahorro G/P (a tributar)    : {_fmt(r.base_ahorro_gp)}")
    print(f"    Base ahorro RCM (a tributar)    : {_fmt(r.base_ahorro_rcm)}")

    # Instrucciones RentaWEB
    print(f"\n  {'─' * 55}")
    print(f"  INSTRUCCIONES RENTAWEB:")
    if r.detalle_aplicacion or r.nuevo_saldo_negativo > ZERO:
        print(f"  *** ATENCION: Hacienda NO aplica esto automaticamente ***")
        print(f"  Debes ir a 'Saldos negativos de ejercicios anteriores'")
        print(f"  (casillas 1186+) e introducir MANUALMENTE:")
        for d in r.detalle_aplicacion:
            print(f"    - Ejercicio {d['ejercicio_origen']}: aplicar {_fmt(d['aplicado'])}")
        if r.nuevo_saldo_negativo > ZERO:
            print(f"  El nuevo saldo de {r.ejercicio} ({_fmt(r.nuevo_saldo_negativo)}) lo declaras")
            print(f"  este ano y lo arrastras a los siguientes (hasta {r.ejercicio + 4}).")
    else:
        print(f"  Sin saldos negativos pendientes de ejercicios anteriores.")
    print()


# ── CLI standalone ───────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Compensacion de perdidas patrimoniales (Art. 49 LIRPF)")
    parser.add_argument("--ejercicio", type=int, required=True)
    parser.add_argument("--gp-neto", type=str, required=True, help="G/P patrimoniales netas")
    parser.add_argument("--rcm-neto", type=str, default="0", help="RCM neto")
    parser.add_argument("--opciones-pl", type=str, default="0", help="P&L opciones")
    parser.add_argument("--gp-2m", type=str, default="0", help="Perdidas no deducibles regla 2M")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--guardar", action="store_true", help="Guardar JSON actualizado")
    args = parser.parse_args()

    r = calcular_compensacion(
        ejercicio=args.ejercicio,
        gp_bruto=Decimal(args.gp_neto),
        gp_no_deducible_2m=Decimal(args.gp_2m),
        rcm_neto=Decimal(args.rcm_neto),
        opciones_pl=Decimal(args.opciones_pl),
        auto_guardar=args.guardar,
        base_dir=args.base_dir,
    )
    imprimir_resumen(r)


if __name__ == "__main__":
    main()
