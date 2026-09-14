"""Pure premium from the GLM predictions in model.glm_prediction."""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg

from frame import canonical_md5, load_frequency_frame

PRICING_RUN = "priced_claims"
VALIDATION_RUN = "priced_claims_risk_group_split"
CLAIMS_BALANCE_TOLERANCE = 1e-6
LOSS_BALANCE_TOLERANCE = 0.01

TRAINING = {
    "all": lambda frame: np.ones(len(frame), dtype=bool),
    "risk_group_split": lambda frame: ~frame["risk_group_holdout"].to_numpy(),
    "idpol_split": lambda frame: ~frame["idpol_holdout"].to_numpy(),
}


class BasisError(ValueError):
    pass


class Run(NamedTuple):
    name: str
    frame_md5: str
    frequency_response: str
    large_loss_cap: float
    large_loss_load: float
    trained_on: str


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


def check_capped_amount_per_claim(frame: pd.DataFrame) -> float:
    ratio = (frame["priced_claim_nb"] * frame["capped_amount_per_claim"]).sum() / frame["capped_loss"].sum()
    if abs(ratio - 1) > LOSS_BALANCE_TOLERANCE:
        raise BasisError(
            f"priced claims times capped_amount_per_claim is {ratio:.4f} of capped losses; "
            "capped_amount_per_claim must be a capped amount per claim, without the large-loss load"
        )
    return ratio


def check_large_loss_load(frame: pd.DataFrame, large_loss_load: float) -> None:
    recomputed = frame["incurred_loss"].sum() / frame["capped_loss"].sum()
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


def assemble(run: Run, frame: pd.DataFrame) -> PurePremium:
    trained = frame[frame["trained"]]
    check_claims_per_year(trained, run.frequency_response)
    check_capped_amount_per_claim(trained)
    check_large_loss_load(trained, run.large_loss_load)
    per_year = amount_per_year(frame["claims_per_year"], frame["capped_amount_per_claim"], run.large_loss_load)
    off_balance = loss_off_balance(trained, per_year[frame["trained"]])
    frame = frame.assign(amount_per_year=per_year * off_balance)
    frame["expected_loss"] = frame["exposure"] * frame["amount_per_year"]
    return PurePremium(run, frame, off_balance)


def load_policies(conn: psycopg.Connection) -> pd.DataFrame:
    policies = load_frequency_frame(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.idpol, h.risk_group, h.risk_group_holdout, h.idpol_holdout, l.priced_claim_nb, l.incurred_loss "
            "FROM model.holdout h JOIN model.policy_loss l USING (idpol) ORDER BY h.idpol"
        )
        rows = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    if not (rows["idpol"].to_numpy() == policies["idpol"].to_numpy()).all():
        raise BasisError("model.holdout and model.policy_loss do not match the frame policy for policy")
    return policies.assign(**{c: rows[c].to_numpy() for c in rows.columns if c != "idpol"})


def load_run(conn: psycopg.Connection, run: str, policies: pd.DataFrame | None = None) -> tuple[Run, pd.DataFrame]:
    if policies is None:
        policies = load_policies(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run, frame_md5, frequency_response, large_loss_cap, large_loss_load, trained_on "
            "FROM model.glm_run WHERE run = %s",
            (run,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"no GLM run {run!r}; run Rscript R/export_predictions.R")
        meta = Run(*row)
        cur.execute(
            "SELECT p.idpol, p.claims_per_year, p.capped_amount_per_claim, coalesce(c.capped_loss, 0) AS capped_loss "
            "FROM model.glm_prediction p "
            "LEFT JOIN (SELECT idpol, sum(least(claim_amount, %s))::float8 AS capped_loss FROM fact.claim GROUP BY idpol) c "
            "USING (idpol) WHERE p.run = %s ORDER BY p.idpol",
            (meta.large_loss_cap, run),
        )
        rows = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])

    if canonical_md5(policies) != meta.frame_md5:
        raise BasisError(f"run {run!r} was fitted on another frame; re-run Rscript R/export_predictions.R")
    if len(rows) != len(policies) or not (rows["idpol"].to_numpy() == policies["idpol"].to_numpy()).all():
        raise BasisError(f"run {run!r} does not cover the frame policy for policy")

    frame = policies.assign(**{c: rows[c].to_numpy() for c in ("claims_per_year", "capped_amount_per_claim", "capped_loss")})
    frame["trained"] = TRAINING[meta.trained_on](frame)
    return meta, frame


def pure_premium(conn: psycopg.Connection, run: str = PRICING_RUN, policies: pd.DataFrame | None = None) -> PurePremium:
    return assemble(*load_run(conn, run, policies))
