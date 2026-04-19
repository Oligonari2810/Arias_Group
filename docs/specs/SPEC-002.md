# SPEC-002 — Migración SQLite → PostgreSQL + capa de abstracción de datos

**Fase:** 0 (Fundamentos)
**Prioridad:** P0 — habilita multi-usuario real, multi-país y todo el roadmap posterior
**Autor (CTO):** Claude
**Ejecutor (Lead Dev):** Qwen-Coder
**Product Owner:** Oliver
**Estado:** Lista para ejecutar (SPEC-001 mergeada 2026-04-19)
**Branch objetivo:** `feature/spec-002-postgres-migration`

---

## Changelog

| Fecha | Versión | Cambio |
|---|---|---|
| 2026-04-19 | v1.0 | Versión inicial |
| 2026-04-19 | v1.1 | **§5.20:** lista de stages reales del pipeline corregida (extraída de `app.py:29-56` — Qwen las hardcoded en el MVP inicial). PO reconoce que meter los 26 en `projects.stage` es un error de modelado porque mezcla ciclos de vida de 5 entidades distintas (cliente, cotización, pedido, envío, postventa). **Decisión:** migración verbatim en SPEC-002 (preserva comportamiento); la descomposición va en SPEC-003 (Domain Cleanup). No mezclar migración de infra con remodelado de dominio en el mismo PR. |
| 2026-04-19 | v1.2 | **§15 nuevo:** SPEC-002 se parte en 3 PRs (002a infra skeleton / 002b schema+migrador / 002c refactor app.py) para que cada uno sea reviewable y rollbackable. Decidido con el PO antes de empezar implementación, dado el alcance real (4-6h). Ver §15 para scope de cada PR. |

---

## 1. Contexto y motivación

### Por qué ahora
SQLite (único archivo, sin concurrencia real de escritura) es el techo de cristal de todo lo que queremos construir:

- **WMS + inventario real-time** (SPEC fase 2): múltiples operarios registrando picking simultáneo → SQLite serializa escrituras → bloqueos.
- **App móvil comerciales** (SPEC fase 2): decenas de comerciales sincronizando pedidos → mismo problema.
- **Multi-país / subsidiarias** (SPEC fase 4): volúmenes y consultas analíticas que SQLite no optimiza.
- **BI avanzado + dashboards** (SPEC fase 3): ventanas, CTEs, índices parciales, particionado → Postgres lo hace; SQLite no.
- **Audit log creciente** (legal/compliance): necesita particionado por fecha — imposible en SQLite.

### Por qué no Oracle/MSSQL/MySQL
PostgreSQL es el estándar de facto para ERPs serios (NetSuite internamente corre sobre Oracle, pero open-source hoy = Postgres 16). Mejor tipo `NUMERIC` para dinero, `JSONB` para datos semiestructurados, tipos `timestamptz`, FK con `ON DELETE`, extensiones (pg_trgm, pg_partman, TimescaleDB si más adelante queremos series temporales en logística).

### Por qué SQLAlchemy Core (y no ORM completo)
- Queries complejas de reporting (márgenes, consolidación multi-proyecto) se escriben mejor como SQL explícito. El ORM mete capas y problemas N+1.
- Core nos da pooling, dialectos, parámetros seguros, transacciones limpias, pero mantenemos el SQL visible.
- Cuando algún flujo simple gane por ORM (CRUD puro de catálogos), lo meteremos puntualmente — no como arquitectura base.

### Por qué Alembic
Migraciones versionadas, reversibles, diffables en git. Matamos las `ALTER TABLE` condicionales que hoy viven en `init_db()` (líneas 377–395 de `app.py`).

---

## 2. Objetivos

1. PostgreSQL 16 como **único motor de datos productivo** y en staging.
2. Capa de acceso a datos centralizada en `db/` con SQLAlchemy Core + connection pooling.
3. Alembic gobernando **todo** cambio de esquema a partir de ahora.
4. Migrador idempotente SQLite → Postgres que preserva datos de Render actual.
5. Tests de SPEC-001 ejecutándose contra Postgres real (docker-compose en dev, service container en CI).
6. Cero regresiones funcionales: todo endpoint existente sigue devolviendo lo mismo byte-a-byte.
7. Plan de rollback documentado y probado.

---

## 3. Alcance y no-alcance

### En alcance
- Nuevo esquema Postgres equivalente al actual (18 tablas — ver §5) con tipos correctos
- Capa `db/` con SQLAlchemy engine, session factory, helpers
- Configuración de Alembic + migración inicial (`0001_initial_schema`)
- Script de migración de datos `scripts/migrate_sqlite_to_postgres.py`
- Reemplazo de todas las llamadas `get_db()` / `sqlite3` en `app.py` por la nueva capa
- Ajuste de `tests/conftest.py` para usar Postgres real
- GitHub Actions con Postgres service
- `docker-compose.yml` para dev local
- Documentación de deploy en Render con managed Postgres

### Fuera de alcance (NO hacer en este PR)
- ❌ Refactor modular de `app.py` en paquetes (SPEC-004)
- ❌ Nuevas features (inventario multi-almacén, WMS, etc.)
- ❌ Cambiar comportamiento de endpoints
- ❌ Añadir ORM completo sobre tablas
- ❌ TimescaleDB, replicación, sharding (si algún día hace falta, nueva SPEC)

---

## 4. Decisiones arquitectónicas (locked)

| Decisión | Elección | Justificación |
|---|---|---|
| Motor DB | PostgreSQL 16 | Estándar, estable, rico en tipos |
| Acceso desde Python | SQLAlchemy Core 2.x | Pooling + abstracción sin ORM pesado |
| Migraciones | Alembic 1.13+ | Versionado, reversible, diffable |
| Driver | `psycopg[binary]>=3.1` (psycopg3) | Mejor que psycopg2, soporte async futuro |
| Pooling | `QueuePool` default (size=5, max_overflow=10) | Suficiente para <100 usuarios concurrentes |
| Config | Variable `DATABASE_URL` (12-factor) | Estándar Render/Heroku |
| Fechas | `TIMESTAMPTZ` siempre UTC | Adiós strings ISO |
| Dinero | `NUMERIC(14, 4)` | Precisión financiera, hasta 10 dígitos enteros |
| JSON | `JSONB` (no JSON texto) | Indexable, operadores nativos |
| IDs | `BIGSERIAL` (compat con INTEGER actual) | Crecimiento a largo plazo sin dolor |
| Tests | Postgres real vía docker / GH services | SQLite mentía sobre el comportamiento real |
| Transacciones | Explícitas con `with engine.begin()` | Nada de autocommit |

---

## 5. Esquema PostgreSQL — cambios tabla por tabla

### Convenciones aplicadas globalmente
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` (antes `TEXT NOT NULL`)
- `updated_at TIMESTAMPTZ` con trigger `moddatetime` (extension `moddatetime`)
- Todos los precios/cantidades monetarias → `NUMERIC(14, 4)` (antes `REAL`)
- Porcentajes → `NUMERIC(6, 4)` (ej. `0.0800` = 8%)
- Cantidades físicas (kg, m², palés) → `NUMERIC(12, 3)`
- IDs → `BIGSERIAL PRIMARY KEY`
- Textos libres → `TEXT`
- Códigos cortos (SKU, país, RNC) → `VARCHAR(N)` con N acotado
- FKs con `ON DELETE RESTRICT` por defecto; `CASCADE` solo donde semánticamente correcto
- Índices compuestos donde las queries lo justifiquen

### Tablas (nombres → cambios clave)

#### 5.1 `clients`
- `country VARCHAR(64) DEFAULT 'República Dominicana'`
- `score SMALLINT CHECK (score BETWEEN 0 AND 100)` (antes INTEGER sin check)
- `rnc VARCHAR(32)` con índice parcial `WHERE rnc IS NOT NULL`

#### 5.2 `products`
- `unit_price_eur NUMERIC(14, 4) NOT NULL`
- `kg_per_unit NUMERIC(12, 3)`, `units_per_pallet NUMERIC(10, 2)`, `sqm_per_pallet NUMERIC(10, 3)`
- `discount_pct NUMERIC(5, 2) DEFAULT 50.00`
- `category VARCHAR(64) NOT NULL`
- Índice: `(category, subfamily)`
- Índice funcional: `LOWER(sku)` para búsqueda case-insensitive

#### 5.3 `systems`
- `default_waste_pct NUMERIC(5, 4) DEFAULT 0.0800`

#### 5.4 `system_components`
- FK con `ON DELETE CASCADE` hacia systems (al borrar sistema desaparecen componentes)
- `consumption_per_sqm NUMERIC(10, 4) NOT NULL`
- Mantener unique compuesto

#### 5.5 `projects`
- `stage` → **enum** `project_stage_enum` (26 valores, listado en §5.20)
- `go_no_go` → **enum** `go_no_go_enum` ('PENDING', 'GO', 'NO_GO')
- `incoterm` → **enum** `incoterm_enum` ('EXW', 'FOB', 'CIF', 'DAP', ...)
- `fx_rate NUMERIC(10, 6)`, `target_margin_pct NUMERIC(5, 4)`, `customs_pct NUMERIC(5, 4)`
- `area_sqm NUMERIC(12, 3) DEFAULT 0`

#### 5.6 `project_quotes`
- `result_json JSONB NOT NULL` (antes TEXT)
- FKs como en origen
- Índice GIN en `result_json` si se va a consultar por claves internas

#### 5.7 `stage_events`
- `from_stage`, `to_stage` referencian al enum `project_stage_enum`
- Índice `(project_id, created_at DESC)`

#### 5.8 `shipping_routes`
- Precios contenedor: `NUMERIC(12, 2)`
- `insurance_pct NUMERIC(5, 4)`
- `valid_from`, `valid_until` → `DATE` (no TEXT, no timestamptz)
- Índice `(origin_port, destination_port, valid_from)`

#### 5.9 `customs_rates`
- `dai_pct, itbis_pct, other_pct NUMERIC(5, 4)`
- Índice `(country, hs_code)`
- UNIQUE (country, hs_code) — ahora mismo NO existe, debería

#### 5.10 `fx_rates`
- `rate NUMERIC(14, 8)` (precisión alta para FX)
- `updated_at TIMESTAMPTZ NOT NULL`
- Índice `(base_currency, target_currency, updated_at DESC)` para lookups "último tipo"
- Considerar partición por fecha si crecen mucho (no en este SPEC)

#### 5.11 `users`
- `role` → **enum** `user_role_enum` ('admin', 'viewer'; ampliable en futuras specs: 'sales', 'warehouse', 'accountant')
- `password_hash VARCHAR(255)` (bcrypt output fits in 60 chars)
- `email VARCHAR(255) UNIQUE`
- `username VARCHAR(64) UNIQUE NOT NULL`
- Añadir `is_active BOOLEAN NOT NULL DEFAULT TRUE` (preparación soft-delete)
- Añadir `last_login_at TIMESTAMPTZ`

#### 5.12 `pending_offers`
- `status` → **enum** `offer_status_enum` ('pending', 'sent', 'accepted', 'rejected', 'expired')
- `incoterm` → enum `incoterm_enum`
- `lines_json JSONB NOT NULL`
- `raw_hash VARCHAR(64)` (SHA-256 hex) con índice parcial `WHERE raw_hash IS NOT NULL`
- Totales monetarios → `NUMERIC(14, 4)`
- Índice `(status, created_at DESC)`
- Índice `(client_name, project_name)` — hoy búsquedas van por estos campos

#### 5.13 `order_lines`
- `qty_input, qty_logistic NUMERIC(12, 3)`
- `price_unit_eur, cost_exw_eur NUMERIC(14, 4)`
- `weight_total_kg, m2_total, pallets_theoretical NUMERIC(12, 3)`
- `pallets_logistic INTEGER` (se queda entero)
- `alerts_text TEXT` (se queda)
- FK con `ON DELETE CASCADE` ya en origen → mantener

#### 5.14 `audit_log`
- `detail JSONB` (antes TEXT; mucho es JSON sin indexar)
- `action VARCHAR(64) NOT NULL`
- `username VARCHAR(64)`
- Índice `(created_at DESC)` para stream de auditoría
- Índice `(offer_id, created_at DESC)`
- Preparado para partición por mes (documentarlo; NO implementar aún)

#### 5.15 `doc_sequences`
- Reemplazar por **secuencias Postgres nativas** (`CREATE SEQUENCE`) por prefijo. Más rápido, transaccional, sin locks.
- Mantener la tabla como metadata (prefix → nombre de secuencia) para cambiar nombres sin redeployar código.

#### 5.16 `pickup_pricing`
- `price_eur_unit NUMERIC(14, 4) NOT NULL`
- Unique compuesto se mantiene

#### 5.17 `family_defaults`
- `discount_pct NUMERIC(5, 2)`
- `display_order SMALLINT DEFAULT 99`

#### 5.18 `price_history`
- `old_value, new_value NUMERIC(14, 4)`
- `changed_at TIMESTAMPTZ NOT NULL`
- Índice `(product_id, changed_at DESC)`

#### 5.19 `app_settings`
- `value JSONB NOT NULL` (hoy es TEXT — muchos valores son numéricos/JSON)
- Aportar wrapper `get_setting(key, default)` que cast según tipo

#### 5.20 Enum `project_stage_enum` — 26 stages reales (Fassa-Arias)

Confirmado con el PO (2026-04-19): se migran **verbatim** los 26 stages que
hoy existen en `app.py:29-56` (constante `STAGES`). La lista mezcla ciclos de
vida de 5 entidades distintas (cliente, cotización, pedido, envío, postventa);
**ese fallo de modelado no se arregla en esta SPEC.** La descomposición en
entidades correctas queda planificada en **SPEC-003 (Domain Cleanup)** para
evitar mezclar migración de infraestructura con remodelado de dominio.

Lista oficial que debe ir en la migración Alembic `0001_initial_schema.py`:

```
'CLIENTE', 'OPORTUNIDAD', 'FILTRO GO / NO-GO', 'PRE-CÁLCULO RÁPIDO',
'CÁLCULO DETALLADO', 'OFERTA V1/V2', 'VALIDACIÓN TÉCNICA',
'VALIDACIÓN CLIENTE', 'CIERRE', 'CONTRATO + CONDICIONES',
'PREPAGO VALIDADO', 'ORDEN BLOQUEADA', 'CHECK INTERNO',
'LOGÍSTICA VALIDADA', 'BOOKING NAVIERA', 'PEDIDO A FASSA',
'CONFIRMACIÓN FÁBRICA', 'READY DATE', 'EXPEDICIÓN (BL)',
'TRACKING + CONTROL ETA', 'ADUANA',
'LIQUIDACIÓN ADUANERA + COSTES FINALES',
'INSPECCIÓN / CONTROL DAÑOS', 'ENTREGA', 'POSTVENTA',
'RECOMPRA / REFERIDOS / ESCALA'
```

**Importante:** Qwen no hardcodea esto sin confirmación. Si `app.py` tiene otra lista, prevalece esa.

---

## 6. Estructura entregable

```
Arias_Group/
├── db/                              # NUEVO
│   ├── __init__.py                  # expone engine, SessionLocal, get_db
│   ├── engine.py                    # create_engine, pool config
│   ├── session.py                   # session_factory, context managers
│   └── models.py                    # tablas SQLAlchemy Core (MetaData + Table)
├── alembic/                         # NUEVO
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial_schema.py   # migración inicial completa
├── alembic.ini                      # NUEVO
├── scripts/
│   └── migrate_sqlite_to_postgres.py  # NUEVO — migrador de datos
├── docker-compose.yml               # NUEVO — Postgres local para dev
├── .env.example                     # NUEVO — plantilla de env vars
├── app.py                           # MODIFICADO — get_db() usa db/ nueva
├── tests/
│   └── conftest.py                  # MODIFICADO — usa Postgres real
├── requirements.txt                 # +sqlalchemy, +alembic, +psycopg[binary]
├── .github/workflows/tests.yml      # MODIFICADO — Postgres service
└── docs/
    └── deployment/
        └── postgres-migration-runbook.md  # NUEVO — runbook de cutover
```

---

## 7. Migrador de datos (`scripts/migrate_sqlite_to_postgres.py`)

### Requisitos funcionales
- Lee `SQLITE_PATH` (env var, default `app.db`) y `DATABASE_URL` (Postgres destino)
- Ejecuta en este orden (respetando FKs): `clients, users, systems, products, system_components, projects, project_quotes, stage_events, shipping_routes, customs_rates, fx_rates, pending_offers, order_lines, audit_log, doc_sequences, pickup_pricing, family_defaults, price_history, app_settings`
- Idempotente: si se re-ejecuta, hace `ON CONFLICT DO NOTHING` por PK (o por claves naturales donde aplique)
- Preserva IDs originales — se llaman `setval(seq, max_id)` al final para que `BIGSERIAL` continúe correctamente
- Convierte:
  - `created_at TEXT` ISO → `TIMESTAMPTZ` (parse con `datetime.fromisoformat`, asume UTC si no hay tz)
  - `REAL` → `NUMERIC` (Decimal)
  - `lines_json`, `result_json` TEXT → `JSONB` (json.loads)
- Reporta al final: filas leídas / filas insertadas / filas saltadas por tabla

### Requisitos no funcionales
- Transaccional por tabla (no queremos estado inconsistente si falla a la mitad)
- Logs con tiempos por tabla
- Dry-run flag: `--dry-run` solo imprime lo que haría
- Flag `--truncate` (solo dev): vacía destino antes de insertar

---

## 8. Tests (obligatorio)

### Cambios en `tests/conftest.py`
- Fixture `postgres_url` que:
  - En local: usa `TEST_DATABASE_URL` env var (dev levanta `docker-compose up -d postgres-test`)
  - En CI: usa el service de GitHub Actions
- Fixture `engine` que crea engine + ejecuta `alembic upgrade head` al principio de la sesión
- Fixture `db` que abre transacción por test y hace `ROLLBACK` al final (tests aislados)
- Reemplazar `':memory:'` SQLite por Postgres real

### Verificación
- Todos los tests de SPEC-001 siguen pasando contra Postgres
- Coverage no baja (sigue ≥85%)
- Nuevos tests dedicados:
  - `tests/integration/test_migrator.py`: genera un SQLite con datos seed, corre migrador, valida que todas las filas están en Postgres con tipos correctos
  - `tests/integration/test_schema.py`: verifica que enums existen, FKs existen, índices existen (introspection sobre `information_schema`)

---

## 9. Configuración y entornos

### `.env.example`
```bash
# Postgres principal
DATABASE_URL=postgresql+psycopg://arias:arias@localhost:5432/arias_dev
TEST_DATABASE_URL=postgresql+psycopg://arias:arias@localhost:5433/arias_test

# Legacy (solo para migrador)
SQLITE_PATH=app.db

# Flask
SECRET_KEY=change-me
BOT_TOKEN=change-me
FLASK_ENV=development
```

### `docker-compose.yml` (dev local)
Dos servicios: `postgres` (puerto 5432, DB `arias_dev`) y `postgres-test` (puerto 5433, DB `arias_test`). Volúmenes persistentes para dev, `tmpfs` para test. Healthchecks.

### CI — `.github/workflows/tests.yml`
```yaml
jobs:
  test:
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: arias
          POSTGRES_PASSWORD: arias
          POSTGRES_DB: arias_test
        ports: ['5432:5432']
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    env:
      TEST_DATABASE_URL: postgresql+psycopg://arias:arias@localhost:5432/arias_test
    steps:
      - ...
      - run: alembic upgrade head
      - run: pytest
```

---

## 10. Plan de rollout (cutover)

### Fase A — Desarrollo (branch `feature/spec-002-postgres-migration`)
1. Implementar todo en la branch
2. Qwen abre PR → CTO review → aprobación
3. **NO mergear aún** — solo mergea Oliver cuando se cumplen fases B y C

### Fase B — Staging / dry run (antes de mergear)
1. Oliver provisiona **Postgres en Render** (instancia staging pequeña, ~7$/mes)
2. Oliver obtiene la `DATABASE_URL` de staging
3. Qwen crea en Render una **preview environment** apuntando a esa DB
4. Oliver exporta el `app.db` actual de producción (snapshot Render disk)
5. Qwen corre el migrador sobre staging: `python scripts/migrate_sqlite_to_postgres.py`
6. **Testing manual end-to-end** en staging (login, cotización, PDF, CRM). Checklist en runbook.
7. Si todo pasa → fase C

### Fase C — Producción
1. **Ventana de mantenimiento** coordinada con Oliver (fuera de horario comercial Caribe, p.ej. domingo 02:00 CET)
2. Oliver provisiona Postgres prod en Render (plan Standard)
3. Qwen ejecuta migrador con la `DATABASE_URL` de prod y snapshot del `app.db` productivo
4. Cambio de env var en Render: `DATABASE_URL` apuntando a Postgres
5. Redeploy
6. Smoke tests (checklist de runbook: login, listar clientes, crear cotización, generar PDF)
7. Monitorizar 24h — logs, errores, latencias

### Rollback plan
- Si fallan smoke tests: env var vuelve a apuntar a SQLite + redeploy (SQLite queda intacto porque migrador no lo toca)
- Si se detecta corrupción en Postgres durante 24h iniciales: volver a SQLite; diff de cambios manuales en ventana debe reaplicarse
- Ventana de rollback: **72h**. Pasado eso, consideramos cutover definitivo y matamos SQLite.

---

## 11. Criterios de aceptación (checklist de merge)

- [ ] `alembic upgrade head` corre limpio desde 0 y crea todas las tablas del §5
- [ ] `alembic downgrade base` revierte todo limpio
- [ ] `db/` expone engine, session factory, `get_db()` compatible
- [ ] `app.py` ya no importa `sqlite3` (excepto el migrador)
- [ ] Todos los tests de SPEC-001 verdes contra Postgres
- [ ] Coverage ≥85% global, ≥90% en `calculate_quote`
- [ ] Nuevos tests de migrador y schema introspection pasan
- [ ] GitHub Actions verde con service Postgres
- [ ] `docker-compose up -d` levanta Postgres dev+test en local
- [ ] Migrador ejecutado contra snapshot real de producción sin errores (Oliver lo valida)
- [ ] Runbook de deploy documentado y revisado
- [ ] Smoke tests en staging pasan (checklist)
- [ ] PR descripción incluye: salida de alembic, tabla filas-origen vs filas-destino, output de pytest, screenshots de la app funcionando en staging

---

## 12. Riesgos y mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Pérdida de datos en cutover | Baja | Crítico | Snapshot SQLite + dry-run en staging previo |
| Bugs por tipos (REAL → NUMERIC) | Media | Alto | Tests de SPEC-001 verdes son la red; revisar asserts sobre floats vs Decimal |
| Timestamps mal parseados | Media | Medio | Tests explícitos en migrador para cada formato de fecha visto |
| Render Postgres más caro | Alta | Bajo | Oliver valida coste (~7$/mes staging, ~20$/mes prod) |
| Tiempo de downtime en cutover | Media | Medio | Ventana planificada, rollback en <5min con env var swap |
| Secuencias mal sincronizadas → colisiones de ID | Media | Alto | Final del migrador llama `setval` por tabla; test dedicado |

---

## 13. Dependencias

- **Bloqueado por:** SPEC-001 mergeada (necesitamos los tests para validar que nada se rompe)
- **Bloquea:** SPEC-003 (módulos), SPEC-004+ (cualquier nueva feature)

---

## 15. Plan de ejecución en 3 PRs (v1.2)

Para mantener cada PR reviewable y con rollback fácil, SPEC-002 se ejecuta en 3 fases independientes. Cada una deja el sistema funcionando — el app sigue corriendo sobre SQLite hasta que 002c se mergee.

### SPEC-002a — Infra + skeleton *(este PR)*
**Branch:** `feature/spec-002a-infra-skeleton`
**Qué hace:** añade dependencias y esqueletos; **no toca** `app.py` ni datos.
- `requirements.txt`: `sqlalchemy>=2.0`, `alembic>=1.13`, `psycopg[binary]>=3.1`
- `docker-compose.yml`: servicios `postgres` (5432, volumen persistente, dev) y `postgres-test` (5433, tmpfs, test)
- `.env.example` con `DATABASE_URL` y `TEST_DATABASE_URL`
- `db/` módulo: `engine.py`, `session.py`, `__init__.py` (factory, connection context, sin modelos)
- `alembic.ini` y `alembic/env.py` configurados contra `DATABASE_URL`; directorio `alembic/versions/` vacío (o con una migración placeholder vacía que sirva de base)
- README: sección "Local Postgres dev environment"

**Criterio de aceptación:**
- `docker compose up -d postgres` responde a `psql`
- `alembic current` y `alembic history` funcionan contra la DB local
- Tests de SPEC-001 siguen verdes (no cambia nada sobre SQLite)

### SPEC-002b — Schema + data migrator
**Branch:** `feature/spec-002b-schema-migrator`
**Depende de:** 002a mergeada
**Qué hace:** crea el esquema Postgres final y el migrador de datos desde SQLite.
- `alembic/versions/0001_initial_schema.py` con las 18 tablas del §5 (tipos NUMERIC, TIMESTAMPTZ, JSONB, enums incluido `project_stage_enum` con los 26 stages verbatim del §5.20)
- `scripts/migrate_sqlite_to_postgres.py` idempotente (§7 de la spec)
- Nuevos tests: `tests/integration/test_schema.py` (introspection) y `tests/integration/test_migrator.py` (end-to-end contra fixture SQLite seed)
- `app.py` sigue usando SQLite — nada tocado

**Criterio de aceptación:**
- `alembic upgrade head` crea todo el esquema limpio
- `alembic downgrade base` revierte limpio
- `python scripts/migrate_sqlite_to_postgres.py` copia un snapshot SQLite a Postgres sin errores
- Tests nuevos verdes

### SPEC-002c — Refactor `app.py` + cutover enable
**Branch:** `feature/spec-002c-app-refactor`
**Depende de:** 002b mergeada
**Qué hace:** conecta `app.py` al nuevo backend vía un adapter que preserva la API de `sqlite3.Connection` (así no reescribimos las ~100 queries).
- `db/adapter.py`: wrapper sobre psycopg3 que implementa `.execute()`, `.row_factory` equivalente a `sqlite3.Row`, `.commit()`, etc.
- `app.py`: modificar **solo** `get_db()` y `close_db` para usar el adapter cuando `DATABASE_URL` esté definido; fallback a SQLite cuando no (backward compat durante la ventana de cutover)
- `tests/conftest.py`: migrar a Postgres de test (docker-compose local + service en CI). SPEC-001 se re-ejecuta contra Postgres — es el proof of safety.
- `.github/workflows/tests.yml`: service Postgres
- `docs/deployment/postgres-migration-runbook.md`: pasos exactos de cutover en Render

**Criterio de aceptación:**
- Todos los tests de SPEC-001 verdes sobre Postgres (es la red de seguridad)
- Coverage gate sigue ≥85% / ≥90%
- Runbook probado en staging (Postgres en Render) — validación manual del PO

---

## 14. Notas del CTO

1. **No es negociable: tests verdes antes de mergear esta SPEC.** Si los tests de SPEC-001 fallan en Postgres, es una señal de un bug que SQLite ocultaba (muy típico con REAL vs NUMERIC).
2. **Decimal everywhere.** Qwen: nada de `float` para dinero, ni en código nuevo ni en asserts de tests. Siempre `Decimal`.
3. **Secuencias nativas Postgres > tabla `doc_sequences`.** Cambio técnico que reduce latencia de generación de número de oferta.
4. **Los 26 stages del pipeline son negocio crítico.** Si la lista que propongo en §5.20 no coincide con la real de Arias, Qwen pregunta antes de inventar.
5. **Render managed Postgres, no self-hosted.** No queremos operar DB en esta fase. Cuando superemos lo que Render ofrece (muchos años), migraremos a RDS/Cloud SQL.
6. **Tras mergear este PR, el siguiente paso natural es SPEC-003 (modularización de `app.py`)** — con DB abstraída y tests verdes, el refactor es seguro.
