# Aprendizajes del WG personal → Cima

> Bitácora ligera de lo que aparece operando con la cartera personal (Excel + agente actual)
> y que conviene heredar al producto Cima. Una entrada por aprendizaje, con fecha y razón.
>
> Convención: la fuente de verdad de la doctrina sigue siendo el `CLAUDE.md` del personal,
> pero los aprendizajes anotados aquí marcan qué necesita reflejar el producto.

---

## 2026-05-XX — Plantilla de entrada

**Categoría**: doctrina / fiscal / regimen-macro / coaching / técnico
**Origen**: incidencia en WG personal, hallazgo en la sesión, fix de bug, etc.
**Aprendizaje**: descripción breve.
**Impacto en Cima**: ¿qué necesita el producto reflejar?
**Acción**: ¿qué hacer ya? ¿anotar en ROADMAP? ¿esperar a fase X?

---

## Aprendizajes consolidados (de las sesiones previas)

### 2026-05-13 — Convención GBX en analisis.xlsx

**Categoría**: técnico / convenciones de divisa
**Aprendizaje**: tickers LSE (`.L`) cotizan en GBX (peniques), no GBP. Mezclar GBX y GBP en celdas adyacentes produce CAGR4 falsos del −63% (DGE, JEQP). Yahoo devuelve dividendos en GBP, no en GBX.
**Impacto en Cima**: el modelo de datos debe tener **divisa canónica por celda** + conversión declarativa. No mezclar nunca.
**Acción**: incluir como ADR cuando se diseñe el modelo de datos (ROADMAP H1.1).

### 2026-05-13 — Auditar plusvalías sospechosas antes de mostrar

**Categoría**: técnico / UX
**Aprendizaje**: snapshots con ratio plusvalía/coste > 80% o CAGR4+Div > 50% suelen ser bugs de unidades, no éxitos reales (Gestamp, DGE, JEQP, 2CKA, NVO).
**Impacto en Cima**: añadir validador automático que detecte anomalías y las marque como "revisar" antes de mostrar al usuario.
**Acción**: integrar en pipeline de ingesta (ROADMAP H1.7 — Operativa diaria).

### 2026-05-18 — TR Bank tiene sucursal ES y retiene IRPF español

**Categoría**: fiscal
**Aprendizaje**: Trade Republic Bank GmbH, Sucursal en España (NIF W0322893I) es pagador residente español desde mediados de 2025. Retiene IRPF al 19% sobre intereses y dividendos. La retención va al popup RCM como nacional (campo "Retenciones"), no a deducción CDI casilla 0588.
**Impacto en Cima**: el parser TR ya lo refleja con `pais='ES'`. Mantener cuando se importe la lógica a Cima.
**Acción**: ya implementado en `/app/720/irpf/generar_irpf.py`. Heredar tal cual al producto.

### 2026-05-18 — Staking cripto = RCM (DGT V0975-22)

**Categoría**: fiscal / doctrinal
**Aprendizaje**: los rendimientos de staking (FREE_RECEIPT en TR, similar en otros) se tratan como rendimientos del capital mobiliario, valorados al precio EUR del momento de recepción.
**Impacto en Cima**: replicar tratamiento. Documentar al usuario que la doctrina mayoritaria es A (RCM), pero permitir consulta a asesor si quiere posición distinta.
**Acción**: ya implementado en parser TR. Documentar en UI cuando se construya.

---

## Cómo usar este archivo

- Cuando un aprendizaje aparece en WG personal: añadirlo aquí en menos de 5 minutos.
- En cada **revisión cruzada mensual** (ROADMAP hito transversal): leer este archivo entero y mover ítems al ROADMAP de Cima si procede.
- Cuando un aprendizaje quede plenamente reflejado en Cima: marcarlo `[INTEGRADO]` con fecha.

No es un changelog. Es una bitácora de tracción del conocimiento de un producto al otro.
