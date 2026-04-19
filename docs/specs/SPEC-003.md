# SPEC-003 — Domain cleanup: descomposición de `STAGES` en entidades reales

**Fase:** 1 (ERP core — desbloquea CRM limpio, reporting correcto)
**Prioridad:** P1 — no bloqueante operativamente, pero la salud del dominio lo exige antes de SPEC-004+
**Autor (CTO):** Claude
**Ejecutor (Lead Dev):** Claude (o Qwen si recupera terminal; o contratación puntual)
**Product Owner:** Oliver
**Estado:** Borrador — activable cuando SPEC-002 (Postgres) esté mergeada
**Branch objetivo:** `feature/spec-003-domain-cleanup`

---

## 1. Contexto

Hoy `app.py` tiene 26 valores de `STAGES` (líneas 29-56) guardados todos en una única columna `projects.stage`. El PO identificó (2026-04-19) que esa lista no pertenece a una sola entidad: es la concatenación de **5 ciclos de vida** que deberían vivir en entidades distintas. SPEC-002 migra verbatim para no mezclar migración con remodelado; esta SPEC arregla el modelado sobre Postgres.

## 2. Descomposición propuesta

| Entidad destino | Stages que le pertenecen | Tabla / columna |
|---|---|---|
| **Lead / Cliente pre-venta** | CLIENTE, OPORTUNIDAD, FILTRO GO / NO-GO | `leads.status` (tabla nueva) o `clients.lifecycle_stage` |
| **Cotización / Presupuesto** | PRE-CÁLCULO RÁPIDO, CÁLCULO DETALLADO, OFERTA V1/V2, VALIDACIÓN TÉCNICA, VALIDACIÓN CLIENTE, CIERRE | `pending_offers.status` (ampliar enum existente) |
| **Pedido / Orden** | CONTRATO + CONDICIONES, PREPAGO VALIDADO, ORDEN BLOQUEADA, CHECK INTERNO, LOGÍSTICA VALIDADA, BOOKING NAVIERA, PEDIDO A FASSA, CONFIRMACIÓN FÁBRICA, READY DATE | `orders.status` (tabla nueva) |
| **Envío / Logística** | EXPEDICIÓN (BL), TRACKING + CONTROL ETA, ADUANA, LIQUIDACIÓN ADUANERA + COSTES FINALES, INSPECCIÓN / CONTROL DAÑOS, ENTREGA | `shipments.status` (tabla nueva) |
| **Postventa** | POSTVENTA, RECOMPRA / REFERIDOS / ESCALA | evento en `customer_events` o campo `clients.post_sale_flag` |

## 3. Alcance de trabajo (orientativo)

1. Nuevas tablas: `leads` (opcional si se prefiere vivir en `clients`), `orders`, `shipments`, `customer_events`.
2. Enums separados: `lead_status_enum`, `order_status_enum`, `shipment_status_enum` (en la migración Alembic siguiente a la de SPEC-002).
3. Migrador de datos: reinterpretar cada `projects.stage` existente y distribuirlo a la entidad correcta. Idempotente. Reporte de casos ambiguos para revisión manual del PO.
4. Refactor de rutas Flask afectadas (`/projects/<id>`, `/dashboard`, `/crm`, `/presupuestos`): donde hoy leen/escriben `projects.stage`, apuntar a la entidad correcta.
5. Tests de regresión ampliados (SPEC-001 se reevalúa: calculate_quote no depende de `stage`, pero integración sí).
6. Deprecación explícita de `projects.stage` con ventana (p. ej., 2 semanas) antes de dropear la columna.

## 4. Riesgos principales

- Pérdida de historia: `stage_events` hoy apunta a `projects`; al dividir entidades, hay que redirigir eventos históricos al nuevo hogar (o mantenerlo en `projects` como "vista histórica legacy").
- UIs de dashboard que agreguen por stage tienen que saber de qué entidad tirar; cambio con tocar templates.
- Reporting (dashboard financiero) que hoy pinta el "pipeline" de 26 columnas debe rediseñarse — es probablemente un cambio de producto, no solo técnico.

## 5. Criterios de aceptación (alto nivel — ampliable)

- [ ] Migración Alembic que añade enums y tablas sin romper SPEC-002
- [ ] Script de backfill idempotente ejecutado en staging y validado por el PO
- [ ] `app.py` ya no lee/escribe `projects.stage` excepto durante ventana de deprecación documentada
- [ ] Dashboard / CRM muestran pipeline segmentado correctamente por entidad
- [ ] Tests de regresión cubren al menos flujo completo: lead → oferta → pedido → envío → postventa

## 6. Dependencias

- **Bloqueado por:** SPEC-002 mergeada (necesitamos Postgres + enums antes)
- **Bloquea:** SPEC-004+ (cualquier feature de CRM, forecast, BI depende de modelado correcto)

## 7. Notas del CTO

Esta SPEC requiere más input de producto que técnico. Lo serio aquí no es la SQL sino **decidir qué es una "oportunidad" vs un "lead" vs un "proyecto"** en el lenguaje real del negocio Arias. El PO debería redactar antes de implementarse un pequeño **glosario de entidades del dominio Arias** (una página) que se archiva en `docs/domain-glossary.md` y sirve de referencia.
