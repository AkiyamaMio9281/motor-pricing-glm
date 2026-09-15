# Power BI report specification

The report itself is not in the repository. `scripts/export_rate_workbook.py` writes the
extract it reads to `exports/powerbi/`. This page specifies the model and the two pages, so the
report can be built in Power BI Desktop from those files.

## Data model

Import every CSV in `exports/powerbi/`.

| Table | Grain | Key | Relationship |
|---|---|---|---|
| `fact_policy_year` | policy-year, 678,013 rows | `idpol` | many-to-one to each dimension below |
| `fact_claim` | priced claim, 26,444 rows | `claim_key` | many-to-one to `fact_policy_year` on `idpol` |
| `dim_region` | region | `region_key` | `fact_policy_year[region_key]` |
| `dim_area` | area, with its density range | `area_key` | `fact_policy_year[area_key]` |
| `dim_vehicle` | brand, fuel and power | `vehicle_key` | `fact_policy_year[vehicle_key]` |
| `dim_rating_band` | band label and lower bound | `factor`, `label` | none; sort-by source for the band columns |
| `rate_relativity` | rating factor level | `factor`, `level` | none; drives page 2 |
| `segment_rate` | region by driver-age band | `region`, `driv_age_band` | none; drives the page 2 table |

Sort `driv_age_band`, `veh_age_band` and `bonus_malus_band` by their lower bound from
`dim_rating_band`. Otherwise 100 sorts before 50.

Do not sum `exposure` across a relationship to `fact_claim`. Each policy-year would count
once per claim.

## Measures

```
Exposure            = SUM(fact_policy_year[exposure])
Priced claims       = SUM(fact_policy_year[priced_claims])
Claim frequency     = DIVIDE([Priced claims], [Exposure])
Incurred loss       = SUM(fact_policy_year[incurred_loss])
Loss per year       = DIVIDE([Incurred loss], [Exposure])
Expected loss       = SUM(fact_policy_year[glm_expected_loss])
Actual over expected = DIVIDE([Incurred loss], [Expected loss])
Capped A/E          = DIVIDE(SUM(fact_policy_year[capped_loss]) * 1.3391, [Expected loss])
```

1.3391 is the large-loss load from the workbook's Summary sheet. Capped A/E is the stable one:
on a segment of a few thousand policy-years, a single large claim moves the uncapped ratio by
tens of percent.

## Page 1: portfolio overview

- Cards: Exposure, Priced claims, Claim frequency, Loss per year, Actual over expected.
- Column chart: Loss per year by `bonus_malus_band`, with Expected loss per year as a line.
- Column chart: Capped A/E by `driv_age_band`, with a constant line at 1.
- Map or bar chart: Loss per year by `dim_region[region_code]`.
- Slicers in one row above the visuals: `dim_area[area_code]`, `dim_vehicle[veh_gas]`, and
  `in_holdout`, which defaults to all.

## Page 2: relativity drilldown

- Slicer: `rate_relativity[factor]`, single select.
- Bar chart: `relativity_largest_base` by `level`, with a constant line at 1. The tooltip adds
  `exposure_share` and `relativity_default_base`.
- Table from `segment_rate`: region, driver-age band, exposure, priced claims, incurred loss,
  loss ratio, manual rate, experience rate, Bühlmann Z, normalized rate. Apply a red-to-blue diverging format to
  normalized rate over manual rate, centred on 1.
- Text box: "Relativities are against the largest-exposure level of each factor. Segment rates
  shrink toward the portfolio mean and are shown for comparison; the rate table uses the GLM
  rates, see docs/credibility.md."
