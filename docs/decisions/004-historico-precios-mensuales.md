# ADR-004 — Histórico de cierres mensuales (caché global compartida)

## Estado

🔵 Aceptada

## Contexto

Hoy Cima solo conoce el precio SPOT de cada valor (caché en disco `precios_cache.json`,
TTL 6h). No hay memoria de cierres pasados, así que no podemos:
- mostrar la evolución real (subidas/bajadas) de la cartera mes a mes,
- pintar una gráfica de evolución global y por acción.

Queremos un histórico de **precios de cierre mensuales (EOM)** de los valores que los
usuarios tienen en cartera. Coste y fuentes:
- Es dato de MERCADO (público, no privado del usuario) → se puede **compartir entre todos
  los usuarios**: si dos personas tienen AAPL, el cierre de marzo-2025 de AAPL es el mismo.
- Solo interesa la franja temporal en la que **algún** usuario tuvo el valor a cierre de mes
  (no toda la historia del valor).

Esto introduce el **primer dato compartido entre carteras** del producto: hasta ahora todas
las tablas eran `cartera_id`-scoped.

## Decisión

Tablas **globales** (sin `cartera_id`) para cachear cierres mensuales:
- `precios_mensuales(simbolo, anio_mes, cierre, divisa, fuente, ts)` — cierre nativo crudo.
- `fx_mensuales(divisa, anio_mes, rate_eur, ts)` — factor a EUR de ese mes.

Clave de negocio `(simbolo, anio_mes)` / `(divisa, anio_mes)` → dedup automático entre
usuarios. Se rellena **incrementalmente**: cada cartera que abre la vista aporta su franja
de tenencia (primer mes con BUY → último mes con cantidad>0) al caché global; con el tiempo
el caché cubre la unión de todas las franjas. Backfill **automático** vía job en segundo
plano (mirror de `regimen_auto`/one-pager), `tipo="historico_precios"`, `isin=""`.

Valoración EUR por mes y cartera (NO se cachea, se calcula al vuelo):
`valor_mes = Σ_isin (cantidad_a_cierre_de_mes_de_ESA_cartera × cierre_nativo × fx_mes)`.
La cantidad a cierre se deriva de las transacciones (suma corrida BUY−SELL hasta el último
día del mes). La gráfica muestra **dos líneas**: valor de mercado y **capital neto aportado**
acumulado (Σ importe_eur de compras − ventas); la separación = plusvalía total.

## Justificación

- **Cierre CRUDO, no ajustado por splits** (`yfinance.history(interval="1mo", auto_adjust=False)`,
  columna `Close`). La cantidad poseída histórica ya refleja las acciones reales de cada
  momento (los splits entran como transacciones/lotes). Usar el cierre AJUSTADO (que reescala
  el pasado al post-split) contra acciones-de-entonces daría un valor mal alrededor del split.
  Crudo × acciones-de-entonces = valor correcto en cada mes.
- **EOM** = `Close` de la barra mensual de yfinance (último día de cotización del mes).
- **FX mensual aparte** (no guardar EUR ya convertido): el mismo cierre nativo sirve para
  cualquier usuario sea cual sea su divisa de visualización; y el FX se reusa entre valores
  de la misma divisa. GBp/GBX → ×0.01 como en el spot.
- Tabla global compartida = una sola descarga sirve a todos → menos llamadas a la API y
  vista instantánea para el segundo usuario que tenga el valor.

## Alternativas consideradas

- **Cachear por cartera**: cada usuario descarga su propio histórico. Simple pero desperdicia
  llamadas y no aprovecha el solapamiento entre carteras. Descartada.
- **Guardar el valor ya en EUR**: pierde reutilización entre divisas/usuarios y ata el caché
  a una divisa de visualización. Descartada a favor de cierre nativo + FX mensual.
- **Cierre ajustado por splits/dividendos**: rompe la valoración histórica con acciones reales
  (ver Justificación). Descartada.
- **Toda la historia del valor**: descarga de más; solo cacheamos la franja de tenencia.

## Consecuencias

**Positivas:** evolución real de la cartera (global y por acción); caché compartida eficiente;
base para futuras métricas (TWR, drawdown, contribución por bloque).
**Negativas / límites conocidos:**
- Primer dato cross-tenant → cuidado: estas tablas NO se filtran por usuario (son públicas por
  diseño). La valoración por cartera sí es privada (se calcula con las transacciones del usuario).
- La "franja unión" se rellena incrementalmente (no se precalcula la unión global de golpe).
- Cierres de meses con corporate actions raras (cambios de ISIN, fusiones) pueden quedar con
  huecos → la serie los salta (best-effort, como el spot).
- yfinance puede no tener cierre mensual de algún mercado raro/cripto → hueco en la serie.

## Referencias

- ADR-003 (multi-tenancy: hasta ahora todo era cartera-scoped).
- `app/services/precios.py` (resolución ISIN→símbolo, FX spot, manejo GBX).
- Petición de Ángel 2026-06-17.

---

**Autor**: Ángel (vía agente)
**Fecha**: 2026-06-17
