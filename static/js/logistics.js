/**
 * logistics.js - Gestión Logística de Ofertas
 * Maneja cálculo de contenedores y confirmación de ofertas
 */

/**
 * Calcula coste logístico cuando se selecciona ruta o cambia nº contenedores
 */
function calcLogistics(el) {
  const card = el.closest('.card');
  const routeSel = card.querySelector('.route-sel');
  const containerInput = card.querySelector('.container-input');
  const logTotal = card.querySelector('.log-total');
  const finalTotal = card.querySelector('.final-total');
  const prodTotal = parseFloat(card.querySelector('[data-prod]').textContent) || 0;
  const containers = parseInt(containerInput.value) || 0;
  let freight = 0;
  if (routeSel.selectedIndex > 0 && containers > 0) {
    const opt = routeSel.options[routeSel.selectedIndex];
    const c40 = parseFloat(opt.dataset.c40) || 0;
    freight = containers * c40;
  }
  logTotal.textContent = freight.toFixed(2);
  finalTotal.textContent = '€ ' + (prodTotal + freight).toFixed(2);
}

/**
 * Confirma oferta y actualiza estado a 'confirmed'
 */
function confirmOffer(offerId) {
  const card = document.getElementById('offer-' + offerId);
  const logTotal = parseFloat(card.querySelector('.log-total').textContent) || 0;
  const prodTotal = parseFloat(card.querySelector('[data-prod]').textContent) || 0;
  fetch('/api/update-offer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      id: offerId,
      incoterm: card.querySelector('.incoterm-sel').value,
      route_id: card.querySelector('.route-sel').value || null,
      container_count: parseInt(card.querySelector('.container-input').value) || 0,
      logistic_cost: logTotal,
      final_total: prodTotal + logTotal,
      status: 'confirmed'
    })
  }).then(r => r.json()).then(d => { if (d.ok) location.reload(); });
}

/**
 * Elimina oferta
 */
function deleteOffer(offerId) {
  if (!confirm('¿Eliminar esta oferta?')) return;
  fetch('/api/delete-offer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: offerId})
  }).then(r => r.json()).then(d => { if (d.ok) location.reload(); });
}
