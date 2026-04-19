# SPEC-001 — Test suite del motor de cálculo

**Fase:** 0 (Fundamentos)
**Prioridad:** P0 — bloquea cualquier refactor posterior
**Autor (CTO):** Claude
**Ejecutor (Lead Dev):** Qwen-Coder
**Product Owner:** Oliver
**Estado:** Pendiente
**Branch objetivo:** `feature/spec-001-calc-engine-tests`

---

## 1. Contexto y motivación

El motor de cálculo de cotización vive en `app.py` (líneas 486–823). Es el **corazón económico** del sistema: todo error aquí impacta directamente en márgenes, logística y decisiones comerciales. Hoy **no tiene tests**.

Antes de refactorizar `app.py` (monolito de 3.331 líneas) en módulos o migrar a PostgreSQL, necesitamos una red de seguridad: si rompemos estos cálculos sin darnos cuenta, el negocio se rompe.

**Regla dura:** No se toca ningún refactor del motor hasta que estos tests estén verdes y con ≥85% de cobertura sobre las funciones listadas abajo.

---

## 2. Alcance

### Funciones bajo test (objetivo: ≥85% coverage línea + rama sobre cada una)

| Función | Línea | Responsabilidad |
|---|---|---|
| `_num(v)` | 508 | Coerción numérica tolerante |
| `detect_family(category)` | 515 | Mapeo categoría → familia (PLACAS, PERFILES, etc.) |
| `compute_line(prod, qty)` | 519 | Cálculo línea: peso, palés, coste, alertas |
| `_container_result(key, units, pallets, weight)` | 578 | Resultado normalizado de contenedor |
| `estimate_containers(pallets_logistic, weight_kg, family_breakdown)` | 597 | Optimizador contenedor (20/40/40HC) |
| `compute_totals(lines)` | 634 | Agregación totales |
| `dedup_alerts(lines)` | 657 | Deduplicación alertas |
| `calculate_quote(system_id, area_sqm, freight_eur, target_margin_pct, fx_rate)` | 737 | Orquestador top-level |

### Constantes bajo verificación
- `CONTAINERS` (línea 486): 20/40/40HC con pallets y kg
- `FAMILY_MAP` (línea 492): mapping de categorías

---

## 3. Estructura entregable

```
Arias_Group/
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # fixtures: DB en memoria, productos mock
│   ├── unit/
│   │   ├── test_num.py                # _num
│   │   ├── test_detect_family.py      # detect_family + FAMILY_MAP
│   │   ├── test_compute_line.py       # compute_line
│   │   ├── test_container_result.py   # _container_result
│   │   ├── test_estimate_containers.py # estimate_containers
│   │   ├── test_compute_totals.py     # compute_totals
│   │   └── test_dedup_alerts.py       # dedup_alerts
│   └── integration/
│       └── test_calculate_quote.py    # calculate_quote con DB en memoria
├── pytest.ini                          # config pytest
├── .coveragerc                         # config coverage
└── .github/
    └── workflows/
        └── tests.yml                   # CI GitHub Actions
```

### Dependencias nuevas en `requirements-dev.txt`
```
pytest>=8.0
pytest-cov>=5.0
pytest-flask>=1.3
```

No añadir a `requirements.txt` — dev deps aparte.

---

## 4. Casos de test obligatorios (mínimo)

### 4.1 `_num(v)`
- `_num(None)` → `0.0`
- `_num(0)` → `0.0`
- `_num(1.5)` → `1.5`
- `_num("2.5")` → `2.5`
- `_num("abc")` → `0.0`
- `_num("")` → `0.0`
- `_num([])` → `0.0`

### 4.2 `detect_family(category)`
- `"Placas"` → `"PLACAS"`
- `"  perfiles "` (con espacios/mayúsculas) → `"PERFILES"`
- `"IMPERMEABILIZACIÓN"` → `"PASTAS"` (verifica tildes)
- `"impermeabilizacion"` (sin tilde) → `"PASTAS"`
- `None` → `"DESCONOCIDA"`
- `""` → `"DESCONOCIDA"`
- `"categoría_rara"` → `"DESCONOCIDA"`

### 4.3 `compute_line(prod, qty)`

**Happy path:** Placa BA13 estándar (sqm_per_pallet=60, units_per_pallet=50, kg_per_unit=8.5, unit_price_eur=4.20, unit="board")
- Input: qty=100
- Verifica: `m2_total`, `weight_total_kg`, `pallets_theoretical`, `pallets_logistic` (ceil), `cost_exw_eur`, `alerts` vacía

**Alertas disparadas:**
- Producto sin `unit_price_eur` → alerta "falta precio unitario"
- Producto PLACAS sin `units_per_pallet` → alerta "falta unidades/palé"
- Producto PLACAS sin `kg_per_unit` → alerta "falta peso unitario"
- Producto TORNILLOS sin `kg_per_unit` → alerta "sin peso unitario, peso total = 0"

**Edge cases:**
- `qty = 0` → todos los totales a 0, sin crash
- `qty` negativa → documentar comportamiento actual (no se valida hoy) y añadir test que fije el comportamiento
- `prod` como `sqlite3.Row` (no dict) → debe funcionar (hay cast interno)
- unidad "m2" → `m2_total = qty` directamente
- unidad "ml" → no calcula m2

### 4.4 `_container_result(key, units, pallets, weight)`
- 20ft con 1 unidad, 10 palés, 21500 kg → `pallet_occupancy=1.0`, `weight_occupancy=1.0`, `score=2.0`
- 40HC con 2 unidades, 48 palés, 53000 kg → ocupación per unit correcta
- `units=0` → ocupaciones = 0 (no división por cero)

### 4.5 `estimate_containers(pallets_logistic, weight_kg, family_breakdown)`

**Reglas de ordenación que hay que verificar:**
- Sólo PLACAS → orden `['20', '40', '40HC']` (prefiere 20ft para cargas pequeñas homogéneas)
- Con PERFILES → orden `['40HC', '40']` (nunca 20ft — los perfiles requieren 40+)
- Mixto → orden `['40HC', '40', '20']`

**Happy paths:**
- 8 palés, 18.000 kg, solo PLACAS → 1×20ft, score bajo
- 15 palés, 22.000 kg, mixto → 1×40HC
- 25 palés, 25.000 kg, solo PLACAS → 2×20ft o 1×40HC (verificar cuál gana por score)

**Edge cases:**
- `pallets=0, weight=0` → `None`
- `family_breakdown=None` → no crash, fallback a "mixto"
- Carga extremadamente grande (100 palés) → devuelve múltiples unidades, nunca `None`
- Solo peso (sin palés) → funciona
- Solo palés (sin peso) → funciona

### 4.6 `compute_totals(lines)`
- Lista vacía → todos los totales = 0, `containers = None`
- 3 líneas con `ok=True` y 1 con `ok=False` → la falsa se excluye
- Sumas correctas de cost/weight/m2/pallets
- `family_breakdown` cuenta correcta por familia
- `pallets_logistic` es ceil del sumatorio (no suma de ceils)

### 4.7 `dedup_alerts(lines)`
- 3 líneas con la misma alerta → se devuelve 1 vez
- Preserva orden de primera aparición
- Líneas sin `alerts` → no crash
- Alertas None → no crash

### 4.8 `calculate_quote(system_id, area_sqm, freight_eur, target_margin_pct, fx_rate)` (integración)

**Fixture:** seed de DB en memoria con 1 system "Tabique PYL 12.5mm BA13" + 3 componentes (placa, perfil, tornillos).

**Tests obligatorios:**
- Happy path: 100 m², freight 500€, margen 25%, FX 1.085 → verifica:
  - `summary.product_cost_eur` > 0
  - `summary.gross_margin_pct` ≈ 0.25 (tolerancia ±0.001)
  - `summary.sale_total_local = sale_total_eur × 1.085`
  - `summary.container_recommendation` no None
  - `line_items` tiene 3 entradas con campos legacy
- Waste compuesto: si `sc.waste_pct > system.default_waste_pct`, se usa el mayor
- `area_sqm = 0` → comportamiento documentado (no crash; `price_per_sqm=0`)
- `target_margin_pct = 0.99` → aplica el clamp `max(1-margin, 0.01)` (no división por cero)
- Producto sin precio → alertas burbujean a `summary.alerts`

---

## 5. Fixtures y helpers

### `conftest.py`
```python
import pytest
from app import app as flask_app, init_db, get_db

@pytest.fixture
def app():
    flask_app.config['TESTING'] = True
    flask_app.config['DATABASE'] = ':memory:'
    with flask_app.app_context():
        init_db()
        yield flask_app

@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()

@pytest.fixture
def product_factory():
    """Produce un dict compatible con compute_line."""
    def _make(**overrides):
        base = {
            'sku': 'TEST-001',
            'name': 'Test Product',
            'category': 'placas',
            'unit': 'board',
            'unit_price_eur': 4.20,
            'kg_per_unit': 8.5,
            'units_per_pallet': 50,
            'sqm_per_pallet': 60,
        }
        base.update(overrides)
        return base
    return _make
```

### Fixtures de sistemas seed (para `calculate_quote`)
Usar datos realistas del seed actual en `seed_db()` (línea 398). NO inventar SKUs.

---

## 6. Configuración

### `pytest.ini`
```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -ra --strict-markers --cov=app --cov-report=term-missing --cov-report=html --cov-fail-under=85
markers =
    unit: tests unitarios puros
    integration: tests con DB en memoria
```

### `.coveragerc`
```ini
[run]
source = app
omit =
    tests/*
    */__init__.py

[report]
exclude_lines =
    pragma: no cover
    raise NotImplementedError
    if __name__ == .__main__.:
```

### `.github/workflows/tests.yml`
```yaml
name: Tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: coverage-html
          path: htmlcov/
```

---

## 7. Criterios de aceptación (checklist de merge)

- [ ] `pytest` pasa 100% en local
- [ ] Coverage global ≥85% sobre las 8 funciones listadas en §2
- [ ] Coverage ≥90% en `calculate_quote` (es el orquestador crítico)
- [ ] Todos los casos de §4 implementados (mínimo)
- [ ] GitHub Actions workflow verde en el PR
- [ ] No se modifica lógica de `app.py` — SOLO se añaden tests + dev deps + CI
- [ ] `requirements-dev.txt` creado (no tocar `requirements.txt`)
- [ ] README actualizado con sección "Running tests: `pip install -r requirements-dev.txt && pytest`"
- [ ] PR descripción incluye salida de `pytest --cov` y captura del HTML coverage

---

## 8. Fuera de alcance (NO hacer en este PR)

- ❌ Refactorizar `app.py` o extraer a módulos
- ❌ Migrar a PostgreSQL
- ❌ Añadir tests a rutas Flask (vendrá en SPEC-003)
- ❌ Añadir tests a funciones de persistencia (`save_order_lines`, etc.) — SPEC-002
- ❌ Modificar comportamiento de las funciones de cálculo, aunque parezca un bug. Si encuentras algo raro, **documéntalo en el PR como hallazgo** y abrimos issue aparte.

---

## 9. Definición de "listo"

1. Qwen crea branch `feature/spec-001-calc-engine-tests`
2. Implementa siguiendo §3–§6
3. Abre PR contra `main`
4. Claude (CTO) revisa: coverage, casos, estructura
5. Iteraciones si hace falta
6. Aprobación CTO → Oliver mergea

---

## 10. Notas del CTO

- Este PR es **aburrido pero crítico**. No hay feature visible al usuario. El ROI viene cuando en SPEC-004+ refactoricemos sin romper nada.
- Si algún test revela un bug real (cálculo incorrecto), **NO lo arregles aquí**. Documenta en el PR con un test marcado `@pytest.mark.xfail(reason="BUG-XXX: descripción")` y abrimos issue separado. Esta SPEC fija comportamiento actual como baseline — los arreglos van en SPECs dedicadas con aprobación de producto.
- Preferimos explicit > implicit: asserts con mensaje claro, sin "magic numbers". Si un test dice `assert result == 4.20`, comentar de dónde sale ese número.
