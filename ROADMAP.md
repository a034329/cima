# Cima — Roadmap

> Documento operativo. Vive, se actualiza, se commitea. No es marketing —
> es la lista de lo que hay que hacer y en qué orden.

**Última actualización**: 2026-06-01.

---

## Estado real (resumen al 2026-06-01)

> Lo construido va MUY por delante de los checkboxes de abajo (que se mantenían en ⬜).
> Backend FastAPI + SQLAlchemy (SQLite dev, micro-migraciones sin Alembic aún) + frontend
> Next 14 funcional. **312 tests backend en verde.** Repo git inicializado (commit base
> `2850de1`); push diferido al usuario.

**Fase 1 — Base: casi completa.**
- ✅ Modelo de datos multi-broker + triple PM (real/fiscal-ES/opciones). PM display = media ponderada.
- ✅ Importadores DEGIRO, IBKR, Trade Republic (+ reconciliación cross-broker FIFO). 🔵 faltan Trading212/ING/MyInvestor.
- ✅ Motor fiscal de Cuádrate in-process: FIFO multi-año, regla 2M, opciones, forex, intereses, letras, dividendos + retención ES (0591) / CDI (0588 parcial), bolsas 4 años, base ahorro.
- ✅ **Bloques de estrategia — ampliado más allá del plan original** (ver Fase 1.4 abajo): catálogo de categorías con fichas (criterios/no_es), objetivos de peso + déficit, colchón especial, y **flag `en_estrategia`** (cualquier bloque dentro/fuera de la IF, generaliza el colchón). 8 categorías + 2 opcionales (cripto, materias primas).
- ✅ **Clasificador IA puntual + lote** (1.6): compuertas deterministas + IA (Claude Max vía CLI) con distribución de probabilidad, razonamiento y few-shot de overrides del usuario.
- ✅ **Plan por valor** (PlanPaso, decisión por ISIN) + **plan de compra top-down** (hueco de asignación: objetivo/actual/planeado/déficit por bloque; watchlist-first para empresas nuevas). NO estaba en el roadmap original.
- ✅ UI: dashboard, posiciones, página de posición, Estrategia (Bloques/Plan/Estimaciones/Seguimiento/Rotación), hub Fiscalidad (7 sub-pestañas), Config.
- ✅ **Onboarding IA (1.5)** — wizard perfil→propuesta IA→firma; produce `PlanFirmado` (contrato de Ulises) + fija objetivos. Corre sobre Max (prod necesita adaptador anthropic). ✅ friction popups en **ambos flujos** (creación de paso del Plan + alta real de transacción contra Colchón/compounders, captura el override). ✅ vigilancia de cartera (alertas precio + panel Dashboard + contexto del asesor). ⬜ integración Cuádrate (1.9). ⬜ beta cerrada (1.10).

**Fase 2 — Estimaciones: el núcleo hecho.**
- ✅ Métricas valoración multi-método (PER/P_FCF/P_BV/P_FRE), CAGR4+Div, consenso analistas FMP, umbrales fiscales R-U.
- ✅ Editor custom + watchlist (Seguimiento) con valoración.
- ✅ Filtro fiscal de rotación (umbrales R-U) — página Rotación + columnas.
- 🔵 Catálogo: feed on-demand (FMP+YF+OpenFIGI) con caché, no un top-1.000 mantenido aún.

**Fase 3 — Plan IA: pendiente.** Stub de créditos (`registrar_uso_ia`). Régimen macro, comandos profundos, 9 filtros, PASO 0, AI Act → no construidos.

**Pendiente de fundador**: registrar `cima.app`, validación legal, git/CI, esquema mercantil, branding.

---

## Convenciones

- **Prioridad**: 🔴 alta · 🟠 media · 🟡 baja
- **Esfuerzo**: <1d, 1-3d, 1w, 2w+, 1m+
- **Estado**: ⬜ pendiente · 🔵 en curso · ✅ hecho · ⏸️ pausado · ❌ descartado
- Cada hito tiene: descripción + criterio de aceptación + dependencias.

---

## Hito 0 — Setup inicial (mes 0-1)

Objetivo: tener un repositorio funcional con stack decidido, CI básico, primeros endpoints stub, validación legal en marcha.

### 0.1 Decisiones de stack 🔴 1-3d ✅

- [ ] **Backend**: Python (FastAPI) por reutilización del motor Cuádrate (Python). Alternativa: Node (peor encaje con `generar_irpf.py`).
- [ ] **Frontend**: Next.js 14 + TypeScript + Tailwind. Alternativa: Remix.
- [ ] **DB**: PostgreSQL 16 (Supabase para fase beta, self-hosted en fase 3).
- [ ] **Auth**: Clerk / Supabase Auth / Auth.js. Decidir según pricing y MFA.
- [ ] **Pagos**: Lemon Squeezy (Merchant of Record, sin IVA propio).
- [ ] **Hosting backend**: Railway (continuidad con Cuádrate) o Fly.io.
- [ ] **Hosting frontend**: Vercel.
- [ ] **CI/CD**: GitHub Actions + tests + deploy a staging automático.
- [ ] **Observability**: Sentry (errores) + Plausible (analytics privacy-friendly).

**Criterio aceptación**: stack decidido, documentado en `docs/decisions/001-stack-tecnico.md`.

### 0.2 Validación legal con abogado fintech 🔴 1w ⬜

- [ ] Identificar 2-3 despachos: Cuatrecasas, finReg360, Futur Legal, Pinsent Masons.
- [ ] Sesión inicial (1-2h): validar modelo "coaching no asesoramiento" para MiFID II.
- [ ] Validar diseño dual modo Owner / modo SaaS.
- [ ] Validar tratamiento staking cripto (DGT V0975-22).
- [ ] Redactar términos y condiciones + disclaimers.

**Criterio aceptación**: documento legal firmado por el despacho confirmando que el diseño actual NO requiere licencia EAFN.

**Coste estimado**: 600-1.500 €.

### 0.3 Setup repositorio + CI 🔴 1-3d ⬜

- [ ] Estructura `cima/{backend,frontend,tests,docs}` (ya creada).
- [ ] GitHub repo privado.
- [ ] `.gitignore` riguroso (PII, .env, secretos).
- [ ] Pre-commit hooks (ruff, black, prettier, mypy).
- [ ] GitHub Actions: tests + lint en cada PR.
- [ ] Branch protection: main protegido, sólo PR con CI verde.

**Criterio aceptación**: PR de prueba pasa CI verde y se despliega a staging.

### 0.4 Entrevistas con usuarios diana 🔴 2w ⬜

- [ ] Identificar 15-20 usuarios de Cuádrate con perfil ICP.
- [ ] Entrevista de 30 min con cada uno (script estandarizado).
- [ ] Preguntas clave:
  - ¿Cómo gestionas hoy tu cartera + plan?
  - ¿Cuánto pagarías por una herramienta que (X, Y, Z)?
  - ¿Qué te frustra de tu Excel/tracker actual?
  - ¿Confiarías en un asesor IA o no?
- [ ] Sintetizar hallazgos en `docs/research/entrevistas-icp-2026-may.md`.

**Criterio aceptación**: 15 entrevistas hechas, hallazgos documentados, pricing validado contra disposición real a pagar.

### 0.5 Auditoría Cuádrate vs micartera.app 🟠 1d ✅

- [ ] Suscribirse 1 mes a micartera (≈10 €).
- [ ] Probar mismos CSVs reales en ambos.
- [ ] Documentar dónde Cuádrate gana, empata o pierde.
- [ ] Resultado: `docs/research/cuadrate-vs-micartera-2026-may.md`.

**Criterio aceptación**: matriz completa de funcionalidades con evidencia.

---

## Fase 1 — Base (mes 1-6)

Objetivo: tracker multi-broker con fiscalidad española completa, bloques de estrategia, onboarding IA y clasificador puntual. Funcionalmente equivalente a "Cuádrate + estrategia + onboarding".

### Criterio de paso a Fase 2

- 500 usuarios pagos sostenidos 90 días
- churn < 5%
- NPS > 50
- > 25% usan importador opciones
- > 50 solicitudes/mes de "estimaciones"

### 1.1 Modelo de datos multi-broker con opciones 🔴 2w ✅ (Alembic pendiente; SQLite dev)

- [ ] Diseño: posición global por ticker, lots por broker.
- [ ] Tablas: `cartera`, `posiciones`, `lots`, `transacciones`, `opciones`, `dividendos`, `corporate_events`.
- [ ] Triple PM por posición: real, fiscal-ES, opciones-total.
- [ ] Migraciones con Alembic.
- [ ] ADR: `docs/decisions/002-modelo-datos-multibroker.md`.

### 1.2 Importadores de broker 🔴 4w 🔵 (DEGIRO/IBKR/TR ✅; faltan Trading212/ING/MyInvestor)

Reutilizar parsers de Cuádrate (`/app/720/irpf/generar_irpf.py`). Orden:

- [ ] **DEGIRO** (transacciones + cuenta) — base ya operativa en Cuádrate.
- [ ] **IBKR** (Activity Statement) — base ya operativa en Cuádrate.
- [ ] **Trade Republic** — parser completado en este sprint ✅.
- [ ] **Trading 212** — nuevo, partir de Pocket Portfolio como referencia.
- [ ] **ING Bróker Naranja** — relevante mercado tradicional ES.
- [ ] **MyInvestor** — completa la oferta principal.
- [ ] Reconciliación cross-broker por ISIN (FIFO global).

### 1.3 Motor fiscal español 🔴 reutilizar ✅ (in-process desde Cuádrate; CDI 0588 parcial)

- [ ] Importar (no fork) las funciones de `/app/720/irpf/`:
  - FIFO multi-año por ISIN.
  - Regla 2 meses.
  - Opciones (5 casos DGT V2172-21).
  - Corporate actions (splits, ISIN_CHANGE, scrip puro/mixto, rights, M&A complejas).
  - Tasas externas (Tobin, Stamp Duty, SEC, FINRA, FTT).
  - Dividendos + retenciones + deducción CDI casilla 0588.
  - Bolsas pérdidas 4 años, RCM vs patrimoniales.
- [ ] Decidir estrategia: copiar a `backend/motor_fiscal/`, o extraer a `wg-core/` compartido entre Cuádrate y Cima.
- [ ] Tests de regresión: importar suite de `/app/720/tests/`.

### 1.4 Bloques de estrategia 🔴 1w ✅ (ampliado más allá del plan)

- [x] Catálogo con **fichas** (descripción + criterios medibles + frontera `no_es`) como fuente única.
- [x] 6 base (Compounders/Dividend Growth/Estable/High Yield/Satélite/Colchón) + 4 opcionales (Índice/Renta Fija/Cripto/Materias primas). Tope 12. Saco "Sin clasificar".
- [x] Objetivos de peso + tolerancia + **déficit/hueco de asignación** (plan de compra top-down).
- [x] **CAGR4+Div proyectado por bloque** (ponderado por valor de mercado) + cobertura de estimación — para estudiar rotaciones dentro de un bloque. Atenuado si no todas las posiciones tienen estimación.
- [x] **ETF/índice sin BPA**: CAGR proxy = revalorización de precio HISTÓRICA (máx. ~20a, yfinance, cacheado) + yield actual = retorno total. Así los bloques Índice/Satélite-ETF aportan al CAGR. (Renta fija se deja en su yield; el tipo BCE se descartó.)
- [x] **Flag `en_estrategia`**: cualquier bloque dentro/fuera de la IF (generaliza el colchón). Fuera → no cuenta para progreso IF ni para el déficit.
- [x] Colchón especial: efectivo asignado + rendimiento. CheckConstraint eliminado (validación en servicio).
- [x] UI agrupada: Estrategia IF / Fuera de estrategia / Disponibles (vacíos compactos).
- [ ] Drag & drop (se usa selector/asignación, no DnD — opcional).

### 1.5 Onboarding IA co-construido 🔴 3-4w ✅ (v1 wizard; prod necesita adaptador anthropic)

- [x] Wizard de 3 pasos: perfil → la IA propone reparto de bloques con objetivos % + razón → firma.
- [x] Output: `PlanFirmado` versionado (contrato de Ulises) + aplica `peso_objetivo` a los bloques.
- [x] Modo SaaS: disclaimer MiFID en la propuesta. Corre sobre Claude Max (dev).
- [x] **Viabilidad**: calcula el capital actual (en estrategia) + el RETORNO ANUAL REQUERIDO para el objetivo/horizonte; lo pasa a la IA y muestra veredicto (no usa "horizonte corto → conservador"; flag si el objetivo es poco realista).
- [x] **Guía de compra desde el déficit** (a nivel de bloque + criterios + chequeo de encaje del candidato; ver 1.6b). El usuario crea el paso COMPRAR — la IA no nombra valores.
- [ ] Simuladores históricos (2008/2020/2022) + chat conversacional — v1.1.

- [ ] Diseño conversacional paso a paso (6 etapas).
- [ ] Integración con Claude API + prompt caching.
- [ ] Tres simuladores: histórico (2008/2020/2022), capacidad IF, comparador mix.
- [ ] Output: plan personal firmado (PDF + JSON guardado en DB).
- [ ] Modo Owner: sin disclaimers; modo SaaS: con disclaimers.
- [ ] Tests: que la IA nunca diga "compra X" en modo SaaS (test adversarial).

### 1.6 Clasificador IA puntual 🔴 1w ✅

- [x] Endpoint puntual + **lote**: IA sugiere bloque con razonamiento + distribución de probabilidad.
- [x] Compuertas deterministas (cripto/ETF/yield) antes de la IA (sin coste). IA = Claude Max vía CLI (sin API key aún).
- [x] **Few-shot del sesgo del usuario**: los overrides (sugerida ≠ elegida) se capturan y reinyectan.
- [x] Usuario decide siempre; la IA nunca asigna sola. Vale para posiciones Y watchlist.
- [ ] Logs auditables formales (hoy se guardan overrides; falta traza completa).

### 1.6b Plan por valor + plan de compra top-down 🔴 ✅ (no estaba en el plan)

- [x] `PlanPaso`: cola de decisiones por ISIN (COMPRAR/REFORZAR/MANTENER/…), réplica de la hoja Plan de WG.
- [x] **Watchlist-first**: empresas nuevas se siguen (Seguimiento), se clasifican y se les crea paso COMPRAR.
- [x] **Hueco de asignación**: por bloque, objetivo% − proyectado% (actual + planeado) = déficit en %/€. El déficit marca dónde comprar; el régimen macro (Fase 3) dará el ritmo.
- [x] **Guía de compra (MiFID-safe)**: bajo el déficit de cada bloque infraponderado, sus **criterios** (de la ficha) + "Buscar candidato →" a Seguimiento. La IA no nombra valores.
- [x] **Chequeo de encaje del candidato**: `evaluar_candidato` (en qué bloque cae + chequeo de los criterios medibles: yield/beta/ROE/crecimiento; payout/cobertura/moat quedan cualitativos). Si vienes del déficit de un bloque, avisa si el candidato no lo cubre. Frontera por modo: prescriptivo (reforzar/rotar valores) solo en Owner.

### 1.7 Operativa diaria 🔴 2w 🔵 (añadir op + reconciliación + friction ✅; WebSocket ⬜)

- [x] Botón "Añadir operación" (modal, selector de posición existente, autocompleta isin/nombre/divisa).
- [x] Reconciliación al importar extractos (matching ±2 días, ±0,5% precio) + aplica el plan por ISIN tocado.
- [x] **Friction popups en alta real** (vender/comprar): `evaluar_friccion` antes del INSERT, `FriccionDialog` reutilizado de la creación de pasos, `POST /api/plan/registrar-friccion` captura el override. La operación entra confirmada (`confirmar_directo=True`) y dispara rebuild FIFO + `aplicar_transaccion` al plan.
- [x] Recálculo en cascada sub-segundo: `estado_posicion` + `calcular_fiscal` memoizados por sesión (dashboard 3,5s → 0,28s) y APIs financieras solo refetchan en prefill/forzar.
- [ ] WebSocket para refresh en tiempo real.

### 1.8 UI básica 🔴 4w ✅ (dashboard, posiciones, Estrategia, hub Fiscalidad, Config)

- [ ] Dashboard cartera (resumen por bloque, posiciones, alertas).
- [ ] Página de cada posición con triple PM y opciones cubiertas.
- [ ] Bolsas fiscales visibles.
- [ ] Vista del Plan firmado (read-only + revisión periódica).
- [ ] Configuración (perfil, brokers conectados, integración Cuádrate).

### 1.9 Integración Cuádrate 🟠 1w ⬜

- [ ] API entre Cima y Cuádrate (SSO + datos cartera).
- [ ] Descuento 50% Cuádrate para usuarios Cima.
- [ ] Botón "Generar declaración IRPF con Cuádrate" desde dashboard.

### 1.10 Beta cerrada 🔴 1m ⬜

- [ ] Apertura a 100-300 usuarios de Cuádrate (exclusiva primeros 3 meses).
- [ ] Feedback semanal.
- [ ] Fix bugs fiscales según aparecen.
- [ ] Monitoreo: NPS, churn, MRR.

---

## Fase 2 — Estimaciones (mes 6-9)

Objetivo: pasar de tracker a "decidir mejor". Catálogo top 1.000 mantenido, editor custom, métricas de valoración.

### Criterio de paso a Fase 3

- 2.000 usuarios pagos
- MRR > 15.000 €
- Validación legal completada
- Auditoría AI Act aprobada
- Prompt caching probado en producción

### 2.1 Catálogo top 1.000 empresas 🔴 2w 🔵 (feed on-demand FMP+YF+OpenFIGI con caché; falta catálogo mantenido)

- [ ] Pipeline ingesta: FMP + Yahoo Finance + SEC EDGAR.
- [ ] Cache 48h (lección del Excel actual).
- [ ] Cron job actualización diaria.
- [ ] Multi-divisa con divisa canónica por celda.

### 2.2 Métricas de valoración 🔴 2w ✅

- [x] Multi-método: PER, P/FCF, P/BV, P/FRE, **SOTP** (suma de partes: P/NAV × NAV/acción, para conglomerados/holdings — CK Hutchison). Migración de CHECK en SQLite vía rebuild idempotente.
- [ ] CAGR4 + Yield neto por posición.
- [ ] Switching cost 1Y, 2Y, 3Y, 4Y.
- [ ] Rentabilidad potencial agregada (C10 del Excel).

### 2.3 Editor custom 🟠 1w ✅ (watchlist/Seguimiento + múltiplo/métrica editables)

- [ ] Usuario añade empresas fuera del top 1.000.
- [ ] Métrica base 4Y editable.
- [ ] Múltiplo objetivo configurable.

### 2.4 Filtro fiscal automático 🟠 1w ✅ (umbrales R-U: página Rotación + columnas)

- [ ] Al considerar rotación, comparar CAGR4+Div destino vs umbrales R-U del origen.
- [ ] Mensaje claro al usuario.

---

## Fase 3 — Plan IA (mes 9-13)

Objetivo: el agente IA continuo con régimen macro, comandos profundos y los 9 filtros.

### 3.1 Régimen macro 🔴 2w ✅ (manual + auto-clasificación híbrida firmable)

- [x] **4 indicadores MANUALES** (ciclo, inflación/tipos, geopolítica, mercado) que fija el usuario — fiable, sin auto-fetch.
- [x] **Clasificador VERDE/AMARILLO/ROJO** por mayoría (empate → el más cauto).
- [x] **Calibración DCA**: régimen → tamaño de tramo + espaciado; integrado en la guía de compra (≈ N tramos por bloque según déficit). `RegimenPanel` en Estrategia.
- [x] **Regla −14% S&P500**: auto-fetch del drawdown del S&P (vs máx. 52s) + VIX (yfinance, cacheado); escala el tramo si caída −10/−20% + ciclo no recesivo + VIX<28; bloquea si ciclo ROJA (bear market), VIX>35 o caída >20%. Ofrece el tramo escalado en la guía con la salvedad COYUNTURAL (no auto-clasifica).
- [x] **Auto-clasificación híbrida** (`services/regimen_auto.py` + `routers/regimen.py` + `PropuestaRegimenCard`): los datos cuantificables (SP500 drawdown, VIX, Brent, WTI, curva 10y-3m) los aplica directamente sobre la tabla WG; la IA con **búsqueda web** (`investigar`) enriquece lo cualitativo (Fed dovish/hawkish, paro/PIB/probabilidad recesión, tensión geopolítica activa) ancla en los números provistos y devuelve señal+razón+fuentes por indicador. El job corre en segundo plano; la propuesta queda **firmable** (no toca el régimen vigente hasta que el usuario aprueba) en una sub-tarjeta del panel de Estrategia: comparativa ahora→propuesta por indicador, regenerable, descartable. `creditos.registrar_uso_ia('regimen_auto', 1)` por refresh.

### 3.2 Comandos profundos 🔴 3w 🔵 (/one-pager hecho; pestaña Análisis)

**Pestaña Análisis** (`/estrategia/analisis`) + `services/one_pager.py` + `GET/POST /api/analisis/{isin}/one-pager`.
- [x] `/one-pager` (estudio inicial): IA + búsqueda web → tesis por secciones (qué hace/tesis/riesgos/valoración/encaje/veredicto) + clasificación + fuentes. **Persistido** (`AnalisisGuardado`): se guarda y solo se regenera a petición.
- [x] **Valoración asistida** (`services/valoracion.py` + `/api/analisis/{isin}/valoracion`): la IA propone 3 escenarios (múltiplo + métrica 4Y) **ligados a la tesis del one-pager**; el backend calcula precio objetivo + CAGR; el usuario edita y **traslada a Estimaciones** (`PUT /api/estimaciones/{isin}` → el modelo recalcula). Persistido. **Multi-método**: PER (ancla en consenso/PER histórico real, sin falsa precisión) y no-PER (P/FCF, P/BV, P/FRE — la IA investiga el múltiplo sectorial de comparables y proyecta la métrica por acción; etiquetas dinámicas en UI).
- [x] **Comparables /comps** (`services/comps.py` + `/api/analisis/{isin}/comps` + sección en Análisis): la IA busca (web) 4-6 pares del sector y arma una **tabla de múltiplos** (PER/EV-EBITDA/P-FCF/yield/crecimiento/ROIC) con la empresa objetivo marcada (anclada en datos reales de Cima) + lectura de prima/descuento vs pares + fuentes. Job en segundo plano + polling. Cierra el ciclo hueco de bloque → elegir candidato → watchlist. Falta de WG: `/earnings` y `/edgar` (este último US-only, bajo valor para cartera EU).
- [x] **Recomendación FISCALMENTE CONSCIENTE** (`services/fiscal_contexto.py`): conecta el motor fiscal al cerebro que recomienda. Resume base YTD + **pérdidas pendientes** y cuánto **caduca este año** (úsalo-o-piérdelo) + **buffer** que absorbe la próxima plusvalía + **cosecha latente** (tax-loss harvesting) + avisos **regla 2M**. Lo consume el **asesor** (contexto + doctrina del prompt), la **auditoría de venta** (chequeos: buffer cubre la plusvalía → switching cost ~0, emparejar con pérdidas, caducidad, 2M) y la **hoja de ruta**. Además se arregló el agujero del **filtro de rotación**: la plusvalía nueva se resta contra el buffer de pérdidas pendientes (antes sobrestimaba el impuesto). **Regla 2M = DIFIERE la pérdida hasta la transmisión total sin recompra (Art. 33.5.f), NO la anula.**
- [x] **Jobs en segundo plano** (`services/jobs.py` + `AnalisisJob`): one-pager, valoración y comps (búsqueda web de minutos) corren en un hilo; POST lanza (202 `en_curso`), la UI hace **polling** del GET hasta `ok`/`error`. Robusto frente a timeouts de petición. (Dev/owner: hilo en 1 proceso; SaaS multi-worker → cola real, p.ej. RQ/Celery.)
- [ ] `/dcf` (validación valoración) — solo-US (cash-flow FMP).
- [ ] `/earnings` (impacto resultados) — vía web; solapa con PASO 0.
- [ ] `/edgar` (filings SEC) — API pública, solo-US.
- [ ] `/comps` (comparables sector) — **bloqueado**: necesita el catálogo (FMP 402).

### 3.3 9 filtros automáticos 🔴 2w 🔵 (auditoría de COMPRA hecha; lado VENTA vía fricción/Rotación)

**Auditoría pre-operación de COMPRA** (`services/auditoria.py` + `GET /api/auditoria/{isin}`, panel en FormCompra): sintetiza los filtros aplicables a comprar en un veredicto con luz verde / reservas.
- [x] Auditoría de bloque (reusa `evaluar_candidato`).
- [x] Filtro de fase (acumulación → cuestiona High Yield).
- [x] Criterio 15/15 (cláusula de oro para Bloque A).
- [x] Abogado del diablo (yield>7% en acumulación).
- [x] Filtro macro (régimen + ventana −14%).
- [x] Plan activo (pasos críticos pendientes) + tamaño de posición vs rango doctrina.
- [x] Calidad cualitativa → marcada como VERIFICAR (no se finge: cobertura/deuda/moat = juicio del usuario).
- [x] **Auditoría de VENTA** (`auditar_venta` + `GET /api/auditoria/{isin}?decision=VENDER`, panel en `NuevoPaso` del Plan): filtro fiscal de rotación (umbrales R-U), anti-churn (>10% → no rotar por <2%), regla del colchón (Bloque F intocable) y calidad de rotación (VERIFICAR). Complementa la fricción. **Cierra los 9 filtros.** `AuditoriaVista` extraído a componente compartido (compra+venta).

### 3.4 PASO 0/0A/0B/0C 🔴 2w 🔵 (contexto web + coyuntural/estructural HECHO en dev sobre Max)

**Desbloqueo clave**: el puerto IA gana `investigar()` (web). En dev corre sobre Claude Max con
`--allowedTools "WebSearch"` (read-only, sin bypass, sin FS/Bash). `services/paso0.py` +
`GET /api/contexto/{isin}` + botón "contexto" en Seguimiento (panel con clasificación/resumen/5
preguntas/fuentes/disclaimer). Smoke real: clasificó Emaar como COYUNTURAL con 12 fuentes.
- [x] Búsqueda contextual web automática (vía Max CLI; sin API key).
- [x] Test coyuntural vs estructural (5 preguntas) + clasificación con fuentes.
- [x] Regla del moat intangible (la aplica la IA en el prompt, no hardcodeada).
- [~] Disección del negocio (0A): la IA la hace en el resumen; no hay desglose por segmento/% estructurado.
- [ ] 0B (2ª búsqueda dirigida a causa raíz) y **adaptador anthropic** (`web_search` de la API) para SaaS.

### 3.5 Sistema de créditos 🟠 1w ⬜

- [ ] 30 análisis IA/mes en Plan IA.
- [ ] Ilimitados en Full/Pro.
- [ ] UI clara del uso.

### 3.6 Cumplimiento AI Act 🔴 2w ⬜

- [ ] Auditoría: riesgo limitado (no alto riesgo).
- [ ] Transparencia: razonamiento + fuentes + confianza por output.
- [ ] Decisión humana siempre final.
- [ ] No scoring patrimonial.

### 3.7 Sandbox CNMV 🟠 6m ⬜

- [ ] Aplicar al sandbox financiero (Ley 7/2020).
- [ ] 12-18 meses bajo supervisión.
- [ ] Validación oficial del modelo.

### 3.8 Asesor conversacional + Vigilancia ✅ (asesor + acciones + voz + vigilancia + hoja de ruta)

- [x] **Chat asesor IA** (`services/asesor.py` + `adapters/ia/asesor.py` + `/api/asesor` + pestaña **Asesor**): conversa con TODA la cartera + estrategia firmada + plan + régimen + alertas de vigilancia + doctrina en contexto; persistido por cartera (`MensajeAsesor`). Dual-rail: `completar` (rápido, sin web) por defecto; `investigar` (web search) cuando la pregunta es de actualidad (`_requiere_web`: "hoy", "noticias", "por qué sube"…). Frontera MiFID por modo (Owner prescriptivo, SaaS análisis+disclaimer).
- [x] **Asesor con ACCIONES** (Owner): propone acciones whitelisteadas (`ajustar_estimacion`, `crear_paso`) como bloque JSON → el frontend las muestra como **tarjetas Aplicar/Descartar** → al confirmar ejecuta el endpoint conocido. La IA nunca ejecuta libremente; el humano es el gate. `ajustar_estimacion` cubre **método (tipo_val), múltiplo+métrica y/o dividendo** (cualquier subconjunto). Anclado en consenso/comparables; "Investigar →" enlaza a la valoración (web). El asesor vive como **widget de chat global** (esquina inf. derecha, persistente al cerrar) + página `/asesor`.
- [x] **Voz → voz** (`SpeechRecognition` + `SpeechSynthesisUtterance`, `lang='es-ES'`): si la pregunta entró por micro, el asesor responde **leído automáticamente** y limpia URLs/markdown del texto hablado (natural). Útil en el día a día sin teclado.
- [x] **Vigilancia de cartera** (`services/vigilancia.py` + `SnapshotPrecio` + `/api/vigilancia` + panel en Dashboard): alertas de movimiento de precio vs el último "visto" (≥5% ALERTA, ≥10% CRÍTICA); "marcar visto" resetea el baseline; cada mover enlaza a Análisis para el "por qué". Alimenta el contexto del asesor. Sin cron (compara acumulado). Earnings/noticias por mover = futuro (datos US-only/web).
- [x] **Hoja de ruta al firmar** (`services/hoja_ruta.py` + `/api/hoja-ruta` job + `HojaRutaReview` como paso final del onboarding): HÍBRIDO — el **déficit € por bloque** es determinista (`calcular_distribucion`), la **IA solo ordena/razona** los pasos (anti-churn, 15/15, fase, tramo del régimen) sobre ISINs reales (cartera ∪ watchlist; descarta inventados). Se genera en segundo plano tras firmar; el usuario **aprueba cada paso** (reusa `AccionCard` → `crear_paso`). Bloques con déficit y sin instrumento → "huecos" con enlace a Análisis/watchlist. Regenerable. Cierra el hueco estrategia→plan.

---

## Fase 4 — Add-on Recuperación CDI (mes 14+)

Objetivo: captar segmento "dividendero serio" con recuperación automática del exceso de retención en origen.

### 4.1 Investigación inicial 🟠 2w ⬜

- [ ] Cuantificar demanda en base de usuarios Fase 1-3.
- [ ] Analizar partner: Divizend (10-15% revenue share).
- [ ] Decidir modelo: A (interno), B (partner), C (formularios pre-rellenos).

### 4.2 Implementación 🔴 2-6m ⬜

- [ ] Según modelo elegido.
- [ ] Foco inicial: DE (Alemania 11,375% recuperable), FR (15% vía 5000), CH (20% vía 85).

---

## Hito transversal — Modo Owner del fundador

Objetivo: que el fundador pueda usar Cima en su día a día como sustituto del Excel, sin las restricciones IA del SaaS, con agente externo (Claude Code, voz) actuando vía API.

### Tareas paralelas a las fases

- [ ] Flag `WG_MODE=owner` en config con efecto en agente, disclaimers y permisos.
- [ ] API REST completa con auth por API key personal.
- [ ] Servidor MCP encima de la API para que Claude Code lo consuma.
- [ ] Cliente de voz: Whisper STT → agente → MCP → API.
- [ ] Importador one-shot del Excel actual (`analisis.xlsx`) a la BD del producto.
- [ ] Documentación: cómo el agente externo opera Cima.

**Activación**: cuando Fase 1 esté en beta cerrada y la API esté estable.

---

## Decisiones pendientes (consolidadas del documento de diseño)

1. Esquema mercantil: misma sociedad que Cuádrate o sociedad separada con marca paraguas.
2. Modelo Fase 4 CDI: A (interno) / B (partner Divizend) / C (formularios).
3. Política empresas fuera top 1.000 en el Base: bloquear, mostrar sin métricas o input manual.
4. Onboarding por voz o solo texto inicial.
5. Compliance officer interno o externalizado.
6. Branding: ¿submarca de Cuádrate o marca paraguas independiente?

---

## Hitos transversales recurrentes

- **Cada 4 semanas**: revisión cruzada WG personal ↔ Cima (qué aprendizajes nuevos integrar).
- **Cada 8 semanas**: revisión del roadmap, repriorización, descartes.
- **Mensualmente**: backup de la BD a almacenamiento off-site (cuando exista BD productiva).

---

## Riesgos y mitigaciones (versión corta del doc Word)

| Riesgo | Mitigación |
|---|---|
| MiFID II — IA podría considerarse asesoramiento | Diseño semántico riguroso, plan firmado por usuario, validación legal H0.2 |
| AI Act — alto riesgo | Tres anclas: decisión humana, transparencia, no scoring |
| Coste API IA | Prompt caching obligatorio, IA puntual en Base, créditos en Plan IA |
| Divergencia con WG personal | Revisión cruzada mensual, extraer `wg-core/` cuando 3+ funciones dupliquen |
| Cambios fiscales en España | Capa fiscal aislada y versionada |
| Atascarse en Fase 1 | Criterios de paso medibles a priori |
| Canibalización Cuádrate | Cuádrate independiente, descuento 50% para Cima users |
| Adquisición fuera Cuádrate | Beta cerrada 3m exclusiva Cuádrate, luego SEO + comunidad + partnerships |

---

## Próximos pasos inmediatos (al 2026-06-01)

Cerrados desde la revisión anterior: friction popups en alta real (1.7), vigilancia de cartera (3.8),
voz↔voz del asesor, asesor con TODA la cartera, búsqueda web condicional, confirmación directa de
tx + rebuild FIFO + aplicar plan al instante, filtros por nombre en posiciones/estimaciones,
columnas opcionales (CAGR4+Div proyectado, rentab histórica con realizadas, primas opciones),
SOTP para conglomerados, opciones por subyacente con fallback FIGI. **Suite: 312 tests verdes.**

Lo siguiente, por valor:

1. 🔴 **Cablear ANTHROPIC_API_KEY** → desbloquea onboarding IA y deja la IA lista para producción (hoy va por Claude Max/CLI en dev). Bloqueador del SaaS.
2. 🟠 **Importadores** Trading 212 / ING / MyInvestor (1.2) + integración Cuádrate (1.9, SSO + descuento).
3. 🟠 **WebSocket** (1.7) para refresh tras tx — hoy es buen polling y memoización, pero el último 1% de UX vive ahí.
4. 🟠 **Catálogo top-1000 mantenido** (2.1) — hoy es feed on-demand con caché.
5. 🟠 **Refinar clasificador**: añadir beta + ROIC a los fundamentales para endurecer cortes Estable/Compounder (hoy los hace la IA por conocimiento).
6. 🟠 **Push del repo** a GitHub (a tu mano) + Actions: hoy es repo local con commit base `2850de1`.
7. 🟠 **Beta cerrada** (1.10) — apertura a 100-300 usuarios de Cuádrate.
8. 🟡 **3.5 Créditos** + **3.6 AI Act** — antes del SaaS.
9. 🟡 **0.2 Validación legal** + **0.4 Entrevistas ICP** — antes de cobrar.
10. 🟡 Decisión fundador: registrar `cima.app`, esquema mercantil, branding.
