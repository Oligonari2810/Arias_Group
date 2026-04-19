# Fassa–Arias Group · Caribbean Ops · v2.1

Sistema operativo comercial, técnico y logístico para la distribución de Fassa Bortolo / Gypsotech en el Caribe.

## Stack
- Python 3.10+
- Flask
- SQLite (embebido)
- ReportLab (PDF)
- openpyxl (lectura Excel)

## Fuente maestra de datos

El catálogo de productos **se lee directamente** desde la hoja `PRODUCT` del archivo Excel:

```
Arias_Group_Master-System_v1.xlsx  ← fuente maestra (186 SKUs)
```

Cada vez que actualices precios, productos o datos logísticos en el Excel, **vuelve a ejecutar** `load_catalog.py`.

## Arranque rápido

```bash
cd Mvp_Arias_Fassa
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py                      # Inicializa DB + seed + arranca en :5000
```

Abrir → http://127.0.0.1:5000/

## Sincronizar catálogo (cada vez que cambies el Excel)

```bash
python load_catalog.py
```

Este script lee las 186 filas de la hoja `PRODUCT` y las carga en la DB SQLite con:
- SKU, nombre, categoría, unidad, precio
- Peso neto por unidad
- Uds/palet y m²/palet
- HS Code + Norma en las notas

## Módulos

| Módulo | Ruta | Descripción |
|---|---|---|
| Dashboard principal | / | KPIs + proyectos recientes |
| Clientes | /clients | CRM básico + scoring |
| Proyectos / Pipeline | /projects | 26 etapas del proceso operacional |
| Detalle proyecto | /projects/<id> | Oferta, historial de etapas, PDF |
| Calculadora | /calculator | m² → SKU → palés → contenedor → margen |
| Catálogo | /products | 186 SKUs con precio EXW Tarancón |
| **Tablas Maestras** | /masters | Rutas logísticas, aranceles aduana, tipos de cambio |
| **Dashboard Financiero** | /dashboard/financial | Pipeline, márgenes, alertas |

## Flujo de datos

```
EXCEL PRODUCT (186 SKUs)
        │
        ▼  load_catalog.py
   SQLite DB (fassa_ops.db)
        │
        ├─► Web App → Catálogo / Cotización / PDF
        ├─► Calculadora → m² → SKUs → palés → contenedor
        └─► Pipeline 26 etapas → CRM → Oferta → Entrega
```

## Exportación PDF de oferta

Desde el detalle de cualquier proyecto → sección Ofertas → botón **Descargar PDF**.

Genera documento con cabecera Fassa–Arias Group, tabla de materiales, resumen económico y condiciones EXW.

## Local Postgres dev environment (SPEC-002a)

A `docker-compose.yml` in the repo provides two Postgres 16 containers:

```bash
docker compose up -d postgres postgres-test    # start both
docker compose down -v                         # stop + wipe dev volume
```

- `postgres`     → host port **5434**, persistent volume, DB `arias_dev`
- `postgres-test` → host port **5433**, volatile tmpfs, DB `arias_test`

Copy `.env.example` to `.env`, adjust the `DATABASE_URL` and `TEST_DATABASE_URL`
if you change ports. App code does **not** use Postgres yet (that comes in
SPEC-002c); only the new `db/` skeleton, Alembic and the integration tests
under `tests/integration/test_db_skeleton.py` talk to it today.

Alembic baseline commands (no migrations landed until SPEC-002b):

```bash
alembic current
alembic history
alembic upgrade head    # no-op while versions/ is empty
```

## Running tests

```bash
# Install dev deps
pip install -r requirements.txt -r requirements-dev.txt

# Ensure Postgres test container is up (needed for db/ skeleton tests)
docker compose up -d postgres-test
export TEST_DATABASE_URL=postgresql+psycopg://arias:arias@localhost:5433/arias_test

# Full test run with coverage (writes coverage.json)
pytest

# Validate per-function coverage gate (SPEC-001 §6.4)
pytest tests/test_coverage_gate.py --no-cov -v -s
```

El test suite cubre el motor de cálculo de cotización (`_num`, `detect_family`,
`compute_line`, `_container_result`, `estimate_containers`, `compute_totals`,
`dedup_alerts`, `calculate_quote`) con ≥85% de cobertura por función y ≥90% en
`calculate_quote`. El workflow CI (`.github/workflows/tests.yml`) corre ambos
comandos en cada push y pull request.

## Próximos pasos

1. Autenticación de usuarios (Oli / Ana / roles)
2. Integración WhatsApp Business API (Make/Zapier)
3. Conexión Holded para facturación automática
4. Tracking automático de contenedor vía API naviera
5. Multi-moneda real con feed FX automático
6. Importación automática desde Excel al abrir la app (watcher)

---
Fassa–Arias Group · Distribución Técnica Fassa Bortolo · Caribe · Abril 2026
