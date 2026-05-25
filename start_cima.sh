#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# start_cima.sh — Arrancar Cima (backend + frontend) dentro del contenedor.
#
# Uso:
#   bash /app/cima/start_cima.sh
#
# Lo que hace:
#   1. Instala dependencias de backend si faltan (pip).
#   2. Instala dependencias de frontend si falta node_modules (npm install).
#   3. Lanza el backend FastAPI en :8000 en segundo plano.
#   4. Lanza el frontend Next.js en :3000 en primer plano (Ctrl+C para parar).
#   5. Al parar el frontend, mata también el backend.
#
# Variables de entorno opcionales:
#   CIMA_MODE         saas|owner  (default saas)
#   CIMA_BACKEND_PORT default 8000
#   CIMA_FRONTEND_PORT default 3000
# ──────────────────────────────────────────────────────────────────────────────

set -e

CIMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${CIMA_DIR}/backend"
FRONTEND_DIR="${CIMA_DIR}/frontend"
BACKEND_PORT="${CIMA_BACKEND_PORT:-8000}"
FRONTEND_PORT="${CIMA_FRONTEND_PORT:-3000}"
export CIMA_MODE="${CIMA_MODE:-saas}"

echo ""
echo "  =================================================="
echo "   Cima — backend + frontend de desarrollo"
echo "  =================================================="
echo "   Modo            : ${CIMA_MODE}"
echo "   Backend         : http://localhost:${BACKEND_PORT}"
echo "   Frontend        : http://localhost:${FRONTEND_PORT}"
echo "   OpenAPI docs    : http://localhost:${BACKEND_PORT}/docs"
echo "  =================================================="
echo ""

# ── Paso 1: dependencias backend (pip) ──────────────────────────────────────
# Verificamos `sqlalchemy` (la dep más reciente añadida). Si falta cualquiera,
# reinstalamos todo en bloque — es idempotente y rápido la 2ª vez.
echo "  [1/4] Verificando dependencias del backend..."
if ! python3 -c "import sqlalchemy, fastapi, pydantic_settings, multipart, email_validator" 2>/dev/null; then
    echo "        Instalando deps del backend (primera vez o tras actualizar)..."
    pip install --quiet --no-warn-script-location \
        'fastapi>=0.110' \
        'uvicorn[standard]>=0.27' \
        'pydantic[email]>=2.6' \
        'pydantic-settings>=2.2' \
        'python-dotenv>=1.0' \
        'python-multipart>=0.0.9' \
        'sqlalchemy>=2.0' \
        'httpx>=0.27' || {
        echo "  [ERROR] No se pudo instalar deps del backend"
        exit 1
    }
fi
echo "        OK"

# ── Paso 2: dependencias frontend (npm) ─────────────────────────────────────
echo "  [2/4] Verificando dependencias del frontend..."
if [ ! -d "${FRONTEND_DIR}/node_modules" ]; then
    echo "        Instalando deps del frontend (primera vez, puede tardar 2-3 min)..."
    cd "${FRONTEND_DIR}"
    npm install --silent --no-audit --no-fund || {
        echo "  [ERROR] No se pudo ejecutar npm install"
        exit 1
    }
    cd - >/dev/null
fi
echo "        OK"

# ── Paso 3: arrancar backend en background ──────────────────────────────────
echo "  [3/4] Arrancando backend FastAPI en :${BACKEND_PORT}..."
cd "${BACKEND_DIR}"
python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${BACKEND_PORT}" \
    --reload \
    > /tmp/cima_backend.log 2>&1 &
BACKEND_PID=$!

# Esperar a que el backend responda (máx 15 s)
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
        echo "        OK (PID ${BACKEND_PID})"
        break
    fi
    if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
        echo "  [ERROR] Backend murió al arrancar. Log:"
        tail -20 /tmp/cima_backend.log
        exit 1
    fi
    sleep 0.5
done

# ── Limpieza al salir ───────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "  Parando backend (PID ${BACKEND_PID})..."
    kill "${BACKEND_PID}" 2>/dev/null || true
    wait "${BACKEND_PID}" 2>/dev/null || true
    echo "  Backend parado. Hasta luego."
}
trap cleanup EXIT INT TERM

# ── Paso 4: frontend en foreground ──────────────────────────────────────────
echo "  [4/4] Arrancando frontend Next.js en :${FRONTEND_PORT}..."
echo ""
echo "  >>> Abre http://localhost:${FRONTEND_PORT} en tu navegador."
echo "  >>> Pulsa Ctrl+C para detener ambos servicios."
echo ""
cd "${FRONTEND_DIR}"
# Limpia la caché de build: en el volumen WSL/Docker, Next puede servir chunks
# viejos tras editar (y el file-watching no siempre detecta cambios).
rm -rf "${FRONTEND_DIR}/.next"
# WATCHPACK_POLLING fuerza el sondeo de ficheros → el hot-reload (Fast Refresh)
# funciona a través del montaje WSL/Docker, donde los eventos inotify no llegan.
export WATCHPACK_POLLING=true
export CHOKIDAR_USEPOLLING=true
exec npx --no-install next dev -H 0.0.0.0 -p "${FRONTEND_PORT}"
