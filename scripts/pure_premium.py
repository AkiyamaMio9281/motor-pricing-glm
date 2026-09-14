"""Pure premium from the GLM predictions in model.glm_prediction."""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd
import psycopg

from frame import canonical_md5, load_frequency_frame

PRICING_RUN = "priced_claims"
CLAIMS_BALANCE_TOLERANCE = 1e-6
LOSS_BALANCE_TOLERANCE = 0.01


class BasisError(ValueError):
    pass


class Run(NamedTuple):
    name: str
    frame_md5: str
    frequency_response: str
    large_loss_cap: float
    large_loss_load: float


class PurePremium(NamedTuple):
    run: Run
    frame: pd.DataFrame
    off_balance: float


def amount_per_year(claims_per_year, capped_amount_per_claim, large_loss_load):
    return claims_per_year * capped_amount_per_claim * large_loss_load


def check_claims_per_year(frame: pd.DataFrame, response: str) -> float:
    ratio = (frame["exposure"] * frame["claims_per_year"]).sum() / frame[response].sum()
    if abs(ratio - 1) > CLAIMS_BALANCE_TOLERANCE:
        raise BasisError(
            f"exposure times claims_per_year is {ratio:.4f} of observed {response}; "
            "claims_per_year must be claims per policy-year, not expected claims for the exposure"
        )
    return ratio


def check_capped_amount_per_claim(frame: pd.DataFrame, capped_losses: float) -> float:
    ratio = (frame["priced_claim_nb"] * frame["capped_amount_per_claim"]).sum() / capped_losses
    if abs(ratio - 1) > LOSS_BALANCE_TOLERANCE:
        raise BasisError(
            f"priced claims times capped_amount_per_claim is {ratio:.4f} of capped losses; "
            "capped_amount_per_claim must be a capped amount per claim, without the large-loss load"
        )
    return ratio


def check_large_loss_load(frame: pd.DataFrame, capped_losses: float, large_loss_load: float) -> None:
    recomputed = frame["incurred_loss"].sum() / capped_losses
    if abs(recomputed / large_loss_load - 1) > 1e-9:
        raise BasisError(f"the run's large-loss load is {large_loss_load:.6f}, the claims give {recomputed:.6f}")


def loss_off_balance(frame: pd.DataFrame, per_year: pd.Series) -> float:
    off_balance = frame["incurred_loss"].sum() / (frame["exposure"] * per_year).sum()
    if abs(off_balance - 1) > LOSS_BALANCE_TOLERANCE:
        raise BasisError(
            f"exposure times amount_per_year needs an off-balance factor of {off_balance:.4f} to meet "
            f"recorded losses, beyond the {LOSS_BALANCE_TOLERANCE:.0%} a rebalance may absorb"
        )
    return off_balance


def assemble(run: Run, frame: pd.DataFrame, capped_losses: float) -> PurePremium:
    check_claims_per_year(frame, run.frequency_response)
    check_capped_amount_per_claim(frame, capped_losses)
    check_large_loss_load(frame, capped_losses, run.large_loss_load)
    per_year = amount_per_year(frame["claims_per_year"], frame["capped_amount_per_claim"], run.large_loss_load)
    off_balance = loss_off_balance(frame, per_year)
    frame = frame.assign(amount_per_year=per_year * off_balance)
    frame["expected_loss"] = frame["exposure"] * frame["amount_per_year"]
    return PurePremium(run, frame, off_balance)


def load_run(conn: psycopg.Connection, run: str, policies: pd.DataFrame | None = None) -> tuple[Run, pd.DataFrame, float]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run, frame_md5, frequency_response, large_loss_cap, large_loss_load "
            "FROM model.glm_run WHERE run = %s",
            (run,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no GLM run {run!r}; run Rscript R/export_predictions.R")
        meta = Run(*row)
        cur.execute(
            "SELECT p.idpol, p.claims_per_year, p.capped_amount_per_claim, l.priced_claim_nb, l.incurred_loss "
            "FROM model.glm_prediction p JOIN model.policy_loss l USING (idpol) "
            "WHERE p.run = %s ORDER BY p.idpol",
            (run,),
        )
        rows = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
        cur.execute("SELECT sum(least(claim_amount, %s))::float8 FROM fact.claim", (meta.large_loss_cap,))
        capped_losses = cur.fetchone()[0]

    if policies is None:
        policies = load_frequency_frame(conn)
    if canonical_md5(policies) != meta.frame_md5:
        raise BasisError(f"run {run!r} was fitted on another frame; re-run Rscript R/export_predictions.R")
    if len(rows) != len(policies) or not (rows["idpol"].to_numpy() == policies["idpol"].to_numpy()).all():
        raise BasisError(f"run {run!r} does not cover the frame policy for policy")

    columns = ["claims_per_year", "capped_amount_per_claim", "priced_claim_nb", "incurred_loss"]
    frame = policies.assign(**{c: rows[c].to_numpy() for c in columns})
    return meta, frame, capped_losses


def pure_premium(conn: psycopg.Connection, run: str = PRICING_RUN, policies: pd.DataFrame | None = None) -> PurePremium:
    return assemble(*load_run(conn, run, policies))
