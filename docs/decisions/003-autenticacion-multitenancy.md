# ADR-003 — Autenticación (Supabase Auth) y multi-tenancy

## Estado

🔵 Aceptada (diseño) — pendiente de implementación

## Contexto

Cima corre hoy como **mono-inquilino sin autenticación**: 38 endpoints resuelven la
cartera con `select(Cartera).first()` (cogen la primera que haya), no hay login, ni
JWT, ni scoping por usuario. Funciona porque en la práctica solo existe **una**
cartera (la de Ángel, modo `owner`). El endpoint `/api/bootstrap` crea `user + cartera
+ brokers` por email, **sin autenticación** — cualquiera podría crear o resolver datos.

Para que Cima sea un SaaS (o admita siquiera 2-3 beta-testers) hace falta:
1. **Autenticar** quién es el usuario.
2. **Aislar** los datos por usuario (multi-tenancy) — hoy es un IDOR por diseño: con
   dos carteras, el `.first()` devolvería datos de otro.

`modo owner` (Ángel, local) debe **seguir funcionando sin login** — no queremos que el
uso diario propio dependa de un proveedor de auth externo.

## Decisión

**Supabase Auth** como proveedor de identidad (emite JWT), verificado en el backend
FastAPI con una dependencia `get_current_user`, y **scoping por usuario** sustituyendo
los 38 `Cartera.first()` por la cartera del usuario autenticado. En **modo owner** la
auth se **puentea** (usuario único fijo, sin token). La BD de la app sigue separada
(SQLite dev → Postgres propio); Supabase se usa **solo para auth**, no como BD de la app.

## Justificación

- **Supabase Auth** encaja con el Postgres/Supabase ya previsto para beta (ROADMAP
  §infra), trae MFA, magic link y OAuth de serie, y evita el coste por MAU de Clerk.
- **Auth-only (no usar Supabase como BD de la app):** desacopla y reduce riesgo. Migrar
  el modelo de datos a la Postgres de Supabase para usar RLS es una decisión mayor
  aparte (ADR futuro); ahora mapeamos `auth.users.sub → users.id` propio.
- **Verificación por JWKS asimétrico (RS256/ES256):** el backend valida la firma con la
  clave pública de Supabase (endpoint JWKS), sin compartir secreto. Cacheamos el JWKS.
- **Puente en modo owner:** preserva el uso local de Ángel sin acoplarlo a la red.

## Arquitectura

### Flujo de auth (modo SaaS)

```
[Next.js] @supabase/ssr  ──signup/login──▶  [Supabase Auth]  ──JWT (access_token)──▶ [Next]
[Next] fetchJson añade  Authorization: Bearer <jwt>  ──▶  [FastAPI]
[FastAPI] get_current_user: verifica firma (JWKS Supabase) → sub + email
          → provisiona/recupera users.id propio  → CurrentUser
[FastAPI] get_current_cartera(CurrentUser): la cartera de ESE usuario
```

### Componentes

**Backend**
- `app/auth/supabase.py`: descarga y cachea el JWKS de Supabase
  (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`), verifica el JWT (firma + `exp` +
  `aud`), devuelve claims (`sub`, `email`).
- Dependencia `get_current_user(db, request) -> models.User`:
  - **modo owner** (`settings.is_owner`): devuelve el usuario owner único (por
    `OWNER_EMAIL`, auto-provisionado), **sin exigir token**.
  - **modo saas**: exige `Authorization: Bearer`; verifica; mapea `sub`→`users.supabase_id`
    (nueva columna) o por email; **auto-provisiona** `User (+ cartera + brokers default)`
    en el primer request autenticado. Sin token válido → 401.
- Dependencia `get_current_cartera(user, db) -> models.Cartera`: la cartera del usuario
  (1:1 por ahora). 404 si no existe (no debería tras el auto-provision).
- **Refactor de los 38 endpoints**: sustituir `select(Cartera).first()` por
  `cartera: models.Cartera = Depends(get_current_cartera)`. La mayoría son mecánicos.

**Frontend (Next 14 App Router)**
- `@supabase/ssr` para sesión (cookies httpOnly gestionadas por el helper SSR).
- Páginas `/login` y `/signup` (email+password y magic link).
- `middleware.ts`: redirige a `/login` si no hay sesión (salvo rutas públicas).
- `lib/api.ts`: `fetchJson` adjunta `Authorization: Bearer <access_token>` leyendo la
  sesión de Supabase. En owner mode (build local) el header es opcional.

**Modelo de datos**
- `User`: añadir `supabase_id: str | None` (UNIQUE, nullable — null para el owner local).
  El mapeo primario es `supabase_id`; email como respaldo/merge.
- Resto sin cambios: `Cartera.user_id` ya existe; el cascade ya aísla por usuario.

### Provisioning: adiós al bootstrap abierto

`/api/bootstrap` deja de ser público. El alta se hace en `get_current_user` la primera
vez que un usuario autenticado llega (crea `User` desde el JWT + cartera + brokers
default — la misma lógica que bootstrap, movida a un servicio `provisioning.py`).
Bootstrap queda como utilidad interna/owner.

## Fases de implementación

- **A — Backend auth core:** `supabase.py` (JWKS+verify), `get_current_user` con puente
  owner + auto-provision, columna `supabase_id` (micro-migración), `provisioning.py`.
- **B — Multi-tenancy:** `get_current_cartera`; refactor de los 38 endpoints a la
  cartera del usuario; quitar los `.first()`. Test IDOR (user A no ve cartera de B).
- **C — Frontend:** cliente Supabase (`@supabase/ssr`), `/login` `/signup`, middleware,
  Bearer en `fetchJson`.
- **D — Tests + endurecimiento:** owner-bypass, saas-requiere-token, 401 sin token,
  aislamiento entre usuarios, expiración/refresh de token.

Cada fase es desplegable; A+B no rompen owner mode (puente). El front (C) se puede
hacer detrás de un flag hasta que esté.

## Alternativas consideradas

- **Clerk:** DX excelente y MFA, pero coste por MAU y otro proveedor fuera del stack
  Supabase ya previsto. Descartado por coste/encaje.
- **Auth.js (NextAuth):** vive en el front; verificar en el backend Python es más
  artesanal (sesiones/JWT propios). Más fontanería para multi-servicio. Descartado.
- **Supabase como BD de la app + RLS:** lo más "puro" multi-tenant (aislamiento en la
  propia BD), pero implica migrar todo el modelo a su Postgres ahora y reescribir el
  acceso a datos. Demasiado de golpe. **Aparcado para un ADR futuro.**
- **Seguir sin auth (owner-only):** bloquea cualquier usuario externo y deja el IDOR.
  Solo válido mientras Cima sea de uso estrictamente personal.

## Consecuencias

**Positivas:** desbloquea usuarios reales (validación por uso) y cierra el IDOR de raíz;
base lista para pagos; owner mode intacto.
**Negativas/coste:** refactor de 38 endpoints (mecánico pero amplio, superficie de
regresión → tests de aislamiento obligatorios); dependencia de Supabase para SaaS;
gestión de refresh de token en el front. Es **fontanería, no diferenciación** — no
acerca ingresos por sí sola (el moat sigue siendo el Plan IA, Fase 3).

## Decisiones abiertas (pendientes de Ángel)

1. **1 cartera por usuario** (como hoy) vs varias. → Propongo **1 por ahora**; el modelo
   ya soporta N, se amplía cuando haga falta.
2. **Métodos de login:** ¿solo email+password, o también magic link y/o OAuth Google?
   → Propongo **email+password + magic link** al inicio; Google después.
3. **OWNER_EMAIL** para el puente del modo owner (tu email) — confirmar cuál.
4. **Transporte del token:** Authorization Bearer (recomendado para la API FastAPI) vs
   cookies. → Bearer desde el front, sesión Supabase en cookie SSR.

## Referencias

- ROADMAP.md §infra (Auth: Clerk/Supabase/Auth.js — esta decisión lo resuelve).
- Estado del código: 38 `select(Cartera).first()`, `/api/bootstrap` abierto, sin auth.
- [[cima-repo-independiente]], [[proyecto-cima]].

---

**Autor**: Ángel (diseño asistido)
**Fecha**: 2026-06-16
