# Cima — backend FastAPI. Imagen de producción para Railway.
# Build context = este directorio (backend/). Railway: Root Directory = backend.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Libs de sistema requeridas por weasyprint (pango/cairo/gdk-pixbuf) para
# renderizar el PDF fiscal del orquestador IRPF (Roadmap 1.9). Sin estas
# libs el `import weasyprint` lanza OSError en runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 \
        libcairo2 libgdk-pixbuf-2.0-0 \
        libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Deps de runtime (sin [build-system] en pyproject → instalación explícita).
# psycopg2-binary para Postgres; requests/yfinance para el feed de precios.
# openpyxl/weasyprint/jinja2: generación XLSX + PDF de la declaración IRPF
# (vendor/cuadrate/webapp/excel_cartera y pdf_generator).
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
    "psycopg2-binary>=2.9" \
    "openpyxl>=3.1" \
    "weasyprint>=60" \
    "jinja2>=3.1"

COPY app ./app
# Motor + generadores XLSX/PDF de Cuádrate vendorizados. NO se sincronizan
# desde el contenedor (scripts/sync_cuadrate.py es solo para dev): la copia
# commiteada en el repo es lo que se despliega.
COPY vendor ./vendor

# Railway inyecta $PORT. CIMA_* vars (DATABASE_URL, FMP, etc.) van como secrets.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
