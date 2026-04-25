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
    n_containers: int
    total_cost_eur: float
    dominant_family: str
    dominant_driver: str
    families: dict[str, FamilyResult]
    skus: list[SkuComputed]
    extra_containers_by_family: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


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
    """Calcula N contenedores, coste total y coste imputado por SKU.

    Lógica:
    1. Por SKU: palets, peso, cbm (con override per-SKU si existe).
    2. Por familia: N alone = max(pallets, weight, cbm).
    3. Familia dominante = la de mayor N alone.
    4. Resto de familias: ocupan suelo sobrante en los N dominantes.
    5. Si una familia complementaria sobrepasa ese sobrante, abre sus propios
       contenedores adicionales.
    6. Imputación: paga quien abre contenedores; dentro de una familia, el
       coste se reparte proporcional a palets.
    """
    if not skus:
        return LogisticsResult(
            container_type=container.type, n_containers=0, total_cost_eur=0,
            dominant_family='', dominant_driver='', families={}, skus=[],
        )
    # 1 + 2: computed per SKU + agrupar por familia.
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

    # Guardamos el pallet profile "operativo" por familia (primer override gana).
    effective_pallet_by_family: dict[str, PalletProfile] = {}
    for sku_in in skus:
        if sku_in.category not in effective_pallet_by_family:
            effective_pallet_by_family[sku_in.category] = _effective_pallet_profile(
                sku_in, pallet_profiles[sku_in.category]
            )

    # 2: FamilyResult por categoría.
    family_results: dict[str, FamilyResult] = {}
    for cat, scs in by_family.items():
        family_results[cat] = _family_result(
            cat, scs, container, effective_pallet_by_family[cat]
        )

    # 3: dominante = el de mayor n_alone.
    dom_cat = max(family_results, key=lambda c: family_results[c].n_alone)
    dom = family_results[dom_cat]
    n_containers = dom.n_alone

    # 4 + 5: cada familia complementaria intenta ocupar suelo sobrante.
    dom_pallet = effective_pallet_by_family[dom_cat]
    extra_by_family: dict[str, int] = {}
    for cat, fr in family_results.items():
        if cat == dom_cat:
            continue
        comp_pallet = effective_pallet_by_family[cat]
        slots_per_cont = _floor_slots_for_complement(container, dom_pallet, comp_pallet)
        available_slots = slots_per_cont * n_containers
        overflow_pallets = max(0, fr.total_pallets - available_slots)
        if overflow_pallets == 0:
            extra_by_family[cat] = 0
            continue
        # Abre sus propios contenedores para lo que no cupo.
        extra_n = math.ceil(overflow_pallets / max(fr.cap_geo_per_container, 1))
        # Y también pueden restringir peso/cbm:
        extra_w = math.ceil(overflow_pallets * (fr.total_weight_kg / max(fr.total_pallets, 1))
                            / container.effective_payload_kg) if fr.total_weight_kg > 0 else 0
        extra = max(extra_n, extra_w)
        extra_by_family[cat] = extra
        n_containers += extra

    total_cost = n_containers * cost_per_container_eur

    # 6: imputación.
    # Dominante paga sus N_dominant contenedores; cada complementaria paga sus extras.
    # Dentro de la familia, se reparte por palets.
    def impute(cat: str, containers_paid_by_this_family: int) -> None:
        fr = family_results[cat]
        if fr.total_pallets <= 0 or containers_paid_by_this_family <= 0:
            return
        cost_family = containers_paid_by_this_family * cost_per_container_eur
        cost_per_pallet = cost_family / fr.total_pallets
        for sc in by_family[cat]:
            if sc.pallets <= 0:
                continue
            sc_cost = cost_per_pallet * sc.pallets
            # €/unidad: cost / (pallets × units_per_pallet)
            # Tenemos los SkuComputed pero no units_per_pallet aquí; recuperamos.
            # Buscamos el SkuInput correspondiente:
            sku_in = next((s for s in skus if s.sku == sc.sku), None)
            if sku_in and sku_in.units_per_pallet > 0:
                units_total = sc.pallets * sku_in.units_per_pallet
                sc.unit_log_cost_eur = round(sc_cost / units_total, 4) if units_total > 0 else 0
                if sku_in.unit_area_m2 > 0:
                    sc.m2_log_cost_eur = round(sc.unit_log_cost_eur / sku_in.unit_area_m2, 4)

    impute(dom_cat, dom.n_alone)
    for cat, extra in extra_by_family.items():
        if extra > 0:
            impute(cat, extra)

    return LogisticsResult(
        container_type=container.type,
        n_containers=n_containers,
        total_cost_eur=round(total_cost, 2),
        dominant_family=dom_cat,
        dominant_driver=dom.dominant_driver,
        families=family_results,
        skus=skus_computed,
        extra_containers_by_family=extra_by_family,
    )
