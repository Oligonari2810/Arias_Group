# Logística — modelo iterativo y calibración Arias

Documento para entender qué calcula el motor de logística y cómo se afina con
la operación real.

## Modelo del motor (calibración 2026-04-25)

El motor toma cada palé del proyecto y calcula 3 capacidades agregadas:

```
Total huella  = Σ palés × (L × A) / niveles_apilables   [m²]
Total peso    = Σ palés × peso_por_palé                 [kg]
Total volumen = Σ palés × (L × A × H)                   [m³]
```

Cada uno se compara contra la capacidad útil del contenedor (40HC con
calibración Arias):

| Capacidad | Usable | Cálculo |
|---|---|---|
| Suelo | **22,67 m²** | inner_floor × `floor_stowage_factor` (0,80) |
| Peso | **23.850 kg** | payload (26.500) × `stowage_factor` (0,90) |
| Volumen | **68,44 m³** | inner_volume × `stowage_factor` (0,90) |

**Número de contenedores** = `MAX` de los 3 drivers (en decimal).

```
N_floor   = total_huella / 22,67
N_weight  = total_peso   / 23.850
N_cbm     = total_volumen / 68,44
N         = MAX(N_floor, N_weight, N_cbm)
```

`n_containers` (entero) = `ceil(N)` — los contenedores físicos a reservar.
`n_containers_decimal` = `N` literal — el factor real de carga, usado para
el coste imputado al cliente (`N_decimal × tarifa_cont`).

## Por qué los stowage factors son lo que son

| Factor | Valor | Significado |
|---|---|---|
| `floor_stowage_factor` | 0,80 | Techo de carga geométrica con placas. El 20% restante son strips, accesos, sujeción. |
| `stowage_factor` (peso) | 0,90 | Margen del 10% sobre payload nominal por distribución desigual de carga. |
| `stowage_factor` (volumen) | 0,90 | Margen del 10% por huecos no aprovechables entre palés. |

Estos valores **no son teóricos**: vienen de la observación operativa de
Oliver con cargas reales Fassa (2026-04-25). Si en el futuro la realidad
operativa cambia (ej. estibadores más eficientes que aprovechen 90% del
suelo), se editan en `container_profiles` desde `/masters` sin redeploy.

## Imputación del coste — "por peso neto"

Cada SKU paga proporcional a su **peso neto de mercancía** (sin tara de palé):

```
coste_sku = (peso_neto_sku / peso_neto_total) × coste_total
coste_unit = coste_sku / qty_pedida
```

Es lo que la naviera factura realmente en flete marítimo. Y es **robusto
frente a errores de datos**: si `units_per_pallet` de un SKU está mal
cargado en DB (ej. cinta cargada como 20 cuando son 600 reales), el reparto
por peso no se distorsiona — sigue pagando lo justo según los kg que pesa.

**Por qué peso NETO, no peso BRUTO**:
- N_containers SÍ se calcula con peso bruto (capacidad real del cont).
- Pero la imputación por peso bruto se distorsionaría con palés inflados:
  cinta con 30 "palés" falsos × 22 kg tara = 660 kg fantasma. Pagaría 4×
  más de lo justo.
- Imputando por peso neto, la tara queda como coste común, repartida
  proporcional al peso real de la mercancía.

Comparación de modelos en un proyecto típico (placas + pastas + cintas):

| Modelo | Placas | Pastas | Cintas | Comentario |
|---|---|---|---|---|
| "Quien abre paga" (vetado) | 100% | 0% | 0% | Distorsiona márgenes |
| "Por palés totales" (vetado) | OK | OK | sobrepaga si units_per_pallet mal | Sensible a datos malos |
| **"Por peso neto" (actual)** | proporcional | proporcional | proporcional | Robusto, lo que cobra la naviera |

## Coste fraccional vs entero

A la naviera se le pagan contenedores enteros (29 si N=28,094). Pero al
cliente se le imputa el coste **decimal** (28,094 × tarifa). La diferencia
del último contenedor "medio vacío" la absorbe Arias o se rellena con otra
carga.

| Ejemplo | Valor |
|---|---|
| N decimal | 28,094 |
| Tarifa contenedor | 4.050 € |
| **Coste imputado al cliente** | 28,094 × 4.050 = **113.781 €** |
| Coste pagado a la naviera | 29 × 4.050 = 117.450 € |
| Absorbe Arias | 3.669 € |

## Datos por familia (`pallet_profiles`)

Calibración inicial:

| Familia | L × A × H (m) | `stackable_levels` | Notas |
|---|---|---|---|
| PLACAS | 2,50 × 1,20 × 0,30 | 3 | Apilables 3 niveles |
| PERFILES | 3,00 × 0,80 × 0,35 | 2 | |
| PASTAS | 1,20 × 0,80 × 1,20 | **1** | Sacos pesados, sin apilar |
| CINTAS | 1,20 × 0,80 × 1,00 | 2 | |
| TORNILLOS | 1,20 × 0,80 × 1,00 | 2 | |
| ACCESORIOS | 1,20 × 0,80 × 1,00 | 2 | |

Cada SKU puede sobreescribir las dimensiones del palé en `products.pallet_*`
si su embalaje físico difiere del default familiar (ej. placas 2.0 m van en
palé 2,0 × 1,2; placas 2,4 m van en palé 2,4 × 1,2).

## Ciclo de mejora continua

La precisión del motor se afina con cada operación real:

1. **Llega un palé Fassa** → contar cajas/unidades reales.
2. Si difiere del catálogo → actualizar `units_per_pallet` del SKU.
3. Si difiere del palé default familiar → actualizar override per-SKU.
4. **Nueva carga al cliente** → contar palés cargados realmente en el 40HC.
5. Si el motor predijo más cont de los reales → revisar `floor_stowage_factor`.
6. Si el motor predijo menos → revisar margen de seguridad.

**Datos pendientes de verificar (estado al 2026-04-25)**:

- ⚠️ Cintas y mallas: `units_per_pallet` actualmente carga "rollos/caja"
  del catálogo (no rollos/palé real). Verificar con albarán Fassa o medir
  en próxima entrega.
- ⚠️ Tornillos: ídem (uds/caja vs uds/palé).
- ⚠️ Accesorios perfiles: ídem.
- ⚠️ Placas 3.000 mm y mayores: no probadas en operación. Strip muerto
  potencial alto, modelo agregado podría sobreestimar capacidad.
- ✅ Placas 2,0 / 2,4 / 2,5 m: validado por Oliver.
- ✅ Pastas 25 kg: validado.

## Roadmap (no implementado)

1. **Mix de contenedores 20' + 40'**. Hoy el motor elige un solo tipo.
   Permitir que decida si conviene 25 × 40' + 2 × 20' en lugar de 28 × 40'
   cuando el último 40' va casi vacío.
2. **Algoritmo de packing 3D real** (en lugar de capacidades agregadas) para
   proyectos atípicos donde el modelo agregado podría sobreestimar.
3. **Auto-aprendizaje**: cuando llegue un albarán de carga real, comparar
   con la predicción del motor y ajustar `floor_stowage_factor` por
   regresión histórica.
