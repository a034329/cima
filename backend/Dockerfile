# Cima — backend FastAPI. Imagen de producción para Railway.
# Build context = este directorio (backend/). Railway: Root Directory = backend.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Deps de runtime (sin [build-system] en pyproject → instalación explícita).
# psycopg2-binary para Postgres; requests/yfinance para el feed de precios.
RUN pip install \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "pydantic[email]>=2.6" \
    "pydantic-settings>=2.2" \
    "python-dotenv>=1.0" \
    "python-multipart>=0.0.9" \
    "sqlalchemy>=2.0" \
    "requests>=2.31" \
    "yfinance>=0.2" \
    "psycopg2-binary>=2.9"

COPY app ./app

# Railway inyecta $PORT. CIMA_* vars (DATABASE_URL, FMP, etc.) van como secrets.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
