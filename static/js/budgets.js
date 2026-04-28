/**
 * budgets.js - Gestión de Presupuestos
 * Maneja aprobación, rechazo y eliminación de ofertas
 */

/**
 * Elimina presupuesto
 */
function deleteOffer(id) {
  if (!confirm('¿Eliminar este presupuesto?')) return;
  fetch('/api/delete-offer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id})
  }).then(r => r.json()).then(data => { if (data.ok) location.reload(); });
}

/**
 * Cambia estado de oferta (approved/rejected/pending)
 */
function changeStatus(id, status) {
  const labels = {approved: 'aprobar', rejected: 'rechazar', pending: 'volver a pendiente'};
  if (!confirm('¿Seguro que quieres ' + labels[status] + ' este presupuesto?')) return;
  fetch('/api/offer-status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: id, status: status})
  }).then(r => r.json()).then(data => {
    if (!data.ok) { alert(data.error || 'Error'); return; }
    // Si al aprobar se crearon preorden Fassa + orden logística, avisar.
    if (status === 'approved' && data.factory_order && data.factory_order.created) {
      alert('✓ Aprobada.\n\nGeneradas automáticamente:\n  🏭 ' +
            data.factory_order.name + ' (preorden Fassa)\n  🚢 ' +
            data.logistics_order.name + ' (orden logística)');
    }
    location.reload();
  });
}
