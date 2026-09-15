# Power BI report

A Power BI Project (PBIP) in source format. The semantic model is TMDL in
`motor_pricing.SemanticModel/definition/`, and the two report pages are PBIR JSON in
`motor_pricing.Report/definition/`. Both can be read and diffed as text. The data comes from
the CSV extract that `scripts/export_rate_workbook.py` writes to `exports/powerbi/`, which is
not committed.

## Open

1. Build the extract: `.venv/Scripts/python scripts/export_rate_workbook.py`.
2. Make the clone reachable at `C:\motor-pricing-glm`, the path the `ExtractFolder`
   parameter uses. Clone it there, or link it from wherever it is:
   `New-Item -ItemType Junction -Path C:\motor-pricing-glm -Target <your clone>` in
   PowerShell.
3. Open `motor_pricing.pbip` in Power BI Desktop and choose **Home → Refresh**.

If you prefer another location, set `ExtractFolder` under **Home → Transform data → Edit
parameters**. Keep the parameter pointing at a folder that exists: when it does not, Power BI
discards the cached data and every visual fails to render. Do not commit a parameter that
names folders on your own machine; the test suite refuses one under `C:\Users`.

Power BI's web service opens `.pbix` files, not projects. To get one, open the project in
Desktop and use **File → Save as**, choosing the Power BI file (.pbix) type.

## Pages

![Portfolio overview](../docs/figures/powerbi-portfolio-overview.png)

![Rate relativities](../docs/figures/powerbi-rate-relativities.png)

**Portfolio overview.** Slicers for area, fuel and held-out rows, and cards for exposure,
priced claims, claim frequency, loss per policy-year and actual over expected. Four charts:

- capped loss against GLM expected by bonus-malus band;
- capped actual over expected by length of cover;
- capped loss per policy-year by region;
- capped loss against GLM expected by driver age.

**Rate relativities.** A single-select rating factor slicer, the factor's base level, and
relativities against the largest-exposure level. Beside them, a region slicer and the region
by driver-age credibility table, with credibility rate over GLM rate shaded around 1.

Expected losses come from the GLM fitted on all policy-years, so the held-out slicer shows
portfolio composition, not validation. Validation is in `RESULTS.md`.

## Model

Ten imported tables:

- two facts, policy-years and claims;
- region, area and vehicle dimensions;
- driver-age, vehicle-age and bonus-malus band dimensions, each filtered from
  `dim_rating_band.csv`;
- the relativity and segment tables, which have no relationships.

All seven relationships are many-to-one and single-direction. Measures live in `_Measures`.
The relativity measures return blank unless exactly one rating factor is selected, because
driver age and bonus-malus share the labels 55-59 and 70-79. The full specification is in
`docs/powerbi-spec.md`.

## Check after a refresh

| With no filters | Value |
|---|---|
| Exposure | 358,360.1 |
| Priced claims | 26,444 |
| Claim frequency | 0.0738 |
| Loss per policy-year | 167.18 |
| Actual over expected | 1.000 |

| Length of cover | Capped actual over expected |
|---|---|
| under 0.1 | 2.847 |
| 0.1 to under 0.5 | 1.445 |
| 0.5 to under 1 | 0.957 |
| a full year | 0.701 |

Other checks:

- **Held-out rows = True:** exposure 71,694.7 and 5,311 priced claims.
- **Bonus-malus relativities:** from 1.000 at 50 to 11.467 at 120+.
- **Driver-age relativities:** base 50-54, with 1.680 at 18-19.

## Not committed

`.pbi/cache.abf` is the local data cache, and `.pbi/localSettings.json` holds settings bound
to the machine. A saved `.pbix` embeds the whole extract, about 27 MB, for the same reason
the data itself is not committed. All three are excluded by `powerbi/.gitignore`.
