# ADR-001 — Stack técnico de Cima

## Estado

⬜ Propuesta — pendiente de validación final por el fundador.

## Contexto

Cima es un SaaS web con motor fiscal complejo, agente IA, multi-tenant y
necesidades de tiempo real moderadas. Hay dos restricciones fuertes:

1. **Reutilizar Cuádrate al máximo**. El motor `/app/720/irpf/generar_irpf.py`
   es Python (7.726 líneas, ~37 tests, 6 años de doctrina). Sustituir lenguaje
   significa reescribir todo eso. Inviable.

2. **Time-to-market corto**. Fase 1 en 3-5 meses con un equipo mínimo (1-2
   personas). No es momento de stacks exóticos.

## Decisión

```
Backend       : Python 3.12 + FastAPI + SQLAlchemy 2 + Alembic
DB            : PostgreSQL 16 (Supabase managed en fase 0-2; self-hosted en Railway desde fase 3)
Auth          : Supabase Auth (correo + MFA TOTP)
Frontend      : Next.js 14 (App Router) + TypeScript + Tailwind + shadcn/ui
Estado UI     : TanStack Query (server state) + Zustand (UI state local)
Pagos         : Lemon Squeezy (Merchant of Record europeo)
IA            : Anthropic SDK con prompt caching obligatorio
Hosting back  : Railway (continuidad con Cuádrate)
Hosting front : Vercel
CI/CD         : GitHub Actions
Observability : Sentry (errores) + Plausible (analytics privacy-friendly)
Tests         : pytest (backend) + Playwright (e2e) + Vitest (frontend unit)
Lint/format   : ruff + black + mypy strict (backend) · biome o eslint + prettier (frontend)
Tipo de fichas: openpyxl + python-docx (compatibilidad Cuádrate)
```

## Justificación por capa

### Backend — Python + FastAPI

Python es **innegociable** por reutilización de Cuádrate. FastAPI vs alternativas:

- **FastAPI** (elegido): async nativo, validación con Pydantic v2, OpenAPI
  automático, ecosistema maduro 2026. Curva de aprendizaje baja.
- Django: demasiado opinionado, ORM no encaja bien con el motor fiscal que
  ya es funcional. Overkill para el SaaS.
- Flask (que usa Cuádrate hoy): sin async, validación manual, no genera
  OpenAPI. Cuádrate funciona, pero Cima necesita más.

**Pydantic v2** para todos los DTOs. Las entidades del motor fiscal pueden
seguir siendo dataclasses como están en `motor_fiscal.py`; se convierten a
Pydantic sólo en la frontera de la API.

### DB — PostgreSQL

Sin debate. JSON columns para datos semi-estructurados (plan personal, configuración),
relacional para todo lo demás. **Supabase managed** durante beta porque ahorra
la operativa de DB (backups, point-in-time recovery, etc.). Migrar a Postgres
self-hosted en Railway cuando el coste de Supabase supere al de operar.

Alembic para migraciones — sin discusión, es el estándar.

### Auth — Supabase Auth

Tres alternativas reales: Clerk, Auth.js, Supabase Auth.

- **Supabase Auth** (elegido): si vamos a usar Supabase como DB, integración
  natural. JWT estándar. MFA TOTP nativo. Coste 0 hasta 50.000 MAUs.
- Clerk: mejor UX out of the box, pero coste agresivo (≈25 USD/mes mínimo
  desde el primer usuario pago) y otro proveedor más.
- Auth.js: gratis, pero gestión propia más compleja.

Si en el futuro queremos OAuth con bancos (open banking PSD2), Supabase Auth
acepta proveedores OAuth custom.

### Frontend — Next.js + Tailwind + shadcn/ui

- **Next.js 14 App Router**: React Server Components reducen JS al cliente,
  bueno para SEO de páginas públicas, y la integración con Vercel es óptima.
- **Tailwind + shadcn/ui**: shadcn/ui no es una librería sino un set de
  componentes copy-paste basados en Radix UI. Cero lock-in, máxima
  customización. Estética profesional sin diseñar desde cero.
- **TanStack Query** para estado de servidor (queries + mutations con cache,
  refetch, etc.). **Zustand** sólo para estado UI local (modales abiertos,
  filtros temporales).

Alternativas descartadas:
- Remix: excelente, pero menos comunidad y menos integración Vercel.
- SvelteKit: curva de aprendizaje extra para 1-2 devs.
- Vue + Nuxt: ecosistema menor en fintech.

### Pagos — Lemon Squeezy

Cuádrate ya lo usa. Es Merchant of Record europeo: se encarga de IVA por país,
de los reembolsos, y de la cumplimentación fiscal de los pagos.
Comisión ~5% + 0,5 €/transacción, asumible.

Stripe sería ~3% + 0,25 € pero hay que gestionar el IVA + facturación por
nuestra cuenta. No vale la pena hasta que escalemos.

### IA — Anthropic SDK con prompt caching

Anthropic SDK directo. **Prompt caching es requisito**, no opcional, dado el
peso de la doctrina cargada en cada conversación (CLAUDE.md ~70 KB).

- Opus 4.7 para onboarding (1 vez por usuario, justifica el coste).
- Sonnet 4.6 para clasificador puntual y comandos /dcf, /comps, /earnings.
- Haiku 4.5 para tareas accesorias (clasificar tickers, parseo de descripción).

OpenAI/Gemini descartados por: (a) Anthropic SDK está más pulido para
extended thinking y tool use, (b) consistencia con Cuádrate y workflow
existente con Claude Code, (c) prompt caching de Anthropic es el de mejor
relación coste/funcionalidad en 2026.

### Hosting

- **Backend en Railway**: continuidad con Cuádrate, mismas credenciales,
  mismo equipo. ≈10-30 USD/mes para fase beta.
- **Frontend en Vercel**: integración nativa con Next.js, edge runtime,
  CI/CD automático. Tier Hobby gratis hasta tracción seria.

### CI/CD — GitHub Actions

Estándar. Pre-commit hooks (ruff, black, mypy, prettier). Branch protection
en `main`. Despliegue automático a staging desde `main`, a producción desde
tag `v*`.

### Observability

- **Sentry**: errores backend + frontend. Tier free hasta 5k eventos/mes.
- **Plausible**: analytics privacy-first (no requiere consent banner). 9 USD/mes.

Descartado Google Analytics: requiere cookie banner, no encaja con el ICP
medio-alto que valora la privacidad.

## Alternativas consideradas globalmente

### A. Stack JavaScript end-to-end (Node + Next.js)

- **Pros**: un único lenguaje, ecosistema unificado, menos context-switching.
- **Contras**: reescribir Cuádrate en Node sería 2-3 meses de trabajo sólo
  para llegar al punto de partida actual. **Inviable**.

### B. Stack Go + Vue

- **Pros**: performance, binarios estáticos.
- **Contras**: mismo problema de reescritura. Ecosistema fintech menor.

### C. Stack PHP (Laravel) + Inertia

- **Pros**: muy maduro para SaaS, ecosistema brutal en pagos y subscripciones.
- **Contras**: reescritura del motor, fuera de la zona de confort del equipo.

## Consecuencias

### Positivas

- Tiempo a primer commit funcional: días, no semanas.
- Motor fiscal de Cuádrate se reutiliza tal cual o con cambios mínimos.
- Stack moderno pero no exótico — fácil contratar si hace falta.
- Coste mensual ≤ 50 USD durante beta (Supabase free + Railway + Vercel + Plausible).
- OpenAPI generado solo → SDK del cliente para el frontend en TypeScript con `openapi-typescript`.

### Negativas

- Dos lenguajes (Python + TypeScript) implican context-switching para devs
  full-stack.
- Mezcla async/sync en Python (el motor fiscal es síncrono y CPU-bound,
  FastAPI es async). Mitigación: ejecutar el motor en background workers
  (Celery o RQ) cuando aparezcan operaciones largas (>500 ms).
- Dependencia de Anthropic (proveedor único). Mitigación: abstraer la capa
  IA detrás de una interfaz para poder cambiar si fuera necesario.

## Referencias

- Cuádrate `/app/720/`: Python + Flask, motor fiscal Python puro.
- ROADMAP H0.1 — esta es la decisión correspondiente.
- Próximo ADR: 002-modelo-datos-multibroker.md

---

**Autor**: Cima Team
**Fecha**: 2026-05-18
