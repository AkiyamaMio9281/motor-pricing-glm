"""Writes docs/holdout-split.md.

    .venv/Scripts/python scripts/holdout_report.py
"""

from __future__ import annotations

import platform
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

import pure_premium as pp
from db import dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "holdout-split.md"
PROFILE = ["area", "veh_power", "veh_age", "driv_age", "bonus_malus", "veh_brand", "veh_gas", "density", "region"]
SPLITS = {"risk_group_split": "risk_group_holdout", "idpol_split": "idpol_holdout"}
BASES = {"priced": "priced_claim_nb", "reported": "claim_nb"}
CREDIBILITY_K = (0.5, 2.0, 8.0)


def poisson_deviance(y, mu) -> float:
    y = np.asarray(y, dtype=float)
    mu = np.asarray(mu, dtype=float)
    safe = np.where(y > 0, y, 1.0)
    return float(2 * np.sum(np.where(y > 0, y * np.log(safe / mu), 0.0) - (y - mu)))


def gamma_deviance(y, mu) -> float:
    y = np.asarray(y, dtype=float)
    mu = np.asarray(mu, dtype=float)
    return float(2 * np.sum(-np.log(y / mu) + (y - mu) / mu))


def memorise(frame: pd.DataFrame, y: np.ndarray, mu: np.ndarray, training: np.ndarray, k: float) -> np.ndarray:
    profile = frame["profile"].to_numpy()
    size = profile.max() + 1
    claims = np.bincount(profile[training], weights=y[training], minlength=size)
    expected = np.bincount(profile[training], weights=mu[training], minlength=size)
    return mu * (claims[profile] + k) / (expected[profile] + k)


def measure() -> dict:
    with psycopg.connect(dsn()) as conn:
        policies = pp.load_policies(conn)
        runs = {}
        for basis in BASES:
            for trained_on in ("all", *SPLITS):
                name = f"{basis}_claims" if trained_on == "all" else f"{basis}_claims_{trained_on}"
                runs[name] = pp.load_run(conn, name, policies)
        with conn.cursor() as cur:
            cur.execute("SELECT idpol, least(claim_amount, %s)::float8 FROM fact.claim", (runs["priced_claims"][0].large_loss_cap,))
            claims = pd.DataFrame(cur.fetchall(), columns=["idpol", "capped_amount"])

    f = runs["priced_claims"][1][[*policies.columns, "capped_loss"]].copy()
    f["profile"] = f.groupby(PROFILE, observed=True, sort=False).ngroup()
    group = f["risk_group"].to_numpy()
    pieces = np.bincount(group)[group]
    exposure = f["exposure"].to_numpy()
    r = {"rows": len(f), "frame_md5": runs["priced_claims"][0].frame_md5, "idpol_unique": f["idpol"].is_unique,
         "risk_groups": int(f["risk_group"].nunique()), "fragmented_groups": int((np.bincount(group) > 1).sum())}

    r["splits"] = {}
    for split, flag in SPLITS.items():
        held = f[flag].to_numpy()
        held_in_group = np.bincount(group, weights=held)[group]
        mixed = (held_in_group > 0) & (held_in_group < pieces)
        sibling_in_training = held & (held_in_group < pieces)
        r["splits"][split] = {
            "holdout_rows": int(held.sum()),
            "straddling_groups": int(np.unique(group[mixed]).size),
            "priced_claims": int(f.loc[held, "priced_claim_nb"].sum()),
            "reported_claims": int(f.loc[held, "claim_nb"].sum()),
            "sibling_rows": int(sibling_in_training.sum()),
            "sibling_exposure_share": float(exposure[sibling_in_training].sum() / exposure[held].sum()),
            "sibling_priced_share": float(f.loc[sibling_in_training, "priced_claim_nb"].sum() / f.loc[held, "priced_claim_nb"].sum()),
            "sibling_reported_share": float(f.loc[sibling_in_training, "claim_nb"].sum() / f.loc[held, "claim_nb"].sum()),
        }

    sizes = np.bincount(group)
    r["sharing"] = {}
    for basis, column in BASES.items():
        y = f[column].to_numpy()
        mu = exposure * runs[f"{basis}_claims"][1]["claims_per_year"].to_numpy()
        expected_all = np.exp(np.bincount(group, weights=np.log(-np.expm1(-mu))))
        claiming = np.bincount(group, weights=(y > 0).astype(float))
        r["sharing"][basis] = {
            "observed": int(((claiming == sizes) & (sizes > 1)).sum()),
            "expected": float(expected_all[sizes > 1].sum()),
        }
    reporting = np.bincount(group, weights=(f["claim_nb"] > 0).astype(float))
    priced_pieces = np.bincount(group, weights=(f["priced_claim_nb"] > 0).astype(float))
    unpriced_by_group = np.bincount(group, weights=(f["claim_nb"] - f["priced_claim_nb"]).astype(float))
    all_reporting = (sizes > 1) & (reporting == sizes)
    largest_piece = f.groupby("risk_group", sort=True)["claim_nb"].max().reindex(np.arange(sizes.size), fill_value=0).to_numpy()
    reported_by_group = np.bincount(group, weights=f["claim_nb"].to_numpy().astype(float))
    repeated = np.where(all_reporting & (priced_pieces == 0), reported_by_group - largest_piece, 0.0)
    new_group = np.zeros(sizes.size, dtype=bool)
    new_group[group[f["veh_age"].to_numpy() == 0]] = True
    r["duplicates"] = {
        "repeated_counts": float(repeated.sum()),
        "repeated_new": float(repeated[new_group].sum()),
        "unpriced_new": float(unpriced_by_group[new_group].sum()),
        "groups": int(all_reporting.sum()),
        "groups_without_amount": int((all_reporting & (priced_pieces == 0)).sum()),
        "unpriced_in_groups": float(unpriced_by_group[all_reporting].sum()),
        "unpriced_total": float(unpriced_by_group.sum()),
    }
    new = f["veh_age"].to_numpy() == 0
    first_piece = np.r_[True, group[1:] != group[:-1]]
    r["fragmented_share"] = {
        "vehicle age 0": float((sizes[group] > 1)[first_piece & new].mean()),
        "older": float((sizes[group] > 1)[first_piece & ~new].mean()),
    }

    both = (f["risk_group_holdout"] & f["idpol_holdout"]).to_numpy()
    r["both_rows"] = int(both.sum())
    r["leak"] = {}
    for basis, column in BASES.items():
        y = f[column].to_numpy().astype(float)
        scores = {}
        for split in SPLITS:
            frame = runs[f"{basis}_claims_{split}"][1]
            training = frame["trained"].to_numpy()
            mu = exposure * frame["claims_per_year"].to_numpy()
            constant = exposure * y[training].sum() / exposure[training].sum()
            scores[split] = {"constant": poisson_deviance(y[both], constant[both]), "glm": poisson_deviance(y[both], mu[both])}
            for k in CREDIBILITY_K:
                scores[split][k] = poisson_deviance(y[both], memorise(f, y, mu, training, k)[both])
            if split == "idpol_split":
                held = frame["idpol_holdout"].to_numpy()
                held_in_group = np.bincount(group, weights=held)[group]
                with_sibling = held & (held_in_group < pieces)
                without = held & ~with_sibling
                memo = memorise(f, y, mu, training, 2.0)
                scores["mechanism"] = {
                    name: 1000 * (poisson_deviance(y[m], memo[m]) - poisson_deviance(y[m], mu[m])) / m.sum()
                    for name, m in (("with a piece in training", with_sibling), ("without", without))
                }
                scores["mechanism_rows"] = {"with a piece in training": int(with_sibling.sum()), "without": int(without.sum())}
        r["leak"][basis] = scores
        r["leak"][basis]["claims_on_both_rows"] = int(y[both].sum())

    r["validation"] = {}
    claim_policy = claims["idpol"].to_numpy()
    position = pd.Index(f["idpol"]).get_indexer(claim_policy)
    for split, flag in SPLITS.items():
        result = pp.assemble(*runs[f"priced_claims_{split}"])
        frame = result.frame
        held = frame[flag].to_numpy()
        training = frame["trained"].to_numpy()
        y = frame["priced_claim_nb"].to_numpy().astype(float)
        mu = exposure * frame["claims_per_year"].to_numpy()
        constant = exposure * y[training].sum() / exposure[training].sum()
        claim_held = held[position]
        claim_training = training[position]
        amount = claims["capped_amount"].to_numpy()
        severity = frame["capped_amount_per_claim"].to_numpy()[position]
        r["validation"][split] = {
            "off_balance": result.off_balance,
            "holdout_rows": int(held.sum()),
            "holdout_claims": int(y[held].sum()),
            "frequency_glm": poisson_deviance(y[held], mu[held]),
            "frequency_constant": poisson_deviance(y[held], constant[held]),
            "severity_glm": gamma_deviance(amount[claim_held], severity[claim_held]),
            "severity_constant": gamma_deviance(amount[claim_held], np.full(claim_held.sum(), amount[claim_training].mean())),
            "capped_ae": float(frame.loc[held, "capped_loss"].sum()
                               / (exposure * frame["claims_per_year"] * frame["capped_amount_per_claim"])[held].sum()),
            "loss_ae": float(frame.loc[held, "incurred_loss"].sum() / frame.loc[held, "expected_loss"].sum()),
            "loss_ae_without_largest": float(
                (frame.loc[held, "incurred_loss"].sum() - frame["incurred_loss"].max() * held[frame["incurred_loss"].to_numpy().argmax()])
                / frame.loc[held, "expected_loss"].sum()),
            "largest_claim_held": bool(held[frame["incurred_loss"].to_numpy().argmax()]),
        }
    r["largest_claim"] = float(f["incurred_loss"].max())
    return r


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


def render(r: dict) -> list[str]:
    rg, ip = r["splits"]["risk_group_split"], r["splits"]["idpol_split"]
    require(r["idpol_unique"], "IDpol is no longer unique")
    require(rg["straddling_groups"] == 0 and rg["sibling_rows"] == 0, "the risk-group split separates pieces of a group")

    sharing = {b: (s["observed"], s["expected"], s["observed"] / s["expected"]) for b, s in r["sharing"].items()}
    require(sharing["reported"][2] > 10 and sharing["priced"][2] < 2, "reported claims are no longer shared, or priced ones are")
    d = r["duplicates"]
    require(d["repeated_new"] / d["unpriced_new"] > d["repeated_counts"] / d["unpriced_total"],
            "repeated counts are no more common among new vehicles")
    require(r["fragmented_share"]["vehicle age 0"] > r["fragmented_share"]["older"], "new vehicles are no longer more fragmented")

    leaks = {}
    for basis in BASES:
        s = r["leak"][basis]
        rating_gain = s["risk_group_split"]["constant"] - s["risk_group_split"]["glm"]
        leaks[basis] = []
        for k in CREDIBILITY_K:
            gain_rg = s["risk_group_split"][k] - s["risk_group_split"]["glm"]
            gain_ip = s["idpol_split"][k] - s["idpol_split"]["glm"]
            leaks[basis].append((k, gain_rg, gain_ip, gain_rg - gain_ip, (gain_rg - gain_ip) / rating_gain))
        leaks[basis + "_rating_gain"] = rating_gain
    require(all(leak > 0 for _, _, _, leak, _ in leaks["reported"]), "the memoriser no longer gains from the IDpol split on reported claims")
    require(all(leak < 0.5 for _, _, _, leak, _ in leaks["priced"]), "the memoriser gains from the IDpol split on priced claims")
    mech = {b: r["leak"][b]["mechanism"] for b in BASES}
    require(mech["reported"]["with a piece in training"] < 5 * mech["reported"]["without"], "the reported gain is not on rows with a piece in training")
    require(mech["priced"]["with a piece in training"] >= mech["priced"]["without"], "the priced gain sits on rows with a piece in training")

    v = r["validation"]
    require(v["idpol_split"]["largest_claim_held"] and not v["risk_group_split"]["largest_claim_held"], "the largest claim moved")
    require(abs(v["idpol_split"]["capped_ae"] - v["risk_group_split"]["capped_ae"]) < 0.01, "capped A/E differs between the splits")
    require(v["idpol_split"]["loss_ae_without_largest"] < v["risk_group_split"]["loss_ae"], "the largest claim no longer explains the gap")

    def k_label(k: float) -> str:
        return f"{k:g}"

    reported_worst = leaks["reported"][0]
    reported_best = leaks["reported"][-1]
    gap = {b: r["leak"][b]["idpol_split"]["glm"] - r["leak"][b]["risk_group_split"]["glm"] for b in BASES}

    def improvement(split: str, part: str) -> float:
        return 1 - v[split][f"{part}_glm"] / v[split][f"{part}_constant"]

    out = [
        "# Holdout split",
        "",
        "Generated by `scripts/holdout_report.py`. Do not edit by hand; re-run the script.",
        "",
        f"Computed from `model.holdout` and the GLM runs `R/export_predictions.R` fits on each split's training rows, "
        f"frame md5 `{r['frame_md5']}`, {fmt(r['rows'], 0)} policy-years. Python {platform.python_version()}.",
        "",
        "**Rule, fixed before measuring.** Validation uses the risk-group split chosen in D2-3: every fifth",
        "risk group, a run of consecutive policy ids with identical rating factors, is held out. This",
        "document measures what the split the plan named, by IDpol, would have cost. Validation run:",
        f"`{pp.VALIDATION_RUN}`.",
        "",
        "## Splitting by IDpol is splitting by row",
        "",
        "IDpol is unique in the frame, so a split by IDpol is a split by row. The IDpol split here holds",
        "out a fifth of rows by an md5 hash of the id, which any language reproduces.",
        "",
        *md_table(
            ["Split", "Held-out rows", "Priced claims", "Reported claims", "Risk groups on both sides",
             "Held-out rows with a piece in training"],
            [["risk group", fmt(rg["holdout_rows"], 0), fmt(rg["priced_claims"], 0), fmt(rg["reported_claims"], 0),
              fmt(rg["straddling_groups"], 0), fmt(rg["sibling_rows"], 0)],
             ["IDpol", fmt(ip["holdout_rows"], 0), fmt(ip["priced_claims"], 0), fmt(ip["reported_claims"], 0),
              fmt(ip["straddling_groups"], 0), fmt(ip["sibling_rows"], 0)]],
        ),
        "",
        f"Under the IDpol split, {fmt(ip['sibling_rows'], 0)} held-out rows have another piece of their risk group in",
        f"training. They hold {pct(ip['sibling_exposure_share'])} of held-out exposure, {pct(ip['sibling_priced_share'])} of held-out priced claims",
        f"and {pct(ip['sibling_reported_share'])} of reported ones. The risk-group split has none by construction.",
        "",
        "## What the pieces share",
        "",
        f"{fmt(r['fragmented_groups'], 0)} of {fmt(r['risk_groups'], 0)} risk groups have two or more pieces. If the pieces were separate",
        "periods, each would claim independently. Expected counts below use each piece's Poisson",
        "probability of at least one claim from the GLM fitted on all rows.",
        "",
        *md_table(
            ["Claims", "Groups where every piece has a claim", "Expected if pieces claimed independently", "Ratio"],
            [[basis, fmt(obs, 0), fmt(exp, 1), fmt(ratio, 1)] for basis, (obs, exp, ratio) in sharing.items()],
        ),
        "",
        f"Reported claims are shared. In {fmt(d['groups'], 0)} groups every piece reports a claim, "
        f"{fmt(sharing['reported'][2], 0)} times what",
        f"independent pieces would give, and {fmt(d['groups_without_amount'], 0)} of those groups have no amount on any piece.",
        f"Priced claims are much closer to independent, {fmt(sharing['priced'][0], 0)} against {fmt(sharing['priced'][1], 1)}. The pattern is one claim count written onto every piece",
        "of a policy-year, with the amount, where there is one, attached to a single piece.",
        "",
        "This bears on D3-1's unpriced claims. In groups where every piece reports a claim and none has",
        f"an amount, counting the claims once, as the largest count on any one piece, removes",
        f"{fmt(d['repeated_counts'], 0)} of the {fmt(d['unpriced_total'], 0)} claims without an amount, "
        f"{pct(d['repeated_counts'] / d['unpriced_total'])}. For vehicle age 0 it removes",
        f"{fmt(d['repeated_new'], 0)} of {fmt(d['unpriced_new'], 0)}, {pct(d['repeated_new'] / d['unpriced_new'])}, "
        f"and {pct(r['fragmented_share']['vehicle age 0'])} of new-vehicle risk groups have two or more pieces",
        f"against {pct(r['fragmented_share']['older'])} for older vehicles. That part of the unpriced claims looks like a repeated count",
        "rather than a missing cost. The rest is still unexplained.",
        "",
        "## The leak, measured on the same rows",
        "",
        "A GLM with 76 parameters cannot remember a policy; a flexible model can. The memoriser stands in",
        "for one. It multiplies the GLM's prediction by the actual over expected claims of training rows",
        "with exactly the same nine rating factors, credibility-weighted as (claims + k) / (expected + k).",
        f"Both splits are scored on the {fmt(r['both_rows'], 0)} rows that both hold out, so the rows and their",
        "outcomes are identical and only the training rows differ. Poisson deviance, lower is better.",
        "",
    ]
    for basis in BASES:
        s = r["leak"][basis]
        rows = [["constant", fmt(s["risk_group_split"]["constant"], 1), fmt(s["idpol_split"]["constant"], 1),
                 fmt(s["idpol_split"]["constant"] - s["risk_group_split"]["constant"], 1)],
                ["GLM", fmt(s["risk_group_split"]["glm"], 1), fmt(s["idpol_split"]["glm"], 1),
                 fmt(s["idpol_split"]["glm"] - s["risk_group_split"]["glm"], 1)]]
        rows += [[f"memoriser, k = {k_label(k)}", fmt(s["risk_group_split"][k], 1), fmt(s["idpol_split"][k], 1),
                  fmt(s["idpol_split"][k] - s["risk_group_split"][k], 1)] for k in CREDIBILITY_K]
        out += [f"{basis.capitalize()} claims, {fmt(s['claims_on_both_rows'], 0)} on these rows:", "",
                *md_table(["Model", "Risk-group split", "IDpol split", "IDpol minus risk group"], rows), ""]
    out += [
        *md_table(
            ["Claims", "k", "Memoriser minus GLM, risk-group split", "Memoriser minus GLM, IDpol split",
             "Handed to the memoriser by the IDpol split", "As a share of the GLM's gain over a constant"],
            [[basis, k_label(k), fmt(g_rg, 1), fmt(g_ip, 1), fmt(leak, 1), pct(share)]
             for basis in BASES for k, g_rg, g_ip, leak, share in leaks[basis]],
        ),
        "",
        f"On reported claims the IDpol split hands the memoriser between {fmt(reported_best[3], 1)} and "
        f"{fmt(reported_worst[3], 1)} of deviance,",
        f"{pct(reported_best[4], 0)} to {pct(reported_worst[4], 0)} of everything the rating factors gain over a constant on the same rows "
        f"({fmt(leaks['reported_rating_gain'], 1)}).",
        "On the risk-group split the same memoriser gains almost nothing. On priced claims the IDpol split",
        "hands it nothing: " + ", ".join(fmt(leak, 1) for _, _, _, leak, _ in leaks["priced"]) + " at the three values of k.",
        "The GLM's own deviance differs between the splits by "
        f"{fmt(gap['priced'], 1)} on priced claims and {fmt(gap['reported'], 1)} on reported ones.",
        "",
        "Where the reported gain comes from, within the IDpol holdout, k = 2, deviance per 1,000 rows:",
        "",
        *md_table(
            ["Claims", "Memoriser minus GLM, rows with a piece in training", "Memoriser minus GLM, other rows"],
            [[basis, fmt(mech[basis]["with a piece in training"], 2), fmt(mech[basis]["without"], 2)] for basis in BASES],
        ),
        "",
        "On reported claims the gain sits on rows whose other pieces were in training, which is the",
        "repeated claim count being read back. On priced claims those rows gain less than the rest.",
        "",
        "## What each split reports for the pricing GLM",
        "",
        "The plan expected two sets of metrics, one per split, and their difference to be the leak.",
        "",
        *md_table(
            ["Priced claims, each split's own holdout", "Risk-group split", "IDpol split"],
            [
                ["held-out rows", fmt(v["risk_group_split"]["holdout_rows"], 0), fmt(v["idpol_split"]["holdout_rows"], 0)],
                ["held-out claims", fmt(v["risk_group_split"]["holdout_claims"], 0), fmt(v["idpol_split"]["holdout_claims"], 0)],
                ["off-balance factor, from training rows", fmt(v["risk_group_split"]["off_balance"], 6), fmt(v["idpol_split"]["off_balance"], 6)],
                ["frequency deviance, GLM against constant", pct(improvement("risk_group_split", "frequency"), 2) + " lower",
                 pct(improvement("idpol_split", "frequency"), 2) + " lower"],
                ["capped severity deviance, GLM against constant", pct(improvement("risk_group_split", "severity"), 2) + " lower",
                 pct(improvement("idpol_split", "severity"), 2) + " lower"],
                ["capped losses, actual over expected", fmt(v["risk_group_split"]["capped_ae"], 3), fmt(v["idpol_split"]["capped_ae"], 3)],
                ["recorded losses, actual over expected", fmt(v["risk_group_split"]["loss_ae"], 3), fmt(v["idpol_split"]["loss_ae"], 3)],
                ["recorded losses without the largest claim", fmt(v["risk_group_split"]["loss_ae_without_largest"], 3),
                 fmt(v["idpol_split"]["loss_ae_without_largest"], 3)],
            ],
        ),
        "",
        "For the GLM, on priced claims, there is no leak to find, and the columns differ by what each",
        f"holdout drew. The largest claim, {fmt(r['largest_claim'], 0)}, fell in the IDpol holdout: capped losses come in at",
        f"{fmt(v['risk_group_split']['capped_ae'], 3)} and {fmt(v['idpol_split']['capped_ae'], 3)} of expected, "
        f"recorded losses at {fmt(v['risk_group_split']['loss_ae'], 3)} and {fmt(v['idpol_split']['loss_ae'], 3)}, "
        f"and {fmt(v['idpol_split']['loss_ae_without_largest'], 3)} without that claim. Read as",
        "split against split, one claim would pass for a leak. That is why the leak above is measured on",
        "rows both splits share.",
        "",
        "## Handed on",
        "",
        "- **D3-3:** LightGBM trains on the risk-group split's training rows, with recorded losses as the",
        "  target. The leak measured here is in reported claim counts; a boosted model fitted to them",
        "  with a row split would be able to read it back.",
        f"- **D3-4 to D3-6:** the holdout is `risk_group_holdout` and the GLM is `{pp.VALIDATION_RUN}`,",
        "  rebalanced on its training rows. Recorded-loss A/E on one holdout moves with a single claim, so",
        "  calibration shows capped losses beside recorded ones, and the bootstrap resamples risk groups.",
        "- **D4:** part of the unpriced claims on new vehicles are repeated counts, which narrows, without",
        "  settling, the question of what the new-vehicle relativity should be.",
    ]
    return out


def main() -> None:
    DOCUMENT.write_bytes(("\n".join(render(measure())) + "\n").encode("utf-8"))
    print(f"wrote {DOCUMENT}")


if __name__ == "__main__":
    main()
