# old/ — Archivos obsoletos

Contenedor de artefactos obsoletos pero conservados como referencia histórica. Ningún código en producción depende de nada aquí. No correr scripts de esta carpeta — pueden ser destructivos contra la DB actual.

## Contenido

### `load_catalog.py`
Script de sincronización Excel → SQLite. **Deprecated 2026-04-21.** Sobrescribiría el catálogo actual de la DB, que ya diverge del Excel (60 SKUs añadidos en DB, precios actualizados, campos logísticos DB-only del motor de contenedores). La DB es ahora la fuente de verdad (ver `HANDOFF.md`).

### `Arias_Group_Master-System_v1.xlsx`
Antiguo Excel maestro del catálogo (11 hojas: CONFIG, PRODUCT, SYSTEM, LOGISTIC, etc., 180 filas en PRODUCT). Snapshot previo a la divergencia DB↔Excel. Conservado como referencia de estructura original.

### `PRODUCT_AriasGroup_v4.xlsx`
Versión aún anterior del catálogo, organizada por familias (PLACAS, PERFILES, CINTAS…) sin hoja PRODUCT unificada. Precursor del Master v1.

### `SPEC-002.md` y `SPEC-003.md`
Specs archivadas el 2026-04-19 tras el pivot (ver `docs/PIVOT-2026-04-19.md`):
- SPEC-002: migración SQLite → PostgreSQL. Archivada — el ERP comercial traerá su DB.
- SPEC-003: descomposición de `STAGES` en entidades. Archivada — Odoo/NetSuite ya trae ese modelado.

## Exportar catálogo a Excel hoy

Usar el endpoint `GET /api/export/catalog.xlsx` (o el botón en `/products`) — genera el xlsx al vuelo desde la DB actual.
