# HANDOFF — Arias Group App

> **Estado:** v1.0 — operativa, en producción para los primeros 4 clientes.
> **Decisión (2026-04-19):** No comprar Odoo / NetSuite / SAP por ahora. Volumen no lo justifica.
> **Principio rector:** seguir construyendo aquí, **pero hablando ya el idioma de un ERP comercial** para que la migración futura sea un import limpio, no una refactorización.

---

## 1. Qué hace la app hoy

Flujo end-to-end soportado:

```
Cliente
   │
   ├─► [/clients]            Alta cliente (CRUD)
   │
   ├─► [/projects]           Alta proyecto vinculado a cliente
   │
   ├─► [/calculator]         Motor cálculo m² → SKU + qty + palés + contenedor
   │
   ├─► [/quote]              Cotización completa con CIF + margen
   │
   ├─► [/api/save-offer]     Persistencia en `pending_offers`
   │
   ├─► [/api/offer-pdf]      PDF profesional para cliente
   │
   ├─► [/api/offer-status]   Workflow: pending → approved | rejected
   │   │
   │   └─► **(GAP, ver §6)** Al aprobar debería disparar:
   │       ├─► Preorden Fassa (PDF + estado)
   │       └─► Orden Logística (PDF + estado)
   │
   ├─► [/api/preorden-pdf]   PDF orden a fábrica Fassa
   ├─► [/api/orden-logistica-pdf]  PDF orden a operador logístico
   │
   └─► [/dashboard/financial]  KPIs + alertas
```

37 rutas Flask, 19 tablas SQLite, 96 tests del motor de cálculo.

---

## 2. Modelo de datos actual

### Tablas core
| Tabla | Propósito | Equivalente Odoo |
|---|---|---|
| `clients` | Clientes finales | `res.partner` (customer=True) |
| `products` | Catálogo 186 SKUs | `product.template` / `product.product` |
| `systems`, `system_components` | Sistemas constructivos (BOM básico) | `mrp.bom` |
| `projects` | Proyectos / oportunidades | `crm.lead` + `project.project` |
| `project_quotes` | Cotizaciones por proyecto | `sale.order` (state=draft/sent) |
| `pending_offers` | Ofertas confirmadas | `sale.order` (state=sale) |
| `order_lines` | Líneas de oferta | `sale.order.line` |
| `stage_events` | Histórico cambios de etapa | `mail.tracking.value` |
| `shipping_routes` | Rutas Caribe (origen→destino) | `stock.route` + `delivery.carrier` |
| `customs_rates` | Aranceles por país + HS code | `account.tax` (custom group) |
| `fx_rates` | Tipos de cambio | `res.currency.rate` |
| `users` | Usuarios + roles | `res.users` |
| `audit_log` | Log de eventos | `mail.message` + `audit_log` module |
| `doc_sequences` | Numeradores de documento | `ir.sequence` |
| `pickup_pricing` | Precios EXW por familia | `product.pricelist` |
| `family_defaults` | Defaults por familia producto | `product.category` |
| `price_history` | Snapshot histórico de precios | (custom en Odoo) |
| `app_settings` | Config global | `ir.config_parameter` |

---

## 3. Convenciones de naming — "hablar el idioma común"

Desde **v1.1 en adelante**, todo nuevo campo o tabla sigue convenciones Odoo. Los campos legacy se renombran progresivamente con vista a la migración futura.

### Mapeo de campos (legacy → Odoo-compatible)

| Legacy actual | Renombrar a | Por qué |
|---|---|---|
| `clients.name` | mantener | OK (Odoo: `res.partner.name`) |
| `clients.company` | `clients.is_company` (bool) + `clients.commercial_name` | Odoo separa persona/empresa |
| `clients.country` | `clients.country_code` (ISO-3166 alpha-2: `DO`, `HT`, `PR`, `JM`) | Odoo: `country_id` referencia ISO |
| `clients.rnc` | `clients.vat` | Odoo usa `vat` como ID fiscal universal |
| `products.sku` | mantener + alias `default_code` en exports | Odoo usa `default_code` |
| `products.price` | `products.list_price` | Convención Odoo |
| `products.cost` | `products.standard_price` | Convención Odoo |
| `pending_offers.status` | valores: `draft`, `sent`, `sale`, `done`, `cancel` | Mapear: `pending`→`sent`, `approved`→`sale`, `rejected`→`cancel` |
| `pending_offers.offer_number` | `pending_offers.name` (con prefijo `SO`) | Odoo: `sale.order.name = SO00001` |
| `order_lines.qty_input` | `order_lines.product_uom_qty` | Convención Odoo |
| `order_lines.price_unit_eur` | `order_lines.price_unit` (+ moneda separada) | Odoo separa precio/moneda |
| `fx_rates.rate` | mantener | OK |

### Nuevas tablas a añadir (auto-trigger ver §6)

```sql
-- Mirror de purchase.order Odoo
CREATE TABLE factory_orders (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL,
    name TEXT NOT NULL,                  -- "PO00001" (Odoo: purchase.order.name)
    state TEXT DEFAULT 'draft',          -- draft, sent, to_approve, purchase, done, cancel
    partner_id_ref TEXT DEFAULT 'FASSA', -- proveedor (vendor)
    date_planned TEXT,                   -- ready_date Fassa
    sent_to_factory_at TEXT,
    confirmed_at TEXT,
    notes TEXT,
    FOREIGN KEY(offer_id) REFERENCES pending_offers(id)
);

-- Mirror de stock.picking Odoo
CREATE TABLE logistics_orders (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL,
    name TEXT NOT NULL,                  -- "OUT00001" (Odoo: stock.picking.name)
    state TEXT DEFAULT 'draft',          -- draft, waiting, confirmed, assigned, done, cancel
    route_id INTEGER,
    booking_ref TEXT,                    -- ref naviera (BL)
    container_type TEXT,                 -- 20'/40'/40HC
    departure_date TEXT,
    eta_date TEXT,
    delivered_at TEXT,
    FOREIGN KEY(offer_id) REFERENCES pending_offers(id),
    FOREIGN KEY(route_id) REFERENCES shipping_routes(id)
);
```

---

## 4. Contrato JSON de export (futuro endpoint)

**Endpoint:** `GET /api/export/cotizacion/<offer_id>` (a implementar)

Este es el "idioma común" para mover datos a cualquier ERP. **Nombres alineados con Odoo**:

```json
{
  "$schema_version": "1.0",
  "exported_at": "2026-04-19T14:30:00Z",
  "source_system": "arias-app-v1",

  "partner": {
    "name": "Constructora Ramírez SRL",
    "is_company": true,
    "vat": "131-12345-6",
    "country_code": "DO",
    "phone": "+18091234567",
    "email": "compras@ramirez.do",
    "street": "Av. 27 de Febrero 123",
    "city": "Santo Domingo"
  },

  "sale_order": {
    "name": "SO00123",
    "state": "sale",
    "date_order": "2026-04-19",
    "validity_date": "2026-05-04",
    "currency_id": "EUR",
    "pricelist_currency": "USD",
    "fx_rate": 1.085,

    "order_line": [
      {
        "default_code": "FAS-BA13-1200",
        "name": "Placa Fassa BA13 12.5mm 1200x2500",
        "product_uom_qty": 120,
        "product_uom": "Units",
        "price_unit": 4.20,
        "weight": 7.25,
        "x_pallets_logistic": 5,
        "x_m2_total": 360
      }
    ],

    "x_logistics": {
      "container_type": "40HC",
      "weight_total_kg": 12400,
      "pallets_total": 8,
      "incoterm": "CIF",
      "route_code": "VAL-SDQ-ROMANA",
      "route_name": "Valencia → Santo Domingo (Romana)"
    },

    "x_economics": {
      "exw_eur": 10500,
      "freight_eur": 1200,
      "insurance_eur": 80,
      "customs_pct": 0.08,
      "cif_usd": 12450,
      "margin_pct": 22,
      "amount_total": 15190,
      "amount_currency": "USD"
    }
  },

  "purchase_order": {
    "name": "PO00045",
    "state": "purchase",
    "partner_ref": "FASSA",
    "date_planned": "2026-05-15"
  },

  "stock_picking": {
    "name": "OUT00045",
    "state": "assigned",
    "scheduled_date": "2026-05-20",
    "carrier_id": "MAERSK",
    "tracking_ref": null
  },

  "audit": [
    {"action": "OFFER_CREATED", "user": "oli", "at": "2026-04-19T14:00:00Z"},
    {"action": "STATUS_APPROVED", "user": "oli", "at": "2026-04-19T14:30:00Z"},
    {"action": "FACTORY_ORDER_CREATED", "user": "system", "at": "2026-04-19T14:30:01Z"},
    {"action": "LOGISTICS_ORDER_CREATED", "user": "system", "at": "2026-04-19T14:30:01Z"}
  ]
}
```

Campos prefijados con `x_` son extensiones custom (convención Odoo Studio). Cualquier ERP los ingesta como custom fields.

---

## 5. Cómo operar la app

```bash
# Local
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env  # editar SECRET_KEY y BOT_API_TOKEN
python app.py         # http://127.0.0.1:5000

# Tests
pytest                # ≥85% coverage en motor cálculo

# Backup DB (cron diario recomendado)
./backup_db.sh        # ⚠ revisar paths hardcodeados antes de producción
```

Despliegue actual: Render (web service free / starter tier), Postgres skeleton sin usar (queda para escala futura).

---

## 6. Gaps — estado tras sprint v1.1

| # | Gap | Estado |
|---|---|---|
| 1 | **Auto-trigger:** al `STATUS_APPROVED` generar `factory_orders` + `logistics_orders` | ✅ **CERRADO** (commit 9fbc467) |
| 2 | Protección CSRF en formularios y APIs `/api/*` con sesión | ✅ **CERRADO** (commit cbf9ea6, Flask-WTF + helper JS global) |
| 3 | Open redirect en `/login?next=` | ✅ **CERRADO** (commit 176761a, helper `_safe_next_url`) |
| 4 | SQL injection latente en `init_db()` (`ALTER TABLE` con f-string) | ✅ **CERRADO** (commit 176761a, helper `_safe_add_column` + allowlist) |
| 5 | Credenciales hardcodeadas (`Arias2026!`, `Fassa2026!`) en seed | ✅ **CERRADO** (commit 176761a, `SEED_*_PASSWORD` env vars) |
| 6 | `SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE` configurados | ✅ **CERRADO** (commit 176761a) |
| 7 | Endpoint `/api/export/cotizacion/<id>` con contrato JSON §4 | ✅ **CERRADO** (commit 331adf5) |
| 8 | Rate-limit en `/login` (brute force) | 🔲 Pendiente — requiere flask-limiter |
| 9 | IDOR en `/api/offer-pdf/<id>` (no valida ownership) | 🔲 Pendiente — diseño de roles |
| 10 | Renombrado de campos legacy a convenciones Odoo (§3) | 🔲 Pendiente — incremental, no-blocking |

---

## 7. Cuándo migrar a un ERP comercial

Triggers para reabrir la decisión:

- **>20 clientes activos** o **>50 cotizaciones/mes** → Odoo Community self-hosted (~0€ licencia)
- **>50 clientes** o **multi-país operativo real** (no solo destino, sino subsidiarias) → Odoo Enterprise (~3-5k€/año)
- **>200 clientes** o **necesidad de consolidación financiera multi-entidad** → evaluar NetSuite

Cuando llegue: el endpoint `/api/export/*` (§4) entrega los datos al ERP. Las convenciones de naming (§3) hacen el mapeo trivial. El motor de cálculo se extrae como microservicio que el ERP llama vía API.

---

## 8. Decisiones de diseño que se mantienen

- **SQLite hasta ~1000 cotizaciones/año.** Suficiente. El skeleton Postgres en `db/` está listo si se necesita.
- **Excel master como fuente de verdad del catálogo.** `load_catalog.py` re-sincroniza. No tocar SKUs en la DB directamente.
- **`@login_required` en todo `/`, `@admin_required` en mutaciones críticas.** Mantener.
- **Bcrypt para passwords, autoescape Jinja, queries parametrizadas.** Buenos cimientos, no romper.
- **PDF generation con ReportLab.** Suficiente para los 3 documentos (oferta, preorden Fassa, orden logística).

---

## 9. Punto de retorno

Tag de referencia: **`v1.0-stable-pre-erp`** — estado congelado de la app antes del sprint de cierre.

Si algo se rompe en el sprint, `git checkout v1.0-stable-pre-erp` recupera este estado.
