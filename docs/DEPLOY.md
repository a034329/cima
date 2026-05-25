# Despliegue — Cima en Railway

Monorepo, **dos servicios** desde el mismo repo + plugin **Postgres**. Auto-deploy en
cada push a la rama conectada.

## 1. Servicios

Crear en Railway, ambos apuntando al repo `a034329/cima`:

| Servicio   | Root Directory | Dockerfile        | Healthcheck   |
|------------|----------------|-------------------|---------------|
| `backend`  | `backend`      | `backend/Dockerfile`  | `/api/health` |
| `frontend` | `frontend`     | `frontend/Dockerfile` | `/`           |

Cada uno lleerá su `railway.json` (builder=DOCKERFILE). Railway despliega en cada push.

## 2. Postgres

Añadir el plugin **Postgres** y referenciarlo en el backend:

```
CIMA_DATABASE_URL=${{Postgres.DATABASE_URL}}     # postgresql://… → psycopg2 (instalado)
```

El esquema se crea solo al arrancar (`init_db` → `create_all`). Las micro-migraciones
`ALTER ADD COLUMN` son solo-SQLite (se saltan en Postgres), así que una BD **nueva** queda
con el esquema completo. Para evolucionar un esquema ya productivo → Alembic (pendiente).

## 3. Variables de entorno

**backend** (Railway → Variables):
```
CIMA_MODE=saas
CIMA_ENVIRONMENT=production
CIMA_DATABASE_URL=${{Postgres.DATABASE_URL}}
CIMA_CORS_ORIGINS=["https://<dominio-del-frontend>"]
CIMA_FMP_API_KEY=<clave FMP>
CIMA_IA_PROVIDER=anthropic            # ver nota IA abajo
CIMA_ANTHROPIC_API_KEY=<clave>
```

**frontend** (se INLINEA en build → ponerla antes del build; Railway la pasa como build arg):
```
NEXT_PUBLIC_API_URL=https://<dominio-público-del-backend>
```

## 4. Notas / pendientes conocidos

- **IA en producción**: el contenedor NO trae el CLI de Claude Max, así que `claude_cli`
  no vale en prod. El adaptador `anthropic` está **stub** (NotImplementedError) → las
  features IA (sugerir/autoclasificar bloque) fallarán hasta implementarlo (reusar
  `build_mensajes`/`parse_respuesta` + prompt caching). Mientras tanto, `CIMA_IA_PROVIDER=mock`
  deja la app usable con clasificación degradada. El resto (tracker, fiscal, plan, hueco) va.
- **Motor fiscal de Cuádrate**: vendorizado en `backend/vendor/cuadrate/` (commiteado) →
  disponible en el contenedor. Sincronizar con `python scripts/sync_cuadrate.py`
  (`--check` para detectar si está desactualizado). La caché BCE (`ecb_fx_cache.json`) va
  incluida; en el contenedor es de solo-lectura → el motor re-consultará al BCE los tipos
  de fechas nuevas (más lento, no persiste). Refactor multi-usuario pendiente.
- **Datos**: la BD productiva arranca VACÍA (los usuarios hacen bootstrap + import). El
  `cima.db` de dev y los `test_data/` reales NO se versionan (ver `.gitignore`).
