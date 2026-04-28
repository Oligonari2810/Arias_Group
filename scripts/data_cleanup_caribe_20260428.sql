-- ============================================
-- Limpieza de Catálogo para Caribe
-- Fecha: 2026-04-28
-- ============================================
-- Elimina productos no viables logísticamente
-- para distribución en el Caribe (contenedores)
-- ============================================

-- 1. Eliminar placas ≥ 2600mm (no caben en contenedor estándar)
-- Razón: Placas de 2600-3600mm exceden longitud de contenedor 40HC
DELETE FROM products 
WHERE category = 'PLACAS' AND length_mm >= 2600;

-- 2. Eliminar montantes ≥ 3590mm (demasiado largos para pared)
-- Razón: Perfiles > 3m no son viables para construcción Caribe
-- Excepción: Perfil TC 47 para techos (SKU C17%) se mantiene
DELETE FROM products 
WHERE category = 'PERFILES' 
  AND length_mm >= 3590
  AND sku NOT LIKE 'C17%';

-- 3. Reordenar subfamilias de PERFILES por orden constructivo
-- Orden: Montantes → Railes → Accesorios → Techos → Fijaciones
UPDATE products SET subfamily = '01_48/35' WHERE category = 'PERFILES' AND subfamily = '48/35';
UPDATE products SET subfamily = '02_70/37' WHERE category = 'PERFILES' AND subfamily = '70/37';
UPDATE products SET subfamily = '03_90/40' WHERE category = 'PERFILES' AND subfamily = '90/40';
UPDATE products SET subfamily = '04_Montante' WHERE category = 'PERFILES' AND subfamily = 'Montante';
UPDATE products SET subfamily = '05_Rail' WHERE category = 'PERFILES' AND subfamily = 'Rail';
UPDATE products SET subfamily = '06_Angular' WHERE category = 'PERFILES' AND subfamily = 'Angular';
UPDATE products SET subfamily = '07_Omega' WHERE category = 'PERFILES' AND subfamily = 'Omega';
UPDATE products SET subfamily = '08_TC 47' WHERE category = 'PERFILES' AND subfamily = 'TC 47';
UPDATE products SET subfamily = '09_Clip' WHERE category = 'PERFILES' AND subfamily = 'Clip';
UPDATE products SET subfamily = '10_Sierra' WHERE category = 'PERFILES' AND subfamily = 'Sierra';

-- 4. Actualizar FX rate a valor de mercado (Abril 2026)
-- Fuente: Tasa de mercado 11/04/2026 = 1.1725 USD/EUR
UPDATE fx_rates SET rate = 1.1725 WHERE base_currency = 'EUR' AND target_currency = 'USD';
UPDATE app_settings SET value = '1.1725' WHERE key = 'fx_eur_usd';

-- ============================================
-- Resultado:
-- - PLACAS: 56 → 24 (32 eliminados)
-- - PERFILES: 41 → 35 (8 eliminados)
-- - TOTAL: 209 → 171 SKUs (40 eliminados)
-- ============================================
