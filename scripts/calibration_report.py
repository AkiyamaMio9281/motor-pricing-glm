"""Writes docs/calibration.md, docs/figures/decile-lift.png and docs/figures/calibration-by-segment.png.

    .venv/Scripts/python scripts/calibration_report.py
"""

from __future__ import annotations

import platform
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import psycopg
from matplotlib.ticker import FixedLocator, NullLocator

import calibration as cal
import pure_premium as pp
from db import dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = REPO_ROOT / "docs" / "calibration.md"
LIFT_FIGURE = REPO_ROOT / "docs" / "figures" / "decile-lift.png"
SEGMENT_FIGURE = REPO_ROOT / "docs" / "figures" / "calibration-by-segment.png"
MODELS = ("GLM", "LightGBM")
COLORS = {"GLM": "#2a78d6", "LightGBM": "#eb6834"}
INK = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
       "grid": "#e1e0d9", "baseline": "#c3c2b7"}


def bands(column: str, lower: list[int]) -> list[tuple[str, object]]:
    upper = [*lower[1:], None]
    out = []
    for low, high in zip(lower, upper):
        label = f"{low}+" if high is None else (str(low) if high == low + 1 else f"{low}-{high - 1}")
        out.append((label, lambda f, low=low, high=high: (f[column] >= low) & (True if high is None else f[column] < high)))
    return out


SEGMENTS = {
    "exposure, years": [
        ("under 0.1", lambda f: f["exposure"] < 0.1),
        ("0.1 to under 0.5", lambda f: (f["exposure"] >= 0.1) & (f["exposure"] < 0.5)),
        ("0.5 to under 1", lambda f: (f["exposure"] >= 0.5) & (f["exposure"] < 1)),
        ("a full year", lambda f: f["exposure"] == 1),
    ],
    "driver age": bands("driv_age", [18, 25, 35, 50, 65]),
    "vehicle age": bands("veh_age", [0, 1, 3, 10]),
    "bonus-malus": bands("bonus_malus", [50, 51, 100]),
}


def measure() -> dict:
    with psycopg.connect(dsn()) as conn:
        policies = pp.load_policies(conn)
        glm = pp.pure_premium(conn, pp.VALIDATION_RUN, policies)
        gbm = pp.benchmark_pure_premium(conn, pp.BENCHMARK_RUN, policies)

    held = glm.frame["risk_group_holdout"].to_numpy()
    frame = glm.frame[held].reset_index(drop=True)
    exposure = frame["exposure"].to_numpy()
    capped = frame["capped_loss"].to_numpy()
    recorded = frame["incurred_loss"].to_numpy()
    group = frame["risk_group"].to_numpy()
    rate = {
        "GLM": (frame["claims_per_year"] * frame["capped_amount_per_claim"]).to_numpy(),
        "LightGBM": gbm.frame.loc[held, "capped_amount_per_year"].to_numpy(),
    }
    expected_recorded = {"GLM": frame["expected_loss"].to_numpy(), "LightGBM": gbm.frame.loc[held, "expected_loss"].to_numpy()}

    deciles = {}
    for model in MODELS:
        decile = cal.equal_exposure_deciles(rate[model], exposure)
        rows = []
        for k in range(10):
            m = decile == k
            ratio, low, high = cal.ratio_with_interval(capped[m], exposure[m] * rate[model][m], group[m])
            predicted = (exposure[m] * rate[model][m]).sum() / exposure[m].sum()
            rows.append({
                "decile": k + 1,
                "exposure_share": float(exposure[m].sum() / exposure.sum()),
                "predicted": float(predicted),
                "actual": float(capped[m].sum() / exposure[m].sum()),
                "actual_low": float(low * predicted),
                "actual_high": float(high * predicted),
                "ae": ratio, "ae_low": low, "ae_high": high,
                "recorded_ae": float(recorded[m].sum() / expected_recorded[model][m].sum()),
            })
        deciles[model] = rows

    segments = []
    for dimension, members in SEGMENTS.items():
        for label, member in members:
            m = np.asarray(member(frame), dtype=bool)
            row = {"dimension": dimension, "segment": label,
                   "exposure_share": float(exposure[m].sum() / exposure.sum()),
                   "claims": int(frame.loc[m, "priced_claim_nb"].sum())}
            for model in MODELS:
                ratio, low, high = cal.ratio_with_interval(capped[m], exposure[m] * rate[model][m], group[m])
                row[model] = {"ae": ratio, "low": low, "high": high,
                              "recorded_ae": float(recorded[m].sum() / expected_recorded[model][m].sum())}
            segments.append(row)

    return {
        "frame_md5": glm.run.frame_md5,
        "holdout_rows": int(held.sum()),
        "holdout_claims": int(frame["priced_claim_nb"].sum()),
        "deciles": deciles,
        "segments": segments,
    }


def style(ax) -> None:
    ax.set_facecolor(INK["surface"])
    ax.grid(color=INK["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK["muted"], labelcolor=INK["secondary"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK["baseline"])


def draw_lift(r: dict) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.8), dpi=150, sharey=True)
    fig.patch.set_facecolor(INK["surface"])
    top = max(max(row["actual_high"], row["predicted"]) for model in MODELS for row in r["deciles"][model])
    for ax, model in zip(axes, MODELS):
        rows = r["deciles"][model]
        x = np.array([row["decile"] for row in rows])
        actual = np.array([row["actual"] for row in rows])
        low = np.array([row["actual_low"] for row in rows])
        high = np.array([row["actual_high"] for row in rows])
        predicted = np.array([row["predicted"] for row in rows])
        style(ax)
        ax.vlines(x, low, high, color=INK["muted"], linewidth=1.5)
        ax.plot(x, actual, "o", color=INK["secondary"], markersize=6, label="actual, 95% interval")
        ax.plot(x, predicted, "-o", color=COLORS[model], linewidth=2, markersize=4, label="predicted")
        ax.set_title(model, color=INK["primary"], loc="left", fontsize=11)
        ax.set_xticks(x)
        ax.set_xlabel(f"Decile of exposure, by {model} predicted pure premium", color=INK["secondary"])
        ax.set_ylim(0, top * 1.05)
        legend = ax.legend(loc="upper left", frameon=False)
        for text in legend.get_texts():
            text.set_color(INK["primary"])
    axes[0].set_ylabel("Capped loss per policy-year, holdout", color=INK["secondary"])
    fig.tight_layout()
    fig.savefig(LIFT_FIGURE, facecolor=INK["surface"], metadata={"Software": None})
    plt.close(fig)


def draw_segments(r: dict) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    rows = r["segments"]
    labels, positions, headers = [], [], []
    y = 0.0
    last = None
    for row in rows:
        if row["dimension"] != last:
            y += 0.6 if last is not None else 0
            headers.append((y, row["dimension"]))
            y += 1.0
            last = row["dimension"]
        positions.append(y)
        labels.append(row["segment"])
        y += 1.0
    fig, ax = plt.subplots(figsize=(7.6, 8.2), dpi=150)
    fig.patch.set_facecolor(INK["surface"])
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.axvline(1, color=INK["baseline"], linewidth=1)
    for offset, model in ((-0.17, "GLM"), (0.17, "LightGBM")):
        ys = np.array(positions) + offset
        ae = np.array([row[model]["ae"] for row in rows])
        ax.hlines(ys, [row[model]["low"] for row in rows], [row[model]["high"] for row in rows], color=COLORS[model], linewidth=1.5)
        ax.plot(ae, ys, "o", color=COLORS[model], markersize=6, label=model)
    for y_header, name in headers:
        ax.text(0.02, y_header, name, transform=ax.get_yaxis_transform(), color=INK["primary"], fontsize=10,
                fontweight="bold", va="center", ha="left")
    ax.set_xscale("log")
    ax.xaxis.set_major_locator(FixedLocator([0.5, 0.75, 1, 1.5, 2, 3, 4]))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_xticklabels(["0.5", "0.75", "1", "1.5", "2", "3", "4"])
    lows = [row[m]["low"] for row in rows for m in MODELS]
    highs = [row[m]["high"] for row in rows for m in MODELS]
    ax.set_xlim(min(0.5, min(lows) * 0.95), max(4, max(highs) * 1.05))
    ax.set_yticks(positions)
    ax.set_yticklabels(labels)
    ax.set_ylim(y, -0.8)
    ax.set_xlabel("Capped losses, actual over expected, holdout (log scale)", color=INK["secondary"])
    ax.set_title("Calibration by segment, 95% intervals by risk group", color=INK["primary"], loc="left", fontsize=11)
    legend = ax.legend(loc="lower right", frameon=False)
    for text in legend.get_texts():
        text.set_color(INK["primary"])
    fig.tight_layout()
    fig.savefig(SEGMENT_FIGURE, facecolor=INK["surface"], metadata={"Software": None})
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


def interval(value: float, low: float, high: float, digits: int = 3, bound_digits: int = 2) -> str:
    return f"{fmt(value, digits)} [{fmt(low, bound_digits)}, {fmt(high, bound_digits)}]"


def excludes_one(low: float, high: float) -> bool:
    return low > 1 or high < 1


def render(r: dict) -> list[str]:
    d = r["deciles"]
    ends = {m: (d[m][0]["actual"], d[m][-1]["actual"]) for m in MODELS}
    spread = {m: ends[m][1] / ends[m][0] for m in MODELS}
    predicted_spread = {m: d[m][-1]["predicted"] / d[m][0]["predicted"] for m in MODELS}
    flagged_deciles = {m: [row["decile"] for row in d[m] if excludes_one(row["ae_low"], row["ae_high"])] for m in MODELS}
    recorded_range = {m: (min(row["recorded_ae"] for row in d[m]), max(row["recorded_ae"] for row in d[m])) for m in MODELS}
    capped_range = {m: (min(row["ae"] for row in d[m]), max(row["ae"] for row in d[m])) for m in MODELS}
    require(all(recorded_range[m][1] - recorded_range[m][0] > capped_range[m][1] - capped_range[m][0] for m in MODELS),
            "recorded-loss deciles no longer swing more than capped ones")
    require(spread["LightGBM"] > spread["GLM"], "LightGBM no longer separates the end deciles further")
    require(ends["LightGBM"][0] < ends["GLM"][0] and ends["LightGBM"][1] > ends["GLM"][1], "LightGBM no longer reaches further at both ends")
    require(d["GLM"][0]["actual"] > d["GLM"][1]["actual"], "the GLM's first two deciles are no longer out of order")
    require(sum(len(v) for v in flagged_deciles.values()) <= 2, "more decile intervals exclude 1 than chance would explain")

    seg = r["segments"]
    by_name = {(row["dimension"], row["segment"]): row for row in seg}
    short, part, full = (by_name[("exposure, years", k)] for k in ("under 0.1", "0.5 to under 1", "a full year"))
    require(all(short[m]["low"] > 1 and full[m]["high"] < 1 for m in MODELS), "the partial-year gap no longer excludes 1")
    require(all(not excludes_one(part[m]["low"], part[m]["high"]) for m in MODELS), "half-to-full-year policies now miss")
    require(all(abs(row["GLM"]["ae"] - row["LightGBM"]["ae"]) < 0.01 for row in seg if row["dimension"] == "exposure, years"),
            "the two models no longer miss exposure equally")
    other = [row for row in seg if row["dimension"] != "exposure, years"]
    flagged_other = {m: [(row["dimension"], row["segment"]) for row in other if excludes_one(row[m]["low"], row[m]["high"])] for m in MODELS}
    require(flagged_other["GLM"] == flagged_other["LightGBM"] == [("driver age", "25-34")],
            "the segments outside exposure whose interval excludes 1 have changed")
    young = by_name[("driver age", "25-34")]

    def flagged_text(values: list[int]) -> str:
        if not values:
            return "none"
        return ("decile " if len(values) == 1 else "deciles ") + ", ".join(str(v) for v in values)

    lift_rows = []
    for k in range(10):
        g, l = d["GLM"][k], d["LightGBM"][k]
        lift_rows.append([str(k + 1), fmt(g["predicted"], 1), interval(g["actual"], g["actual_low"], g["actual_high"], 1, 1),
                          fmt(g["recorded_ae"], 2), fmt(l["predicted"], 1), interval(l["actual"], l["actual_low"], l["actual_high"], 1, 1),
                          fmt(l["recorded_ae"], 2)])
    segment_rows = [[row["dimension"], row["segment"], pct(row["exposure_share"]), fmt(row["claims"], 0),
                     interval(row["GLM"]["ae"], row["GLM"]["low"], row["GLM"]["high"]),
                     interval(row["LightGBM"]["ae"], row["LightGBM"]["low"], row["LightGBM"]["high"]),
                     fmt(row["GLM"]["recorded_ae"], 2), fmt(row["LightGBM"]["recorded_ae"], 2)] for row in seg]
    other_intervals = 2 * len(other)

    draw_lift(r)
    draw_segments(r)
    return [
        "# Lift and calibration",
        "",
        "Generated by `scripts/calibration_report.py`, which also draws `docs/figures/decile-lift.png` and",
        "`docs/figures/calibration-by-segment.png`. Do not edit by hand; re-run the script.",
        "",
        f"Frame md5 `{r['frame_md5']}`. Risk-group holdout, {fmt(r['holdout_rows'], 0)} policy-years and {fmt(r['holdout_claims'], 0)} priced claims.",
        f"GLM run `{pp.VALIDATION_RUN}`, benchmark run `{pp.BENCHMARK_RUN}`. Python {platform.python_version()}.",
        "",
        "**Rules, fixed before computing.** Deciles hold equal exposure, ordered by each model's own",
        "predicted pure premium per policy-year, with identical predictions kept in one decile. Segments",
        "are exposure under 0.1, 0.1 to under 0.5, 0.5 to under 1 and a full year; driver age 18-24,",
        "25-34, 35-49, 50-64 and 65+; vehicle age 0, 1-2, 3-9 and 10+; bonus-malus 50, 51-99 and 100+.",
        "Actual over expected uses capped losses, with recorded losses beside it. Intervals are 95%,",
        "from the variance of actual minus expected summed within risk groups. A segment counts as",
        "miscalibrated only if its interval excludes 1.",
        "",
        "## Decile lift",
        "",
        "![Decile lift](figures/decile-lift.png)",
        "",
        *md_table(
            ["Decile", "GLM predicted", "GLM actual [95%]", "GLM recorded A/E",
             "LightGBM predicted", "LightGBM actual [95%]", "LightGBM recorded A/E"],
            lift_rows,
        ),
        "",
        "Predicted and actual are capped loss per policy-year.",
        "",
        f"Both models' actual losses climb with their predictions. The top decile's actual capped loss rate is",
        f"{fmt(spread['GLM'], 1)} times the bottom decile's for the GLM and {fmt(spread['LightGBM'], 1)} times for LightGBM, against predicted",
        f"ratios of {fmt(predicted_spread['GLM'], 1)} and {fmt(predicted_spread['LightGBM'], 1)}. LightGBM reaches further at both ends: its bottom decile runs at",
        f"{fmt(ends['LightGBM'][0], 1)} against the GLM's {fmt(ends['GLM'][0], 1)}, its top at {fmt(ends['LightGBM'][1], 1)} against {fmt(ends['GLM'][1], 1)}. "
        f"The GLM's first two deciles are out of order, {fmt(d['GLM'][0]['actual'], 1)} then",
        f"{fmt(d['GLM'][1]['actual'], 1)}.",
        "",
        f"Deciles whose interval excludes 1: GLM {flagged_text(flagged_deciles['GLM'])}; LightGBM {flagged_text(flagged_deciles['LightGBM'])}. Among twenty",
        "intervals, about one would exclude 1 by chance.",
        "",
        f"On recorded losses the same deciles swing from {fmt(recorded_range['GLM'][0], 2)} to {fmt(recorded_range['GLM'][1], 2)} of expected for the GLM and",
        f"from {fmt(recorded_range['LightGBM'][0], 2)} to {fmt(recorded_range['LightGBM'][1], 2)} for LightGBM. On capped losses they run from "
        f"{fmt(capped_range['GLM'][0], 2)} to {fmt(capped_range['GLM'][1], 2)} and from",
        f"{fmt(capped_range['LightGBM'][0], 2)} to {fmt(capped_range['LightGBM'][1], 2)}. That is why the chart is drawn on capped losses.",
        "",
        "## Calibration by segment",
        "",
        "![Calibration by segment](figures/calibration-by-segment.png)",
        "",
        *md_table(
            ["Dimension", "Segment", "Exposure", "Priced claims", "GLM A/E [95%]", "LightGBM A/E [95%]",
             "GLM recorded A/E", "LightGBM recorded A/E"],
            segment_rows,
        ),
        "",
        "Exposure is where both models miss, and they miss identically. Policy-years under 0.1 of a",
        f"year run at {fmt(short['GLM']['ae'], 2)} and {fmt(short['LightGBM']['ae'], 2)} of expected, full years at {fmt(full['GLM']['ae'], 2)} and {fmt(full['LightGBM']['ae'], 2)}. "
        "Policy-years of half a year",
        "or more but short of a full year are within noise. This is D3-1's partial-year gap, found",
        "in-sample, now on held-out risk groups. LightGBM does not close it because neither model is",
        "given exposure as an input: both price a policy-year and scale it pro rata, by design.",
        "",
        f"Outside exposure, one segment's interval excludes 1, and for both models: drivers aged 25-34,",
        f"at {fmt(young['GLM']['ae'], 3)} and {fmt(young['LightGBM']['ae'], 3)}. Among the {other_intervals} intervals outside exposure, about one would exclude 1",
        "by chance, and the two models' errors are correlated, so it is recorded here and not acted on.",
        "Vehicle age and bonus-malus are within noise for both.",
        "",
        "## Handed on",
        "",
        "- **D3-6:** bootstrap the Gini and deviance gaps over risk groups.",
        f"- **D4:** a full-year rate carries the partial-year gap. Full policy-years run at {fmt(full['GLM']['ae'], 2)} of",
        "  expected on the holdout, and the rate table has to say whether the claims of short",
        "  policy-years belong in a full-year price. Drivers aged 25-34 are worth a second look there.",
    ]


def main() -> None:
    DOCUMENT.write_bytes(("\n".join(render(measure())) + "\n").encode("utf-8"))
    print(f"wrote {DOCUMENT}, {LIFT_FIGURE} and {SEGMENT_FIGURE}")


if __name__ == "__main__":
    main()
