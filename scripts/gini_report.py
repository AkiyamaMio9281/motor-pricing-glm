"""Writes docs/gini.md and docs/figures/lorenz-curves.png.

    .venv/Scripts/python scripts/gini_report.py
"""

from __future__ import annotations

import platform
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import psycopg

import gini as g
import pure_premium as pp
from db import dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "gini.md"
FIGURE = REPO_ROOT / "docs" / "figures" / "lorenz-curves.png"
NOISE_SEED = 20260914
NOISE_SIGMA = 1.0
MODELS = ("constant", "GLM", "LightGBM", "noisy GLM")
COLORS = {"GLM": "#2a78d6", "LightGBM": "#eb6834"}
INK = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
       "grid": "#e1e0d9", "baseline": "#c3c2b7"}


def measure() -> dict:
    with psycopg.connect(dsn()) as conn:
        policies = pp.load_policies(conn)
        glm = pp.pure_premium(conn, pp.VALIDATION_RUN, policies)
        gbm = pp.benchmark_pure_premium(conn, pp.BENCHMARK_RUN, policies)

    held = glm.frame["risk_group_holdout"].to_numpy()
    frame = glm.frame[held]
    exposure = frame["exposure"].to_numpy()
    capped = frame["capped_loss"].to_numpy()
    recorded = frame["incurred_loss"].to_numpy()
    training = glm.frame[glm.frame["trained"]]
    noise = np.exp(np.random.default_rng(NOISE_SEED).normal(0, NOISE_SIGMA, exposure.size) - NOISE_SIGMA**2 / 2)
    predicted = {
        "constant": np.full(exposure.size, training["incurred_loss"].sum() / training["exposure"].sum()),
        "GLM": frame["amount_per_year"].to_numpy(),
        "LightGBM": gbm.frame.loc[held, "amount_per_year"].to_numpy(),
    }
    predicted["noisy GLM"] = predicted["GLM"] * noise

    definitions = {
        "reported": lambda p: g.lorenz_gini(p, capped, exposure),
        "recorded": lambda p: g.lorenz_gini(p, recorded, exposure),
        "normalized": lambda p: g.lorenz_gini(p, capped, exposure) / g.lorenz_gini(capped / exposure, capped, exposure),
        "descending": lambda p: g.lorenz_gini(-p, capped, exposure),
        "ties_in_row_order": lambda p: g.lorenz_gini_ties_in_row_order(p, capped, exposure),
        "row_per_year": lambda p: g.normalized_row_gini(p, capped),
        "row_expected_loss": lambda p: g.normalized_row_gini(p * exposure, capped),
        "auc": lambda p: g.auc_gini(p, capped > 0),
        "concentration": lambda p: g.lorenz_gini(p, p * exposure, exposure),
    }
    values = {name: {model: fn(predicted[model]) for model in MODELS} for name, fn in definitions.items()}

    low_ids = frame["idpol"].to_numpy() <= np.median(frame["idpol"].to_numpy())
    top_ten = np.sort(recorded)[-10:].sum() / recorded.sum()
    kept = recorded < np.sort(recorded)[-10]
    without_top_ten = {model: g.lorenz_gini(predicted[model][kept], recorded[kept], exposure[kept]) for model in ("GLM", "LightGBM")}
    curves = {model: g.lorenz_curve(predicted[model], capped, exposure) for model in ("GLM", "LightGBM")}
    return {
        "frame_md5": glm.run.frame_md5,
        "holdout_rows": int(held.sum()),
        "holdout_claims": int(frame["priced_claim_nb"].sum()),
        "values": values,
        "oracle": g.lorenz_gini(capped / exposure, capped, exposure),
        "unique_glm_predictions": int(np.unique(predicted["GLM"]).size),
        "top_ten_share": float(top_ten),
        "recorded_without_top_ten": without_top_ten,
        "largest_policy_loss": float(recorded.max()),
        "id_halves": {
            "low_rate": float(capped[low_ids].sum() / exposure[low_ids].sum()),
            "high_rate": float(capped[~low_ids].sum() / exposure[~low_ids].sum()),
            "low_exposure": float(exposure[low_ids].mean()),
            "high_exposure": float(exposure[~low_ids].mean()),
        },
        "curves": curves,
    }


def draw(r: dict) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, ax = plt.subplots(figsize=(6.4, 5.6), dpi=150)
    fig.patch.set_facecolor(INK["surface"])
    ax.set_facecolor(INK["surface"])
    ax.plot([0, 1], [0, 1], color=INK["baseline"], linewidth=1, solid_capstyle="round")
    ax.text(0.62, 0.575, "constant premium, Gini 0", color=INK["muted"], rotation=45, fontsize=9, ha="center", va="center")
    for model in ("GLM", "LightGBM"):
        x, y = r["curves"][model]
        gini = r["values"]["reported"][model]
        ax.plot(x, y, color=COLORS[model], linewidth=2, solid_joinstyle="round", solid_capstyle="round",
                label=f"{model}, Gini {gini:.3f}")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    ax.set_xlabel("Share of held-out exposure, lowest predicted pure premium first", color=INK["secondary"])
    ax.set_ylabel("Share of held-out capped losses", color=INK["secondary"])
    ax.set_title("Ordered Lorenz curves on the risk-group holdout", color=INK["primary"], loc="left", fontsize=11)
    ax.grid(color=INK["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK["muted"], labelcolor=INK["secondary"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK["baseline"])
    legend = ax.legend(loc="upper left", frameon=False)
    for text in legend.get_texts():
        text.set_color(INK["primary"])
    fig.tight_layout()
    FIGURE.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE, facecolor=INK["surface"], metadata={"Software": None})
    plt.close(fig)


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
    v = r["values"]
    rep, rec = v["reported"], v["recorded"]
    ids = r["id_halves"]
    require(rep["constant"] == 0 and rec["constant"] == 0, "a constant model no longer scores zero on the reported definition")
    require(rep["LightGBM"] > rep["GLM"], "LightGBM no longer ranks capped losses better")
    require(rec["GLM"] > rec["LightGBM"], "the recorded-loss Gini no longer reverses the order")
    require(all(abs(v["descending"][m] + rep[m]) < 1e-12 for m in MODELS), "the descending Gini is no longer the negative")
    require(all(abs(v["normalized"][m] - rep[m]) < 0.01 for m in ("GLM", "LightGBM")), "normalizing now moves the Gini")
    require(all(abs(v["ties_in_row_order"][m] - rep[m]) < 5e-4 for m in ("GLM", "LightGBM")), "ties now move the models' Gini")
    require(v["auc"]["LightGBM"] > v["auc"]["GLM"], "the AUC Gini no longer orders the models as the reported one does")
    top = r["recorded_without_top_ten"]
    require(top["LightGBM"] > top["GLM"], "removing the ten largest losses no longer restores the capped order")
    require(v["ties_in_row_order"]["constant"] < 0 < v["row_per_year"]["constant"], "row-order ties no longer move the constant model both ways")
    require(ids["low_rate"] > ids["high_rate"] and ids["low_exposure"] > ids["high_exposure"], "low policy ids no longer carry more")
    require(v["row_expected_loss"]["constant"] > v["row_per_year"]["constant"] > 0.05, "the unweighted Gini no longer credits the constant model")
    require(v["concentration"]["noisy GLM"] > max(v["concentration"]["GLM"], v["concentration"]["LightGBM"]), "noise no longer maximises spread")
    require(rep["noisy GLM"] < min(rep["GLM"], rep["LightGBM"]), "noise no longer lowers the reported Gini")
    require(abs(v["concentration"]["GLM"] - v["concentration"]["LightGBM"]) < 0.01, "the two models' price spreads now differ")

    def row(label: str, key: str, digits: int = 3) -> list[str]:
        return [label, *[fmt(v[key][m], digits) for m in MODELS]]

    draw(r)
    out = [
        "# Gini",
        "",
        "Generated by `scripts/gini_report.py`, which also draws `docs/figures/lorenz-curves.png`. Do not",
        "edit by hand; re-run the script.",
        "",
        f"Frame md5 `{r['frame_md5']}`. Risk-group holdout, {fmt(r['holdout_rows'], 0)} policy-years and {fmt(r['holdout_claims'], 0)} priced claims.",
        f"GLM run `{pp.VALIDATION_RUN}`, benchmark run `{pp.BENCHMARK_RUN}`. Python {platform.python_version()}.",
        "",
        "**Rule, fixed before computing.** The Gini reported for this project is the ordered Lorenz Gini",
        "on capped losses, defined below. The same Gini on recorded losses is shown beside it. Every",
        "other version is computed to show what it would say, and none is reported.",
        "",
        "## The reported Gini",
        "",
        "**Definition.** Sort held-out policy-years by predicted pure premium per policy-year, lowest",
        "first, merging policy-years with identical predictions into one step. Plot cumulative share of",
        "exposure on the x-axis against cumulative share of capped losses on the y-axis. The Gini is",
        "1 minus twice the area under that curve: 0 for a model that charges everyone the same, and",
        "higher the more of the losses sit at the expensive end.",
        "",
        *md_table(
            ["Model", "Gini, capped losses (reported)", "Gini, recorded losses", "Reported Gini over the best possible ordering"],
            [[m, fmt(rep[m], 3), fmt(rec[m], 3), fmt(v["normalized"][m], 3)] for m in ("constant", "GLM", "LightGBM")],
        ),
        "",
        "![Ordered Lorenz curves](figures/lorenz-curves.png)",
        "",
        f"On capped losses LightGBM ranks better, {fmt(rep['LightGBM'], 3)} against {fmt(rep['GLM'], 3)}. On recorded losses the order reverses,",
        f"{fmt(rec['GLM'], 3)} against {fmt(rec['LightGBM'], 3)}. The ten largest held-out policy losses are {pct(r['top_ten_share'])} of recorded losses, and",
        f"the largest is {fmt(r['largest_policy_loss'], 0)}. A Lorenz curve on recorded losses jumps wherever one of them lands,",
        f"and without those ten policy-years LightGBM leads again, {fmt(top['LightGBM'], 3)} against {fmt(top['GLM'], 3)}. The reversal is",
        "about where ten claims landed. Both models price large losses with the same flat load, which is",
        "why the capped Gini is the one that measures what they rate. Whether either gap is larger than",
        "the holdout's noise is D3-6's question.",
        "",
        f"Dividing by the best possible ordering, sorting by actual capped loss per policy-year, changes",
        f"little, because that ordering reaches {fmt(r['oracle'], 3)} on data this sparse.",
        "",
        "## Other things called Gini",
        "",
        "The same held-out rows and predictions under each version. The noisy GLM multiplies every GLM",
        f"prediction by an independent lognormal factor with sigma = {NOISE_SIGMA:g}, mean 1: the same model with its",
        "ranking deliberately degraded.",
        "",
        *md_table(
            ["Version", *MODELS],
            [
                row("ordered Lorenz, capped losses (reported)", "reported"),
                row("ordered Lorenz, recorded losses", "recorded"),
                row("ordered Lorenz, sorted highest premium first", "descending"),
                row("ordered Lorenz, ties broken by policy id", "ties_in_row_order"),
                row("unweighted by exposure, normalized, sorted by premium per year", "row_per_year"),
                row("unweighted by exposure, normalized, sorted by expected loss", "row_expected_loss"),
                row("2 × AUC − 1, any priced claim", "auc"),
                row("Gini coefficient of the predicted premiums", "concentration"),
            ],
        ),
        "",
        "- **Sorted highest premium first.** The same formula returns the negative. The sign carries the",
        "  direction of the sort, so a negative Gini usually means the sort was reversed, not that the",
        "  model is worse than a constant.",
        f"- **Ties broken by policy id.** Only the constant model moves, to {fmt(v['ties_in_row_order']['constant'], 3)}, because its every",
        f"  prediction is a tie. Policy id order is not neutral here: the lower half of held-out ids has a",
        f"  capped loss rate of {fmt(ids['low_rate'], 1)} per policy-year and a mean exposure of {fmt(ids['low_exposure'], 3)}, against {fmt(ids['high_rate'], 1)} and",
        f"  {fmt(ids['high_exposure'], 3)} for the upper half. The GLM has {fmt(r['unique_glm_predictions'], 0)} distinct predictions on {fmt(r['holdout_rows'], 0)} rows, and",
        "  breaking its ties by id moves its Gini by less than 0.0005.",
        "- **Unweighted by exposure.** This is the version common in machine-learning competitions:",
        "  rows sorted by prediction, each row counted once, normalized by the actual ordering. A",
        f"  constant model scores {fmt(v['row_per_year']['constant'], 3)} from row order alone. Sorted by expected loss, exposure times",
        f"  the annual rate, it scores {fmt(v['row_expected_loss']['constant'], 3)}: credit for row order and for knowing how long each",
        "  policy ran, which is the units mistake D3-1 guards against in another form.",
        "- **2 × AUC − 1.** Ranks policies with and without a claim. It ignores claim size and",
        "  exposure, so it measures a different thing, though here it orders the models the same way.",
        f"- **Gini coefficient of the predicted premiums.** This measures how spread out the prices are,",
        f"  not whether they are right. The noisy GLM has the widest spread, {fmt(v['concentration']['noisy GLM'], 3)}, and the worst",
        f"  reported Gini, {fmt(rep['noisy GLM'], 3)}. The GLM and LightGBM spread their prices almost equally, "
        f"{fmt(v['concentration']['GLM'], 3)} and {fmt(v['concentration']['LightGBM'], 3)}.",
        "",
        "## Handed on",
        "",
        "- **D3-5:** lift and calibration use the same holdout and the same ordering by predicted pure",
        "  premium per policy-year.",
        f"- **D3-6:** bootstrap the Gini gap over risk groups, on capped losses, where LightGBM leads by",
        f"  {fmt(rep['LightGBM'] - rep['GLM'], 3)}, and on recorded losses, where the GLM leads by {fmt(rec['GLM'] - rec['LightGBM'], 3)}.",
    ]
    return out


def main() -> None:
    DOCUMENT.write_bytes(("\n".join(render(measure())) + "\n").encode("utf-8"))
    print(f"wrote {DOCUMENT} and {FIGURE}")


if __name__ == "__main__":
    main()
