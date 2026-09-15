# Credibility

Numbers from `scripts/rate_table.py` on the full frame (frame md5
`2ecb69783bcd2387d8e00d6b88335ae2`), pricing run `priced_claims`. The functions are in
`scripts/credibility.py`.

## The full credibility standard, derived

Limited-fluctuation credibility asks how many claims a segment needs before its own
experience can be trusted on its own. "Trusted" has to be stated as a target: the observed
pure premium should land within ±p of its expected value with probability P.

Let a segment's aggregate loss S be a compound Poisson sum: N claims with N ~ Poisson(λ), and
independent amounts X with mean μ and coefficient of variation CV. Then

- E[S] = λμ
- Var[S] = λ E[X²] = λμ²(1 + CV²)

With S approximately normal, P(|S − E[S]| ≤ p E[S]) ≥ P requires

  p E[S] / √Var[S] ≥ z, where z is the (1 + P)/2 quantile of the standard normal.

Substituting the two moments:

  p λμ / (μ √(λ(1 + CV²))) ≥ z  ⇒  λ ≥ (z / p)² (1 + CV²)

λ is the expected number of claims, so the standard is a claim count.

**The familiar 1,082 is the special case of a frequency-only target (CV = 0) with p = 5% and
P = 90%.** Then z = 1.645, and (1.645 / 0.05)² = 1,082.4. With the unrounded quantile,
1.64485, the result is 1,082.2. The number carries three choices, none of them a law: 5%, 90%,
and ignoring severity.

This project credibility-weights pure premium, so severity counts. The claims are capped at
34,377 as in the pricing model, and their coefficient of variation is 1.918:

  n_F = (1.64485 / 0.05)² × (1 + 1.918²) = **5,062 claims**

Partial credibility for n claims uses the square-root rule, Z = min(1, √(n / n_F)). It
makes the standard deviation of the credibility-weighted estimate match that of a fully
credible one.

How the standard moves with the two choices, in claims:

| Probability P | p = 2.5% | p = 5% | p = 10% |
|---|---|---|---|
| 90%, frequency only | 4,329 | 1,082 | 271 |
| 90%, capped pure premium | 20,249 | **5,062** | 1,266 |
| 95%, capped pure premium | 28,751 | 7,188 | 1,797 |
| 99%, capped pure premium | 49,658 | 12,414 | 3,104 |

The choice of 5% and 90% is the conventional one, stated here as a choice. Halving the
tolerance quadruples the standard.

## Why Bühlmann is applied, not the standard

The segments are region by driver-age band, 308 cells, the same bands as the GLM. The largest
cell, R24 at 50-54, has 833 priced claims, so no cell reaches 5,062. Limited-fluctuation Z
tops out at 0.406, with an exposure-weighted mean of 0.228. That rule only ever looks at a
cell's own volume, never at whether cells actually differ.

Bühlmann-Straub estimates that from the data. Each policy-year contributes its capped loss
times the large-loss load per unit of exposure, weighted by exposure.

- **EPV,** the expected process variance: the exposure-weighted spread of policy-years around
  their cell's mean, pooled over cells.
- **VHM,** the variance of hypothetical means: the exposure-weighted spread of cell means
  around the portfolio mean, less the part the EPV alone would produce.
- **Credibility:** k = EPV / VHM, and a cell with exposure m gets Z = m / (m + k).

## Two complements, two answers

**Against the portfolio mean,** the textbook exhibit, EPV is 5,343,754 and VHM is 2,853, so
k = 1,873 policy-years. Z runs from 0 to 0.877, and 52 cells holding 67.5% of exposure reach
0.5 or more. The credibility-weighted rate is Z × experience + (1 − Z) × 167.18. Rescaling so
that exposure times rate reproduces capped losses times the load needs a normalization
factor of 1.0224.

**Against the GLM,** credibility is applied to each cell's actual over expected, with expected
loss as the weight. VHM comes out at −0.103: the cells differ from the GLM by less than
process variance alone would make them. Bühlmann-Straub reads that as no credible
difference, so k is infinite and Z = 0 in every cell. The GLM's main effects leave no
region-by-driver-age signal it can detect.

The complement decides the price. The 18-19 cells have a GLM rate of 950.0 per policy-year
and an experience rate of 971.5: the model and the data agree. Weighted toward the portfolio
mean, the same cells come out at 247.7, because each one is too thin to escape the pull. R21
at 18-19 has 1.47 policy-years and no claims; its GLM rate is 1,164.80 and its normalized
portfolio-complement rate 170.78. Across all cells, only 54% of exposure sits within 10% of
the GLM rate, and the log correlation between the two rates is 0.49.

**Recommendation.** The rate table uses the GLM rates, which is credibility against the GLM
with Z = 0. The portfolio-complement table is kept in the workbook's Segments sheet as the
traditional exhibit and as the demonstration of why its complement is wrong here. A
segment's thin experience should borrow strength from a model that already knows young
drivers cost more, not from the average driver.
