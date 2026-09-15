# Motor liability pricing: what the model says, in one page

For underwriting and product colleagues. The technical record is in `RESULTS.md`.

## What drives the price

A policy's annual price starts from a base rate of 129, the price for a policy in the largest
group of every characteristic. It is then multiplied by one factor per characteristic.

| Characteristic | Cheapest level | Most expensive level | Largest group, priced at 1.00 |
|---|---|---|---|
| Bonus-malus | 50, 1.00 | 120 and above, 11.47 | 50, 63% of policies |
| Driver age | 25-29, 0.49 | 18-19, 1.68 | 50-54 |
| Region | R73, 0.70 | R94, 1.43 | R24 |
| Vehicle age | 20 years and more, 0.48 | 2 years, 1.10 | 7-9 years |
| Vehicle brand | B12, 0.79 | B11, 1.24 | B1 |
| Vehicle power | 4, 0.88 | 14, 1.48 | 6 |
| Fuel | regular, 1.00 | diesel, 1.18 | regular |

Denser areas pay slightly more: doubling the population density raises the price by 5%.

Bonus-malus, the claims history, carries the price. Driver age has to be read together with it.
Drivers aged 25-29 average a bonus-malus of 74.5, and only 4% are at the 50 floor. Drivers aged
50-54 average 53.3, and 81% are at the floor. The driver-age factor is what is left after that
history is priced, which is why drivers in their late twenties look cheaper than drivers in
their fifties.

## How good it is

Held-out policies the model never saw were ranked by the model's price. The most expensive
tenth produced about six times the cost per year of cover of the cheapest tenth. Overall,
predicted losses came within 1% of actual once very large claims are set aside.

A machine-learning model was trained on the same data as a benchmark. It ranks slightly
better, and a resampling test says the difference is genuine, but small. The GLM is kept for
the rate table because every factor in it can be read, checked and filed.

## What to be careful about

- **Claims without an amount.** More than a quarter of reported claims have no cost recorded.
  The model treats them as costing nothing. If they are real but unsettled costs, new vehicles
  in particular are underpriced. This is the largest open question.
- **Short policies.** Policies in force for about five weeks or less produced 2.7 times the losses a
  pro-rata price expects, and full-year policies 0.7 times. A full-year rate currently carries
  part of the cost of policies that end early. How cancellations are refunded should decide
  whether that is right.
- **Large claims.** Claims above 34,377 are not rated by segment. Their cost, a quarter of all
  losses, is spread evenly as a 34% loading. The test for whether some segments attract more
  of them was inconclusive.
- **Thin segments.** No region and driver-age combination has enough claims to be priced on its
  own experience. Credibility checks found nothing the model misses at that level, so no
  segment adjustments are proposed.
