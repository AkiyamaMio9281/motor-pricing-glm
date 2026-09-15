"""Rate relativities from the pricing GLM, and credibility-weighted segment rates.

    .venv/Scripts/python scripts/rate_table.py
"""

from __future__ import annotations

import re
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg

import credibility as cr
import pure_premium as pp
from db import dsn

FACTORS = {
    "veh_brand": "veh_brand",
    "veh_gas": "veh_gas",
    "region": "region",
    "driv_age_band": "driv_age_band",
    "veh_age_band": "veh_age_band",
    "bonus_malus_band": "bonus_malus_band",
    "veh_power": "factor(veh_power)",
}
DENSITY_TERM = "log(density)"
SEGMENT = ["region", "driv_age_band"]
TOLERANCE = 0.05
PROBABILITY = 0.90


class RateTable(NamedTuple):
    frame: pd.DataFrame
    relativities: pd.DataFrame
    base: dict
    segments: pd.DataFrame
    credibility: dict


def load(conn: psycopg.Connection) -> tuple[pp.PurePremium, pd.DataFrame, dict]:
    policies = pp.load_policies(conn)
    priced = pp.pure_premium(conn, pp.PRICING_RUN, policies)
    with conn.cursor() as cur:
        cur.execute("SELECT idpol, driv_age_band, veh_age_band, bonus_malus_band FROM mart.policy_segment ORDER BY idpol")
        bands = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
        cur.execute("SELECT model, term, estimate FROM model.glm_coefficient WHERE run = %s", (pp.PRICING_RUN,))
        coefficients = {(model, term): estimate for model, term, estimate in cur.fetchall()}
    if not (bands["idpol"].to_numpy() == priced.frame["idpol"].to_numpy()).all():
        raise ValueError("mart.policy_segment does not match the frame policy for policy")
    frame = priced.frame.assign(**{c: bands[c].to_numpy() for c in ("driv_age_band", "veh_age_band", "bonus_malus_band")})
    frame["veh_power"] = frame["veh_power"].astype(str)
    return priced, frame, coefficients


def natural_key(level: str) -> tuple:
    digits = re.search(r"\d+", level)
    return (int(digits.group()) if digits else -1, level)


def log_relativity(coefficients: dict, term: str) -> float:
    return coefficients.get(("frequency", term), 0.0) + coefficients.get(("severity", term), 0.0)


def relativities(frame: pd.DataFrame, coefficients: dict) -> pd.DataFrame:
    rows = []
    exposure_total = frame["exposure"].sum()
    for factor, prefix in FACTORS.items():
        exposure = frame.groupby(frame[factor].astype(str), observed=True)["exposure"].sum()
        levels = pd.DataFrame({"level": exposure.index, "exposure": exposure.to_numpy()})
        levels = levels.iloc[sorted(range(len(levels)), key=lambda i: natural_key(levels["level"].iloc[i]))].reset_index(drop=True)
        levels["log_relativity"] = [log_relativity(coefficients, prefix + level) for level in levels["level"]]
        levels["has_term"] = [("frequency", prefix + level) in coefficients for level in levels["level"]]
        if (~levels["has_term"]).sum() != 1:
            raise ValueError(f"{factor} does not have exactly one level without a coefficient")
        default_base = levels.loc[~levels["has_term"], "level"].item()
        largest = levels.loc[levels["exposure"].idxmax(), "level"]
        largest_log = levels.loc[levels["level"] == largest, "log_relativity"].item()
        for _, level in levels.iterrows():
            rows.append({
                "factor": factor,
                "level": level["level"],
                "exposure_share": level["exposure"] / exposure_total,
                "default_base": default_base,
                "relativity_default_base": float(np.exp(level["log_relativity"])),
                "largest_exposure_base": largest,
                "relativity_largest_base": float(np.exp(level["log_relativity"] - largest_log)),
            })
    return pd.DataFrame(rows)


def base_rates(frame: pd.DataFrame, coefficients: dict, table: pd.DataFrame, run: pp.Run, off_balance: float) -> dict:
    intercept = log_relativity(coefficients, "(Intercept)")
    density_slope = log_relativity(coefficients, DENSITY_TERM)
    level = run.large_loss_load * off_balance
    order = np.argsort(frame["density"].to_numpy())
    cumulative = np.cumsum(frame["exposure"].to_numpy()[order])
    reference_density = float(frame["density"].to_numpy()[order][np.searchsorted(cumulative, cumulative[-1] / 2)])
    largest_logs = table.loc[table["level"] == table["largest_exposure_base"]].set_index("factor")
    default = float(np.exp(intercept) * level)
    largest = float(np.exp(intercept + np.log(largest_logs["relativity_default_base"]).sum() + density_slope * np.log(reference_density)) * level)
    return {"default": default, "largest": largest, "density_slope": float(density_slope), "reference_density": reference_density,
            "large_loss_load": run.large_loss_load, "off_balance": off_balance}


def premium_from_table(frame: pd.DataFrame, table: pd.DataFrame, base: dict, which: str) -> np.ndarray:
    column = "relativity_default_base" if which == "default" else "relativity_largest_base"
    premium = np.full(len(frame), base[which])
    for factor in FACTORS:
        lookup = table.loc[table["factor"] == factor].set_index("level")[column]
        premium *= frame[factor].astype(str).map(lookup).to_numpy()
    reference = 1.0 if which == "default" else base["reference_density"]
    return premium * (frame["density"].to_numpy() / reference) ** base["density_slope"]


def segment_credibility(frame: pd.DataFrame, load: float, severity_cv: float) -> tuple[pd.DataFrame, dict]:
    exposure = frame["exposure"].to_numpy()
    expected = frame["expected_loss"].to_numpy()
    actual = frame["capped_loss"].to_numpy() * load
    cell = frame["region"].astype(str) + "|" + frame["driv_age_band"].astype(str)
    portfolio = cr.buhlmann_straub(actual / exposure, exposure, cell)
    against_model = cr.buhlmann_straub(actual / expected, expected, cell)
    grouped = pd.DataFrame({"region": frame["region"].astype(str), "driv_age_band": frame["driv_age_band"],
                            "exposure": exposure, "reported_claims": frame["claim_nb"], "priced_claims": frame["priced_claim_nb"],
                            "incurred_loss": frame["incurred_loss"], "expected": expected,
                            "actual": actual}).groupby(SEGMENT, sort=False).sum().reset_index()
    standard = cr.full_credibility_standard(TOLERANCE, PROBABILITY, severity_cv)
    grouped["loss_ratio"] = grouped["incurred_loss"] / grouped["expected"]
    grouped["manual_rate"] = grouped["expected"] / grouped["exposure"]
    grouped["experience_rate"] = grouped["actual"] / grouped["exposure"]
    grouped["buhlmann_z"] = cr.buhlmann_credibility(grouped["exposure"], portfolio["k"])
    grouped["limited_fluctuation_z"] = cr.limited_fluctuation_credibility(grouped["priced_claims"], standard)
    grouped["model_complement_z"] = cr.buhlmann_credibility(grouped["expected"], against_model["k"])
    grouped["weighted_rate"] = grouped["buhlmann_z"] * grouped["experience_rate"] + (1 - grouped["buhlmann_z"]) * portfolio["grand_mean"]
    normalization = grouped["actual"].sum() / (grouped["weighted_rate"] * grouped["exposure"]).sum()
    grouped["normalized_rate"] = grouped["weighted_rate"] * normalization
    return grouped, {
        "portfolio": portfolio,
        "against_model": against_model,
        "full_credibility_claims": standard,
        "severity_cv": severity_cv,
        "normalization_factor": float(normalization),
    }


def build(conn: psycopg.Connection) -> RateTable:
    priced, frame, coefficients = load(conn)
    table = relativities(frame, coefficients)
    base = base_rates(frame, coefficients, table, priced.run, priced.off_balance)
    for which in ("default", "largest"):
        gap = np.max(np.abs(premium_from_table(frame, table, base, which) / frame["amount_per_year"].to_numpy() - 1))
        if gap > 1e-9:
            raise ValueError(f"the {which}-base rate table misprices a policy by {gap:.2e}")
    with conn.cursor() as cur:
        cur.execute("SELECT stddev_pop(least(claim_amount, %s)) / avg(least(claim_amount, %s)) FROM fact.claim",
                    (priced.run.large_loss_cap, priced.run.large_loss_cap))
        severity_cv = float(cur.fetchone()[0])
    segments, credibility = segment_credibility(frame, priced.run.large_loss_load, severity_cv)
    return RateTable(frame, table, base, segments, credibility)


def main() -> None:
    with psycopg.connect(dsn()) as conn:
        result = build(conn)
    t, b, s, c = result.relativities, result.base, result.segments, result.credibility
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    print("base rates", {k: round(v, 6) for k, v in b.items()})
    defaults = t[t["level"] == t["default_base"]][["factor", "level", "exposure_share"]]
    largest = t[t["level"] == t["largest_exposure_base"]][["factor", "level", "exposure_share"]]
    print("default base levels\n", defaults.round(4).to_string(index=False))
    print("largest-exposure base levels\n", largest.round(4).to_string(index=False))
    spread = t.groupby("factor").agg(default_min=("relativity_default_base", "min"), default_max=("relativity_default_base", "max"),
                                     largest_min=("relativity_largest_base", "min"), largest_max=("relativity_largest_base", "max"))
    print(spread.round(3).to_string())
    print("credibility", c)
    print("cells", len(s), "Z quantiles", np.round(np.quantile(s["buhlmann_z"], [0, 0.25, 0.5, 0.75, 1]), 3),
          "LF Z quantiles", np.round(np.quantile(s["limited_fluctuation_z"], [0, 0.25, 0.5, 0.75, 1]), 3),
          "LF fully credible", int((s["limited_fluctuation_z"] >= 1).sum()), "max claims", int(s["priced_claims"].max()))
    s["against_manual"] = s["normalized_rate"] / s["manual_rate"]
    print("normalized over GLM manual, exposure-weighted quantiles",
          np.round(np.quantile(s["against_manual"], [0, 0.1, 0.5, 0.9, 1]), 3),
          "corr", round(float(np.corrcoef(np.log(s["normalized_rate"]), np.log(s["manual_rate"]))[0, 1]), 3))
    cols = [*SEGMENT, "exposure", "priced_claims", "manual_rate", "experience_rate", "buhlmann_z", "limited_fluctuation_z",
            "weighted_rate", "normalized_rate", "against_manual"]
    print(s.sort_values("exposure").iloc[np.r_[0:4, -4:0]][cols].round(3).to_string(index=False))
    print(s.sort_values("experience_rate").iloc[np.r_[0:3, -3:0]][cols].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
