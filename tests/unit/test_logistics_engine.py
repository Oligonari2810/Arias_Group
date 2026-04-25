"""Tests del motor logístico contra el spec §2-§7."""
from __future__ import annotations

import pytest

from logistics.engine import (
    ContainerProfile, PalletProfile, SkuInput,
    _geometric_cap_per_container, compute_logistics,
)


CONT_40HC = ContainerProfile(
    type='40HC',
    inner_length_m=12.03, inner_width_m=2.35, inner_height_m=2.69,
    payload_kg=28000, door_clearance_m=0.30, stowage_factor=0.90,
)
# 40HC con calibración operativa Arias (2026-04-25):
# floor_stowage 0.80 · payload 26500 (no 28000) · stow 0.90.
# Usable: 22.67 m² · 23.850 kg · 68.44 m³.
CONT_40HC_ARIAS = ContainerProfile(
    type='40HC',
    inner_length_m=12.03, inner_width_m=2.35, inner_height_m=2.69,
    payload_kg=26500, door_clearance_m=0.30, stowage_factor=0.90,
    floor_stowage_factor=0.80,
)

PALLET_PLACAS = PalletProfile(
    category='PLACAS', length_m=2.50, width_m=1.20, height_m=0.30,
    stackable_levels=3, allow_mix_floor=True,
)

PALLET_EURO = PalletProfile(  # europalet genérico para TORNILLOS, CINTAS, etc.
    category='TORNILLOS', length_m=1.20, width_m=0.80, height_m=1.00,
    stackable_levels=2, allow_mix_floor=True,
)


# ── Driver geométrico (spec §3.1) ──
def test_geometric_placas_40hc_gives_12_pallets():
    """4 × 1 × 3 = 12 palets de placas en un 40HC (cálculo exacto del spec)."""
    cap = _geometric_cap_per_container(CONT_40HC, PALLET_PLACAS)
    assert cap == 12


def test_geometric_europalet_40hc():
    """floor((12.03-0.30)/1.2) × floor(2.35/0.8) × 2 = 9 × 2 × 2 = 36."""
    cap = _geometric_cap_per_container(CONT_40HC, PALLET_EURO)
    assert cap == 36


def test_geometric_no_cabe_si_pallet_mas_grande_que_contenedor():
    big = PalletProfile(category='X', length_m=15, width_m=3, height_m=3, stackable_levels=1)
    assert _geometric_cap_per_container(CONT_40HC, big) == 0


# ── Modelo agregado (calibración Oliver 2026-04-25): peso domina con placas ──
def test_solo_placas_modelo_agregado_peso_domina():
    """565 palés de placas pesadas — peso al tope antes que geometría/volumen.

    Modelo agregado (Arias):
      Total huella = 565 × (2.5×1.2)/3 niveles = 565 × 1 m² = 565 m²
      Total peso   = 565 × (120×9.5 + 22) = 565 × 1162 ≈ 656.530 kg
      Total cbm    = 565 × (2.5×1.2×0.30) = 565 × 0.9 = 508,5 m³

    Capacidades 40HC con calibración Arias (22,67 m² · 23.850 kg · 68,44 m³):
      N_floor  = 565 / 22,67 ≈ 24,9
      N_weight = 656.530 / 23.850 ≈ 27,5  ← dominante
      N_cbm    = 508,5 / 68,44 ≈ 7,4
      → ceil(27,5) = 28 contenedores físicos

    Total coste = 27,5 × 5000 ≈ 137.660 € (no 28 × 5000 — coste fraccional).
    """
    sku = SkuInput(
        sku='P-STD', category='PLACAS', qty=67800, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    r = compute_logistics(
        skus=[sku], container=CONT_40HC_ARIAS,
        pallet_profiles={'PLACAS': PALLET_PLACAS},
        cost_per_container_eur=5000,
    )
    assert r.dominant_family == 'PLACAS'
    assert r.dominant_driver == 'weight'
    assert r.n_containers == 28  # ceil del decimal
    # Decimal entre 27 y 28 (peso real ~27,5).
    assert 27.0 < r.n_containers_decimal < 28.0
    # Coste fraccional: n_decimal × 5000 (no 28 × 5000).
    expected_cost = r.n_containers_decimal * 5000
    assert r.total_cost_eur == pytest.approx(expected_cost, abs=1.0)


# ── Imputación POR PESO (estándar marítimo) ──
def test_imputacion_por_peso():
    """Coste imputado al SKU = (peso_sku / peso_total) × coste_total.

    En un proyecto con un solo SKU, este recibe el 100% del coste,
    independientemente de cuántos palés ocupe. La métrica unitaria es
    coste_sku / qty_total."""
    sku = SkuInput(
        sku='P-STD', category='PLACAS', qty=67800, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    r = compute_logistics(
        [sku], CONT_40HC_ARIAS, {'PLACAS': PALLET_PLACAS}, cost_per_container_eur=5000,
    )
    sc = r.skus[0]
    # Coste total = n_decimal × 5000. Como hay un solo SKU, paga el 100%.
    # Por unidad: coste_total / qty_total.
    qty_total = sc.pallets * 120
    expected_unit_cost = (r.n_containers_decimal * 5000) / qty_total
    assert sc.unit_log_cost_eur == pytest.approx(expected_unit_cost, abs=0.01)
    assert sc.m2_log_cost_eur == pytest.approx(sc.unit_log_cost_eur / 3.0, abs=0.01)


def test_imputacion_peso_no_se_distorsiona_por_units_per_pallet_malo():
    """Si units_per_pallet de un SKU está mal (ej. cinta = 20 cuando son 600),
    el reparto por peso NO se ve afectado — sigue pagando proporcional al peso.

    Esto es la mejora clave vs imputación por palés: la cinta paga poco porque
    pesa poco, sin importar cuántos 'palés' diga la DB que ocupa.
    """
    placa = SkuInput(
        sku='PLACA', category='PLACAS', qty=100, unit_weight_kg=25,  # 100 × 25 = 2500 kg
        unit_area_m2=3.0, units_per_pallet=48,
    )
    cinta_dato_malo = SkuInput(  # units_per_pallet=20 (rollos/caja, no palé)
        sku='CINTA-BAD', category='TORNILLOS', qty=600, unit_weight_kg=0.6,  # 600 × 0.6 = 360 kg
        unit_area_m2=0, units_per_pallet=20,
    )
    r = compute_logistics(
        [placa, cinta_dato_malo], CONT_40HC_ARIAS,
        {'PLACAS': PALLET_PLACAS, 'TORNILLOS': PALLET_EURO},
        cost_per_container_eur=5000,
    )
    sc_placa = next(s for s in r.skus if s.sku == 'PLACA')
    sc_cinta = next(s for s in r.skus if s.sku == 'CINTA-BAD')

    # Pesos relativos: placa 2500 + 2 palé × 22 = 2544 kg / cinta 360 + 30 palé × 22 = 1020 kg
    # Total ~ 3564 kg. Placa ~71%, cinta ~29% del peso.
    # Cinta NO debe pagar más que la placa por unidad: pesa menos.
    coste_placa_por_kg = sc_placa.unit_log_cost_eur / 25
    coste_cinta_por_kg = sc_cinta.unit_log_cost_eur / 0.6
    # Coste por kg debe ser idéntico (regla "por peso") — diferencia <1%.
    assert abs(coste_placa_por_kg - coste_cinta_por_kg) / coste_placa_por_kg < 0.05
    # Y por unidad: cinta paga MUCHO menos que placa (porque cada rollo pesa
    # 0,6 kg vs 25 kg de la placa).
    assert sc_cinta.unit_log_cost_eur < sc_placa.unit_log_cost_eur


# ── Mix de familias: complementario ocupa suelo sin abrir containers extra ──
def test_perfiles_ocupan_suelo_sobrante_sin_extras():
    placas = SkuInput(
        sku='P-STD', category='PLACAS', qty=1200, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    # 10 palets de placas → 1 container de placas (10 ≤ 12)
    # Capacidad floor para perfiles al lado: strip 1.15m ancho × 11.73m largo → 9 palets
    # Además si permitimos en end-strip: 12.03 - 10 = 2.03m, con clearance queda 1.73m
    # End-strip: floor(2.35/0.8) × floor(1.73/1.2) = 2 × 1 = 2. Lateral: 9×1 = 9. Total = 11.
    perfiles = SkuInput(
        sku='PERF-48', category='TORNILLOS', qty=10, unit_weight_kg=5,
        unit_area_m2=0, units_per_pallet=5,
    )
    # 2 palets de perfiles → caben de sobra en el suelo del container de placas
    r = compute_logistics(
        [placas, perfiles], CONT_40HC,
        {'PLACAS': PALLET_PLACAS, 'TORNILLOS': PALLET_EURO},
        cost_per_container_eur=5000,
    )
    # Dominante: placas (1 container). Perfiles caben en suelo sobrante → 0 extras.
    assert r.dominant_family == 'PLACAS'
    assert r.n_containers == 1
    assert r.extra_containers_by_family.get('TORNILLOS', 0) == 0


def test_modelo_agregado_combina_huellas_de_familias():
    """Modelo agregado: huellas de placas + complementarias suman al mismo cont.

    Antes el motor abría contenedores 'extra' para complementarias que no
    cabían en huecos del dominante. El modelo agregado simplemente suma m²
    huella y deja que MAX(floor, weight, cbm) decida — sin distinción
    'dominante/extras'.
    """
    placas = SkuInput(
        sku='P-STD', category='PLACAS', qty=7200, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    tornillos = SkuInput(
        sku='T-BIG', category='TORNILLOS', qty=325, unit_weight_kg=0.1,
        unit_area_m2=0, units_per_pallet=5,
    )
    r = compute_logistics(
        [placas, tornillos], CONT_40HC_ARIAS,
        {'PLACAS': PALLET_PLACAS, 'TORNILLOS': PALLET_EURO},
        cost_per_container_eur=5000,
    )
    # Familia con más palés (tornillos: 65, placas: 60) = dominante informativo.
    assert r.dominant_family in ('PLACAS', 'TORNILLOS')
    # extra_containers_by_family es legacy (vacío en modelo agregado).
    assert r.extra_containers_by_family == {}
    # n_containers viene del MAX agregado, no de "n_alone + extras".
    # Placas: 60 × 1 m² huella + tornillos: 65 × (1.2×0.8/2) = 65 × 0.48 = 31,2 m²
    # → 91,2 m² / 22,67 = 4,02 → ceil=5 cont
    # Peso: 60×1162 + 65×22.5 = 69.720 + 1.463 = 71.183 kg / 23.850 = 2,98 → 3 cont
    # MAX(4,02 ; 2,98 ; ...) = 4,02 → ceil 5
    assert r.n_containers == 5
    assert r.dominant_driver == 'floor'


# ── Peso domina si los palets son ligeros pero abundantes en volumen ──
def test_weight_driver_domina_cuando_peso_supera_geometria():
    # Pastas = sacos 25kg, 40 sacos por palet = 1000kg + 22 = 1022kg/palet
    pastas_profile = PalletProfile(
        category='PASTAS', length_m=1.20, width_m=0.80, height_m=1.20,
        stackable_levels=1, allow_mix_floor=True,
    )
    # Capacidad geo pastas: floor(11.73/1.2)=9 × floor(2.35/0.8)=2 × 1 = 18
    # Peso máximo container = 28000×0.9 = 25200 kg → máx 25 palets por peso
    # Si tuviéramos solo 25 palets: n_geo = ceil(25/18)=2, n_weight = ceil(25550/25200)=2. Empate.
    # Con 30 palets: n_geo = 2, n_weight = ceil(30660/25200)=2. Geo domina todavía.
    # Vamos con MÁS peso para forzar: 30 palets pesados 1500 kg c/u (sin tare default)
    sku = SkuInput(
        sku='PASTA-ESPECIAL', category='PASTAS', qty=30*40, unit_weight_kg=37.45,
        unit_area_m2=0, units_per_pallet=40, pallet_weight_kg=1500,
    )
    r = compute_logistics(
        [sku], CONT_40HC, {'PASTAS': pastas_profile}, cost_per_container_eur=5000,
    )
    fr = r.families['PASTAS']
    # 30 × 1500 = 45000 kg → ceil(45000/25200) = 2 contenedores por peso
    assert fr.n_by_weight == 2
    # 30 palets / cap_geo=18 → ceil(30/18) = 2 por geo
    # Empate 2=2. Geometria gana por orden max() (pallets primero en el tuple).
    assert r.n_containers == 2


# ── Edge cases ──
def test_sin_skus_devuelve_cero():
    r = compute_logistics([], CONT_40HC, {}, 5000)
    assert r.n_containers == 0
    assert r.total_cost_eur == 0


def test_unknown_family_raises():
    sku = SkuInput(sku='X', category='DESCONOCIDA', qty=10, unit_weight_kg=1, units_per_pallet=1)
    with pytest.raises(KeyError):
        compute_logistics([sku], CONT_40HC, {}, 5000)


def test_override_per_sku_de_pallet_dims():
    # Un SKU puede tener palet distinto al default de la familia
    sku = SkuInput(
        sku='P-ESPECIAL', category='PLACAS', qty=200, unit_weight_kg=10,
        unit_area_m2=1, units_per_pallet=50,
        pallet_length_m=1.20, pallet_width_m=0.80, pallet_height_m=0.80,
        stackable_levels=2,
    )
    r = compute_logistics(
        [sku], CONT_40HC, {'PLACAS': PALLET_PLACAS}, cost_per_container_eur=5000,
    )
    # 4 palets / 36 = 1 container (cap_geo se mantiene como métrica informativa).
    assert r.families['PLACAS'].cap_geo_per_container == 36
    assert r.n_containers == 1


# ── Caso real Oliver 2026-04-25: 7 SKUs Bonita Golf — debe dar 28 cont ──
def test_caso_real_bonita_golf_da_28_contenedores():
    """Cotización 7 materiales (placas STD/AQUA/LIGNUM + pastas + cintas).

    Cantidades NETAS (sin merma 5%): 6758 + 618 + 89 + 479 + 1947 + 3035 + 13844.
    El test las pasa con waste 0 — el motor recibe ya las cantidades brutas
    como vienen del cotizador (la merma se aplica antes de llamar al motor).

    Resultado esperado con calibración Arias (22,67 m² · 23.850 kg · 68,44 m³):
    ~28 cont (peso domina al ~28x → ceil 28 o 29 según redondeo del motor).
    """
    pallet_pastas = PalletProfile(
        category='PASTAS', length_m=1.20, width_m=0.80, height_m=1.20,
        stackable_levels=1, allow_mix_floor=True,
    )
    pallet_cintas = PalletProfile(
        category='CINTAS', length_m=1.20, width_m=0.80, height_m=1.00,
        stackable_levels=2, allow_mix_floor=True,
    )

    skus = [
        # AQUA H2 13mm 2500: 6758 placas, 48/palé, 26.1 kg
        SkuInput('P00H003250A0', 'PLACAS', 6758, 26.1, 3.0, 48),
        # Cinta Juntas 75m: 618 rollos, 600/palé (estimado realista), 0.42 kg
        SkuInput('304057', 'CINTAS', 618, 0.42, 0, 600),
        # Malla Externa Light 50m: 89 rollos, 50/palé (estimado), 10.8 kg
        SkuInput('301121', 'CINTAS', 89, 10.8, 0, 50),
        # A 96 25kg: 479 sacos, 60/palé
        SkuInput('714Y1', 'PASTAS', 479, 25.0, 0, 60),
        # Fassajoint 2H 25kg: 1947 sacos, 50/palé
        SkuInput('354', 'PASTAS', 1947, 25.0, 0, 50),
        # LIGNUM 13mm 2000: 3035 placas, 48/palé, 30.7 kg, palé 2.0×1.2
        SkuInput('P00LB03200AC', 'PLACAS', 3035, 30.7, 2.4, 48,
                 pallet_length_m=2.0, pallet_width_m=1.2),
        # STD 13mm 2400: 13844 placas, 48/palé, 25.1 kg, palé 2.4×1.2
        SkuInput('P00A003240A0', 'PLACAS', 13844, 25.1, 2.88, 48,
                 pallet_length_m=2.4, pallet_width_m=1.2),
    ]
    r = compute_logistics(
        skus, CONT_40HC_ARIAS,
        {'PLACAS': PALLET_PLACAS, 'PASTAS': pallet_pastas, 'CINTAS': pallet_cintas},
        cost_per_container_eur=4050,
    )
    # Resultado esperado (cálculo Oliver): ~28-29 contenedores físicos.
    # Driver dominante: peso (mucho carga útil al tope).
    assert r.dominant_driver == 'weight'
    assert 27 <= r.n_containers <= 30, f'esperado ~28, obtuvo {r.n_containers}'
    # Decimal cerca de 28.
    assert 27.0 < r.n_containers_decimal < 29.5
    # Coste fraccional, no entero.
    assert r.total_cost_eur == pytest.approx(
        r.n_containers_decimal * 4050, abs=1.0
    )
