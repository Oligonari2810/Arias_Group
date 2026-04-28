/**
 * common.js - Utilidades Compartidas Arias Group
 * Funciones helper usadas en múltiples módulos
 */

/**
 * Formatea número con separadores de miles y decimales (ES)
 * @param {number} n - Número a formatear
 * @param {number} dec - Decimales (default 2)
 * @returns {string} Número formateado
 */
function fmtN(n, dec) {
  if (dec === undefined) dec = 2;
  return Number(n).toLocaleString('es-ES', {minimumFractionDigits: dec, maximumFractionDigits: dec});
}

/**
 * Formatea número entero sin decimales
 * @param {number} n - Número a formatear
 * @returns {string} Número formateado
 */
function fmtQ(n) {
  return Number(n).toLocaleString('es-ES', {maximumFractionDigits: 0});
}

/**
 * Escapa HTML para prevenir XSS
 * @param {string} s - String a escapar
 * @returns {string} String escapado
 */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[c]));
}

/**
 * Muestra/oculta elemento por ID
 * @param {string} id - ID del elemento
 */
function toggle(id) {
  var e = document.getElementById(id);
  if (e) e.style.display = e.style.display === 'none' ? 'block' : 'none';
}

/**
 * Obtiene valor de cookie por nombre
 * @param {string} name - Nombre de la cookie
 * @returns {string|null} Valor de la cookie o null
 */
function getCookie(name) {
  const value = '; ' + document.cookie;
  const parts = value.split('; ' + name + '=');
  if (parts.length === 2) return parts.pop().split(';').shift();
  return null;
}

/**
 * Configura CSRF token para fetch() - auto-inject en todos los requests
 * Usar en init de cada módulo que haga peticiones AJAX
 */
function setupCSRF() {
  const originalFetch = window.fetch;
  window.fetch = function(url, options) {
    options = options || {};
    const token = getCookie('csrf_token') || document.querySelector('meta[name=csrf-token]');
    if (!options.headers) options.headers = {};
    if (token && !options.headers['X-CSRFToken']) {
      options.headers['X-CSRFToken'] = typeof token === 'string' ? token : token.content;
    }
    return originalFetch.call(window, url, options);
  };
}

// Setup automático de CSRF al cargar
setupCSRF();
