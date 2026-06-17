# ADR-003 — Autenticación (propia, FastAPI) y multi-tenancy

## Estado

🔵 Aceptada — implementación por fases (A en curso). Supersede el borrador previo
basado en Supabase Auth (descartado: añadía vendor externo y servidor aparte).

## Contexto

Cima corre hoy como **mono-inquilino sin autenticación**: 38 endpoints resuelven la
cartera con `select(Cartera).first()`, no hay login ni JWT ni scoping por usuario.
Funciona porque solo existe una cartera (la de Ángel, modo `owner`). `/api/bootstrap`
crea `user + cartera + brokers` por email **sin autenticar** — IDOR por diseño en cuanto
haya dos carteras.

Para admitir usuarios externos hace falta autenticar + aislar por usuario. Requisitos
del fundador (2026-06-16): **todo autocontenido en Docker, sin vendor externo (no
Supabase alojado) y sin servidor aparte** (un `docker-compose`, no Vercel+Railway). El
`modo owner` debe seguir funcionando **sin login**.

## Decisión

**Autenticación propia en el backend FastAPI**, sin dependencias nuevas:
- **JWT HS256** firmado/verificado con `hmac`+`hashlib` de la stdlib.
- **Hash de contraseña con `hashlib.scrypt`** (KDF vetado de la stdlib) + sal por usuario.
- **Multi-tenancy**: los 38 `Cartera.first()` pasan a la cartera del usuario autenticado.
- **Modo owner**: la auth se **puentea** (usuario único por `OWNER_EMAIL`, sin token).
- **Despliegue autocontenido**: un `docker-compose` (FastAPI + Next + Postgres) en una
  caja. Sin servicios de auth externos ni hosting separado.

## Justificación

- **Cero dependencias / cero vendor**: stdlib cubre JWT (HS256) y KDF (scrypt). No es
  "rolling your own crypto" — son algoritmos estándar de la librería estándar; el riesgo
  real (confusión de algoritmo, no validar `exp`, comparación no constante) se controla
  validando explícitamente y usando `hmac.compare_digest`.
- **Self-contained gana a esta escala** (pre-usuarios, un dev): un compose es más simple,
  barato y sin CORS gymnastics. Vercel/Railway/Supabase son optimizaciones de escala que
  a 0 usuarios no aportan; se reconsideran si hay tráfico.
- **Contrato JWT desacoplado**: si algún día se quiere MFA/OAuth sin currárselo, se migra
  a GoTrue self-hosted o a un proveedor — el backend sigue verificando un JWT, el cambio
  queda localizado en cómo se EMITE.

## Arquitectura

### Flujo (modo SaaS)
```
[Next] /signup|/login ─▶ POST /api/auth/{signup,login} ─▶ verifica/crea + scrypt
                         ◀── { access_token (JWT HS256) } ──
[Next] fetchJson añade  Authorization: Bearer <jwt>  ──▶ [FastAPI]
[FastAPI] get_current_user: decode_token (firma+exp) → users.id (sub) → CurrentUser
[FastAPI] get_current_cartera(CurrentUser): la cartera de ESE usuario
```

### Componentes backend (stdlib, sin deps)
- `app/auth/passwords.py`: `hash_password` → `scrypt$n$r$p$salt$hash`; `verify_password`
  (constant-time). Params scrypt n=2^14, r=8, p=1.
- `app/auth/tokens.py`: `create_access_token(sub,email,ttl)` / `decode_token` (HS256;
  valida `alg`, `exp`, firma con `hmac.compare_digest`). Secreto `CIMA_JWT_SECRET`.
- `app/auth/deps.py`: `get_current_user`:
  - **owner**: devuelve/provisiona el usuario `OWNER_EMAIL`, **sin token**.
  - **saas**: exige Bearer; decodifica; busca `users.id == sub`; 401 si falta/ inválido.
- `app/services/provisioning.py`: `provision_user(db, email, modo)` → User + cartera +
  brokers default (lógica extraída de bootstrap, reutilizada por signup y owner).
- `app/routers/auth.py`: `POST /api/auth/signup`, `POST /api/auth/login`,
  `GET /api/auth/me`. (Magic link y OAuth: futuro — requieren SMTP/proveedor.)
- `get_current_cartera`: cartera del usuario (1:1 por ahora). 404 si no existe.

### Modelo de datos
- `User`: añadir `password_hash: str | None` (null para owner y, futuro, magic-link).
  `email` ya es UNIQUE. `Cartera.user_id` ya existe → el aislamiento es por ese FK.

### Frontend (Fase C)
- `/login` y `/signup` (email+password) contra `/api/auth/*`; token en memoria/cookie.
- `middleware.ts`: redirige a `/login` sin sesión. `fetchJson` adjunta el Bearer.

## Fases
- **A — Auth core (HECHA):** passwords, tokens, `get_current_user` (+puente owner),
  `provisioning`, endpoints `/api/auth/*`, columna `password_hash`. **NO toca los 38
  endpoints** → no rompe nada; aditivo y testeado en aislamiento.
- **B — Multi-tenancy (HECHA):** `get_current_cartera` scopa por usuario (cierra el IDOR)
  y se inyectó vía `Depends` en TODOS los routers (regimen + 31 ficheros, ~80 endpoints;
  helpers `_cartera`/`_cartera_o_404`/`_resolver_cartera_por_defecto` y los `select(Cartera).first()`
  inline eliminados). Tests de endpoint siguen verdes vía override autouse en `conftest`
  (resuelve "la única cartera" sin token); `tests/test_multitenancy.py` quita ese override
  y prueba el aislamiento real con tokens Bearer en modo saas (user A no ve la cartera de B).
  Bootstrap se conserva como atajo de seed (no concede acceso en saas: sin token y
  `password_hash=None`); el squatting de email se anota para Fase D. Suite 488 verde.
- **C — Frontend (HECHA):** `lib/auth.ts` (token JWT en cookie `cima_token` legible,
  isomórfica vía `next/headers` en SSR y `document.cookie` en cliente); `apiFetch` inyecta
  `Authorization: Bearer` en TODAS las llamadas (`fetchJson` + 16 fetch crudos reconvertidos);
  páginas `/login` y `/signup` (componente `AuthForm`); `middleware.ts` (gate de UX sólo en
  SaaS — redirige a `/login` sin cookie, y de `/login` a `/` con cookie); botón "Cerrar sesión"
  en Config (sólo SaaS). En owner mode no hay token y el backend puentea → UI intacta.
  El modo se expone al front con `NEXT_PUBLIC_CIMA_MODE` (default `owner`). `next build` verde.
- **D — Endurecimiento (HECHA):** rate-limit + lockout de login (`app/auth/ratelimit.py`,
  ventana deslizante en memoria por `(IP, email)` → 429 con Retry-After; clavado por IP+email
  para no permitir DoS de lockout a terceros); endpoint `POST /api/auth/refresh` (renueva token
  válido) + `AuthRefresher` en el front (renovación proactiva cada 12 h → evita el 401 en SSR);
  `fetchJson` redirige a `/login?next=` ante un 401 en cliente saas; el **squatting de email
  queda cerrado**: el signup RECLAMA una cuenta sin contraseña (provisionada por bootstrap)
  fijando la password en vez de 409. CORS ya era explícito (orígenes de settings, no `*`) y
  usamos Bearer (no cookies cross-origin). Suite 492 verde.
  - **Pendiente menor (cuando escale):** el rate-limit en memoria no se comparte entre
    réplicas/workers (necesitaría Redis); cookie httpOnly + token fuera de JS si el riesgo
    XSS lo justifica; rotación de secreto con `kid`/multi-clave para no invalidar todo de golpe.

## Alternativas consideradas
- **Supabase Auth alojado:** vendor externo + datos fuera. Descartado por el requisito
  autocontenido.
- **GoTrue self-hosted (Docker):** auth probada sin escribir código, pero un contenedor
  más + SMTP + upgrades. Reservado como ruta de migración si se necesita MFA/OAuth.
- **PyJWT + passlib/argon2:** estándar del ecosistema, pero añade dependencias; stdlib
  basta para HS256 + scrypt. Si se prefiere argon2 más adelante, swap localizado.

## Consecuencias
**Positivas:** cero vendor y cero deps nuevas; todo en un docker-compose; cierra el IDOR;
owner mode intacto; contrato JWT que permite migrar el emisor sin tocar el resto.
**Negativas:** poseemos código sensible de seguridad (mitigado: algoritmos stdlib,
validación explícita, tests); falta endurecimiento (rate-limit/lockout → Fase D); magic
link/OAuth quedan fuera del MVP; refactor amplio de 38 endpoints en Fase B (mecánico,
con test de aislamiento obligatorio). Sigue siendo fontanería, no diferenciación (el moat
es el Plan IA, Fase 3).

## Decisiones abiertas (defaults confirmados por Ángel)
1. **1 cartera por usuario** por ahora (modelo soporta N).
2. **Login**: email+password en el MVP; magic link/Google después.
3. **OWNER_EMAIL** = `gmarrero.angel@gmail.com` (confirmar/ajustar vía env).
4. **Token**: Authorization Bearer.

## Referencias
- ROADMAP.md §infra (Auth — resuelto aquí). Estado código: 38 `Cartera.first()`,
  `/api/bootstrap` abierto, sin auth. [[cima-repo-independiente]], [[proyecto-cima]].

---

**Autor**: Ángel (diseño asistido)
**Fecha**: 2026-06-16
