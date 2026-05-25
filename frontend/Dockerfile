# Cima — frontend Next.js (standalone). Imagen de producción para Railway.
# Build context = este directorio (frontend/). Railway: Root Directory = frontend.
#
# NEXT_PUBLIC_API_URL se INLINEA en build → debe pasarse como build arg con la
# URL pública del backend en Railway (p. ej. https://cima-backend.up.railway.app).

# ── deps ──
FROM node:20-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ── build ──
FROM node:20-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# ── runner ──
FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
# Next standalone respeta $PORT (Railway lo inyecta) y $HOSTNAME.
CMD ["node", "server.js"]
