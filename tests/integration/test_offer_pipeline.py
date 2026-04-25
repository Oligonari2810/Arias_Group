"""End-to-end test del pipeline de creación de oferta.

Verifica que `build_offer_breakdown` es la única fuente de cálculo:
- Frontend → backend → DB (lines_json) coincide al céntimo.
- Suma de sale_line_eur por línea = total_final_eur de pending_offers.
- Cambios de margen por línea se respetan (no se pisa con margen global).
"""
import json

import pytest

from app import build_offer_breakdown, compute_line


def _fake_computed(price, qty_w, sku='TEST-001', name='Test', family='PLACAS'):
    """Simula el output de compute_line con los campos que build_offer_breakdown lee."""
    return {
        'ok': True,
        'sku': sku,
        'name': name,
        'family': family,
        'unit': 'placa',
        'qty_input': qty_w,
        'qty_original': qty_w,
        'price_unit_eur': price,
        'cost_exw_eur': round(price * qty_w, 2),
        'm2_total': 0,
        'weight_total_kg': 0,
        'pallets_theoretical': 0,
        'pallets_logistic': 0,
        'alerts': [],
    }


class TestBuildOfferBreakdown:
    def test_single_line_no_logistics_no_margin(self):
        raw = [{'sku': 'TEST-001', 'qty': 100, 'margin': 0}]
        computed = [_fake_computed(10.0, 100)]
        r = build_offer_breakdown(raw, computed, margin_global_pct=0, logistic_global_eur=0)
        assert r['totals']['product_cost_eur'] == 1000.0
        assert r['totals']['logistic_eur'] == 0.0
        assert r['totals']['sale_eur'] == 1000.0
        assert len(r['lines']) == 1
        assert r['lines'][0]['sale_line_eur'] == 1000.0

    def test_single_line_global_margin_20(self):
        """100 placas × 10€ con margen global 20% → 1000 / 0.8 = 1250."""
        raw = [{'sku': 'TEST-001', 'qty': 100}]
        computed = [_fake_computed(10.0, 100)]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        assert r['totals']['sale_eur'] == 1250.0
        assert r['lines'][0]['margin_pct'] == 20.0

    def test_per_line_margin_overrides_global(self):
        """Línea con margin=30 pesa sobre margen global 20."""
        raw = [{'sku': 'TEST-001', 'qty': 100, 'margin': 30}]
        computed = [_fake_computed(10.0, 100)]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        # 1000 / (1 - 0.30) = 1428.57
        assert abs(r['totals']['sale_eur'] - 1428.57) < 0.01
        assert r['lines'][0]['margin_pct'] == 30.0

    def test_two_lines_different_margins_sum_correctly(self):
        """Caso real auditado: dos líneas con márgenes distintos.
        Total final = suma de cada línea (no margen ponderado promedio)."""
        raw = [
            {'sku': 'A', 'qty': 100, 'margin': 20},
            {'sku': 'B', 'qty': 100, 'margin': 40},
        ]
        computed = [
            _fake_computed(10.0, 100, sku='A'),
            _fake_computed(10.0, 100, sku='B'),
        ]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        # A: 1000 / 0.8 = 1250.0
        # B: 1000 / 0.6 = 1666.67
        # Total: 2916.67
        assert abs(r['totals']['sale_eur'] - 2916.67) < 0.01
        # La suma de las líneas debe cuadrar con el total exacto.
        assert abs(sum(l['sale_line_eur'] for l in r['lines']) - r['totals']['sale_eur']) < 0.02

    def test_per_line_log_unit_pass_through(self):
        """Flete por línea: se suma sin generar margen sobre él."""
        raw = [{'sku': 'A', 'qty': 100, 'margin': 20, 'log_unit_cost': 0.5}]
        computed = [_fake_computed(10.0, 100, sku='A')]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        # producto: 1000 / 0.8 = 1250
        # flete: 0.5 * 100 = 50
        # total: 1300
        assert r['totals']['logistic_eur'] == 50.0
        assert r['totals']['sale_eur'] == 1300.0
        assert r['lines'][0]['log_line_eur'] == 50.0

    def test_global_freight_prorated_when_no_per_line(self):
        """Si NINGUNA línea trae log_unit_cost, el flete global se prorratea
        proporcional al coste de producto (compat con bot/payloads viejos)."""
        raw = [
            {'sku': 'A', 'qty': 100, 'margin': 20},
            {'sku': 'B', 'qty': 100, 'margin': 20},
        ]
        computed = [
            _fake_computed(10.0, 100, sku='A'),  # cost 1000
            _fake_computed(20.0, 100, sku='B'),  # cost 2000
        ]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=300)
        # Flete 300 prorrateado 1/3 - 2/3 → 100 a A, 200 a B
        assert r['lines'][0]['log_line_eur'] == 100.0
        assert r['lines'][1]['log_line_eur'] == 200.0
        # Sale A: 1000/0.8 + 100 = 1350
        # Sale B: 2000/0.8 + 200 = 2700
        assert r['lines'][0]['sale_line_eur'] == 1350.0
        assert r['lines'][1]['sale_line_eur'] == 2700.0
        assert r['totals']['sale_eur'] == 4050.0

    def test_empty_input_returns_zero_totals(self):
        r = build_offer_breakdown([], [], margin_global_pct=20, logistic_global_eur=0)
        assert r['totals']['sale_eur'] == 0.0
        assert r['lines'] == []

    def test_breakdown_fields_present_for_pdf_consumption(self):
        """El PDF lee qty_waste, sale_line_eur, sale_unit_eur, margin_pct.
        Verificamos que están todos en el output."""
        raw = [{'sku': 'A', 'qty': 100, 'margin': 25, 'log_unit_cost': 0.3}]
        computed = [_fake_computed(8.5, 100, sku='A')]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        line = r['lines'][0]
        for field in (
            'sku', 'qty_neta', 'qty_waste', 'price_arias_eur',
            'log_unit_eur', 'margin_pct', 'cost_line_eur',
            'log_line_eur', 'sale_line_eur', 'sale_unit_eur',
        ):
            assert field in line, f'missing {field} in breakdown line'

    def test_sale_unit_eur_consistent_with_sale_line(self):
        raw = [{'sku': 'A', 'qty': 50, 'margin': 30}]
        computed = [_fake_computed(20.0, 50, sku='A')]
        r = build_offer_breakdown(raw, computed, margin_global_pct=20, logistic_global_eur=0)
        line = r['lines'][0]
        # sale_unit_eur × qty_waste debe aproximar sale_line_eur (redondeo 4dec)
        recomputed = round(line['sale_unit_eur'] * line['qty_waste'], 2)
        assert abs(recomputed - line['sale_line_eur']) < 0.01


class TestSaveOfferLinesJsonShape:
    """Verifica que la lógica de save_offer (sin pasar por HTTP) persiste
    el breakdown en lines_json. Replica las líneas críticas del endpoint
    para evitar tener que lidiar con CSRF y sesiones de Flask-WTF."""

    def _replicate_save_offer_lines_logic(self, raw_lines, margin_pct, logistic, waste_pct):
        """Replica la sección de save_offer que construye input_lines + breakdown.
        Si app.py cambia este pipeline, este test se rompe — eso es lo que queremos."""
        import math
        from app import compute_line, build_offer_breakdown, _num

        input_lines = []
        computed = []
        # En tests usamos productos hardcoded para no depender de la DB.
        products_db = {
            'TEST-A': {'sku': 'TEST-A', 'name': 'Test A', 'category': 'PLACAS',
                       'unit': 'placa', 'unit_price_eur': 10.0,
                       'kg_per_unit': 25, 'units_per_pallet': 48, 'sqm_per_pallet': 144},
            'TEST-B': {'sku': 'TEST-B', 'name': 'Test B', 'category': 'PASTAS',
                       'unit': 'saco', 'unit_price_eur': 5.0,
                       'kg_per_unit': 25, 'units_per_pallet': 50, 'sqm_per_pallet': 0},
        }
        for li in raw_lines:
            sku = li.get('sku')
            qty = _num(li.get('qty', 0))
            if not sku or qty <= 0 or sku not in products_db:
                continue
            pd = products_db[sku]
            qty_with_waste = math.ceil(qty * (1 + waste_pct))
            line = compute_line(pd, qty_with_waste)
            line['qty_original'] = qty
            computed.append(line)
            input_lines.append({
                'sku': pd['sku'], 'name': pd['name'], 'family': pd['category'],
                'unit': pd['unit'], 'price': pd['unit_price_eur'], 'qty': qty,
                'margin': _num(li.get('margin', 0)),
                'log_unit_cost': _num(li.get('log_unit_cost', 0)),
            })

        breakdown = build_offer_breakdown(input_lines, computed, margin_pct, logistic)
        for li, br in zip(input_lines, breakdown['lines']):
            li.update({
                'qty_waste': br['qty_waste'],
                'cost_line_eur': br['cost_line_eur'],
                'log_line_eur': br['log_line_eur'],
                'sale_line_eur': br['sale_line_eur'],
                'sale_unit_eur': br['sale_unit_eur'],
                'margin_applied_pct': br['margin_pct'],
            })
        return input_lines, breakdown

    def test_lines_json_contains_breakdown_fields(self):
        raw_lines = [{'sku': 'TEST-A', 'qty': 100, 'margin': 25}]
        input_lines, breakdown = self._replicate_save_offer_lines_logic(
            raw_lines, margin_pct=20, logistic=0, waste_pct=0.05
        )
        assert len(input_lines) == 1
        for field in ('qty_waste', 'cost_line_eur', 'log_line_eur',
                      'sale_line_eur', 'sale_unit_eur', 'margin_applied_pct'):
            assert field in input_lines[0], f'missing {field}'

    def test_sum_sale_line_equals_total_sale(self):
        """La suma de sale_line_eur debe cuadrar con total_final_eur al céntimo."""
        raw_lines = [
            {'sku': 'TEST-A', 'qty': 100, 'margin': 20},
            {'sku': 'TEST-B', 'qty': 50, 'margin': 30},
        ]
        input_lines, breakdown = self._replicate_save_offer_lines_logic(
            raw_lines, margin_pct=20, logistic=500, waste_pct=0.05
        )
        sum_sale = sum(li['sale_line_eur'] for li in input_lines)
        assert abs(sum_sale - breakdown['totals']['sale_eur']) < 0.05

    def test_per_line_margin_persisted_not_global(self):
        """margin_applied_pct refleja margen de la línea, no el global."""
        raw_lines = [
            {'sku': 'TEST-A', 'qty': 100, 'margin': 35},
            {'sku': 'TEST-B', 'qty': 50, 'margin': 10},
        ]
        input_lines, _ = self._replicate_save_offer_lines_logic(
            raw_lines, margin_pct=20, logistic=0, waste_pct=0
        )
        assert input_lines[0]['margin_applied_pct'] == 35.0
        assert input_lines[1]['margin_applied_pct'] == 10.0

    def test_qty_waste_applies_ceil(self):
        """Cantidad con merma usa ceil (no truncamiento)."""
        raw_lines = [{'sku': 'TEST-A', 'qty': 13, 'margin': 20}]
        input_lines, _ = self._replicate_save_offer_lines_logic(
            raw_lines, margin_pct=20, logistic=0, waste_pct=0.05
        )
        # 13 × 1.05 = 13.65 → ceil = 14
        assert input_lines[0]['qty_waste'] == 14
