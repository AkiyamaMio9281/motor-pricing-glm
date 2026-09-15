# Power BI report specification

The report is in `powerbi/` as a Power BI Project; see `powerbi/README.md` to open it. It reads
the extract `scripts/export_rate_workbook.py` writes to `exports/powerbi/`. This page specifies
the model and the two pages the project implements.

## Data model

Every CSV in `exports/powerbi/` is imported through a text parameter, `ExtractFolder`, set to
`C:\motor-pricing-glm\exports\powerbi\`. A clone elsewhere is linked to that path or the
parameter is changed.

| Table | Grain | Key | Relationship |
|---|---|---|---|
| `fact_policy_year` | policy-year, 678,013 rows | `idpol` | many-to-one to each dimension below |
| `fact_claim` | priced claim, 26,444 rows | `claim_key` | many-to-one to `fact_policy_year` on `idpol` |
| `dim_region` | region | `region_key` | `fact_policy_year[region_key]` |
| `dim_area` | area, with its density range | `area_key` | `fact_policy_year[area_key]` |
| `dim_vehicle` | brand, fuel and power | `vehicle_key` | `fact_policy_year[vehicle_key]` |
| `dim_driver_age_band` | driver-age band | `driv_age_band` | `fact_policy_year[driv_age_band]` |
| `dim_vehicle_age_band` | vehicle-age band | `veh_age_band` | `fact_policy_year[veh_age_band]` |
| `dim_bonus_malus_band` | bonus-malus band | `bonus_malus_band` | `fact_policy_year[bonus_malus_band]` |
| `rate_relativity` | rating factor level | `factor`, `level` | none; drives page 2 |
| `segment_rate` | region by driver-age band | `region`, `driv_age_band` | none; drives the page 2 table |

The three band dimensions are filtered from `dim_rating_band.csv`, one per factor. It cannot
be one dimension, because driver age and bonus-malus share the labels 55-59 and 70-79. Each
band sorts by its lower bound. `level` in `rate_relativity` sorts by the first number in its
label, which gives shared labels the same sort value.

Every band and level column is imported as text. Left to type detection, a vehicle-age band
of "3-4" becomes an error.

`fact_policy_year` gains `exposure_group` (under 0.1, 0.1 to under 0.5, 0.5 to under 1, a
full year) and its sort order. `segment_rate` gains `normalized_over_manual`.

Do not sum `exposure` across a relationship to `fact_claim`. Each policy-year would count
once per claim.

## Measures

```
Exposure                      = SUM(fact_policy_year[exposure])
Priced claims                 = SUM(fact_policy_year[priced_claims])
Reported claims               = SUM(fact_policy_year[reported_claims])
Claim frequency               = DIVIDE([Priced claims], [Exposure])
Incurred loss                 = SUM(fact_policy_year[incurred_loss])
Loss per policy-year          = DIVIDE([Incurred loss], [Exposure])
Expected loss                 = SUM(fact_policy_year[glm_expected_loss])
Expected loss per policy-year = DIVIDE([Expected loss], [Exposure])
Large-loss load               = 1.3391071
Capped loss per policy-year   = DIVIDE(SUM(fact_policy_year[capped_loss]) * [Large-loss load], [Exposure])
Actual over expected          = DIVIDE([Incurred loss], [Expected loss])
Capped actual over expected   = DIVIDE(SUM(fact_policy_year[capped_loss]) * [Large-loss load], [Expected loss])
Relativity against base       = IF(HASONEVALUE(rate_relativity[factor]), MAX(rate_relativity[relativity_largest_base]))
```

1.3391 is the large-loss load from the workbook's Summary sheet. The capped measures are the
stable ones. On a segment of a few thousand policy-years, one large claim moves the uncapped
ratio by tens of percent. The largest claim, 4,075,401, sits in bonus-malus 100 and driver age
18-19.

## Page 1: Portfolio overview

- Slicers in one row: `dim_area[area_code]`, `dim_vehicle[veh_gas]` and `in_holdout`. A note
  beside them says the held-out rows show composition, not validation, because expected
  losses come from the model fitted on all policy-years.
- Cards: Exposure, Priced claims, Claim frequency, Loss per policy-year, Actual over expected.
- Line and column chart by bonus-malus band: capped loss per policy-year as columns and
  expected loss per policy-year as a line.
- Column chart by `exposure_group`: capped actual over expected, with a constant line at 1.
  Actual over expected by a rating factor is close to 1 by construction for a model fitted on
  all rows; by length of cover it is not, and it shows the partial-year gap.
- Bar chart: capped loss per policy-year by region, descending. Regions are codes, so a map
  cannot place them.
- Line chart by driver-age band: capped and expected loss per policy-year.

## Page 2: Rate relativities

- Slicer: `rate_relativity[factor]`, single select.
- Card: the selected factor's largest-exposure base level.
- Bar chart: Relativity against base by `level`, with exposure share and the first-level-base
  relativity in the tooltip.
- Table from `segment_rate`, with its own region slicer: region, driver-age band, exposure,
  priced claims, incurred loss, loss ratio, GLM rate, experience rate, Buhlmann Z,
  credibility rate, and credibility rate over GLM rate. That last column is shaded red to
  blue around 1.
- Text: "Relativities are against the largest-exposure level of each factor. Segment rates
  shrink toward the portfolio mean and are shown for comparison; the rate table uses the GLM
  rates, see docs/credibility.md."
