/**
 * config.js - Configuración y Tablas Maestras
 * Maneja edición y eliminación de rutas, aranceles y FX
 */

/**
 * Muestra/oculta elemento por ID
 */
function toggle(id) {
  var e = document.getElementById(id);
  e.style.display = e.style.display === 'none' ? 'block' : 'none';
}

/**
 * Abre formulario de edición de ruta con datos existentes
 */
function editRoute(id, carrier, origin, dest, c20, c40, c40hc) {
  const f = document.getElementById('routeForm');
  f.style.display = 'block';
  f.querySelector('[name=action]').value = 'update_route';
  f.querySelector('[name=carrier]').value = carrier;
  f.querySelector('[name=origin_port]').value = origin;
  f.querySelector('[name=destination_port]').value = dest;
  f.querySelector('[name=container_20_eur]').value = c20;
  f.querySelector('[name=container_40_eur]').value = c40;
  f.querySelector('[name=container_40hc_eur]').value = c40hc;
  if (!f.querySelector('[name=route_id]')) {
    var h = document.createElement('input');
    h.type = 'hidden';
    h.name = 'route_id';
    f.querySelector('form').appendChild(h);
  }
  f.querySelector('[name=route_id]').value = id;
  f.querySelector('button[type=submit]').textContent = 'Actualizar ruta';
}

/**
 * Elimina elemento (ruta, arancel, FX) vía API
 */
function deleteItem(type, id, label) {
  if (!confirm('¿Eliminar ' + label + '?')) return;
  fetch('/api/config-delete', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type: type, id: id})
  }).then(r => r.json()).then(data => {
    if (data.ok) location.reload();
    else alert(data.error || 'Error');
  });
}
