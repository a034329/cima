# Cima backend

API REST construida con FastAPI 0.110 + Pydantic 2 + Python 3.12.

## Estado actual

🟡 **Scaffolding inicial** — endpoints stub que devuelven mocks con el
shape final. Sin BD, sin auth, sin motor fiscal aún conectado.

## Arrancar en desarrollo

```bash
cd /app/cima/backend

# 1. Crear venv (una vez)
python3 -m venv .venv
source .venv/bin/activate

# 2. Instalar dependencias
pip install -e ".[dev]"

# 3. Copiar config
cp .env.example .env

# 4. Lanzar servidor con autoreload
uvicorn app.main:app --reload --port 8000
```

Abrir:

- API: <http://localhost:8000>
- OpenAPI docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/api/health>
- Cartera mock: <http://localhost:8000/api/cartera>

## Tests

```bash
cd /app/cima/backend
pytest -q
```

## Estructura

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py          ← punto de entrada FastAPI
│   ├── config.py        ← settings via pydantic-settings (modo SaaS/Owner)
│   └── routers/
│       ├── health.py    ← GET /api/health
│       └── cartera.py   ← GET /api/cartera (mock con shape final)
├── tests/
│   └── test_health.py
├── pyproject.toml
├── .env.example
└── .gitignore
```

## Modos de ejecución

Controlado por `CIMA_MODE` en `.env`:

- `CIMA_MODE=saas`: modo producción para clientes. IA capada, disclaimers
  MiFID II, decisión humana explícita.
- `CIMA_MODE=owner`: modo instancia personal del fundador. IA sin
  restricciones, agente externo (Claude Code, voz) puede operar vía API.
  Defendible legalmente porque el usuario es a la vez prestador y cliente.

## Próximos pasos (ROADMAP H0.3 → H1.1)

- Migraciones Alembic con el modelo de ADR-002.
- SQLAlchemy 2 + asyncpg para acceso a Postgres.
- Auth con Supabase JWT verification.
- Importador one-shot del `analisis.xlsx` actual para tener cartera real
  del fundador en BD.
- Endpoint POST /api/transacciones para añadir operación manual.
- Conectar motor fiscal de Cuádrate (`/app/720/irpf/`).

## Decisiones técnicas relevantes

Ver `cima/docs/decisions/`:

- ADR-001 — Stack técnico de Cima
- ADR-002 — Modelo de datos multi-broker con opciones
