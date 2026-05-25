# Cima frontend

Next.js 14 (App Router) + TypeScript + Tailwind CSS.

## Estado actual

🟡 **Scaffolding inicial** — una sola página `/` que consume el backend
mock (`/api/cartera`) y renderiza:

- KPIs de la cartera (capital, progreso IF, años a IF, yield).
- Tarjetas de bloques con peso objetivo vs actual y desviación.
- Tabla de posiciones con triple precio medio (real, fiscal-ES, opc. total).

## Arrancar en desarrollo

```bash
cd /app/cima/frontend

# 1. Instalar dependencias (una vez)
npm install

# 2. Copiar config
cp .env.local.example .env.local

# 3. Arrancar dev server
npm run dev
```

Abrir <http://localhost:3000>.

**El backend debe estar corriendo en paralelo** en el puerto 8000:

```bash
# En otra terminal:
cd /app/cima/backend
uvicorn app.main:app --reload --port 8000
```

## Estructura

```
frontend/
├── app/
│   ├── layout.tsx          ← layout global con header y footer
│   ├── page.tsx            ← homepage / dashboard de cartera
│   └── globals.css         ← tailwind + variables CSS
├── components/
│   ├── ResumenCartera.tsx  ← KPIs (capital, IF, yield, años)
│   ├── Bloques.tsx         ← tarjetas con peso obj vs actual
│   └── Posiciones.tsx      ← tabla con triple PM
├── lib/
│   ├── api.ts              ← fetch helpers + formato es-ES
│   └── types.ts            ← tipos espejo del backend
├── next.config.mjs         ← rewrites a backend en /api/*
├── tailwind.config.ts
├── tsconfig.json
└── package.json
```

## Decisiones (ver ADR-001)

- **Server Components por defecto**: la página principal es `async function` con
  `fetch` directo, sin TanStack Query todavía.
- **Sin estado global**: cada componente recibe sus props.
- **Sin shadcn/ui** inicialmente: componentes propios con Tailwind para no
  añadir dependencias prematuramente.
- **Formato es-ES** centralizado en `lib/api.ts`.
- **Tipos espejo manualmente** ahora; futuro: `openapi-typescript` desde el
  OpenAPI del backend.

## Próximos pasos

- Persistencia real en backend → la página dejará de mostrar mock.
- Acción "Añadir transacción" → modal + POST a `/api/transacciones`.
- Página `/plan` con plan firmado.
- Página `/fiscalidad` con bolsas de pérdidas + previa Modelo 100.
- Modo oscuro toggle (ya hay variables CSS dark).
- Autenticación (Supabase Auth) cuando haya datos reales.
