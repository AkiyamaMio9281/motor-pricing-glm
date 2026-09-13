# Compare linear terms, equal-exposure quantile bands and risk-structured bands
# for driver age, vehicle age and bonus-malus, and write docs/frequency-banding.md.
#
#   Rscript R/frequency_banding.R
#
# Ten fits of a Poisson GLM with up to 76 parameters on 678,013 rows; several
# minutes. Deterministic for a given frame: nothing that varies between runs is
# written into the document.
#
# The rule for choosing between specifications is fixed here, before any result
# is seen, so that the choice cannot be fitted to the answer:
#
#   lowest Poisson deviance on a holdout of 20% of risk groups wins.
#
# BIC on the full data is reported beside it as a check, not as a second vote.
#
# The holdout is split by risk group, not by row. 14% of rows have exactly the
# same nine rating factors as the row with the previous policy id, which is what
# one policy split into several records looks like. A row-level split would put
# pieces of the same policy on both sides. A group here is a run of consecutive
# policy ids with identical rating factors.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  source(file.path(here, "db.R"))
  source(file.path(here, "model_frame.R"))
  source(file.path(here, "bands.R"))
  source(file.path(here, "frequency.R"))
})

con <- connect()
frame <- load_frequency_frame(con)
DBI::dbDisconnect(con)
frame_md5 <- canonical_md5(frame)

# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

# The plan's method: equal-exposure deciles. Bonus-malus is split first at 50,
# which holds 63% of exposure and would otherwise swallow six of the ten deciles.
weighted_upper <- function(x, w, probs) {
  o <- order(x); cw <- cumsum(w[o]) / sum(w)
  vapply(probs, function(p) x[o][which(cw >= p)[1]], numeric(1))
}
quantile_lower <- function(x, w, k) {
  lower <- unique(c(min(x), weighted_upper(x, w, seq_len(k - 1) / k) + 1))
  lower[lower <= max(x)]
}
above_floor <- frame$bonus_malus > 50
QUANTILE_LOWER <- list(
  driv_age    = quantile_lower(frame$driv_age, frame$exposure, 10),
  veh_age     = quantile_lower(frame$veh_age, frame$exposure, 10),
  bonus_malus = c(50, quantile_lower(frame$bonus_malus[above_floor],
                                     frame$exposure[above_floor], 10))
)
RISK_LOWER <- list(driv_age = DRIVER_AGE_LOWER, veh_age = VEH_AGE_LOWER,
                   bonus_malus = BONUS_MALUS_LOWER)

frame <- apply_bands(frame)
frame$driv_age_q    <- band_of(frame$driv_age, QUANTILE_LOWER$driv_age)
frame$veh_age_q     <- band_of(frame$veh_age, QUANTILE_LOWER$veh_age)
frame$bonus_malus_q <- band_of(frame$bonus_malus, QUANTILE_LOWER$bonus_malus)

for (v in names(RISK_LOWER)) {
  check_bands(frame, v, paste0(v, "_band"), RISK_LOWER[[v]])
  check_bands(frame, v, paste0(v, "_q"), QUANTILE_LOWER[[v]])
}

# ---------------------------------------------------------------------------
# Risk groups and the holdout
# ---------------------------------------------------------------------------

profile_columns <- c("area", "veh_power", "veh_age", "driv_age", "bonus_malus",
                     "veh_brand", "veh_gas", "density", "region")
profile <- do.call(paste, c(lapply(frame[profile_columns], as.character), sep = "|"))
starts_group <- c(TRUE, profile[-1] != profile[-length(profile)])
frame$risk_group <- cumsum(starts_group)
frame$holdout <- frame$risk_group %% 5 == 0

train <- frame[!frame$holdout, ]
test <- frame[frame$holdout, ]

# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------

shared_terms <- c("veh_brand", "veh_gas", "region")
SPECS <- list(
  linear = list(
    label = "linear driver age, vehicle age, bonus-malus",
    terms = c(shared_terms, "driv_age", "veh_age", "bonus_malus", "log(density)", "veh_power")),
  quantile = list(
    label = "equal-exposure decile bands",
    terms = c(shared_terms, "driv_age_q", "veh_age_q", "bonus_malus_q", "log(density)", "veh_power")),
  risk = list(
    label = "risk-structured bands",
    terms = c(shared_terms, "driv_age_band", "veh_age_band", "bonus_malus_band", "log(density)", "veh_power")),
  risk_power = list(
    label = "risk-structured bands, vehicle power as a factor",
    terms = c(shared_terms, "driv_age_band", "veh_age_band", "bonus_malus_band", "log(density)", "factor(veh_power)"))
)

poisson_deviance <- function(y, mu) 2 * sum(ifelse(y > 0, y * log(y / mu), 0) - (y - mu))

# Groups for the actual-over-expected diagnostics, scored on the holdout. That is
# not a refinement. In sample, a Poisson GLM with a log link reproduces observed
# claims exactly within every level of every factor it contains, so a group that
# coincides with one of its bands reads exactly 1 whatever the fit is like. The
# first draft of this document scored in sample and printed a column of 1.000s
# for the chosen bands, which looked like a perfect fit and was arithmetic.
ae_groups <- list(
  "driver age"  = cut(frame$driv_age, c(17, 18, 19, 20, 22, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 100),
                      labels = c("18", "19", "20", "21-22", "23-25", "26-30", "31-35", "36-40",
                                 "41-45", "46-50", "51-55", "56-60", "61-70", "71-80", "81+")),
  "vehicle age" = cut(frame$veh_age, c(-1, 0, 1, 2, 4, 6, 9, 11, 14, 19, 100),
                      labels = c("0", "1", "2", "3-4", "5-6", "7-9", "10-11", "12-14", "15-19", "20+")),
  "bonus-malus" = cut(frame$bonus_malus, c(49, 50, 54, 59, 69, 79, 89, 99, 100, 119, 230),
                      labels = c("50", "51-54", "55-59", "60-69", "70-79", "80-89", "90-99", "100",
                                 "101-119", "120+")),
  "vehicle power" = factor(frame$veh_power)
)
ae_test <- lapply(ae_groups, function(g) g[frame$holdout])
holdout_claims <- lapply(ae_test, function(g) tapply(test$claim_nb, g, sum))

# Holdout: fit on train, score test.
for (key in names(SPECS)) {
  fit <- fit_frequency(train, SPECS[[key]]$terms)
  mu <- stats::predict(fit, newdata = test, type = "response")
  SPECS[[key]]$holdout_deviance <- poisson_deviance(test$claim_nb, mu)
  SPECS[[key]]$holdout_ae <- lapply(ae_test, function(g) tapply(test$claim_nb, g, sum) / tapply(mu, g, sum))
  rm(fit, mu); invisible(gc())
  message(sprintf("  holdout  %-10s %.1f", key, SPECS[[key]]$holdout_deviance))
}

holdout <- vapply(SPECS, function(s) s$holdout_deviance, numeric(1))
chosen <- names(which.min(holdout))

# Density is read in sample, on the linear specification. log(density) is a
# single slope rather than a factor, so its decile groups are not reproduced by
# construction, and the full data gives a steadier reading than the holdout.
density_decile <- cut(log(frame$density), unique(stats::quantile(log(frame$density), seq(0, 1, 0.1))),
                      include.lowest = TRUE)

n <- nrow(frame)
for (key in names(SPECS)) {
  fit <- fit_frequency(frame, SPECS[[key]]$terms)
  p <- length(stats::coef(fit))
  SPECS[[key]]$parameters <- p
  SPECS[[key]]$deviance <- fit$deviance
  SPECS[[key]]$aic <- fit$aic
  SPECS[[key]]$bic <- fit$aic - 2 * p + p * log(n)
  if (key == "linear") {
    fitted_claims <- stats::fitted(fit)
    density_ae <- tapply(frame$claim_nb, density_decile, sum) / tapply(fitted_claims, density_decile, sum)
    rm(fitted_claims)
  }
  rm(fit); invisible(gc())
  message(sprintf("  full     %-10s p=%d  aic %.1f", key, p, SPECS[[key]]$aic))
}

# ---------------------------------------------------------------------------
# Vehicle age 0, and proportionality on the chosen terms
# ---------------------------------------------------------------------------

new_car_terms <- if ("veh_age_band" %in% SPECS[[chosen]]$terms) SPECS[[chosen]]$terms else SPECS$risk$terms
relativity_zero_vs_one <- function(coef) exp(-coef[["veh_age_band1"]])

long_exposure <- frame[frame$exposure >= 0.5, ]
fit <- fit_frequency(long_exposure, new_car_terms)
new_car_long <- relativity_zero_vs_one(stats::coef(fit))
rm(fit); invisible(gc())

fit <- fit_frequency(frame, new_car_terms)
new_car_all <- relativity_zero_vs_one(stats::coef(fit))
rm(fit); invisible(gc())

raw_frequency <- function(d, zero) sum(d$claim_nb[zero]) / sum(d$exposure[zero])
new_car_raw <- c(
  all  = raw_frequency(frame, frame$veh_age == 0) / raw_frequency(frame, frame$veh_age >= 1),
  long = raw_frequency(long_exposure, long_exposure$veh_age == 0) /
         raw_frequency(long_exposure, long_exposure$veh_age >= 1)
)

fit <- stats::glm(
  stats::as.formula(paste("claim_nb ~", paste(SPECS[[chosen]]$terms, collapse = " + "), "+ log(exposure)")),
  family = stats::poisson(), data = frame)
exposure_coef <- stats::coef(fit)[["log(exposure)"]]
exposure_se <- sqrt(stats::vcov(fit)["log(exposure)", "log(exposure)"])
rm(fit); invisible(gc())

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

fmt <- function(x, digits) formatC(x, format = "f", digits = digits, big.mark = ",")
md_table <- function(header, rows) {
  c(paste0("| ", paste(header, collapse = " | "), " |"),
    paste0("|", paste(rep("---", length(header)), collapse = "|"), "|"),
    vapply(rows, function(r) paste0("| ", paste(r, collapse = " | "), " |"), ""))
}

base_holdout <- SPECS$linear$holdout_deviance
spec_rows <- lapply(names(SPECS), function(k) {
  s <- SPECS[[k]]
  c(paste0(if (k == chosen) "**" else "", s$label, if (k == chosen) "**" else ""),
    fmt(s$parameters, 0), fmt(s$deviance, 0), fmt(s$aic, 0), fmt(s$bic, 0),
    fmt(s$holdout_deviance, 1), fmt(s$holdout_deviance - base_holdout, 1))
})

bic <- vapply(SPECS, function(s) s$bic, numeric(1))
bic_agrees <- names(which.min(bic)) == chosen

ae_header <- function(first) c(first, "Holdout claims", "Linear", "Deciles", "Chosen")
ae_rows <- function(group) {
  lapply(names(holdout_claims[[group]]), function(l) c(
    l, fmt(holdout_claims[[group]][[l]], 0),
    fmt(SPECS$linear$holdout_ae[[group]][[l]], 3),
    fmt(SPECS$quantile$holdout_ae[[group]][[l]], 3),
    fmt(SPECS[[chosen]]$holdout_ae[[group]][[l]], 3)))
}
new_car_exposure <- c(new = mean(frame$exposure[frame$veh_age == 0]),
                      older = mean(frame$exposure[frame$veh_age >= 1]))

band_list <- function(lower) paste(band_labels(lower), collapse = ", ")

out <- c(
  "# Banding the continuous frequency terms",
  "",
  "Generated by `R/frequency_banding.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf("Computed from `model.frequency_frame`, %s rows, frame md5 `%s`. %s.",
          fmt(n, 0), frame_md5, R.version.string),
  "",
  "Every specification is an offset Poisson GLM through `fit_frequency()`. They",
  "differ only in how driver age, vehicle age, bonus-malus and vehicle power enter.",
  "",
  "**Rule, fixed before fitting:** the lowest Poisson deviance on the holdout wins.",
  "",
  "## Where linear terms fail",
  "",
  "One-way actual over expected claims, out of sample: each specification is",
  "fitted on the training risk groups and scored on the holdout. A specification",
  "that fits leaves every group near 1, within the noise its holdout claim count",
  "implies.",
  "",
  "These are holdout figures on purpose. In sample, a GLM reproduces observed",
  "claims exactly within every level of every factor it fits, so any group that",
  "coincides with one of its bands reads 1.000 however good or bad the model is.",
  "",
  "### Driver age",
  "",
  md_table(ae_header("Driver age"), ae_rows("driver age")),
  "",
  "### Vehicle age",
  "",
  md_table(ae_header("Vehicle age"), ae_rows("vehicle age")),
  "",
  "### Bonus-malus",
  "",
  md_table(ae_header("Bonus-malus"), ae_rows("bonus-malus")),
  "",
  "### Vehicle power",
  "",
  md_table(ae_header("Vehicle power"), ae_rows("vehicle power")),
  "",
  sprintf(paste(
    "Density, by decile of log density, stays within %s of 1 in sample under the",
    "linear specification, so log density is left as a single slope. As a continuous",
    "term it has no levels to reproduce, so reading it in sample is not circular."),
    fmt(max(abs(density_ae - 1)), 3)),
  "",
  "## Two ways to place the bands",
  "",
  "The plan was equal-exposure deciles. Their first driver band runs from 18 to",
  sprintf("%s, because drivers under 21 hold %s%% of exposure, and the first",
          QUANTILE_LOWER$driv_age[2] - 1,
          fmt(100 * sum(frame$exposure[frame$driv_age <= 20]) / sum(frame$exposure), 2)),
  sprintf("vehicle band merges age 0 with age %s. Those are exactly the two places",
          QUANTILE_LOWER$veh_age[2] - 1),
  "where the linear diagnostics above show risk changing fastest.",
  "",
  "The risk-structured bands are narrow where the diagnostics move and wide where",
  "they do not, with bonus-malus 50 and 100 on their own as states of the scale.",
  "They have close to the same number of parameters as the deciles, so the",
  "comparison is about where the boundaries sit rather than how many there are.",
  "",
  md_table(c("Variable", "Equal-exposure deciles", "Risk-structured"),
           list(c("driver age", band_list(QUANTILE_LOWER$driv_age), band_list(RISK_LOWER$driv_age)),
                c("vehicle age", band_list(QUANTILE_LOWER$veh_age), band_list(RISK_LOWER$veh_age)),
                c("bonus-malus", band_list(QUANTILE_LOWER$bonus_malus), band_list(RISK_LOWER$bonus_malus)))),
  "",
  "## Result",
  "",
  sprintf(paste(
    "Holdout: %s risk groups, %s rows, %s claims, every fifth risk group. %s of %s",
    "rows sit in a group with at least one other row."),
    fmt(length(unique(test$risk_group)), 0), fmt(nrow(test), 0),
    fmt(sum(test$claim_nb), 0),
    fmt(sum(table(frame$risk_group)[table(frame$risk_group) > 1]), 0), fmt(n, 0)),
  "",
  md_table(c("Specification", "Parameters", "Deviance", "AIC", "BIC",
             "Holdout deviance", "Against linear"), spec_rows),
  "",
  sprintf("Chosen: **%s**. BIC on the full data %s.", SPECS[[chosen]]$label,
          if (bic_agrees) "picks the same specification"
          else paste0("would pick ", SPECS[[names(which.min(bic))]]$label, " instead")),
  "",
  paste0("Chosen terms: `", paste(SPECS[[chosen]]$terms, collapse = " + "), "`"),
  "",
  "## Vehicle age 0",
  "",
  sprintf(paste(
    "New vehicles have the highest one-way frequency in the book and the shortest",
    "exposures, a mean of %s of a year at vehicle age 0 against %s for older",
    "vehicles. D2-2 found that short exposures carry more claims per policy-year",
    "than an offset allows for, so part of the new-car effect may belong to the",
    "exposure relationship rather than to new cars."),
    fmt(new_car_exposure[["new"]], 3), fmt(new_car_exposure[["older"]], 3)),
  "",
  md_table(c("Vehicle age 0 against 1", "All policy-years", "Exposure of half a year or more"),
           list(c("raw frequency ratio, 0 against 1 and older", fmt(new_car_raw[["all"]], 2), fmt(new_car_raw[["long"]], 2)),
                c("GLM relativity, band 0 against band 1", fmt(new_car_all, 2), fmt(new_car_long, 2)))),
  "",
  sprintf(paste(
    "Restricting to exposures of half a year or more cuts the modelled relativity",
    "from %s to %s, which is %s%% of it on the log scale. The pricing model is fitted",
    "on all policy-years and carries the larger figure. Which one a new car should",
    "pay is a rate-table decision, and this is the relativity in the model most",
    "exposed to the non-proportionality D2-2 measured."),
    fmt(new_car_all, 2), fmt(new_car_long, 2),
    fmt(100 * (1 - log(new_car_long) / log(new_car_all)), 0)),
  "",
  "## Proportionality on the chosen terms",
  "",
  sprintf(paste(
    "Estimated rather than fixed, the coefficient on log(exposure) under the chosen",
    "terms is %s, standard error %s. Under the linear terms of D2-2 it was 0.367, so",
    "banding does not change the conclusion that expected claims are not proportional",
    "to exposure."),
    fmt(exposure_coef, 3), fmt(exposure_se, 4))
)

target <- file.path(find_repo_root(), "docs", "frequency-banding.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message("wrote ", target, "; chosen: ", chosen)
