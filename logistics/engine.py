"""Motor de cálculo logístico con drivers físicos.

Implementa el spec §2-§7:
  - Driver GEOMÉTRICO (packing 3D por familia)
  - Driver PESO (payload con stowage factor)
  - Driver VOLUMEN (safety check)
  - N_containers = max(N por cada driver)
  - Mix de familias complementarias en huecos de dominante
  - Imputación: paga quien abre contenedor

Pure Python — sin dependencias de Flask ni de la DB.
El caller (Fase C) carga ContainerProfile / PalletProfile desde la DB y los
pasa como dataclasses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# Fallback: europalet vacío pesa 22 kg (decisión operativa Arias).
# Se suma al peso bruto de mercancía cuando no hay pallet_weight_kg registrado.
EUROPALET_TARE_KG = 22.0


@dataclass(frozen=True)
class ContainerProfile:
    type: str                  # '20', '40', '40HC'
    inner_length_m: float
    inner_width_m: float
    inner_height_m: float
    payload_kg: float
    door_clearance_m: float = 0.30
    stowage_factor: float = 0.90  # 0.85-0.95; aplica a peso y volumen
    # Techo de carga geométrica del suelo en estiba real con palés de placa.
    # 0.80 (operativa Arias) = 80% del suelo aprovechable; 20% reservado para
    # sujeción, accesos y palés irregulares. Antes se asumía 1.0 (100%) lo
    # que sobreestimaba la capacidad geométrica.
    floor_stowage_factor: float = 1.0

    @property
    def usable_length_m(self) -> float:
        return self.inner_length_m - self.door_clearance_m

    @property
    def effective_payload_kg(self) -> float:
        return self.payload_kg * self.stowage_factor

    @property
    def effective_cbm(self) -> float:
        return (self.inner_length_m * self.inner_width_m * self.inner_height_m
                * self.stowage_factor)

    @property
    def usable_floor_m2(self) -> float:
        """Suelo aprovechable real para apilar palés (m²)."""
        return self.inner_length_m * self.inner_width_m * self.floor_stowage_factor


@dataclass(frozen=True)
class PalletProfile:
    category: str              # 'PLACAS', 'PERFILES', ...
    length_m: float
    width_m: float
    height_m: float            # altura cargada
    stackable_levels: int = 1
    allow_mix_floor: bool = True
    pallet_tare_kg: float = EUROPALET_TARE_KG

    @property
    def footprint_m2(self) -> float:
        return self.length_m * self.width_m


@dataclass(frozen=True)
class SkuInput:
    sku: str
    category: str              # debe matchear una PalletProfile
    qty: float
    unit_weight_kg: float
    unit_area_m2: float = 0.0  # si no aplica, 0
    units_per_pallet: float = 1
    # Overrides opcionales — si None, se usa el PalletProfile de la familia.
    pallet_length_m: float | None = None
    pallet_width_m: float | None = None
    pallet_height_m: float | None = None
    pallet_weight_kg: float | None = None    # bruto total (mercancía + tara)
    stackable_levels: int | None = None


@dataclass
class SkuComputed:
    sku: str
    category: str
    pallets: int
    weight_total_kg: float
    cbm_total: float
    unit_log_cost_eur: float = 0.0
    m2_log_cost_eur: float = 0.0


@dataclass
class FamilyResult:
    category: str
    total_pallets: int
    total_weight_kg: float
    total_cbm: float
    cap_geo_per_container: int          # palets/cont con driver geométrico
    n_by_pallets: int                   # N si solo este familia ocupara el cont
    n_by_weight: int
    n_by_cbm: int
    n_alone: int                        # = max(pallets, weight, cbm) driver dominante
    dominant_driver: str                # 'pallets' | 'weight' | 'cbm'


@dataclass
class LogisticsResult:
    container_type: str
    # Número de contenedores físicos a reservar (entero, ceil del decimal).
    n_containers: int
    # Número decimal "efectivo de carga" — usado para imputar coste. El cliente
    # paga n_containers_decimal × cost_per_container; la fracción restante hasta
    # n_containers (entero) la absorbe Arias o se rellena con otra carga.
    n_containers_decimal: float = 0.0
    total_cost_eur: float = 0.0  # = n_containers_decimal × cost_per_container
    dominant_family: str = ''
    # Driver dominante GLOBAL del cálculo agregado: 'floor' | 'weight' | 'cbm'
    dominant_driver: str = ''
    families: dict[str, FamilyResult] = field(default_factory=dict)
    skus: list[SkuComputed] = field(default_factory=list)
    extra_containers_by_family: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Capacidad libre POR CONTENEDOR (alerta de oportunidad cross-sell).
    free_weight_kg_per_cont: float = 0.0
    free_cbm_per_cont: float = 0.0
    free_floor_m2_per_cont: float = 0.0
    free_weight_kg_total: float = 0.0
    free_cbm_total: float = 0.0
    free_floor_m2_total: float = 0.0
    is_optimized: bool = False
    # Drivers desglosados (para diagnóstico y UI):
    n_by_floor: float = 0.0
    n_by_weight: float = 0.0
    n_by_cbm: float = 0.0
    total_floor_m2: float = 0.0
    total_weight_kg: float = 0.0
    total_cbm: float = 0.0


def _effective_pallet_profile(sku: SkuInput, family_profile: PalletProfile) -> PalletProfile:
    """Mezcla overrides del SKU con defaults de la familia."""
    return PalletProfile(
        category=family_profile.category,
        length_m=sku.pallet_length_m if sku.pallet_length_m is not None else family_profile.length_m,
        width_m=sku.pallet_width_m if sku.pallet_width_m is not None else family_profile.width_m,
        height_m=sku.pallet_height_m if sku.pallet_height_m is not None else family_profile.height_m,
        stackable_levels=sku.stackable_levels if sku.stackable_levels is not None else family_profile.stackable_levels,
        allow_mix_floor=family_profile.allow_mix_floor,
        pallet_tare_kg=family_profile.pallet_tare_kg,
    )


def _geometric_cap_per_container(container: ContainerProfile, pallet: PalletProfile) -> int:
    """Palets que caben en un contenedor por packing 3D estricto (sin mezcla).

    No considera posibilidad de girar el palé — se asume pallet.length_m en el
    eje largo del contenedor. Casos con rotación se modelan declarando la
    orientación más favorable en el PalletProfile.
    """
    positions_length = math.floor(container.usable_length_m / pallet.length_m)
    positions_width = math.floor(container.inner_width_m / pallet.width_m)
    max_levels_by_height = math.floor(container.inner_height_m / pallet.height_m) if pallet.height_m > 0 else 1
    levels = min(pallet.stackable_levels, max_levels_by_height) if pallet.stackable_levels > 0 else 1
    return max(0, positions_length * positions_width * levels)


def _compute_sku(sku: SkuInput, pallet: PalletProfile) -> SkuComputed:
    upp = sku.units_per_pallet if sku.units_per_pallet > 0 else 1
    pallets = math.ceil(sku.qty / upp)
    if sku.pallet_weight_kg is not None:
        weight_per_pallet = float(sku.pallet_weight_kg)
    else:
        weight_per_pallet = upp * sku.unit_weight_kg + pallet.pallet_tare_kg
    weight_total = pallets * weight_per_pallet
    cbm_per_pallet = pallet.length_m * pallet.width_m * pallet.height_m
    cbm_total = pallets * cbm_per_pallet
    return SkuComputed(
        sku=sku.sku,
        category=sku.category,
        pallets=pallets,
        weight_total_kg=round(weight_total, 2),
        cbm_total=round(cbm_total, 4),
    )


def _family_result(
    category: str,
    skus: list[SkuComputed],
    container: ContainerProfile,
    pallet: PalletProfile,
) -> FamilyResult:
    cap_geo = _geometric_cap_per_container(container, pallet)
    # Evita div/0: si por geometría no entra ningún palé, marcamos N muy alto.
    if cap_geo == 0:
        return FamilyResult(
            category=category,
            total_pallets=sum(s.pallets for s in skus),
            total_weight_kg=sum(s.weight_total_kg for s in skus),
            total_cbm=sum(s.cbm_total for s in skus),
            cap_geo_per_container=0,
            n_by_pallets=10**9,
            n_by_weight=10**9,
            n_by_cbm=10**9,
            n_alone=10**9,
            dominant_driver='unfeasible',
        )
    tot_pallets = sum(s.pallets for s in skus)
    tot_weight = sum(s.weight_total_kg for s in skus)
    tot_cbm = sum(s.cbm_total for s in skus)
    n_pal = math.ceil(tot_pallets / cap_geo) if tot_pallets > 0 else 0
    n_wt  = math.ceil(tot_weight / container.effective_payload_kg) if tot_weight > 0 else 0
    n_cbm = math.ceil(tot_cbm / container.effective_cbm) if tot_cbm > 0 else 0
    drivers = (('pallets', n_pal), ('weight', n_wt), ('cbm', n_cbm))
    dominant = max(drivers, key=lambda x: x[1])
    return FamilyResult(
        category=category,
        total_pallets=tot_pallets,
        total_weight_kg=round(tot_weight, 2),
        total_cbm=round(tot_cbm, 4),
        cap_geo_per_container=cap_geo,
        n_by_pallets=n_pal,
        n_by_weight=n_wt,
        n_by_cbm=n_cbm,
        n_alone=dominant[1],
        dominant_driver=dominant[0],
    )


def _floor_slots_for_complement(
    container: ContainerProfile,
    dominant_pallet: PalletProfile,
    complement_pallet: PalletProfile,
) -> int:
    """Palets del complementario que caben en el hueco de suelo libre.

    Modelo conservador: el dominante ocupa footprint rectangular completo y el
    hueco lateral + end-strip se evalúan con la geometría del complementario.
    Sin apilado de complementarios sobre dominante (el spec permite, lo dejamos
    como mejora futura si la UI quiere sobre-estimar).
    """
    if not dominant_pallet.allow_mix_floor or not complement_pallet.allow_mix_floor:
        return 0
    # Uso del suelo por el dominante (solo planta baja, nivel 1):
    dom_positions_length = math.floor(container.usable_length_m / dominant_pallet.length_m)
    dom_positions_width = math.floor(container.inner_width_m / dominant_pallet.width_m)
    # Strip lateral (ancho sobrante):
    strip_width = container.inner_width_m - (dom_positions_width * dominant_pallet.width_m)
    # End-strip (largo sobrante tras último palé dominante):
    end_length = container.usable_length_m - (dom_positions_length * dominant_pallet.length_m)
    # Palets complementarios en el strip lateral:
    lat_len_fits = math.floor(container.usable_length_m / complement_pallet.length_m) if strip_width >= complement_pallet.width_m else 0
    lat_wid_fits = math.floor(strip_width / complement_pallet.width_m) if complement_pallet.width_m > 0 else 0
    lateral = lat_len_fits * lat_wid_fits
    # Palets complementarios en end-strip:
    end_wid_fits = math.floor(container.inner_width_m / complement_pallet.width_m) if complement_pallet.width_m > 0 else 0
    end_len_fits = math.floor(end_length / complement_pallet.length_m) if complement_pallet.length_m > 0 and end_length >= complement_pallet.length_m else 0
    end = end_wid_fits * end_len_fits
    return lateral + end


def compute_logistics(
    skus: list[SkuInput],
    container: ContainerProfile,
    pallet_profiles: dict[str, PalletProfile],
    cost_per_container_eur: float,
) -> LogisticsResult:
    """Modelo agregado de capacidad — calibración Oliver 2026-04-25.

    En lugar de hacer packing 3D estricto por familia (que dejaba strips
    laterales muertos y daba números muy conservadores), se calculan
    capacidades agregadas del proyecto y se compara contra las capacidades
    útiles del contenedor:

      N = MAX(
        Σ huella_m² / usable_floor,    # huella de palé considera apilamiento
        Σ peso_kg   / usable_payload,
        Σ volumen_m³/ usable_cbm,
      )

    Donde:
      - huella_palé = (L × A) / niveles_apilables (un palé apilado 3 niveles
        ocupa 1/3 del suelo)
      - usable_floor = inner_l × inner_w × floor_stowage_factor (0,80 = 80%)
      - usable_payload = payload × stowage_factor (0,90)
      - usable_cbm    = inner_l × inner_w × inner_h × stowage_factor (0,90)

    El resultado N es **decimal** (no se redondea con ceil para el coste);
    el cliente paga proporcional a la carga real. n_containers (entero) se
    reporta para el operador (contenedores físicos a reservar = ceil del decimal).

    Imputación del coste: POR PESO real (lo que la naviera factura). Cada SKU
    paga (peso_sku / peso_total) × coste_total. Robusto frente a errores en
    units_per_pallet de algún SKU — antes la imputación por palés daba números
    absurdos para cintas/mallas con units_per_pallet inflado.
    """
    if not skus:
        return LogisticsResult(
            container_type=container.type, n_containers=0,
            n_containers_decimal=0.0, total_cost_eur=0,
            dominant_family='', dominant_driver='', families={}, skus=[],
        )

    # 1: computed per SKU + agrupar por familia.
    skus_computed: list[SkuComputed] = []
    by_family: dict[str, list[SkuComputed]] = {}
    for sku_in in skus:
        family_profile = pallet_profiles.get(sku_in.category)
        if family_profile is None:
            raise KeyError(f"PalletProfile no definido para categoría {sku_in.category!r}")
        eff_pallet = _effective_pallet_profile(sku_in, family_profile)
        sc = _compute_sku(sku_in, eff_pallet)
        skus_computed.append(sc)
        by_family.setdefault(sku_in.category, []).append(sc)

    effective_pallet_by_family: dict[str, PalletProfile] = {}
    for sku_in in skus:
        if sku_in.category not in effective_pallet_by_family:
            effective_pallet_by_family[sku_in.category] = _effective_pallet_profile(
                sku_in, pallet_profiles[sku_in.category]
            )

    # 2: FamilyResult por categoría (mantenido por compat con UI/diagnóstico).
    family_results: dict[str, FamilyResult] = {}
    for cat, scs in by_family.items():
        family_results[cat] = _family_result(
            cat, scs, container, effective_pallet_by_family[cat]
        )

    # 3: AGREGADOS — el cálculo principal del modelo nuevo.
    total_huella_m2 = 0.0
    total_weight_kg = 0.0
    total_cbm = 0.0
    total_pallets = 0
    for cat, scs in by_family.items():
        pallet = effective_pallet_by_family[cat]
        levels = max(pallet.stackable_levels, 1)
        # Huella considerando apilamiento: un palé apilado 3 niveles ocupa 1/3
        # del suelo. Si stackable=1, ocupa el suelo completo.
        huella_per_pallet = (pallet.length_m * pallet.width_m) / levels
        for sc in scs:
            total_huella_m2 += sc.pallets * huella_per_pallet
            total_weight_kg += sc.weight_total_kg
            total_cbm += sc.cbm_total
            total_pallets += sc.pallets

    usable_floor = container.usable_floor_m2
    usable_payload = container.effective_payload_kg
    usable_cbm = container.effective_cbm

    n_floor = total_huella_m2 / usable_floor if usable_floor > 0 else 0.0
    n_weight = total_weight_kg / usable_payload if usable_payload > 0 else 0.0
    n_cbm = total_cbm / usable_cbm if usable_cbm > 0 else 0.0

    drivers = [('floor', n_floor), ('weight', n_weight), ('cbm', n_cbm)]
    dominant_driver, n_decimal = max(drivers, key=lambda x: x[1])
    n_containers = math.ceil(n_decimal) if n_decimal > 0 else 0

    # Familia dominante = la que más palés aporta (informativo).
    dom_cat = max(family_results, key=lambda c: family_results[c].total_pallets) if family_results else ''

    # Imputación POR PESO NETO de mercancía (sin tara de palé). La naviera
    # factura por peso, pero usamos peso NETO en lugar de bruto porque:
    # - El N_containers SÍ cuenta peso bruto (capacidad real del cont).
    # - Pero la imputación al cliente por peso bruto se distorsiona si
    #   algún SKU tiene units_per_pallet mal cargado (ej. cinta=20 cuando
    #   son 600). Eso infla los "palés" → infla la tara → infla el peso
    #   relativo → paga de más.
    # - Imputando por peso neto, la tara queda como coste común proporcional
    #   y los datos imprecisos no envenenan el reparto.
    total_cost_imputable = n_decimal * cost_per_container_eur
    total_cost = round(total_cost_imputable, 2)
    total_weight_neto = sum(s.qty * s.unit_weight_kg for s in skus if s.unit_weight_kg > 0)
    cost_per_kg = (total_cost_imputable / total_weight_neto) if total_weight_neto > 0 else 0.0
    for sc in skus_computed:
        sku_in = next((s for s in skus if s.sku == sc.sku), None)
        if not sku_in or sku_in.unit_weight_kg <= 0 or sku_in.qty <= 0:
            continue
        weight_neto_sku = sku_in.qty * sku_in.unit_weight_kg
        sc_cost = cost_per_kg * weight_neto_sku
        # Dividir por qty pedida (no por capacidad del palé): el contenedor
        # transporta exactamente qty unidades, no la capacidad teórica del palé.
        sc.unit_log_cost_eur = round(sc_cost / sku_in.qty, 4)
        if sku_in.unit_area_m2 > 0:
            sc.m2_log_cost_eur = round(sc.unit_log_cost_eur / sku_in.unit_area_m2, 4)

    # Capacidad libre por contenedor para alerta de cross-sell.
    if n_containers > 0:
        free_floor = max(0.0, usable_floor - (total_huella_m2 / n_containers))
        free_weight = max(0.0, usable_payload - (total_weight_kg / n_containers))
        free_cbm = max(0.0, usable_cbm - (total_cbm / n_containers))
        pct_weight_free = free_weight / usable_payload if usable_payload > 0 else 0
        pct_floor_free = free_floor / usable_floor if usable_floor > 0 else 0
        optimized = pct_weight_free < 0.05 and pct_floor_free < 0.10
    else:
        free_floor = free_weight = free_cbm = 0.0
        optimized = True

    return LogisticsResult(
        container_type=container.type,
        n_containers=n_containers,
        n_containers_decimal=round(n_decimal, 4),
        total_cost_eur=total_cost,
        dominant_family=dom_cat,
        dominant_driver=dominant_driver,
        families=family_results,
        skus=skus_computed,
        extra_containers_by_family={},  # legacy field — el modelo agregado no separa "extras"
        free_weight_kg_per_cont=round(free_weight, 2),
        free_cbm_per_cont=round(free_cbm, 2),
        free_floor_m2_per_cont=round(free_floor, 2),
        free_weight_kg_total=round(free_weight * n_containers, 2),
        free_cbm_total=round(free_cbm * n_containers, 2),
        free_floor_m2_total=round(free_floor * n_containers, 2),
        is_optimized=optimized,
        n_by_floor=round(n_floor, 4),
        n_by_weight=round(n_weight, 4),
        n_by_cbm=round(n_cbm, 4),
        total_floor_m2=round(total_huella_m2, 2),
        total_weight_kg=round(total_weight_kg, 2),
        total_cbm=round(total_cbm, 2),
    )
