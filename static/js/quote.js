/**
 * quote.js - Cotizador Arias Group
 * Maneja la lógica de cotización por producto, sistema o importación masiva
 */

// === CONFIGURACIÓN INICIAL ===
let lines = [];
let mode = 'product';

/**
 * Inicializa el cotizador con datos del servidor
 */
function initQuote(options) {
  options = options || {};
  window.products = options.products || [];
  window.systems = options.systems || [];
  window.subfamilies = options.subfamilies || {};
  window.allProjects = options.projects || [];
  window.PALLET_PROFILES = options.pallet_profiles || {};
  window.CONTAINER_40HC = options.container_40hc || {};

  // Setup inicial de UI
  onFamilyChange();
  onClientSelect();
  onIncotermChange();
  checkFxWarning();

  // Modo edición
  if (options.editOffer) {
    loadEditOffer(options.editOffer);
  }

  recalcAll();
}

/**
 * Cambia entre modos: product, system, import
 */
function setMode(m) {
  mode = m;
  document.getElementById('productSection').style.display = m === 'product' ? 'block' : 'none';
  document.getElementById('systemSection').style.display = m === 'system' ? 'block' : 'none';
  document.getElementById('importSection').style.display = m === 'import' ? 'block' : 'none';
  document.querySelectorAll('#btnProduct,#btnSystem,#btnImport').forEach(b => b.style.fontWeight = 'normal');
  var active = m === 'product' ? 'btnProduct' : m === 'system' ? 'btnSystem' : 'btnImport';
  document.getElementById(active).style.fontWeight = 'bold';
}

/**
 * Maneja cambio de familia - actualiza subfamilias y filtra productos
 */
function onFamilyChange() {
  const fam = document.getElementById('familySelect').value;
  const subDiv = document.getElementById('subfamilyContainer');
  const sfSel = document.getElementById('subfamilySelect');
  const hasSub = window.subfamilies[fam] && window.subfamilies[fam].length > 0;
  if (hasSub) {
    subDiv.style.display = 'block';
    sfSel.innerHTML = '<option value="">— Todos —</option>';
    window.subfamilies[fam].forEach(sf => {
      const opt = document.createElement('option');
      opt.value = sf.key;
      opt.textContent = sf.label;
      sfSel.appendChild(opt);
    });
  } else {
    subDiv.style.display = 'none';
  }
  filterProducts();
}

/**
 * Filtra productos según familia y subfamilia seleccionadas
 */
function filterProducts() {
  const fam = document.getElementById('familySelect').value;
  const sf = document.getElementById('subfamilySelect').value;
  const sel = document.getElementById('productSelect');
  sel.innerHTML = '<option value="">— Selecciona —</option>';
  window.products.forEach(p => {
    let show = (p.category === fam);
    if (show && sf) show = (p.subfamily === sf);
    if (show) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.dataset.sku = p.sku;
      opt.dataset.name = p.name;
      opt.dataset.price = p.price_per_unit;
      opt.dataset.unit = p.unit_label;
      opt.dataset.family = p.category;
      opt.dataset.subfamily = p.subfamily || '';
      opt.dataset.unitsPallet = p.units_per_pallet || 0;
      opt.dataset.sqmPallet = p.sqm_per_pallet || 0;
      opt.dataset.kgPerUnit = p.kg_per_unit || 0;
      opt.textContent = p.sku.substring(0,12) + ' — ' + p.name.substring(0,40) + ' (' + p.unit_label + ')';
      sel.appendChild(opt);
    }
  });
}

/**
 * Filtra proyectos según cliente seleccionado
 */
function onClientSelect() {
  const clientId = document.getElementById('clientSelect').value;
  const projSel = document.getElementById('projectSelect');
  projSel.innerHTML = '<option value="">— Selecciona proyecto —</option>';
  window.allProjects.forEach(p => {
    if (!clientId || String(p.client_id) === String(clientId)) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.dataset.name = p.name;
      opt.dataset.client = p.client_id;
      opt.dataset.area = p.area_sqm || 0;
      opt.dataset.incoterm = p.incoterm || 'EXW';
      opt.textContent = p.name;
      projSel.appendChild(opt);
    }
  });
}

/**
 * Actualiza incoterm cuando se selecciona proyecto
 */
function onProjectSelect() {
  const sel = document.getElementById('projectSelect');
  const opt = sel.options[sel.selectedIndex];
  if (!opt || !opt.value) return;
  const inc = (opt.dataset.incoterm || '').toUpperCase();
  if (inc && ['EXW','FOB','CIF'].includes(inc)) {
    document.getElementById('incotermSelect').value = inc;
    onIncotermChange();
  }
}

/**
 * Muestra/oculta sección de logística según incoterm
 */
function onIncotermChange() {
  const inc = document.getElementById('incotermSelect').value;
  document.getElementById('logisticsSection').style.display = inc !== 'EXW' ? 'block' : 'none';
  document.getElementById('incotermLabel').textContent = inc;
  recalcAll();
}

/**
 * Obtiene margen global por defecto del input
 */
function getDefaultMargin() {
  const v = parseFloat(document.getElementById('marginInput').value);
  return isNaN(v) ? 20 : v;
}

/**
 * Añade producto seleccionado a las líneas de la oferta
 */
function addProduct() {
  const sel = document.getElementById('productSelect');
  const opt = sel.options[sel.selectedIndex];
  if (!opt.value) return;
  const qty = parseFloat(document.getElementById('qtyInput').value) || 1;
  lines.push({
    sku: opt.dataset.sku, name: opt.dataset.name,
    family: opt.dataset.family, unit: opt.dataset.unit,
    price: parseFloat(opt.dataset.price), qty: qty,
    margin: getDefaultMargin()
  });
  sel.selectedIndex = 0;
  document.getElementById('qtyInput').value = 1;
  recalcAll();
}

/**
 * Aplica margen global a todas las líneas
 */
function applyGlobalMargin() {
  const m = getDefaultMargin();
  lines.forEach(l => { l.margin = m; });
  recalcAll();
}

/**
 * Importa líneas desde texto (SKU, cantidad)
 */
function importLines() {
  const raw = document.getElementById('importText').value.trim();
  if (!raw) return;
  const rows = raw.split('\n').filter(l => l.trim());
  let added = 0, errors = [];
  const margin = getDefaultMargin();
  rows.forEach((row, i) => {
    const parts = row.split(/[,;\t]+/).map(s => s.trim());
    if (parts.length < 2) { errors.push('Línea ' + (i+1) + ': faltan datos'); return; }
    const sku = parts[0];
    const qty = parseFloat(parts[1]);
    if (!sku || isNaN(qty) || qty <= 0) { errors.push('Línea ' + (i+1) + ': SKU o cantidad inválida'); return; }
    const prod = window.products.find(p => p.sku === sku);
    if (!prod) { errors.push('Línea ' + (i+1) + ': SKU "' + sku + '" no encontrado'); return; }
    lines.push({
      sku: prod.sku, name: prod.name,
      family: prod.category, unit: prod.unit_label,
      price: prod.price_per_unit, qty: qty,
      margin: margin
    });
    added++;
  });
  recalcAll();
  let msg = added + ' líneas importadas.';
  if (errors.length) msg += ' ' + errors.length + ' errores: ' + errors.join(' | ');
  document.getElementById('importResult').textContent = msg;
  if (added > 0) document.getElementById('importText').value = '';
}

/**
 * Actualiza margen de una línea específica
 */
function updateLineMargin(i, val) {
  lines[i].margin = parseFloat(val) || 0;
  recalcAll();
}

/**
 * Actualiza cantidad de una línea y marca logística como stale
 */
function updateLineQty(i, val) {
  const v = parseFloat(val);
  if (isNaN(v) || v <= 0) return;
  lines[i].qty = v;
  const state = document.getElementById('logisticsState');
  if (state && !state.textContent.includes('stale')) {
    state.innerHTML = '<span style="color:#a23;">⚠ stale — vuelve a Calcular logística</span>';
  }
  recalcAll();
}

/**
 * Añade sistema constructivo completo (múltiples SKUs)
 */
function addSystem() {
  const sel = document.getElementById('systemSelect');
  if (!sel.value) return;
  const area = parseFloat(document.getElementById('areaInput').value) || 0;
  if (area <= 0) return;
  const sys = window.systems.find(s => s.id == sel.value);
  if (!sys) return;
  sys.components.forEach(comp => {
    const qty = Math.ceil(area * comp.consumption_per_sqm);
    lines.push({
      sku: comp.sku, name: comp.name,
      family: comp.category, unit: comp.unit,
      price: comp.unit_price_eur, qty: qty,
      margin: getDefaultMargin()
    });
  });
  recalcAll();
}

/**
 * Elimina una línea de la oferta
 */
function removeLine(i) { lines.splice(i, 1); recalcAll(); }

/**
 * Limpia todas las líneas
 */
function clearAll() { lines.length = 0; recalcAll(); document.getElementById('offerBanner').style.display = 'none'; }

/**
 * Actualiza coste logístico unitario de una línea (override manual)
 */
function updateLineLogCost(i, val) {
  const v = parseFloat(val);
  lines[i].logUnitCost = isNaN(v) ? 0 : v;
  lines[i].logCostManual = true;
  recalcAll();
}

/**
 * Calcula logística usando el motor del backend
 */
async function recomputeLogistics() {
  if (lines.length === 0) { alert('Añade al menos un producto.'); return; }
  const wasteInput = parseFloat(document.getElementById('wasteInput').value);
  const wastePct = isNaN(wasteInput) ? 5 : wasteInput;
  const routeSel = document.getElementById('routeSelect');
  const routeOpt = routeSel.options[routeSel.selectedIndex];
  const containerType = '40HC';
  const costPerCont = routeOpt && routeOpt.value
    ? (parseFloat(routeOpt.dataset.c40hc) || parseFloat(routeOpt.dataset.c40) || 0)
    : 0;
  const state = document.getElementById('logisticsState');
  state.textContent = 'calculando…';
  try {
    const r = await fetch('/api/compute-logistics', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        lines: lines.map(l => ({ sku: l.sku, qty: l.qty, waste_pct: wastePct })),
        container_type: containerType,
        cost_per_container_eur: costPerCont,
      }),
    });
    const j = await r.json();
    if (!j.ok) { state.textContent = 'error: ' + (j.error || r.status); return; }
    lines.forEach(l => {
      if (l.logCostManual) return;
      const p = j.per_sku.find(x => x.sku === l.sku);
      l.logUnitCost = p ? p.unit_log_cost_eur : 0;
    });
    document.getElementById('containerCount').value = j.n_containers;
    window._motorCalc = {
      nContainers: j.n_containers,
      nContainersDecimal: j.n_containers_decimal,
      costPerContainer: costPerCont,
    };
    const drv = {'floor':'suelo','pallets':'geometría','weight':'peso','cbm':'volumen'}[j.dominant_driver] || j.dominant_driver;
    const decimalLabel = j.n_containers_decimal && j.n_containers_decimal !== j.n_containers
      ? ` (carga real ${j.n_containers_decimal.toFixed(2)})` : '';
    state.innerHTML = `<b>${j.n_containers}×${containerType}</b>${decimalLabel} · dominante: <b>${j.dominant_family}</b> (por ${drv}) · € ${fmtN(j.total_cost_eur)}`;
    renderCapacityAlert(j.free_capacity, j.n_containers, j.dominant_family);
    renderLogisticsAnalysis(j, wastePct);
    recalcAll();
  } catch (e) {
    state.textContent = 'error de red: ' + e.message;
  }
}

/**
 * Reescala coste logístico cuando usuario cambia manualmente nº contenedores
 */
function onContainerCountChange() {
  const newN = parseInt(document.getElementById('containerCount').value) || 0;
  const motor = window._motorCalc;
  if (motor && motor.nContainers > 0 && newN > 0 && newN !== motor.nContainers) {
    const ratio = newN / motor.nContainers;
    lines.forEach(l => {
      if (l.logCostManual) return;
      l.logUnitCost = (l.logUnitCost || 0) * ratio;
    });
    motor.nContainers = newN;
    const state = document.getElementById('logisticsState');
    if (state) {
      state.innerHTML += ` · <span style="color:#b8860b;">ajustado a ${newN} cont.</span>`;
    }
  }
  recalcAll();
}

/**
 * Muestra alerta de capacidad disponible en contenedores
 */
function renderCapacityAlert(fc, nCont, dominantFamily) {
  const div = document.getElementById('capacityAlert');
  if (!div) return;
  if (!fc || nCont <= 0) { div.style.display = 'none'; return; }

  const CAP_40HC = { weight_kg: 25200, floor_m2: 28.3, cbm: 68.5 };
  const per = fc.per_container || {};
  const weightPer = Number(per.weight_kg || 0);
  const floorPer = Number(per.floor_m2 || 0);
  const cbmPer = Number(per.cbm || 0);
  const pctWeight = Math.min(100, (weightPer / CAP_40HC.weight_kg) * 100);
  const pctFloor  = Math.min(100, (floorPer  / CAP_40HC.floor_m2)  * 100);
  const pctCbm    = Math.min(100, (cbmPer    / CAP_40HC.cbm)       * 100);

  function tag(label, value, unit, pct) {
    let color, bg;
    if (pct > 40)      { color = '#a23'; bg = '#fbe5e5'; }
    else if (pct > 10) { color = '#b8860b'; bg = '#fffbea'; }
    else               { color = '#060'; bg = '#eef7ea'; }
    return `<span style="display:inline-block; margin-right:12px; padding:2px 8px; background:${bg}; color:${color}; border-radius:3px;">
              <b>${fmtN(value)} ${unit}</b> ${label} libre <small>(${pct.toFixed(0)}%)</small>
            </span>`;
  }

  const tot = fc.total || {};
  const header = fc.is_optimized
    ? `✅ <b>Contenedores optimizados.</b> Carga al tope — no cabe producto adicional.`
    : `✨ <b>Oportunidad: queda capacidad en tus ${nCont} contenedores.</b>`;

  const suggestion = fc.is_optimized
    ? ''
    : `<div style="margin-top:6px; font-size:0.85em; color:#666;">Considera añadir pastas / cintas / cubos / cajas en palés (mejor margen) sin abrir contenedor extra.</div>`;

  div.style.background = fc.is_optimized ? '#eef7ea' : '#fffbea';
  div.style.border = fc.is_optimized ? '1px solid #5a8' : '1px solid #b8860b';
  div.innerHTML =
    header +
    `<div style="margin-top:6px;"><b>Por contenedor:</b> ` +
    tag('peso',   weightPer, 'kg', pctWeight) +
    tag('suelo',  floorPer,  'm²', pctFloor)  +
    tag('volumen',cbmPer,    'm³', pctCbm)    +
    `</div>` +
    `<div style="margin-top:4px;"><b>Total (${nCont} cont.):</b> ${fmtN(tot.weight_kg || 0)} kg · ${fmtN(tot.floor_m2 || 0)} m² · ${fmtN(tot.cbm || 0)} m³ libres.</div>` +
    suggestion;
  div.style.display = 'block';
}

/**
 * Renderiza análisis detallado de logística (drivers: suelo, peso, volumen)
 */
function renderLogisticsAnalysis(j, wastePctNum) {
  const body = document.getElementById('logAnalysisBody');
  const summary = document.getElementById('logSummary');
  if (!body) return;
  if (!j || !j.aggregate) {
    body.innerHTML = '<p class="muted" style="font-size:0.85em;">Sin datos del motor logístico.</p>';
    summary.textContent = '';
    return;
  }
  const ag = j.aggregate;
  const cont = window.CONTAINER_40HC || {};
  const stow = Number(cont.stowage_factor) || 0.90;
  const floorStow = Number(cont.floor_stowage_factor) || 0.80;
  const usableWeight = (Number(cont.payload_kg) || 26500) * stow;
  const usableFloor = (Number(cont.inner_length_m) || 12.03) * (Number(cont.inner_width_m) || 2.35) * floorStow;
  const usableCbm = (Number(cont.inner_length_m) || 12.03) * (Number(cont.inner_width_m) || 2.35) * (Number(cont.inner_height_m) || 2.69) * stow;

  const wasteFactor = 1 + (wastePctNum || 0) / 100;
  const rows = [];
  let totalKg = 0, totalFloor = 0, totalCbm = 0;
  lines.forEach(line => {
    const prod = window.products.find(p => p.sku === line.sku);
    if (!prod) return;
    const qtyW = Math.ceil((Number(line.qty) || 0) * wasteFactor);
    if (qtyW <= 0) return;
    const upp = Number(prod.units_per_pallet) || 1;
    const kgUd = Number(prod.kg_per_unit) || 0;
    const cat = prod.category || '?';
    const pp = window.PALLET_PROFILES[cat] || {length_m: 1.2, width_m: 0.8, height_m: 1.0, levels: 2};
    const pallets = Math.ceil(qtyW / upp);
    const kgTot = qtyW * kgUd;
    const footprint = (pp.length_m * pp.width_m) / Math.max(pp.levels || 1, 1);
    const floorTot = pallets * footprint;
    const cbmTot = pallets * pp.length_m * pp.width_m * pp.height_m;
    totalKg += kgTot;
    totalFloor += floorTot;
    totalCbm += cbmTot;
    rows.push({sku: line.sku, name: prod.name, cat, qtyW, kgUd, upp, pallets, kgTot, footprint, floorTot, cbmTot});
  });

  const nFloor = Number(ag.n_by_floor) || 0;
  const nWeight = Number(ag.n_by_weight) || 0;
  const nCbm = Number(ag.n_by_cbm) || 0;
  const nMax = Math.max(nFloor, nWeight, nCbm);
  const dominant = nMax === nWeight ? 'peso' : (nMax === nFloor ? 'suelo' : 'volumen');
  const nCeil = Math.ceil(nMax);

  function rowFmt(r) {
    return `<tr>
      <td>${escapeHtml(r.sku)}</td>
      <td>${escapeHtml((r.name || '').slice(0, 40))}</td>
      <td>${escapeHtml(r.cat)}</td>
      <td class="num">${fmtQ(r.qtyW)}</td>
      <td class="num">${fmtN(r.kgUd, 2)}</td>
      <td class="num">${fmtQ(r.upp)}</td>
      <td class="num">${fmtQ(r.pallets)}</td>
      <td class="num">${fmtN(r.kgTot, 0)}</td>
      <td class="num">${fmtN(r.floorTot, 2)}</td>
      <td class="num">${fmtN(r.cbmTot, 2)}</td>
    </tr>`;
  }

  function driverBlock(label, value, usable, unit, valueDec, isDominant) {
    const n = value / usable;
    const bg = isDominant ? '#fffbea' : '#f5f5f5';
    const border = isDominant ? '2px solid #b8860b' : '1px solid #ddd';
    const dom = isDominant ? '<b style="color:#b8860b;"> ← DOMINANTE</b>' : '';
    return `<div style="display:inline-block; margin-right:8px; padding:6px 10px; background:${bg}; border:${border}; border-radius:4px; min-width:220px; vertical-align:top;">
      <div style="font-size:0.8em; color:#666; text-transform:uppercase;">${label}${dom}</div>
      <div style="margin-top:2px; font-size:1.05em;"><b>${fmtN(n, 2)} contenedores</b></div>
      <div class="muted" style="font-size:0.85em; margin-top:2px;">${fmtN(value, valueDec)} ${unit} ÷ ${fmtN(usable, 2)} ${unit}/cont.</div>
    </div>`;
  }

  const headerHtml = `<table style="margin-top:6px;">
    <thead><tr>
      <th>SKU</th><th>Producto</th><th>Familia</th>
      <th class="num">Cant.+merma</th>
      <th class="num">kg/ud</th>
      <th class="num">Ud/palé</th>
      <th class="num">Palés</th>
      <th class="num">Peso (kg)</th>
      <th class="num">Suelo (m²)</th>
      <th class="num">Vol (m³)</th>
    </tr></thead>
    <tbody>${rows.map(rowFmt).join('')}</tbody>
    <tfoot><tr style="border-top:2px solid #333; font-weight:bold;">
      <td colspan="7">Totales</td>
      <td class="num">${fmtN(totalKg, 0)}</td>
      <td class="num">${fmtN(totalFloor, 2)}</td>
      <td class="num">${fmtN(totalCbm, 2)}</td>
    </tr></tfoot>
  </table>
  <div style="margin-top:10px;"><b>N requerido por driver:</b></div>
  <div style="margin-top:6px;">
    ${driverBlock('SUELO (m²)',   totalFloor, usableFloor,  'm²', 2, dominant === 'suelo')}
    ${driverBlock('PESO (kg)',    totalKg,    usableWeight, 'kg', 0, dominant === 'peso')}
    ${driverBlock('VOLUMEN (m³)', totalCbm,   usableCbm,    'm³', 2, dominant === 'volumen')}
  </div>
  <div style="margin-top:10px; padding:8px 12px; background:#eef3f9; border-left:3px solid #2563A8; border-radius:3px;">
    <b>Resultado:</b> ${nCeil} contenedores 40HC (carga real ${fmtN(nMax, 2)}). El driver dominante es <b>${dominant.toUpperCase()}</b> — es lo que está limitando.
    ${dominant === 'peso' ? 'Para reducir nº de contenedores, busca producto más ligero por m² o cambia el mix.' : ''}
    ${dominant === 'suelo' ? 'Producto que ocupa mucho suelo por palé. Subir niveles apilables (si el embalaje lo permite) reduce huella.' : ''}
    ${dominant === 'volumen' ? 'Producto voluminoso pero ligero. Difícil de optimizar — la geometría del palé manda.' : ''}
  </div>`;
  body.innerHTML = headerHtml;
  summary.textContent = `(${nCeil} cont. · dominante ${dominant} · ${rows.length} SKUs)`;
}

/**
 * Verifica si el FX difiere >5% del oficial y muestra alerta
 */
function checkFxWarning() {
  const input = document.getElementById('fxInput');
  const warnDiv = document.getElementById('fxWarning');
  if (!input || !warnDiv) return;
  const official = parseFloat(input.dataset.official) || 0;
  const current = parseFloat(input.value) || 0;
  if (official <= 0 || current <= 0) { warnDiv.style.display = 'none'; return; }
  const diffPct = Math.abs(current - official) / official * 100;
  if (diffPct > 5) {
    warnDiv.innerHTML = `⚠ FX ${current.toFixed(3)} difiere ${diffPct.toFixed(1)}% del oficial (${official.toFixed(3)}). Verifica antes de emitir.`;
    warnDiv.style.display = 'block';
  } else {
    warnDiv.style.display = 'none';
  }
}

/**
 * Formatea número con separadores de miles y decimales
 */
function fmtN(n, dec) {
  if (dec === undefined) dec = 2;
  return Number(n).toLocaleString('es-ES', {minimumFractionDigits: dec, maximumFractionDigits: dec});
}

/**
 * Formatea número entero sin decimales
 */
function fmtQ(n) { return Number(n).toLocaleString('es-ES', {maximumFractionDigits: 0}); }

/**
 * Escapa HTML para prevenir XSS
 */
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

/**
 * Recalcula toda la oferta (costes, márgenes, logística, paneles)
 */
function recalcAll() {
  const _w = parseFloat(document.getElementById('wasteInput').value);
  const wastePct = (isNaN(_w) ? 5 : _w) / 100;
  const fx = parseFloat(document.getElementById('fxInput').value) || 1.18;
  const incoterm = document.getElementById('incotermSelect').value;

  let totalPallets = 0;
  let totalWeight = 0;
  lines.forEach(line => {
    const qtyW = Math.ceil(line.qty * (1 + wastePct));
    const prod = window.products.find(p => p.sku === line.sku);
    if (prod) {
      const upp = prod.units_per_pallet || 0;
      if (upp > 0) totalPallets += qtyW / upp;
      totalWeight += qtyW * (prod.kg_per_unit || 0);
    }
  });
  const palletsLogistic = Math.ceil(totalPallets);

  const manualContainers = document.getElementById('containerCount');
  let containerCount = parseInt(manualContainers.value) || 0;

  const _lm = parseFloat(document.getElementById('logMarginInput').value);
  const logMargin = (isNaN(_lm) ? 0 : _lm) / 100;
  let logisticCost = 0;
  if (incoterm !== 'EXW') {
    let oceanFreight = 0;
    const routeSel = document.getElementById('routeSelect');
    const routeOpt = routeSel.options[routeSel.selectedIndex];
    if (routeOpt && routeOpt.value) {
      oceanFreight = parseFloat(routeOpt.dataset.c40hc) || parseFloat(routeOpt.dataset.c40) || 0;
    }
    const logBase = oceanFreight * containerCount;
    logisticCost = logMargin > 0 ? logBase / (1 - logMargin) : logBase;
    const freightLabel = oceanFreight > 0 ? fmtN(oceanFreight) + ' €/cont × ' + containerCount + ' = ' : '';
    document.getElementById('logisticCostDisplay').textContent =
      freightLabel + '€ ' + fmtN(logBase) + (logMargin > 0 ? ' → € ' + fmtN(logisticCost) + ' (+' + (logMargin*100).toFixed(0) + '%)' : '');
  } else {
    document.getElementById('logisticCostDisplay').textContent = '€ 0.00';
  }

  const body = document.getElementById('linesBody');
  body.innerHTML = '';
  let totalCost = 0;
  let totalSale = 0;

  let totalLogistics = 0;
  lines.forEach((line, i) => {
    const qtyW = Math.ceil(line.qty * (1 + wastePct));
    const logPerUnit = parseFloat(line.logUnitCost) || 0;
    const logLine = logPerUnit * qtyW;
    const costProduct = line.price * qtyW;
    const m = (line.margin || 0) / 100;
    const saleProduct = m < 1 ? costProduct / (1 - m) : costProduct;
    const lineSale = saleProduct + logLine;
    const lineCost = costProduct + logLine;
    totalCost += lineCost;
    totalSale += lineSale;
    totalLogistics += logLine;
    const prod = window.products.find(p => p.sku === line.sku);
    let packAlert = '';
    if (prod && ['caja','paquete'].includes(line.unit)) {
      const upp = prod.units_per_pallet || 0;
      if (upp > 1) {
        const totalUds = qtyW * upp;
        const isHigh = qtyW > 100 || totalUds > 10000;
        const color = isHigh ? 'color:#a23;font-weight:bold;' : 'color:#888;';
        packAlert = `<div style="${color}font-size:0.8em;">${fmtQ(qtyW)} ${line.unit}s = ${fmtQ(totalUds)} uds</div>`;
      }
    }
    const logInputStyle = line.logCostManual ? 'background:#fff4d6;' : '';
    body.innerHTML += `<tr>
      <td class="mono">${escapeHtml(line.sku)}</td>
      <td>${escapeHtml(line.name)}</td>
      <td>${escapeHtml(line.family)}</td>
      <td>${escapeHtml(line.unit)}</td>
      <td class="num">${fmtN(line.price)}</td>
      <td class="num">${fmtN(line.price * fx)}</td>
      <td class="num"><input type="number" value="${line.qty}" min="1" step="1"
          style="width:70px; text-align:right;" onchange="updateLineQty(${i}, this.value)">${
            wastePct > 0 && qtyW > line.qty
              ? `<div style="font-size:0.75em; color:#666;" title="Incluye ${(wastePct*100).toFixed(0)}% de merma">+merma → ${fmtQ(qtyW)}</div>`
              : ''
          }${packAlert}</td>
      <td class="num"><input type="number" value="${(logPerUnit || 0).toFixed(4)}" min="0" step="0.0001"
          style="width:85px; text-align:right; ${logInputStyle}"
          title="${line.logCostManual ? 'Override manual' : 'Imputación automática del motor'}"
          onchange="updateLineLogCost(${i}, this.value)"></td>
      <td class="num"><input type="number" value="${line.margin}" min="0" max="90" step="1"
          style="width:50px; text-align:right;" onchange="updateLineMargin(${i}, this.value)"></td>
      <td class="num">${fmtN((lineSale / qtyW) * fx)}</td>
      <td class="num">${fmtN(lineSale)}</td>
      <td class="num">${fmtN(lineSale * fx)}</td>
      <td><button type="button" onclick="removeLine(${i})">×</button></td>
    </tr>`;
  });

  const useLogImputed = totalLogistics > 0;
  const effectiveLogistic = useLogImputed ? totalLogistics : logisticCost;
  const totalSaleFinal = useLogImputed ? totalSale : totalSale + logisticCost;
  const totalCostFinal = useLogImputed ? totalCost : totalCost + logisticCost;
  const productsOnlyCost = useLogImputed ? totalCost - totalLogistics : totalCost;
  const marginEur = totalSaleFinal - totalCostFinal;
  const salesProductsOnly = totalSaleFinal - effectiveLogistic;
  const marginOnProductPct = salesProductsOnly > 0 ? ((salesProductsOnly - productsOnlyCost) / salesProductsOnly * 100) : 0;
  const totalSaleWithLogistic = totalSaleFinal;

  document.getElementById('subtotalProducts').textContent = fmtN(productsOnlyCost);
  const wpct = (wastePct * 100).toFixed(0);
  document.getElementById('wasteLabel').textContent = wastePct > 0 ? '(+' + wpct + '% desperdicio)' : '';
  document.getElementById('logisticTotal').textContent = fmtN(effectiveLogistic);
  document.getElementById('costProduct').textContent = fmtN(totalCostFinal);
  document.getElementById('marginEur').textContent = fmtN(marginEur);
  if (useLogImputed) {
    const perCont = containerCount > 0 ? (totalLogistics / containerCount) : 0;
    document.getElementById('logisticCostDisplay').textContent =
      fmtN(perCont) + ' €/cont × ' + containerCount + ' = € ' + fmtN(totalLogistics);
  }
  document.getElementById('marginPctLabel').textContent = marginOnProductPct.toFixed(0);
  document.getElementById('totalEur').textContent = fmtN(totalSaleWithLogistic);
  document.getElementById('totalUsd').textContent = fmtN(totalSaleWithLogistic * fx);

  renderCompetitionPanel(lines, wastePct, fx);
  renderPreflightChecks(incoterm, useLogImputed, effectiveLogistic, containerCount);
}

/**
 * Muestra advertencias pre-vuelo antes de generar oferta
 */
function renderPreflightChecks(incoterm, useLogImputed, effectiveLogistic, containerCount) {
  const warns = [];
  const globalMargin = parseFloat(document.getElementById('marginInput').value) || 0;
  if (lines.length === 0) {
    document.getElementById('preflightWarnings').style.display = 'none';
    return;
  }
  if (globalMargin > 0 && globalMargin < 5) {
    warns.push(`Margen base muy bajo (${globalMargin}%). Verifica que es intencional.`);
  }
  lines.forEach((l, i) => {
    if (!l.qty || l.qty <= 0) {
      warns.push(`Línea ${i+1} (${l.sku || '—'}): cantidad 0.`);
    }
    if (l.margin !== undefined && l.margin !== '' && l.margin < 5) {
      warns.push(`Línea ${i+1} (${l.sku || '—'}): margen ${l.margin}% — confirmar que es correcto.`);
    }
  });
  if (incoterm !== 'EXW' && !useLogImputed && effectiveLogistic <= 0) {
    warns.push(`Incoterm ${incoterm} pero flete 0€. Pulsa "🔄 Calcular logística" o introduce coste manual.`);
  }
  if (incoterm !== 'EXW' && containerCount <= 0) {
    warns.push(`Incoterm ${incoterm} sin contenedores definidos. Pulsa "🔄 Calcular logística".`);
  }
  const warnDiv = document.getElementById('preflightWarnings');
  if (warns.length === 0) {
    warnDiv.style.display = 'none';
  } else {
    warnDiv.innerHTML = '<b>⚠ Antes de emitir, revisa:</b><ul style="margin:4px 0 0 18px; padding:0;">'
      + warns.map(w => `<li>${escapeHtml(w)}</li>`).join('') + '</ul>';
    warnDiv.style.display = 'block';
  }
}

/**
 * Renderiza panel de análisis de competencia (precios en USD)
 */
function renderCompetitionPanel(lines, wastePct, fx) {
  const body = document.getElementById('compBody');
  body.innerHTML = '';
  let sumSaleUsd = 0, sumCompUsd = 0, skusBelow = 0, skusAbove = 0, skusSet = 0;
  lines.forEach((line, i) => {
    const qtyW = Math.ceil(line.qty * (1 + wastePct));
    const logPerUnit = parseFloat(line.logUnitCost) || 0;
    const costProduct = line.price * qtyW;
    const m = (line.margin || 0) / 100;
    const saleProduct = m < 1 ? costProduct / (1 - m) : costProduct;
    const lineSaleEur = saleProduct + (logPerUnit * qtyW);
    const lineSaleUsd = lineSaleEur * fx;
    const unitSaleUsd = qtyW > 0 ? (lineSaleUsd / qtyW) : 0;
    const compEur = parseFloat(line.competitorPrice) || 0;
    const compUsd = compEur * fx;
    const compLineUsd = compUsd * qtyW;
    const difUnitUsd = compUsd > 0 ? (compUsd - unitSaleUsd) : null;
    const difPct = compUsd > 0 ? ((compUsd - unitSaleUsd) / compUsd * 100) : null;
    sumSaleUsd += lineSaleUsd;
    sumCompUsd += compLineUsd;
    if (compUsd > 0) {
      skusSet++;
      if (difUnitUsd > 0) skusBelow++;
      else if (difUnitUsd < 0) skusAbove++;
    }
    const color = difUnitUsd == null ? '#888' : (difUnitUsd >= 0 ? '#060' : '#a23');
    const arrow = difUnitUsd == null ? '' : (difUnitUsd >= 0 ? ' ▼' : ' ▲');
    body.innerHTML += `<tr>
      <td class="mono">${escapeHtml(line.sku)}</td>
      <td class="num">${fmtN(unitSaleUsd)}</td>
      <td class="num"><input type="number" value="${compUsd > 0 ? compUsd.toFixed(4) : ''}" min="0" step="0.01"
          placeholder="—" style="width:90px; text-align:right;"
          onchange="updateLineCompetitor(${i}, this.value)"></td>
      <td class="num" style="color:${color};">${difUnitUsd == null ? '—' : fmtN(difUnitUsd) + arrow}</td>
      <td class="num" style="color:${color};">${difPct == null ? '—' : difPct.toFixed(1) + '%'}</td>
      <td class="num">${fmtN(lineSaleUsd)}</td>
      <td class="num">${compLineUsd > 0 ? fmtN(compLineUsd) : '—'}</td>
    </tr>`;
  });
  const totalDifUsd = sumCompUsd - sumSaleUsd;
  const totalDifPct = sumCompUsd > 0 ? (totalDifUsd / sumCompUsd * 100) : null;
  const tColor = totalDifUsd >= 0 ? '#060' : '#a23';
  document.getElementById('compTotalSale').textContent = '$ ' + fmtN(sumSaleUsd);
  document.getElementById('compTotalComp').textContent = sumCompUsd > 0 ? ('$ ' + fmtN(sumCompUsd)) : '—';
  document.getElementById('compTotalDifUnit').textContent = sumCompUsd > 0 ? ('$ ' + fmtN(totalDifUsd)) : '—';
  document.getElementById('compTotalDifUnit').style.color = sumCompUsd > 0 ? tColor : '';
  document.getElementById('compTotalDifPct').textContent = totalDifPct == null ? '—' : totalDifPct.toFixed(1) + '%';
  document.getElementById('compTotalDifPct').style.color = sumCompUsd > 0 ? tColor : '';
  const summary = document.getElementById('compSummary');
  if (skusSet === 0) {
    summary.textContent = '(sin datos de competencia — rellena precios en USD para ver el análisis)';
  } else {
    summary.textContent = `(${skusSet} SKUs · ${skusBelow} bajo · ${skusAbove} sobre · dif total ${totalDifPct == null ? '—' : totalDifPct.toFixed(1) + '%'})`;
  }
}

/**
 * Actualiza precio de competencia para una línea (en EUR)
 */
function updateLineCompetitor(i, val) {
  const fx = parseFloat(document.getElementById('fxInput').value) || 1.18;
  const v = parseFloat(val);
  lines[i].competitorPrice = isNaN(v) || v <= 0 ? 0 : v / fx;
  recalcAll();
}

/**
 * Valida datos de la oferta antes de generar
 * @returns {{valid: boolean, errors: string[]}}
 */
function validateOffer() {
  const errors = [];
  
  // Cliente requerido
  const clientSel = document.getElementById('clientSelect');
  if (!clientSel.value) {
    errors.push('Selecciona un cliente');
  }
  
  // Al menos una línea
  if (lines.length === 0) {
    errors.push('Añade al menos un producto');
  }
  
  // Validar cada línea
  lines.forEach((l, i) => {
    if (!l.sku) {
      errors.push(`Línea ${i+1}: SKU requerido`);
    }
    if (!l.qty || l.qty <= 0) {
      errors.push(`Línea ${i+1} (${l.sku || '?'}): cantidad inválida`);
    }
    if (!l.price || l.price <= 0) {
      errors.push(`Línea ${i+1} (${l.sku || '?'}): precio inválido`);
    }
    if (l.margin < 0 || l.margin > 100) {
      errors.push(`Línea ${i+1} (${l.sku || '?'}): margen debe estar entre 0-100%`);
    }
  });
  
  // Validar parámetros globales
  const wastePct = parseFloat(document.getElementById('wasteInput').value) || 0;
  if (wastePct < 0 || wastePct > 50) {
    errors.push('Desperdicio debe estar entre 0-50%');
  }
  
  const marginPct = parseFloat(document.getElementById('marginInput').value) || 0;
  if (marginPct < 0 || marginPct > 100) {
    errors.push('Margen base debe estar entre 0-100%');
  }
  
  const fx = parseFloat(document.getElementById('fxInput').value) || 0;
  if (fx <= 0 || fx > 10) {
    errors.push('FX EUR/USD inválido');
  }
  
  // Validar logística si incoterm no es EXW
  const incoterm = document.getElementById('incotermSelect').value;
  if (incoterm !== 'EXW') {
    const containers = parseInt(document.getElementById('containerCount').value) || 0;
    if (containers <= 0) {
      errors.push(`Incoterm ${incoterm}: calcula contenedores primero`);
    }
  }
  
  return {
    valid: errors.length === 0,
    errors: errors
  };
}

/**
 * Genera oferta y la envía al backend
 */
function generateOffer() {
  // Validación previa
  const validation = validateOffer();
  if (!validation.valid) {
    const errorMsg = '⚠ Errores en la oferta:\n\n' + validation.errors.map(e => '• ' + e).join('\n');
    alert(errorMsg);
    return;
  }
  
  const clientSel = document.getElementById('clientSelect');
  if (!clientSel.value) { alert('Selecciona un cliente.'); return; }
  const clientOpt = clientSel.options[clientSel.selectedIndex];
  const client = clientOpt.dataset.name || clientOpt.textContent;

  if (lines.length === 0) { alert('Añade al menos un producto.'); return; }
  const projSel = document.getElementById('projectSelect');
  const projOpt = projSel.options[projSel.selectedIndex];
  const project = (projOpt && projOpt.value) ? projOpt.dataset.name : 'Proyecto sin nombre';
  const offerNum = document.getElementById('offerNumber').value || '';

  const _wg = parseFloat(document.getElementById('wasteInput').value);
  const wastePct = isNaN(_wg) ? 5 : _wg;
  const _mg = parseFloat(document.getElementById('marginInput').value);
  const margin = isNaN(_mg) ? 20 : _mg;
  const fx = parseFloat(document.getElementById('fxInput').value) || 1.18;
  const incoterm = document.getElementById('incotermSelect').value;

  const logisticCost = parseFloat(document.getElementById('logisticTotal').textContent.replace(/\./g,'').replace(',','.')) || 0;
  const containers = parseInt(document.getElementById('containerCount').value) || 0;
  const routeId = document.getElementById('routeSelect').value || null;

  const wastePctDecimal = wastePct / 100;
  let totalPallets = 0;
  let totalWeight = 0;
  lines.forEach(line => {
    const qtyWithWaste = Math.ceil(line.qty * (1 + wastePctDecimal));
    const prod = window.products.find(p => p.sku === line.sku);
    if (prod) {
      const upp = prod.units_per_pallet || 0;
      if (upp > 0) totalPallets += qtyWithWaste / upp;
      totalWeight += qtyWithWaste * (prod.kg_per_unit || 0);
    }
  });

  const editId = document.getElementById('editOfferId').value;
  const _vd = parseInt(document.getElementById('validityInput').value);
  const validityDays = isNaN(_vd) || _vd < 1 ? 30 : _vd;
  const offer = {
    client, project, offerNumber: offerNum,
    wastePct, margin, fx, incoterm, validityDays,
    logisticCost, containerCount: containers, routeId,
    totalPallets: Math.ceil(totalPallets),
    totalWeight: Math.round(totalWeight),
    lines: lines.map(l => ({ sku: l.sku, name: l.name, family: l.family, unit: l.unit, price: l.price, qty: l.qty, margin: l.margin, log_unit_cost: l.logUnitCost || 0, log_cost_manual: !!l.logCostManual, competitor_price_eur: l.competitorPrice || 0 }))
  };
  if (editId) offer.editId = parseInt(editId);

  const url = editId ? '/api/update-full-offer' : '/api/save-offer';
  fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(offer)
  })
  .then(async (r) => {
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    if (!ct.includes('application/json')) {
      const body = await r.text();
      if (r.status === 401 || r.status === 403 || body.toLowerCase().includes('login')) {
        throw new Error('Sesión expirada o sin permisos. Recarga la página e inicia sesión de nuevo.');
      }
      throw new Error('El servidor devolvió HTML en lugar de JSON (status ' + r.status + ').');
    }
    const data = await r.json();
    if (!r.ok) {
      throw new Error(data.error || ('Error HTTP ' + r.status));
    }
    return data;
  })
  .then(data => {
    if (data.ok) {
      const action = editId ? 'actualizada' : 'generada';
      const assignedNum = data.offer_number || offerNum;
      document.getElementById('offerNumber').value = assignedNum;
      const banner = document.getElementById('offerBanner');
      banner.innerHTML = 'Oferta <b>' + escapeHtml(assignedNum) + '</b> ' + action + '. Total: <b>€ ' + document.getElementById('totalEur').textContent + '</b>. <a href="/presupuestos">Ver presupuestos</a>';
      banner.style.display = 'block';
      if (editId) {
        document.getElementById('editOfferId').value = '';
        document.getElementById('editBanner').style.display = 'none';
      }
    } else {
      alert('Error: ' + data.error);
    }
  })
  .catch(err => alert('Error: ' + err.message));
}

/**
 * Carga oferta en modo edición
 */
function loadEditOffer(editData) {
  document.getElementById('editOfferId').value = editData.id;
  document.getElementById('editOfferNum').textContent = editData.offer_number;
  document.getElementById('editBanner').style.display = 'block';
  document.getElementById('offerNumber').value = editData.offer_number || '';
  document.getElementById('wasteInput').value = editData.waste_pct || 0;
  document.getElementById('marginInput').value = editData.margin_pct || 20;
  if (editData.validity_days) document.getElementById('validityInput').value = editData.validity_days;
  if (editData.fx_rate) {
    document.getElementById('fxInput').value = editData.fx_rate;
    checkFxWarning();
  }
  if (editData.incoterm) {
    document.getElementById('incotermSelect').value = editData.incoterm;
    onIncotermChange();
  }
  if (editData.container_count) document.getElementById('containerCount').value = editData.container_count;
  (editData.lines || []).forEach(l => {
    lines.push({
      sku: l.sku, name: l.name, family: l.family, unit: l.unit,
      price: l.price, qty: l.qty, margin: l.margin || editData.margin_pct || 20,
      logUnitCost: l.log_unit_cost || 0,
      logCostManual: !!l.log_cost_manual,
      competitorPrice: l.competitor_price_eur || 0,
    });
  });
  const clientSel = document.getElementById('clientSelect');
  for (let i = 0; i < clientSel.options.length; i++) {
    if (clientSel.options[i].dataset.name == editData.client_name) {
      clientSel.selectedIndex = i;
      onClientSelect();
      break;
    }
  }
}

// === INIT AUTOMÁTICO AL CARGAR DOM ===
document.addEventListener('DOMContentLoaded', function() {
  // Las variables globales se inyectan desde Jinja2 en el template
  initQuote({
    products: window.products || [],
    systems: window.systems || [],
    subfamilies: window.subfamilies || {},
    projects: window.allProjects || [],
    pallet_profiles: window.PALLET_PROFILES || {},
    container_40hc: window.CONTAINER_40HC || {},
    editOffer: window.editOfferData || null
  });
});
