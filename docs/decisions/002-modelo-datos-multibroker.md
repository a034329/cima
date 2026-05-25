# ADR-002 — Modelo de datos multi-broker con opciones

## Estado

⬜ Propuesta — pendiente de validación.

## Contexto

El motor fiscal de Cuádrate trabaja con estructuras Python en memoria
(dataclasses + listas). Cima necesita un modelo relacional persistente
que cumpla con:

1. **Multi-broker**: una misma posición (mismo ISIN) puede tener lots en
   varios brokers. El FIFO global debe casar cross-broker.
2. **Multi-divisa canónica**: cada celda monetaria lleva su divisa explícita.
   Sin mezclas (lección directa del bug GBX/HKD del Excel).
3. **Triple PM por posición**: real, fiscal-ES, opciones-total.
4. **Opciones**: contratos con strike, vencimiento, tipo, estado.
5. **Corporate actions**: splits, ISIN_CHANGE, scrip puro/mixto, fusiones,
   spin-offs, OPAs.
6. **Auditabilidad**: cada transacción tiene un `external_id` (transaction_id
   del broker) para deduplicación al reimportar extractos.
7. **Multi-tenant**: cada usuario tiene su cartera aislada. Row-level security.
8. **Versionado del plan personal**: el plan firmado en onboarding tiene
   versiones; un re-onboarding crea una versión nueva sin perder la previa.

## Decisión

Modelo relacional en PostgreSQL con 12 entidades principales:

```
                           ┌──────────────┐
                           │ users        │
                           └──────┬───────┘
                                  │ 1
                                  │
                  ┌───────────────┼───────────────┐
                  │ N             │ N             │ N
            ┌─────▼─────┐  ┌──────▼──────┐  ┌─────▼──────┐
            │ brokers   │  │ planes      │  │ carteras   │
            │ (cuentas) │  │ (versiones) │  │ (1 por user│
            └─────┬─────┘  └─────────────┘  │  típicamen-│
                  │ N      ┌─────────────┐  │  te)       │
                  │        │ bloques     │◄─┤            │
                  │        └─────┬───────┘  └─────┬──────┘
                  │              │ N              │
                  │        ┌─────▼──────┐         │ N
                  │        │ posiciones │◄────────┘
                  │        └─────┬──────┘
                  │              │ 1
                  │ N            │
                  │              │ N
            ┌─────▼──────────────▼─┐
            │ transacciones        │
            │ (BUY/SELL/DIV/INT/...) │
            └──────────┬───────────┘
                       │ 1
                       │
            ┌──────────▼───────────┐
            │ lots (FIFO)          │
            └──────────────────────┘

            ┌──────────────────┐  ┌──────────────────┐
            │ opciones         │  │ corporate_events │
            └──────────────────┘  └──────────────────┘

            ┌──────────────────┐  ┌──────────────────┐
            │ bolsas_fiscales  │  │ snapshots        │
            │ (4 años)         │  │ (auditoría)      │
            └──────────────────┘  └──────────────────┘
```

### Entidades principales

#### `users`
```sql
id              uuid PK
email           text UNIQUE
nif             text   -- residente fiscal ES
modo            text   -- 'owner' | 'saas' (control IA capada)
created_at      timestamptz
```

#### `brokers` (cuentas conectadas por user)
```sql
id              uuid PK
user_id         uuid FK
broker_tipo     text   -- 'degiro' | 'ibkr' | 'tr' | 'trading212' | 'ing' | 'myinvestor'
alias           text   -- "Mi DeGiro principal"
external_id     text   -- ID del broker para deduplicar reimports
divisa_base     text   -- 'EUR' | 'USD' | ...
created_at      timestamptz
```

#### `carteras` (1 por user típicamente)
```sql
id              uuid PK
user_id         uuid FK
nombre          text   -- "Cartera IF principal"
plan_activo_id  uuid FK -> planes(id)
```

#### `planes` (versiones del plan personal firmado)
```sql
id              uuid PK
cartera_id      uuid FK
version         int
firmado_en      timestamptz
contenido_md    text   -- el plan en primera persona
reglas_json     jsonb  -- reglas anti-pánico / anti-euforia parseadas
caduca_en       date   -- 3 años por defecto
```

#### `bloques`
```sql
id              uuid PK
cartera_id      uuid FK
plan_id         uuid FK
nombre          text                    -- libre (ej. "Cash Cows EU")
categoria_base  text                    -- 'defensivo'|'income'|'growth'|'aggressive'|'colchon'|'sin_clasificar'
peso_objetivo   numeric(5,4)            -- 0.4000 = 40%
tolerancia      numeric(5,4)            -- 0.0500 = ±5%
yield_esperado_min  numeric(5,4)
volatilidad_esperada text              -- 'baja'|'media'|'alta'
criterio        text                   -- ej. "Yield neto + CAGR4 > 12%"
orden           int                     -- para UI drag&drop
UNIQUE (cartera_id, nombre)
CHECK (peso_objetivo BETWEEN 0 AND 1)
```

Tope 8 bloques por cartera (enforced por trigger).

#### `posiciones`
```sql
id              uuid PK
cartera_id      uuid FK
isin            text                    -- canónico, casing
ticker          text                    -- info display
nombre          text                    -- empresa
divisa_local    text                    -- 'EUR'|'USD'|'GBX'|'GBP'|'DKK'|'HKD'|...
bloque_id       uuid FK NULLABLE        -- NULL = saco "Sin clasificar"
clasif_origen   text                    -- 'manual' | 'ia' | 'auto'
ia_confianza    numeric(3,2)            -- 0.00 a 1.00, si fue clasif IA
UNIQUE (cartera_id, isin)
```

#### `transacciones` — entidad central
```sql
id                uuid PK
cartera_id        uuid FK
broker_id         uuid FK
posicion_id       uuid FK
fecha             date
tipo              text     -- 'BUY'|'SELL'|'DIVIDEND'|'INTEREST'|'STAKING_REWARD'|
                            --  'CORPORATE_SPLIT'|'CORPORATE_ISIN_CHANGE'|...
cantidad          numeric(20,10)
precio_local      numeric(20,10)         -- precio en divisa local
divisa_local      text                   -- 'EUR'|'USD'|...
importe_local     numeric(18,4)          -- cantidad * precio (signed)
fx_rate           numeric(20,10)         -- divisa_local→EUR del día (BCE)
importe_eur       numeric(18,4)          -- importe convertido a EUR
gastos_eur        numeric(18,4)          -- comisión TR/broker
tasas_externas_eur numeric(18,4)         -- Tasa Tobin, FTT, Stamp Duty, etc.
retencion_eur     numeric(18,4)          -- retención origen o IRPF nacional
retencion_pais    text                   -- 'ES' (nacional) o ISO país origen
external_id       text                   -- transaction_id del broker, para
                                          -- deduplicar al reimportar extracto
notas             text
created_at        timestamptz
UNIQUE (broker_id, external_id) WHERE external_id IS NOT NULL
INDEX (cartera_id, fecha)
INDEX (posicion_id, fecha)
```

Toda celda monetaria lleva su divisa explícita.
**No hay mezclas implícitas**. La conversión a EUR siempre va junto al `fx_rate`
que se usó. Reproducible y auditable.

#### `lots` (inventario FIFO)
```sql
id              uuid PK
posicion_id     uuid FK
transaccion_origen_id  uuid FK   -- la transacción A que creó este lote
fecha_compra    date
cantidad_inicial    numeric(20,10)
cantidad_restante   numeric(20,10)
coste_unit_eur      numeric(20,10)   -- coste por acción (incl. gastos/tasas)
coste_total_eur     numeric(18,4)
gastos_eur          numeric(18,4)
es_scrip            boolean
ejercicio_opcion    boolean
strike_eur          numeric(18,4) NULLABLE
prima_eur           numeric(18,4) NULLABLE
tipo_opcion         text           -- 'CALL'|'PUT'|NULL
broker_id           uuid FK         -- origen del lote (informativo, FIFO casa por ISIN)
INDEX (posicion_id, fecha_compra)
```

#### `opciones` (contratos detallados)
```sql
id              uuid PK
cartera_id      uuid FK
broker_id       uuid FK
subyacente_isin text                    -- ISIN de la acción subyacente
tipo            text                    -- 'CALL'|'PUT'
strike_local    numeric(18,4)
strike_divisa   text
vencimiento     date
fecha_apertura  date
estado          text                    -- 'ABIERTA'|'CERRADA'|'EXPIRADA'|'ASIGNADA'|'EJERCIDA'
direccion       text                    -- 'SHORT'|'LONG'
prima_neta_eur  numeric(18,4)           -- cobrada (+) o pagada (-)
gastos_eur      numeric(18,4)
ejercicio_diferido boolean              -- DGT V2172-21: open al 31/12
ano_imputacion  int                     -- año en que tributa la prima
```

#### `corporate_events`
```sql
id              uuid PK
cartera_id      uuid FK
fecha           date
tipo            text                    -- 'SPLIT'|'CONTRASPLIT'|'NAME_CHANGE'|
                                          -- 'ISIN_CHANGE'|'SCRIP_PURO'|'SCRIP_MIXTO'|
                                          -- 'RIGHTS_ISSUE'|'SPINOFF'|'MERGER'|'OPA'|
                                          -- 'CAMBIO_MERCADO'|'COMPLEX'
isin_antiguo    text
isin_nuevo      text
ratio_origen    numeric(20,10)
ratio_destino   numeric(20,10)
descripcion     text
requiere_revision boolean              -- true para COMPLEX
```

#### `bolsas_fiscales` (4 ejercicios anteriores)
```sql
id              uuid PK
cartera_id      uuid FK
ejercicio       int                     -- 2020, 2021, 2022, 2023, 2024 ...
tipo            text                    -- 'PATRIMONIAL'|'RCM'
saldo_eur       numeric(18,4)           -- positivo = pérdida disponible
compensado_eur  numeric(18,4)           -- usado contra ganancias del ejercicio
disponible_eur  numeric(18,4) GENERATED -- saldo - compensado
```

#### `snapshots` (auditoría)
```sql
id              uuid PK
cartera_id      uuid FK
fecha           timestamptz
contenido_jsonb jsonb                   -- estado completo cartera en el momento
evento          text                    -- 'manual'|'tras_compra'|'cierre_anyo'|...
```

### Decisiones específicas relevantes

1. **Sólo Decimal, nunca float**. Todas las columnas monetarias `numeric(N,M)`.
   Mapeado a `decimal.Decimal` en Python. Cero `float`.
2. **Divisa canónica explícita por celda**. No hay "implícitamente EUR".
3. **transaction_id como deduplication key**. Cuando un usuario reimporta
   un CSV de TR/DeGiro, las filas con mismo `external_id` se ignoran. Si una
   transacción manual luego aparece en el extracto, se reconcilia (no se
   duplica).
4. **FIFO casado en `lots` por `posicion_id`**, que es único por ISIN+cartera.
   Cross-broker funciona automáticamente.
5. **Row-Level Security en Supabase**: cada query lleva `WHERE user_id = current_user`
   por defecto. Configurado con políticas RLS en cada tabla.
6. **Plan firmado en `planes` con `contenido_md`** (texto markdown firmado por
   el usuario, en primera persona) **y `reglas_json`** (las reglas parseables
   por el agente: anti-pánico, anti-euforia, etc.).
7. **Snapshots inmutables** para auditoría: tras cada operación significativa,
   serializar estado y guardar.

## Alternativas consideradas

### A. NoSQL (MongoDB, DynamoDB)

- **Pros**: schemas flexibles, fácil JSON.
- **Contras**: las consultas fiscales necesitan JOIN, agregaciones, transacciones
  ACID. Postgres lo hace bien. NoSQL nos obligaría a desnormalizar y mantener
  invariantes a mano.
- **Veredicto**: Postgres.

### B. Event Sourcing puro

- **Pros**: auditabilidad perfecta, viajar en el tiempo gratis.
- **Contras**: complejidad ×3, curva de aprendizaje, mucha más infra.
- **Veredicto**: por ahora **CRUD con snapshots periódicos** cubre la
  auditabilidad sin la complejidad de event sourcing puro. Evaluable si en
  el futuro queremos "ver mi cartera tal y como estaba el 15 de marzo".

### C. Schema single-tenant (un user, una BD)

- **Pros**: aislamiento total, simplicidad mental.
- **Contras**: complicado para SaaS multi-tenant. Mejor schema multi-tenant
  con `user_id` en cada tabla + RLS.

## Consecuencias

### Positivas

- Modelo escalable, multi-tenant nativo.
- Reproducibilidad fiscal total: dada una `transaccion`, podemos rastrear
  qué lots consumió, qué corporate_events afectaron, qué tipo de cambio
  usamos.
- Lección Excel GBX no repetible: divisa explícita siempre.

### Negativas

- 12 entidades es bastante. Curva de aprendizaje para nuevos devs.
- Migraciones tempranas serán cambios fuertes hasta estabilizar el esquema.
- Performance de FIFO con muchos lots: hay que crear índices y benchmark.

## Próximos pasos

1. Implementar migraciones Alembic con este esquema en `cima/backend/migrations/`.
2. Generar modelos SQLAlchemy 2 a partir del esquema.
3. Crear primer test integración: cargar CSV de TR (anonimizado), persistir,
   leer cartera, verificar FIFO.

## Referencias

- ROADMAP H1.1
- Lecciones del Excel actual: GBX, plusvalías sospechosas, bug C22 GBP/EUR
- `docs/aprendizajes-del-personal.md`
- Cuádrate `motor_fiscal.py:Lot` y `FIFOMatch` (referencia para `lots`)

---

**Autor**: Cima Team
**Fecha**: 2026-05-18
