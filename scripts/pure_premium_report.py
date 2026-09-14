"""Writes docs/pure-premium.md.

    .venv/Scripts/python scripts/pure_premium_report.py
"""

from __future__ import annotations

import math
import platform
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

import pure_premium as pp
from db import dsn
from frame import load_frequency_frame

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "pure-premium.md"
REPORTED_RUN = "reported_claims"


def fmt(x: float, digits: int) -> str:
    return f"{x:,.{digits}f}"


def pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"the document's text no longer matches the numbers: {message}")


def main() -> None:
    with psycopg.connect(dsn()) as conn:
        policies = load_frequency_frame(conn)
        priced = pp.pure_premium(conn, pp.PRICING_RUN, policies)
        reported_run, reported, capped_losses = pp.load_run(conn, REPORTED_RUN, policies)
        with conn.cursor() as cur:
            cur.execute("SELECT run, term, estimate FROM model.glm_coefficient WHERE model = 'frequency'")
            coefficients = {(run, term): estimate for run, term, estimate in cur.fetchall()}
            cur.execute(
                "SELECT idpol, sum(least(claim_amount, %s))::float8, count(*) FILTER (WHERE claim_amount > %s) "
                "FROM fact.claim GROUP BY idpol",
                (priced.run.large_loss_cap, priced.run.large_loss_cap),
            )
            per_policy = pd.DataFrame(cur.fetchall(), columns=["idpol", "capped_loss", "large_claims"])

    try:
        pp.assemble(reported_run, reported, capped_losses)
        reported_refusal = None
    except pp.BasisError as exc:
        reported_refusal = str(exc)
    require(reported_refusal is not None, "the reported-claims run was not refused")

    f = priced.frame.merge(per_policy, on="idpol", how="left").fillna({"capped_loss": 0.0, "large_claims": 0})
    run = priced.run
    load = run.large_loss_load
    losses = f["incurred_loss"].sum()
    exposure = f["exposure"]
    claims_per_year = f["claims_per_year"]
    severity = f["capped_amount_per_claim"]

    claims_ratio = pp.check_claims_per_year(f, run.frequency_response)
    severity_ratio = pp.check_capped_amount_per_claim(f, capped_losses)
    raw_total_ratio = (exposure * claims_per_year * severity * load).sum() / losses

    bases = {
        "chosen": exposure * claims_per_year * severity * load,
        "claims_per_year used as expected claims, exposure left out": claims_per_year * severity * load,
        "expected claims used as claims_per_year, exposure applied twice": exposure**2 * claims_per_year * severity * load,
        "frequency of reported claims": exposure * reported["claims_per_year"] * severity * load,
        "large-loss load left out": exposure * claims_per_year * severity,
    }
    rebalanced = {name: x * losses / x.sum() for name, x in bases.items()}
    chosen = rebalanced["chosen"]

    def against_chosen(name: str, mask: pd.Series) -> float:
        return rebalanced[name][mask].sum() / chosen[mask].sum()

    segments = {
        "exposure under 0.1": exposure < 0.1,
        "a full year": exposure == 1,
        "vehicle age 0": f["veh_age"] == 0,
        "vehicle age 10+": f["veh_age"] >= 10,
    }
    basis_rows = []
    for name, x in bases.items():
        if name == "chosen":
            continue
        basis_rows.append([name, fmt(x.sum() / losses, 3)] + [fmt(against_chosen(name, m), 2) for m in segments.values()])
    require(all(abs(x.sum() / losses - 1) > pp.LOSS_BALANCE_TOLERANCE for n, x in bases.items() if n != "chosen"),
            "a wrong basis falls inside the rebalancing tolerance")
    load_row = [against_chosen("large-loss load left out", m) for m in segments.values()]
    require(all(abs(r - 1) < 5e-4 for r in load_row), "a missing flat load changes prices after rebalancing")

    veh_age_groups = pd.cut(f["veh_age"], [-1, 0, 2, 9, np.inf], labels=["0", "1-2", "3-9", "10+"])
    by_age = []
    for label in veh_age_groups.cat.categories:
        m = veh_age_groups == label
        unpriced = 1 - f["priced_claim_nb"][m].sum() / f["claim_nb"][m].sum()
        by_age.append((label, exposure[m].sum() / exposure.sum(), unpriced, against_chosen("frequency of reported claims", m)))
    require(max(by_age, key=lambda r: r[2])[0] == "0", "vehicle age 0 is not the group with most unpriced claims")
    require(by_age[0][3] > 1 and all(r[3] < 1 for r in by_age[1:]), "the reported basis does not charge only new vehicles more")

    regions = []
    for region in f["region"].cat.categories:
        m = f["region"] == region
        unpriced = 1 - f["priced_claim_nb"][m].sum() / f["claim_nb"][m].sum()
        regions.append((region, unpriced, against_chosen("frequency of reported claims", m)))
    region_frame = pd.DataFrame(regions, columns=["region", "unpriced", "ratio"])
    most_unpriced = region_frame.loc[region_frame["unpriced"].idxmax()]
    least_unpriced = region_frame.loc[region_frame["unpriced"].idxmin()]
    require(most_unpriced["ratio"] == region_frame["ratio"].max(), "the most unpriced region is not the most loaded")
    require(least_unpriced["ratio"] == region_frame["ratio"].min(), "the least unpriced region is not the least loaded")

    def relativity(run_name: str, term: str, against: str | None) -> float:
        base = coefficients[(run_name, against)] if against else 0.0
        return math.exp(coefficients.get((run_name, term), 0.0) - base)

    relativity_specs = [
        ("vehicle age 0 against 1", None, "veh_age_band1"),
        ("vehicle age 2 against 1", "veh_age_band2", "veh_age_band1"),
        ("vehicle age 10-11 against 1", "veh_age_band10-11", "veh_age_band1"),
        ("vehicle age 20+ against 1", "veh_age_band20+", "veh_age_band1"),
        ("regular fuel against diesel", "veh_gasRegular", None),
    ]
    relativity_rows = []
    for label, term, against in relativity_specs:
        values = {name: relativity(name, term, against) for name in (REPORTED_RUN, pp.PRICING_RUN)}
        relativity_rows.append((label, values[REPORTED_RUN], values[pp.PRICING_RUN]))
    new_car_reported, new_car_priced = relativity_rows[0][1], relativity_rows[0][2]
    fuel_reported, fuel_priced = relativity_rows[-1][1], relativity_rows[-1][2]
    require(new_car_reported > 2 and abs(new_car_priced - 1) < 0.05, "the new-car relativity no longer collapses")
    require(fuel_reported > 1 > fuel_priced, "the fuel relativity no longer reverses")

    exposure_groups = {
        "under 0.1": exposure < 0.1,
        "0.1 to under 0.5": (exposure >= 0.1) & (exposure < 0.5),
        "0.5 to under 1": (exposure >= 0.5) & (exposure < 1),
        "a full year": exposure == 1,
    }
    exposure_rows = []
    exposure_stats = {}
    for label, m in exposure_groups.items():
        count_ae = f["priced_claim_nb"][m].sum() / (exposure * claims_per_year)[m].sum()
        capped_ae = f["capped_loss"][m].sum() / (exposure * claims_per_year * severity)[m].sum()
        recorded_ae = f["incurred_loss"][m].sum() / priced.frame["expected_loss"][m].sum()
        large_rate = 1000 * f["large_claims"][m].sum() / f["priced_claim_nb"][m].sum()
        exposure_stats[label] = (count_ae, capped_ae, recorded_ae, large_rate)
        exposure_rows.append([
            label, pct(exposure[m].sum() / exposure.sum()), fmt(f["priced_claim_nb"][m].sum(), 0),
            fmt(count_ae, 2), fmt(capped_ae, 2), fmt(recorded_ae, 2), fmt(large_rate, 1),
        ])
    short, full = exposure_stats["under 0.1"], exposure_stats["a full year"]
    require(short[0] > 1 > full[0], "short exposures no longer have more claims than pro-rata")
    require(short[1] / short[0] > 1 and full[1] / full[0] < 1, "claims on short exposures are no longer larger")
    require(short[3] > full[3], "large claims are no longer more frequent on short exposures")

    tolerance = pct(pp.LOSS_BALANCE_TOLERANCE, 0)
    out = [
        "# Pure premium",
        "",
        "Generated by `scripts/pure_premium_report.py`. Do not edit by hand; re-run the script.",
        "",
        f"Computed from runs `{pp.PRICING_RUN}` and `{REPORTED_RUN}` in `model.glm_prediction`, written by "
        f"`R/export_predictions.R` from frame md5 `{run.frame_md5}`: {fmt(len(f), 0)} policy-years, "
        f"{fmt(f['priced_claim_nb'].sum(), 0)} priced claims totalling {fmt(losses, 2)}. "
        f"Python {platform.python_version()}.",
        "",
        "**Rules, fixed before computing.** *Claim count:* frequency counts priced claims, the claims",
        "that carry an amount, because recorded losses are the only cost in the data and every",
        f"validation metric compares with them. *Level:* if exposure times pure premium sums to within {tolerance}",
        f"of recorded losses, it is rebalanced to them exactly; beyond {tolerance} the assembly is refused as a",
        "probable units error.",
        "",
        "## Three quantities",
        "",
        *md_table(
            ["Quantity", "Unit", "From", "Check", "Value"],
            [
                ["`claims_per_year`", "priced claims per policy-year", "Poisson GLM, predicted at exposure 1",
                 "exposure times it, against priced claims", fmt(claims_ratio, 6)],
                ["`capped_amount_per_claim`", f"amount per claim, capped at {fmt(run.large_loss_cap, 0)}",
                 "Gamma GLM on capped claims", "priced claims times it, against capped losses", fmt(severity_ratio, 4)],
                ["`amount_per_year`", "recorded loss per policy-year",
                 f"the two above times the load {fmt(load, 4)} and the off-balance factor",
                 "exposure times it, against recorded losses, before rebalancing", fmt(raw_total_ratio, 4)],
            ],
        ),
        "",
        "The expected loss of a row is its exposure times `amount_per_year`. The frequency check is",
        "exact because a Poisson log-link fit balances its claims; the severity check is not, because",
        "a Gamma log link balances the ratio of amount to fitted amount rather than the total.",
        "",
        f"Off-balance factor: **{fmt(priced.off_balance, 6)}**, inside the {tolerance} the rule allows, and applied.",
        f"Across the book the pure premium is {fmt(losses / exposure.sum(), 2)} per policy-year.",
        "",
        "## Which claims frequency counts",
        "",
        f"Reported claims number {fmt(f['claim_nb'].sum(), 0)} and priced claims "
        f"{fmt(f['priced_claim_nb'].sum(), 0)}. The run fitted on reported claims is refused:",
        "",
        f"> {reported_refusal}",
        "",
        "Refusing it is the rule working, not the evidence for the rule. What matters is where the",
        "unpriced claims are.",
        "",
        *md_table(
            ["Vehicle age", "Exposure", "Reported claims without an amount",
             "Reported-claim pure premium, rebalanced, against priced"],
            [[label, pct(share), pct(unpriced), fmt(ratio, 2)] for label, share, unpriced, ratio in by_age],
        ),
        "",
        *md_table(
            ["Frequency relativity", "Fitted on reported claims", "Fitted on priced claims"],
            [[label, fmt(r, 3), fmt(p, 3)] for label, r, p in relativity_rows],
        ),
        "",
        f"On reported claims a new vehicle's claim frequency is {fmt(new_car_reported, 2)} times a one-year-old",
        f"vehicle's, the largest relativity in the book. On priced claims it is {fmt(new_car_priced, 2)}. The new-car",
        "effect is made of claims with no amount. Regular fuel reverses, from above diesel to below it.",
        "",
        f"By region, reported claims without an amount run from {pct(least_unpriced['unpriced'])} in "
        f"{least_unpriced['region']} to {pct(most_unpriced['unpriced'])} in {most_unpriced['region']}, and the reported basis "
        f"prices those two regions at {fmt(least_unpriced['ratio'], 2)} and {fmt(most_unpriced['ratio'], 2)} of the priced basis.",
        "",
        "The price columns are consequences, not further evidence. In-sample, a Poisson fit balances",
        "claims within every level of a rating factor, so each basis prices a vehicle-age band or a",
        "region at its own claim count, and the price ratio restates the unpriced share. The",
        "relativities are different: they are estimated with every other factor held fixed.",
        "",
        "So the choice is not a level adjustment. If an unpriced claim cost nothing, the reported basis",
        "overcharges new vehicles about threefold and the priced basis is right. If unpriced claims are",
        "real costs whose amounts are missing or not yet settled, recorded losses leave most of the",
        "new-vehicle cost out, the priced basis undercharges new vehicles by about as much, and no model",
        "fitted to this data can recover it. The data does not say which. The priced basis is used",
        "because it is the one that can be checked.",
        "",
        "## Errors a rebalance would hide",
        "",
        "Each row assembles pure premium on a wrong basis, rebalances it to recorded losses as a naive",
        "off-balance step would, and compares the result with the chosen basis by segment.",
        "",
        *md_table(
            ["Basis", "Total against recorded, before rebalancing", *[f"After rebalancing: {s}" for s in segments]],
            basis_rows,
        ),
        "",
        "Rebalancing makes every row's total right. A missing flat load is then harmless: the load and",
        "the off-balance factor are the same kind of factor. The other three move money between",
        "policies. Leaving exposure out charges the shortest policy-years many times their price;",
        "applying it twice nearly exempts them; reported claims load new vehicles. A Gini or a lift",
        "chart would score each of these as just another model. `pure_premium()` checks the total",
        f"before rebalancing, and all four are more than {tolerance} out.",
        "",
        "## What the chosen basis still misses: partial years",
        "",
        "Expected loss for a partial year is exposure times the annual pure premium, pro rata. Against",
        "recorded experience, in-sample, by exposure group:",
        "",
        *md_table(
            ["Exposure", "Share of exposure", "Priced claims", "Claims, actual over expected",
             "Capped losses, actual over expected", "Recorded losses, actual over expected",
             "Claims above the cap per 1,000 claims"],
            exposure_rows,
        ),
        "",
        "Exposure is not a rating factor, so these ratios are not balanced by construction the way a",
        "rating factor's would be. Policy-years under 0.1 of a year have "
        f"{fmt(short[0], 2)} times their pro-rata claims, D2-2's non-proportionality, and their claims are also",
        f"larger: capped losses come to {fmt(short[1], 2)} times expected against {fmt(short[0], 2)} for claims, and claims above",
        f"the cap are {fmt(short[3], 1)} per 1,000 against {fmt(full[3], 1)} on full years. A policy that ends early because",
        "of a costly claim, a write-off for instance, would produce both. The data has no dates or",
        "cancellation reasons to confirm it.",
        "",
        "The fitted annual rate pools all of this, so full years run at "
        f"{fmt(full[0], 2)} of their expected claims.",
        "Which is right depends on what a partial year is. If short policy-years end because of a",
        "claim, those claims belong to policies sold for a year and the pooled rate should carry",
        "them. If they are policies sold short, a full-year policy is overcharged. The data cannot",
        "separate the two, and any comparison of expected with actual losses by exposure will show",
        "the gap.",
        "",
        "## Handed on",
        "",
        "- **D3-2:** runs fitted on training policies only. The claims check holds on the rows a",
        "  model was fitted to, so it moves to the training rows, and the off-balance factor comes from",
        "  them too.",
        "- **D3-3:** the benchmark's target is recorded losses per policy-year on the same frame, so",
        "  its exposure basis has to be the one written down here.",
        "- **D3-5:** calibration by exposure group will show the partial-year gap above for any model",
        "  that prices pro rata.",
        "- **D4:** the vehicle-age bands were chosen on reported claims, and band 0 was kept separate",
        "  because risk changed fastest there. On priced claims it does not. Whether new vehicles",
        f"  pay {fmt(new_car_priced, 2)} or something nearer {fmt(new_car_reported, 2)} depends on what an unpriced claim is,",
        "  and the rate table has to say which it assumed.",
    ]

    DOCUMENT.write_bytes(("\n".join(out) + "\n").encode("utf-8"))
    print(f"wrote {DOCUMENT}; off-balance {priced.off_balance:.6f}; new car {new_car_reported:.2f} -> {new_car_priced:.2f}")


if __name__ == "__main__":
    main()
