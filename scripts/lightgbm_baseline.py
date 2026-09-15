"""Tunes and fits the LightGBM benchmark, stores its predictions, writes docs/lightgbm-baseline.md.

    .venv/Scripts/python scripts/lightgbm_baseline.py
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg

import benchmark as bm
import pure_premium as pp
from db import dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "lightgbm-baseline.md"


def store(conn: psycopg.Connection, frame: pd.DataFrame, glm_run: pp.Run, params: dict, rounds: int, factor: float,
          predictions: np.ndarray) -> None:
    run = pp.BENCHMARK_RUN
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM model.benchmark_run WHERE run = %s", (run,))
        cur.execute(
            "INSERT INTO model.benchmark_run (run, frame_md5, trained_on, large_loss_cap, large_loss_load, "
            "variance_power, boosting_rounds, balance_factor, parameters) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (run, glm_run.frame_md5, glm_run.trained_on, glm_run.large_loss_cap, glm_run.large_loss_load,
             bm.VARIANCE_POWER, rounds, factor, json.dumps({**bm.BASE_PARAMS, **bm.TWEEDIE, **params}, sort_keys=True)),
        )
        with cur.copy("COPY model.benchmark_prediction (run, idpol, capped_amount_per_year) FROM STDIN") as copy:
            for idpol, value in zip(frame["idpol"].to_numpy(), predictions):
                copy.write_row((run, int(idpol), float(value)))
    with conn.cursor() as cur:
        cur.execute("SELECT capped_amount_per_year FROM model.benchmark_prediction WHERE run = %s ORDER BY idpol", (run,))
        stored = np.array([row[0] for row in cur.fetchall()])
    if not np.array_equal(stored, predictions):
        raise RuntimeError("benchmark predictions did not survive the round trip to Postgres bit for bit")


def measure() -> dict:
    with psycopg.connect(dsn()) as conn:
        policies = pp.load_policies(conn)
        glm = pp.pure_premium(conn, pp.VALIDATION_RUN, policies)
        frame = glm.frame
        training = frame["trained"].to_numpy()
        train = frame[training].reset_index(drop=True)
        exposure = train["exposure"].to_numpy()
        capped_rate = train["capped_loss"].to_numpy() / exposure

        grid = []
        for params in bm.GRID:
            cv = bm.cross_validate(train, capped_rate, exposure, bm.TWEEDIE, params, bm.fold_ids(train, "risk_group"))
            grid.append({**params, "rounds": cv["rounds"],
                         "deviance": bm.tweedie_deviance(capped_rate, cv["out_of_fold"], exposure)})
        chosen = min(grid, key=lambda g: g["deviance"])
        params = {"num_leaves": chosen["num_leaves"], "min_data_in_leaf": chosen["min_data_in_leaf"]}

        leak = {}
        targets = {
            "capped losses, Tweedie": (capped_rate, bm.TWEEDIE, bm.tweedie_deviance),
            "priced claims, Poisson": (train["priced_claim_nb"].to_numpy() / exposure, bm.POISSON, bm.poisson_deviance),
            "reported claims, Poisson": (train["claim_nb"].to_numpy() / exposure, bm.POISSON, bm.poisson_deviance),
        }
        for name, (rate, objective, deviance) in targets.items():
            leak[name] = {}
            for by in ("risk_group", "row"):
                runs = [bm.cross_validate(train, rate, exposure, objective, params, bm.fold_ids(train, by, seed))
                        for seed in bm.FOLD_SEEDS]
                leak[name][by] = {"rounds": [cv["rounds"] for cv in runs],
                                  "deviance": [deviance(rate, cv["out_of_fold"], exposure) for cv in runs]}

        booster = bm.fit(train, capped_rate, exposure, bm.TWEEDIE, params, chosen["rounds"])
        raw = booster.predict(frame[bm.FEATURES])
        drift = {rounds: 1 / bm.balance_factor(capped_rate, booster.predict(train[bm.FEATURES], num_iteration=rounds), exposure)
                 for rounds in sorted({1, 50, chosen["rounds"]})}
        score = bm.tweedie_score(capped_rate, raw[training], exposure)
        factor = bm.balance_factor(capped_rate, raw[training], exposure)
        store(conn, frame, glm.run, params, chosen["rounds"], factor, raw * factor)
        run, stored = pp.load_benchmark_run(conn, pp.BENCHMARK_RUN, policies)
        benchmark = pp.assemble_benchmark(run, stored)
        try:
            pp.assemble_benchmark(run, stored.assign(capped_amount_per_year=raw))
            refusal = None
        except pp.BasisError as exc:
            refusal = str(exc)

    gain = pd.Series(booster.feature_importance("gain"), index=bm.FEATURES)
    held = frame["risk_group_holdout"].to_numpy()
    b = benchmark.frame
    rate = frame["capped_loss"].to_numpy() / frame["exposure"].to_numpy()
    weight = frame["exposure"].to_numpy()
    glm_capped = (frame["claims_per_year"] * frame["capped_amount_per_claim"]).to_numpy()
    gbm_capped = b["capped_amount_per_year"].to_numpy()
    constant = frame.loc[training, "capped_loss"].sum() / weight[training].sum()

    def ae(expected: np.ndarray) -> float:
        return float(frame.loc[held, "capped_loss"].sum() / (weight[held] * expected[held]).sum())

    return {
        "frame_md5": glm.run.frame_md5,
        "large_loss_cap": glm.run.large_loss_cap,
        "training_rows": int(training.sum()),
        "holdout_rows": int(held.sum()),
        "risk_groups_in_training": int(train["risk_group"].nunique()),
        "grid": grid,
        "chosen": chosen,
        "leak": leak,
        "gain_share": (gain / gain.sum()).sort_values(ascending=False).to_dict(),
        "benchmark": {
            "drift": drift,
            "score": score,
            "balance_factor": factor,
            "refusal_without_correction": refusal,
            "training_capped_ratio": pp.check_capped_amount_per_year(b[b["trained"]]),
            "off_balance": benchmark.off_balance,
            "large_loss_load": benchmark.run.large_loss_load,
        },
        "holdout": {
            "claims": int(frame.loc[held, "priced_claim_nb"].sum()),
            "constant": bm.tweedie_deviance(rate[held], np.full(held.sum(), constant), weight[held]),
            "glm": bm.tweedie_deviance(rate[held], glm_capped[held], weight[held]),
            "lightgbm": bm.tweedie_deviance(rate[held], gbm_capped[held], weight[held]),
            "glm_capped_ae": ae(glm_capped),
            "lightgbm_capped_ae": ae(gbm_capped),
            "glm_recorded_ae": float(frame.loc[held, "incurred_loss"].sum() / frame.loc[held, "expected_loss"].sum()),
            "lightgbm_recorded_ae": float(b.loc[held, "incurred_loss"].sum() / b.loc[held, "expected_loss"].sum()),
            "correlation": float(np.corrcoef(np.log(glm_capped[held]), np.log(gbm_capped[held]))[0, 1]),
        },
    }


def fmt(x: float, digits: int) -> str:
    return f"{round(x, digits) + 0.0:,.{digits}f}"


def pct(x: float, digits: int = 1) -> str:
    return f"{round(100 * x, digits) + 0.0:.{digits}f}%"


def md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"the document's text no longer matches the numbers: {message}")


def gamma_shape() -> float:
    text = (REPO_ROOT / "docs" / "model-diagnostics.md").read_text(encoding="utf-8")
    match = re.search(r"Gamma shape estimated by maximum likelihood: [\d.]+ on uncapped amounts and ([\d.]+) capped", text)
    require(match is not None, "docs/model-diagnostics.md no longer states the capped Gamma shape")
    return float(match.group(1))


def render(r: dict) -> list[str]:
    shape = gamma_shape()
    implied_power = (shape + 2) / (shape + 1)
    require(abs(implied_power - bm.VARIANCE_POWER) < 0.01, "the variance power no longer follows from the Gamma shape")

    grid = r["grid"]
    chosen = r["chosen"]
    best = chosen["deviance"]
    rounds = chosen["rounds"]
    edge = [name for name, key in (("tree size", "num_leaves"), ("minimum leaf size", "min_data_in_leaf"))
            if chosen[key] in (min(g[key] for g in grid), max(g[key] for g in grid))]

    b = r["benchmark"]
    require(b["refusal_without_correction"] is not None, "the uncorrected benchmark is no longer refused")
    require(abs(b["score"]) < 1e-4, "the Tweedie score equation no longer holds on the training rows")
    require(b["drift"][rounds] < 1 - pp.LOSS_BALANCE_TOLERANCE, "the training total no longer falls short")
    require(abs(b["training_capped_ratio"] - 1) < 1e-9, "the balance correction no longer balances")

    leak_rows = []
    separated = {}
    for target, schemes in r["leak"].items():
        group, row = schemes["risk_group"], schemes["row"]
        separated[target] = max(row["deviance"]) < min(group["deviance"])
        difference = np.mean(row["deviance"]) - np.mean(group["deviance"])
        leak_rows.append([
            target,
            f"{fmt(min(group['deviance']), 0)} to {fmt(max(group['deviance']), 0)}",
            f"{fmt(min(row['deviance']), 0)} to {fmt(max(row['deviance']), 0)}",
            fmt(difference, 0),
            pct(difference / np.mean(group["deviance"]), 2),
            f"{fmt(np.mean(group['rounds']), 0)} / {fmt(np.mean(row['rounds']), 0)}",
        ])
    reported = r["leak"]["reported claims, Poisson"]
    capped = r["leak"]["capped losses, Tweedie"]
    require(separated["reported claims, Poisson"] and separated["capped losses, Tweedie"]
            and not separated["priced claims, Poisson"],
            "the fold comparison no longer separates reported claims and capped losses but not priced claims")
    require(np.mean(reported["row"]["rounds"]) > 1.3 * np.mean(reported["risk_group"]["rounds"]),
            "row folds no longer keep the reported-claims model boosting longer")
    capped_gap = (np.mean(capped["risk_group"]["deviance"]) - np.mean(capped["row"]["deviance"])) / np.mean(capped["risk_group"]["deviance"])
    longer = np.mean(reported["row"]["rounds"]) / np.mean(reported["risk_group"]["rounds"]) - 1

    h = r["holdout"]
    require(h["lightgbm"] < h["glm"] < h["constant"], "LightGBM no longer has the lowest holdout deviance")
    require(abs(h["glm_capped_ae"] - 1) < 0.01 and abs(h["lightgbm_capped_ae"] - 1) < 0.01,
            "a model's capped holdout level is more than 1% off")
    glm_gain, gbm_gain = h["constant"] - h["glm"], h["constant"] - h["lightgbm"]

    out = [
        "# LightGBM baseline",
        "",
        "Generated by `scripts/lightgbm_baseline.py`, which also stores the benchmark's predictions. Do not",
        "edit by hand; re-run the script.",
        "",
        f"Frame md5 `{r['frame_md5']}`. Fitted on the {fmt(r['training_rows'], 0)} training rows of the risk-group split, "
        f"{fmt(r['risk_groups_in_training'], 0)} risk groups. LightGBM {lgb.__version__}, Python {platform.python_version()}.",
        "",
        "**Rules, fixed before fitting.** *Tuning:* the sixteen combinations of 7, 15, 31 or 63 leaves",
        "and a minimum leaf of 100, 500, 2,000 or 5,000 rows, learning rate 0.05, stopping after 200",
        "rounds without improvement, on five folds of the training rows assigned by risk group; the",
        "lowest total out-of-fold Tweedie deviance wins. *Holdout:* used for no choice, scored once",
        "below. *Level:* `pure_premium()` refuses a benchmark whose training total is more than",
        f"{pct(pp.LOSS_BALANCE_TOLERANCE, 0)} from capped losses.",
        "",
        "## One comparison, two models",
        "",
        *md_table(
            ["", "GLM", "LightGBM"],
            [
                ["rows fitted", "training rows of the risk-group split", "the same rows"],
                ["what is predicted", "priced claims per policy-year times capped amount per claim",
                 "capped loss per policy-year, with exposure as the weight"],
                ["inputs", "eight rating factors; driver age, vehicle age and bonus-malus banded; vehicle power a factor",
                 "the same eight, unbanded"],
                ["large losses", f"capped at {fmt(r['large_loss_cap'], 0)}, flat load {fmt(b['large_loss_load'], 4)} from training claims",
                 "the same cap and load"],
                ["level", "Poisson balance, then the off-balance factor on training rows",
                 "a balance correction on training rows, then the same off-balance factor"],
                ["holdout", f"`risk_group_holdout`, {fmt(r['holdout_rows'], 0)} rows", "the same rows"],
            ],
        ),
        "",
        "The Tweedie here is LightGBM's loss function. It is not the Tweedie GLM the plan cut from the",
        "comparison; that would have been a third model, and this is how the second one is trained.",
        "",
        "Capped loss divided by exposure, with exposure as the weight, has the same likelihood under a",
        "compound Poisson model as capped loss with an exposure offset, the equivalence D2-2 checked for",
        "the Poisson GLM. A prediction is a capped amount per policy-year, and a row's expected capped",
        "loss is its exposure times that.",
        "",
        f"The variance power is {bm.VARIANCE_POWER}. A compound Poisson sum of Gamma claims with shape alpha is Tweedie with",
        f"power (alpha + 2) / (alpha + 1). The capped claims' shape in `docs/model-diagnostics.md` is {shape}, which gives",
        f"{fmt(implied_power, 3)}.",
        "",
        "The trees get the rating factors without the GLM's bands. Bands are how a GLM becomes",
        "non-linear, and handing a tree the GLM's cut points would impose them on it. Area is left out,",
        "as in the GLM, because it is a coarse banding of density.",
        "",
        "## Tuning",
        "",
        *md_table(
            ["Leaves", "Minimum leaf", "Rounds", "Out-of-fold Tweedie deviance", "Above the best"],
            [[str(g["num_leaves"]), fmt(g["min_data_in_leaf"], 0), str(g["rounds"]), fmt(g["deviance"], 0),
              pct(g["deviance"] / best - 1, 3)] for g in grid],
        ),
        "",
        f"Chosen: {chosen['num_leaves']} leaves, a minimum leaf of {fmt(chosen['min_data_in_leaf'], 0)} rows, {rounds} rounds.",
    ]
    if edge:
        out += [
            f"The winner sits on the edge of the grid in {' and '.join(edge)}, so the grid does not bracket the",
            "optimum there. It was not widened after seeing that. The whole grid spans",
            f"{pct(max(g['deviance'] for g in grid) / best - 1, 2)} of deviance.",
        ]
    out += [
        "",
        "## Tweedie boosting does not keep totals",
        "",
        *md_table(
            ["Rounds", "Predicted capped losses over actual, training rows"],
            [[str(k), fmt(v, 4)] for k, v in sorted(b["drift"].items())],
        ),
        "",
        "The Tweedie score equation, exposure times mu^(1-p) times (rate - mu) summed over the training",
        f"rows and scaled, is {b['score']:.1e}: the objective is satisfied. What it balances is a weighted",
        "residual, not the total, the same property D2-5 found in the Gamma GLM. Uncorrected, the",
        "benchmark is refused:",
        "",
        f"> {b['refusal_without_correction']}",
        "",
        "The tolerance was not widened, since it exists to catch units errors. The benchmark instead",
        f"carries a balance correction of {fmt(b['balance_factor'], 4)}, estimated on its training rows and stored in",
        "`model.benchmark_run`. After it the training total is exact, and the off-balance factor is",
        f"{fmt(b['off_balance'], 6)}.",
        "",
        "## The leak, seen by LightGBM itself",
        "",
        "The chosen configuration, cross-validated on the training rows with folds assigned by risk",
        "group and by row, three fold seeds each. Every row is scored out of fold under both, so the",
        "rows are the same and only the folds differ.",
        "",
        *md_table(
            ["Target", "Risk-group folds, deviance", "Row folds, deviance", "Row minus risk group, mean", "Relative",
             "Rounds, risk group / row"],
            leak_rows,
        ),
        "",
        f"On reported claims the row folds score better at every seed and keep boosting {pct(longer, 0)} longer,",
        "because the validation fold keeps rewarding what the training fold remembers. That is D3-2's",
        "repeated claim count, read back by a real model. On priced claim counts the two schemes",
        "overlap. On capped losses, the benchmark's own target, the row folds are better at every seed by",
        f"{pct(capped_gap, 2)}: small, and not zero. D3-2 found priced claims shared across pieces only mildly,",
        "and a Tweedie deviance on capped rates weighs a claim on a short piece heavily, which may be how",
        "a mild sharing shows up here and not in counts. That is a reading, not a measurement.",
        "Validation uses the risk-group split either way.",
        "",
        "## First look at the holdout",
        "",
        *md_table(
            ["Model", "Tweedie deviance, capped", "Below constant", "Capped losses, actual over expected",
             "Recorded losses, actual over expected"],
            [
                ["constant", fmt(h["constant"], 0), "", "", ""],
                ["GLM", fmt(h["glm"], 0), pct(glm_gain / h["constant"], 2), fmt(h["glm_capped_ae"], 3),
                 fmt(h["glm_recorded_ae"], 3)],
                ["LightGBM", fmt(h["lightgbm"], 0), pct(gbm_gain / h["constant"], 2), fmt(h["lightgbm_capped_ae"], 3),
                 fmt(h["lightgbm_recorded_ae"], 3)],
            ],
        ),
        "",
        f"LightGBM's deviance falls {pct(gbm_gain / glm_gain - 1, 0)} further below the constant than the GLM's, on "
        f"{fmt(h['claims'], 0)} held-out claims. Their log predictions have a correlation of {fmt(h['correlation'], 2)}.",
        "Whether the gap is larger than the holdout's own noise is D3-6's question, and ranking is D3-4's;",
        "neither is answered here.",
        "",
        "## What the trees use",
        "",
        *md_table(["Rating factor", "Share of split gain"], [[k, pct(v)] for k, v in r["gain_share"].items()]),
        "",
        "## Handed on",
        "",
        f"- **D3-4:** Gini on the holdout for `pure_premium('{pp.VALIDATION_RUN}')` and",
        f"  `benchmark_pure_premium('{pp.BENCHMARK_RUN}')`.",
        "- **D3-5:** both models' capped holdout levels are within 1% of actual; calibration by segment",
        "  is the open question.",
        "- **D3-6:** resample risk groups to test the deviance and Gini gaps. The capped Tweedie deviance",
        "  is dominated by claims on short exposures, so the interval may be wide.",
    ]
    return out


def main() -> None:
    DOCUMENT.write_bytes(("\n".join(render(measure())) + "\n").encode("utf-8"))
    print(f"wrote {DOCUMENT}")


if __name__ == "__main__":
    main()
