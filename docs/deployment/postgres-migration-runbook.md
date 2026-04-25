# Runbook — cutover de SQLite a PostgreSQL en Render

**Audiencia:** Oliver (product owner / operador) y cualquier persona que ejecute el cutover.
**Precondición:** SPEC-002a, 002b y 002c mergeadas a `main`.
**Duración estimada:** 15-30 min si todo va limpio; plan de rollback en <5 min.

---

## 0. Resumen del cambio

La app en Render (`srv-d7hc57bbc2fs73des1vg`, URL `https://arias-fassa.onrender.com`) vive hoy sobre SQLite efímero (free tier, sin disk). Esta operación activa PostgreSQL como backend sin tocar el código: el switch es **una variable de entorno** (`DATABASE_URL`).

Propiedades tras el cutover:
- **Persistencia real**: los datos sobreviven redeploys y sleeps del free tier.
- **Concurrencia real**: múltiples usuarios escriben sin bloquearse.
- **Comportamiento idéntico de la app**: las mismas rutas, las mismas respuestas.

---

## 1. Precondiciones

- [ ] Rama `main` local y Render están sincronizadas
- [ ] Has aprobado el coste de ~7 €/mes por la instancia Postgres en Render
- [ ] Tienes `gh` CLI autenticado y acceso a los logs del service
- [ ] Tu `fassa_ops.db` local contiene los datos que quieres preservar (si hay) — o aceptas arrancar desde el `seed_db()`

---

## 2. Aprovisionar Postgres en Render

1. Render Dashboard → **New → PostgreSQL**
2. **Plan**: `Starter` (~7 $/mes, 1 GB storage, 256 MB RAM).
3. **Name**: `arias-postgres-prod`
4. **Region**: la misma que `arias-fassa` (típicamente `Frankfurt` u `Oregon`).
5. **PostgreSQL version**: 16.
6. Crear. Render provisiona en 2-5 min.
7. Entra al Dashboard de la DB → **Connections** → copia la línea **Internal Database URL** (tipo `postgres://user:pass@host-int:5432/arias_postgres_prod`). Guárdala fuera de git.

---

## 3. Ejecutar la migración de esquema (Alembic) contra Render prod

Desde tu máquina local:

```bash
cd ~/Arias_Group
source .venv/bin/activate

# Variable de entorno temporal, SOLO en esta shell.
export DATABASE_URL='postgresql+psycopg://<user>:<pass>@<external-host>/arias_postgres_prod'
# Nota: para ejecutar desde fuera de Render necesitas la External Database URL,
# no la Internal. La Internal solo resuelve desde Render infra.

alembic upgrade head
```

Salida esperada:
```
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, initial schema (SPEC-002b)
```

Verificación:
```bash
python -c "
from sqlalchemy import text
from db import get_engine
eng = get_engine()
with eng.connect() as c:
    n = c.execute(text(\"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'\")).scalar()
print(f'Tables en public: {n}  (esperado: 20 — 19 business + alembic_version)')"
```

---

## 4. (Opcional) Migrar datos desde `fassa_ops.db`

Si tienes datos reales en tu SQLite local que quieres preservar:

```bash
python scripts/migrate_sqlite_to_postgres.py \
    --sqlite ./fassa_ops.db \
    --postgres "$DATABASE_URL" \
    --dry-run          # primero, para ver qué va a hacer
```

Revisa el output. Si todo se ve bien:

```bash
python scripts/migrate_sqlite_to_postgres.py \
    --sqlite ./fassa_ops.db \
    --postgres "$DATABASE_URL"
```

Al final verás el resumen (`Rows read / inserted / skipped` por tabla) y posibles warnings (stages desconocidos coercidos, etc.).

Si no tienes datos reales, este paso es **opcional**. La primera vez que Flask arranque contra la nueva DB, `seed_db()` poblará los sistemas, el demo client, rutas de envío y tipos de cambio.

---

## 5. Activar Postgres en Render (el switch real)

1. Render Dashboard → service `arias-fassa` → tab **Environment**.
2. Pulsa **Add Environment Variable** y añade:
   - **Key**: `DATABASE_URL`
   - **Value**: *la Internal Database URL del paso 2* (la interna, no la externa — Render resuelve por DNS interno).
3. Mientras estés, asegúrate de que también existen:
   - **SECRET_KEY**: un valor aleatorio robusto (`python -c "import secrets; print(secrets.token_hex(32))"`).
   - **FLASK_DEBUG**: `0` (inseguro en prod tenerlo a 1).
   - **BOT_API_TOKEN** (opcional) si usas el bot; valor aleatorio.
4. Pulsa **Save Changes**. Render hace un redeploy automático (~2 min).

En los logs del deploy deberías ver:
```
Build successful 🎉
Deploy live
```

Y al primer request no deberías ver traceback alguno.

---

## 6. Smoke tests

Abre los logs del service en vivo (`Logs` tab) mientras haces estos checks:

- [ ] `GET https://arias-fassa.onrender.com/` → 200 / dashboard se pinta
- [ ] Login con usuario `ana` / `Arias2026!` (seed) → redirige al dashboard
- [ ] `/clients` → listado muestra al menos `Promotor Demo`
- [ ] `/calculator` → renderiza formulario
- [ ] Crear una cotización simple y guardarla
- [ ] `/products` → muestra el catálogo (si migraste datos) o vacío (si solo seed)
- [ ] `/dashboard/financial` → muestra KPIs

Y verifica en los logs: **cero trazas de error**, cero `OperationalError`.

Si todo va bien: el cutover está hecho. ✅

---

## 7. Rollback (si algo falla)

El switch es reversible en <5 min:

1. Render Dashboard → service `arias-fassa` → tab **Environment**.
2. Borra la variable `DATABASE_URL`.
3. Save Changes → redeploy automático.

En el siguiente arranque, `app.get_db()` detecta que `DATABASE_URL` no está y vuelve a SQLite. La app funcionará como antes (efímera en free tier) y ningún usuario nota nada raro.

La Postgres provisionada sigue ahí con sus datos — puedes volver a activarla cuando hayas resuelto el problema.

---

## 8. Después del cutover

Ventana de confianza: **72 h**.

- Deja `DATABASE_URL` activa.
- Revisa logs diariamente las primeras 72 h — busca `IntegrityError`, `DataError`, `OperationalError`.
- Pasado ese tiempo, consideramos el cutover definitivo y podemos **borrar el `.gitignore` de `fassa_ops.db`** (nunca estuvo en git desde commit `1ae0946`, ya lo está).
- Eventualmente: eliminar la rama SQLite del código (SPEC-004+) para simplificar `get_db()`.

---

## 9. Cosas que NO hace este runbook

- ❌ Configurar backups automáticos de Postgres (Render Starter plan lo incluye; ve a la tab **Backups** de la DB para programarlos).
- ❌ Migrar a un provider distinto de Render (SPEC-005+ si escala el proyecto).
- ❌ Scaling horizontal (replicas, read replicas). Render Starter no lo permite; los planes superiores sí.
- ❌ Partición temporal del `audit_log` (planificado para cuando crezca).

---

## 10. Contacto

Dudas operacionales → Oliver (PO). Dudas de arquitectura → Claude (CTO). Bugs reales durante cutover → abrir issue en GitHub con etiqueta `spec-002c-rollback` y pegar los logs relevantes.
