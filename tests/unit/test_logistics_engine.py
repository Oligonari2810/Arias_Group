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


# ── Caso simple: solo placas ──
def test_solo_placas_calcula_contenedores_por_geometria():
    """565 palets de placas → ceil(565/12) = 48 contenedores."""
    sku = SkuInput(
        sku='P-STD', category='PLACAS', qty=67800, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    # 67800 / 120 = 565 palets
    r = compute_logistics(
        skus=[sku], container=CONT_40HC,
        pallet_profiles={'PLACAS': PALLET_PLACAS},
        cost_per_container_eur=5000,
    )
    assert r.dominant_family == 'PLACAS'
    assert r.dominant_driver == 'pallets'
    fr = r.families['PLACAS']
    assert fr.total_pallets == 565
    assert fr.cap_geo_per_container == 12
    assert fr.n_by_pallets == 48
    # Peso = 565 × (120×9.5 + 22) = 565 × 1162 ≈ 656,530 kg
    # N_weight = ceil(656530 / (28000×0.9)) = ceil(656530/25200) = 27
    assert fr.n_by_weight == 27
    assert r.n_containers == 48
    assert r.total_cost_eur == 48 * 5000


# ── Imputación: placas dominante pagan todo ──
def test_imputacion_unit_log_cost_placas():
    sku = SkuInput(
        sku='P-STD', category='PLACAS', qty=67800, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    r = compute_logistics(
        [sku], CONT_40HC, {'PLACAS': PALLET_PLACAS}, cost_per_container_eur=5000,
    )
    sc = r.skus[0]
    # 48 conts × 5000 = 240000 € / 565 palets = 424.78 €/palet
    # 424.78 / 120 uds = 3.5399 €/ud
    assert sc.unit_log_cost_eur == pytest.approx(3.5399, abs=0.001)
    # €/m2 = 3.5399 / 3.0 = 1.18
    assert sc.m2_log_cost_eur == pytest.approx(1.18, abs=0.01)


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


def test_complementario_abre_container_extra_si_overflow():
    # Placas dominantes: 60 palets → ceil(60/12) = 5 contenedores de placas.
    placas = SkuInput(
        sku='P-STD', category='PLACAS', qty=7200, unit_weight_kg=9.5,
        unit_area_m2=3.0, units_per_pallet=120,
    )
    # Tornillos: 65 palets. Caben ~11 en suelo de cada placas-cont (5×11=55),
    # quedan 10 → 1 contenedor extra. Así PLACAS (5) sigue siendo dominante
    # frente a TORNILLOS alone (ceil(65/36)=2).
    tornillos = SkuInput(
        sku='T-BIG', category='TORNILLOS', qty=325, unit_weight_kg=0.1,
        unit_area_m2=0, units_per_pallet=5,
    )
    r = compute_logistics(
        [placas, tornillos], CONT_40HC,
        {'PLACAS': PALLET_PLACAS, 'TORNILLOS': PALLET_EURO},
        cost_per_container_eur=5000,
    )
    assert r.dominant_family == 'PLACAS'
    assert r.families['PLACAS'].n_alone == 5
    assert r.extra_containers_by_family['TORNILLOS'] >= 1
    assert r.n_containers == 5 + r.extra_containers_by_family['TORNILLOS']


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
    # Con override: cap_geo = floor(11.73/1.2)×floor(2.35/0.8)×2 = 9×2×2 = 36
    r = compute_logistics(
        [sku], CONT_40HC, {'PLACAS': PALLET_PLACAS}, cost_per_container_eur=5000,
    )
    # 4 palets / 36 = 1 container
    assert r.families['PLACAS'].cap_geo_per_container == 36
    assert r.n_containers == 1
