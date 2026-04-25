# Dev environment — Arias Group

Workflow para auditar / probar cambios contra una copia de producción **sin tocar prod**.

## Setup (una vez)

```bash
cd /Users/anamarperezmarrero/work/Arias_Group
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env  # ya hecho — ajusta SECRET_KEY si vas a arrancar la app
```

## Snapshot de la DB de producción

La DB de producción vive en `/Users/olivergonzalezarias/Arias_Group/instance/arias.db`.
Para auditarla en este entorno:

```bash
# desde el user olivergonzalezarias:
cp /Users/olivergonzalezarias/Arias_Group/instance/arias.db \
   /Users/anamarperezmarrero/work/Arias_Group/instance/arias.db
chmod 644 /Users/anamarperezmarrero/work/Arias_Group/instance/arias.db
```

`instance/` está en `.gitignore` — la DB nunca se commitea. Tampoco `*.db`,
`*.sqlite`, `dev/snapshots/`.

## Auditoría rápida

```bash
.venv/bin/python dev/audit_catalog.py instance/arias.db
```

Lista anomalías de catálogo: kg=0, descuento desviado, m²/palé en familias no
planares, peso estimado, unidades inconsistentes.

## Tests

```bash
.venv/bin/pytest tests/                # toda la suite
.venv/bin/pytest tests/unit/            # solo unitarios (rápido)
.venv/bin/pytest --cov=app tests/       # con cobertura
```
