/**
 * products.js - Catálogo de Productos Arias Group
 * Maneja búsqueda, filtrado y edición de productos
 */

/**
 * Filtra filas de la tabla por SKU, nombre o subfamilia
 */
function filterRows() {
  const q = document.getElementById('search').value.toLowerCase().trim();
  document.querySelectorAll('tbody tr[data-search]').forEach(tr => {
    tr.style.display = (!q || tr.dataset.search.includes(q)) ? '' : 'none';
  });
}

/**
 * Expande o colapsa todos los bloques de categorías
 */
function toggleAll(open) {
  document.querySelectorAll('details.cat-block, details.sub-block').forEach(d => d.open = open);
}

/**
 * Extra por defecto si el input del producto está vacío (hereda de la familia)
 */
const DEFAULT_EXTRA_PCT = 5;

/**
 * Sincroniza precio Arias automáticamente al cambiar PVP o descuentos
 * Fórmula: PVP × (1 - disc%) × (1 - extra%)
 */
function syncArias() {
  const pvp = parseFloat(document.getElementById('f_pvp_eur_unit').value) || 0;
  const disc = parseFloat(document.getElementById('f_discount_pct').value) || 0;
  const extraRaw = document.getElementById('f_discount_extra_pct').value;
  const extra = extraRaw === '' ? DEFAULT_EXTRA_PCT : (parseFloat(extraRaw) || 0);
  const arias = pvp * (1 - disc/100) * (1 - extra/100);
  document.getElementById('f_precio_arias_eur_unit').value = arias.toFixed(4);
}

/**
 * Abre modal de edición cargando datos del producto + historial
 */
async function openEdit(id) {
  const r = await fetch('/api/products/' + id);
  const j = await r.json();
  if (!j.ok) { alert(j.error || 'error'); return; }
  const p = j.product;
  document.getElementById('f_id').value = p.id;
  document.getElementById('modalTitle').textContent = p.sku + ' — ' + p.name;
  document.getElementById('modalMeta').textContent = 'Categoría: ' + p.category + ' · Unidad: ' + p.unit;
  for (const f of ['name','subfamily','unit','content_per_unit','pack_size','pvp_eur_unit','precio_arias_eur_unit','discount_pct','discount_extra_pct','kg_per_unit','units_per_pallet','sqm_per_pallet','notes']) {
    const el = document.getElementById('f_' + f);
    if (el) el.value = (p[f] !== null && p[f] !== undefined) ? p[f] : '';
  }
  const hb = document.getElementById('historyBox');
  if (j.history && j.history.length) {
    hb.innerHTML = j.history.map(h =>
      `<div>${h.changed_at.substring(0,16).replace('T',' ')} — <b>${h.field}</b>: ${h.old_value ?? '∅'} → ${h.new_value ?? '∅'} <span class="muted">(${h.username})</span></div>`
    ).join('');
  } else {
    hb.innerHTML = '<span class="muted">Sin historial.</span>';
  }
  document.getElementById('saveMsg').textContent = '';
  document.getElementById('editModal').showModal();
}

/**
 * Valida datos del producto antes de guardar
 * @param {Object} payload - Datos a validar
 * @returns {{valid: boolean, errors: string[]}}
 */
function validateProduct(payload) {
  const errors = [];
  
  // Nombre requerido
  if (!payload.name || !payload.name.trim()) {
    errors.push('Nombre del producto es requerido');
  }
  
  // PVP debe ser positivo
  if (payload.pvp_eur_unit !== null && payload.pvp_eur_unit !== undefined) {
    const pvp = parseFloat(payload.pvp_eur_unit);
    if (isNaN(pvp) || pvp < 0) {
      errors.push('PVP debe ser >= 0');
    }
  }
  
  // Descuentos entre 0-100
  if (payload.discount_pct !== null && payload.discount_pct !== undefined) {
    const disc = parseFloat(payload.discount_pct);
    if (isNaN(disc) || disc < 0 || disc > 100) {
      errors.push('Descuento debe estar entre 0-100%');
    }
  }
  
  if (payload.discount_extra_pct !== null && payload.discount_extra_pct !== undefined) {
    const extra = parseFloat(payload.discount_extra_pct);
    if (isNaN(extra) || extra < 0 || extra > 100) {
      errors.push('Descuento extra debe estar entre 0-100%');
    }
  }
  
  // Cantidad positiva
  ['kg_per_unit', 'units_per_pallet', 'sqm_per_pallet'].forEach(field => {
    if (payload[field] !== null && payload[field] !== undefined) {
      const val = parseFloat(payload[field]);
      if (isNaN(val) || val < 0) {
        errors.push(`${field} debe ser >= 0`);
      }
    }
  });
  
  return {
    valid: errors.length === 0,
    errors: errors
  };
}

/**
 * Guarda cambios del producto vía API con validación previa
 */
async function saveEdit() {
  const id = document.getElementById('f_id').value;
  const payload = {};
  for (const f of ['name','subfamily','unit','content_per_unit','pack_size','pvp_eur_unit','precio_arias_eur_unit','discount_pct','discount_extra_pct','kg_per_unit','units_per_pallet','sqm_per_pallet','notes']) {
    const v = document.getElementById('f_' + f).value;
    payload[f] = v === '' ? null : v;
  }
  
  // Validación previa
  const validation = validateProduct(payload);
  if (!validation.valid) {
    const errorMsg = '⚠ Errores en el producto:\n\n' + validation.errors.map(e => '• ' + e).join('\n');
    alert(errorMsg);
    return;
  }
  
  if (!confirm('¿Guardar cambios en este SKU?')) return;
  document.getElementById('saveBtn').disabled = true;
  const r = await fetch('/api/products/' + id, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const j = await r.json();
  document.getElementById('saveBtn').disabled = false;
  document.getElementById('saveMsg').textContent = j.ok ? (j.changed + ' campos guardados. Recargando…') : ('Error: ' + (j.error || r.status));
  if (j.ok) setTimeout(() => location.reload(), 800);
}

/**
 * Inicializa listeners para sync automático de precio Arias
 */
function initProducts() {
  document.addEventListener('DOMContentLoaded', () => {
    ['f_pvp_eur_unit','f_discount_pct','f_discount_extra_pct'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('input', syncArias);
    });
  });
}

// Init automático
initProducts();
