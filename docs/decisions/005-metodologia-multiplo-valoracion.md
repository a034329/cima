# ADR-005 — Metodología de asignación del múltiplo de valoración

## Estado

🔵 Aceptada (Fase 2a implementada; 2b/2c pendientes)

## Contexto

La valoración de Cima es `precio_obj = multiplo_objetivo × metrica_base_4y` según `tipo_val`.
Hasta ahora:
- **Qué TIPO de múltiplo** se aplica: heurística pobre — por defecto PER; las familias
  "contables" solo se MARCABAN para revisar (`revisar_tipo_val`), nunca se clasificaban.
  → gestoras, BDCs, REITs y bancos se valoraban por PER sobre EPS volátil (CAGR errático).
- **Qué VALOR de múltiplo** objetivo: solo PER tenía `min(forward consenso, mediana histórica)`;
  el resto, manual.

Ángel (2026-06-18): la asignación del múltiplo debe mirar (1) qué múltiplo aplica a la empresa
—"pero u otro", catálogo extensible—, (2) el múltiplo histórico normalizado, (3) los pares, y
(4) la calidad del negocio frente a sus pares (moat, márgenes, ROIC → prima/descuento).

## Decisión

Plano 2 en tres fases:

### Fase 2a — Clasificador de TIPO (HECHA)
`clasificar_tipo_val(industry, sector, nombre, fundamentales) → (tipo_val, confianza, razón)`,
determinista por modelo de negocio. SETEA el `tipo_val` cuando hay confianza (no solo marca), es
overridable por el usuario (queda fijado), y siembra la métrica donde es derivable del feed
(P_FCF = FCF/acción, P_BV = valor contable/acción). Clasifica DENTRO del catálogo actual
(PER, P_FCF, P_BV, P_FRE, SOTP) para no requerir migración del CHECK. Reglas:
- REIT / inmobiliario patrimonialista → **P_BV** (NAV; P_AFFO llega en 2c).
- Banca / seguros / financieras de balance → **P_BV**.
- Gestoras de activos / alternativos (OWL, BAM) → **P_FRE**.
- EPS ≤ 0 o errático con FCF positivo, o capital-intensivo → **P_FCF**.
- Holdings / conglomerados → **SOTP**.
- Resto → **PER**.
Captura señales de calidad (ROE, márgenes, crecimiento) en `consenso_json` para 2b.

### Fase 2b — VALOR objetivo del múltiplo (pendiente)
Para cada tipo, múltiplo objetivo = blend de **histórico normalizado** (mediana propia, sin
años atípicos) + **pares** (servicio `comps`, IA+web) **ajustado por calidad relativa** (prima/
descuento según ROE/márgenes/crecimiento vs mediana de pares). Hoy solo PER usa histórico; el
resto es manual. Requiere serie histórica de múltiplos no-PER y plomería de pares.

### Fase 2c — Catálogo extensible y múltiplos enterprise (pendiente)
Añadir **P_S** (no rentables/crecimiento), **P_AFFO** (REITs) y **EV/EBITDA·EV/EBIT**
(enterprise: `precio_obj = (multiplo × EBITDA − deuda_neta) / acciones`, rama distinta a la de
equity por-acción). Requiere migración del CHECK `tipo_val` (recrear tabla o quitar el CHECK y
validar en capa de app vía `TIPOS_VAL`) y campos de deuda neta + acciones.

## Justificación

- El tipo correcto es la base: sin él, el valor (2b) no tiene sentido. Por eso 2a primero.
- Clasificar dentro del catálogo actual da valor inmediato y CERO riesgo de migración.
- EV-based no encaja en el modelo equity por-acción → se aísla en 2c con su propia rama.

## Consecuencias

**Positivas:** financieras dejan de valorarse por PER; el usuario parte del tipo correcto + métrica
sembrada. **Límites:** 2a no fija el VALOR del múltiplo no-PER (sigue manual hasta 2b); P_S/P_AFFO/
EV-EBITDA aún no existen (2c). Misclasificación posible → siempre overridable + razón visible.

## Referencias

- ADR-004 (esquema estimaciones), auditoría valoraciones 2026-06-18 (Grupo 2), `comps.py`.

---

**Autor**: Ángel (vía agente) · **Fecha**: 2026-06-18
