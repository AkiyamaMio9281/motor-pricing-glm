# Fit every tempting way of putting exposure into the frequency GLM, on the same
# frame with the same rating terms, and write docs/frequency-exposure.md.
#
#   Rscript R/frequency_exposure.R
#
# About ninety seconds: eight fits of a 38-parameter Poisson GLM on 678,013 rows.
# Each fit object is around 600 MB, so every fit is summarised and discarded
# before the next one starts.
#
# The output is deterministic for a given frame: no timestamps or timings go into
# the document, so re-running on unchanged data produces no diff.
#
# The document is stamped with the md5 of the frame it was computed from, and
# tests/test_frequency_exposure.py fails if that no longer matches the data, so a
# stale document cannot quietly outlive a rebuild of the mart.
#
# Two claims in the document carry the argument, so the script refuses to write
# it unless they hold: exposure as a weight on the claim rate is the same model as
# the offset, and the estimated coefficient on log(exposure) is not 1.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  source(file.path(here, "db.R"))
  source(file.path(here, "model_frame.R"))
  source(file.path(here, "bands.R"))
  source(file.path(here, "frequency.R"))
})

started <- proc.time()[["elapsed"]]

con <- connect()
frame <- load_frequency_frame(con)
DBI::dbDisconnect(con)
frame_md5 <- canonical_md5(frame)

# Why area is not a rating term: it is a banding of density.
area_density_rank_cor <- stats::cor(as.integer(frame$area), frame$density, method = "spearman")

frame$claim_rate <- frame$claim_nb / frame$exposure
frame$exposure_band <- cut(
  frame$exposure, breaks = c(0, 0.02, 0.1, 0.5, 1), right = FALSE,
  include.lowest = TRUE,
  labels = c("under 1 week", "1 to 5 weeks", "5 weeks to 6 months", "6 months to 1 year")
)
long <- frame[!frame$is_short_exposure, ]

# The rating terms this analysis was run on: the D2-2 specification, with the
# continuous variables linear. They are fixed here rather than taken from
# FREQUENCY_TERMS, which moved to banded terms in D2-3. The questions this
# document answers are about how exposure enters the model, and its relativity
# table reports per-year slopes that banded terms do not have. D2-3 re-estimates
# the exposure coefficient on the banded terms, in docs/frequency-banding.md.
EXPOSURE_ANALYSIS_TERMS <- c(
  "veh_brand", "veh_gas", "region",
  "driv_age", "veh_age", "bonus_malus", "log(density)", "veh_power"
)

rhs <- paste(EXPOSURE_ANALYSIS_TERMS, collapse = " + ")
with_rhs <- function(lhs, extra = "") stats::as.formula(paste(lhs, "~", rhs, extra))

specs <- list(
  offset = list(
    label = "offset(log(exposure))", role = "pricing model", data = "frame",
    fit = function(d) fit_frequency(d, EXPOSURE_ANALYSIS_TERMS)),
  rate_weight = list(
    label = "claim rate as response, exposure as weight", role = "equivalent", data = "frame",
    fit = function(d) stats::glm(with_rhs("claim_rate"), stats::quasipoisson(), d, weights = exposure),
    fitted_are_rates = TRUE),
  weight_count = list(
    label = "claim count as response, exposure as weight", role = "error", data = "frame",
    fit = function(d) stats::glm(with_rhs("claim_nb"), stats::poisson(), d, weights = exposure)),
  offset_nolog = list(
    label = "offset(exposure), log omitted", role = "error", data = "frame",
    fit = function(d) stats::glm(with_rhs("claim_nb", "+ offset(exposure)"), stats::poisson(), d)),
  no_exposure = list(
    label = "exposure left out", role = "error", data = "frame",
    fit = function(d) stats::glm(with_rhs("claim_nb"), stats::poisson(), d)),
  covariate = list(
    label = "log(exposure) with its coefficient estimated", role = "diagnostic", data = "frame",
    fit = function(d) stats::glm(with_rhs("claim_nb", "+ log(exposure)"), stats::poisson(), d)),
  offset_long = list(
    label = "offset(log(exposure)), short exposure removed", role = "sensitivity", data = "long",
    fit = function(d) fit_frequency(d, EXPOSURE_ANALYSIS_TERMS)),
  covariate_long = list(
    label = "log(exposure) estimated, short exposure removed", role = "sensitivity", data = "long",
    fit = function(d) stats::glm(with_rhs("claim_nb", "+ log(exposure)"), stats::poisson(), d))
)

summarise_fit <- function(spec, fit, d) {
  claims_fitted <- stats::fitted(fit)
  if (isTRUE(spec$fitted_are_rates)) claims_fitted <- claims_fitted * d$exposure
  annual <- annual_frequency(fit, d)
  list(
    label = spec$label, role = spec$role, rows = nrow(d),
    converged = fit$converged, iter = fit$iter,
    deviance = if (fit$family$family == "poisson") fit$deviance else NA_real_,
    aic = if (is.finite(fit$aic %||% NA)) fit$aic else NA_real_,
    dispersion = sum(stats::residuals(fit, type = "pearson")^2) / fit$df.residual,
    weighted_annual = sum(d$exposure * annual) / sum(d$exposure),
    observed_annual = sum(d$claim_nb) / sum(d$exposure),
    coef = stats::coef(fit), se = sqrt(diag(stats::vcov(fit))),
    by_band = tapply(claims_fitted, d$exposure_band, sum)
  )
}

`%||%` <- function(a, b) if (is.null(a)) b else a

results <- list()
for (key in names(specs)) {
  spec <- specs[[key]]
  d <- if (spec$data == "long") long else frame
  fit <- spec$fit(d)
  results[[key]] <- summarise_fit(spec, fit, d)
  rm(fit); invisible(gc())
  message(sprintf("  %-16s %s", key, spec$label))
}

# ---------------------------------------------------------------------------
# The claims the document rests on
# ---------------------------------------------------------------------------

not_converged <- names(results)[!vapply(results, function(r) isTRUE(r$converged), TRUE)]
if (length(not_converged)) {
  stop("fits did not converge: ", paste(not_converged, collapse = ", "), call. = FALSE)
}

equivalence_gap <- max(abs(results$rate_weight$coef - results$offset$coef))
if (equivalence_gap > 1e-6) {
  stop("rate-with-weights no longer matches the offset model: max coefficient gap ",
       format(equivalence_gap), call. = FALSE)
}

exposure_test <- function(r) {
  b <- r$coef[["log(exposure)"]]; s <- r$se[["log(exposure)"]]
  list(b = b, se = s, lo = b - 1.96 * s, hi = b + 1.96 * s,
       z = (1 - b) / s, z_scaled = (1 - b) / (s * sqrt(r$dispersion)),
       doubling = 2^b)
}
proportionality <- list(all = exposure_test(results$covariate),
                        long = exposure_test(results$covariate_long))
if (proportionality$all$hi >= 1 && proportionality$all$lo <= 1) {
  stop("the estimated exposure coefficient is consistent with 1; the document's ",
       "argument no longer holds and needs rewriting", call. = FALSE)
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

fmt <- function(x, digits) formatC(x, format = "f", digits = digits, big.mark = ",")

md_table <- function(header, rows) {
  c(paste0("| ", paste(header, collapse = " | "), " |"),
    paste0("|", paste(rep("---", length(header)), collapse = "|"), "|"),
    vapply(rows, function(r) paste0("| ", paste(r, collapse = " | "), " |"), ""))
}

relativity_terms <- list(
  "annual frequency at base levels"   = function(b) exp(b[["(Intercept)"]]),
  "driver age, per 10 years"          = function(b) exp(10 * b[["driv_age"]]),
  "vehicle age, per 5 years"          = function(b) exp(5 * b[["veh_age"]]),
  "bonus-malus, per 10 points"        = function(b) exp(10 * b[["bonus_malus"]]),
  "density, per doubling"             = function(b) 2^b[["log(density)"]],
  "vehicle power, per unit"           = function(b) exp(b[["veh_power"]]),
  "regular fuel against diesel"       = function(b) exp(b[["veh_gasRegular"]]),
  "brand B12 against B1"              = function(b) exp(b[["veh_brandB12"]])
)

relativity <- function(key, term) relativity_terms[[term]](results[[key]]$coef)

a <- results$offset
b <- results$weight_count
observed_band <- tapply(frame$claim_nb, frame$exposure_band, sum)
key_terms <- c("driv_age", "veh_age", "bonus_malus", "log(density)", "veh_power", "veh_gasRegular")
shift <- function(from, to, terms) exp(results[[to]]$coef[terms] - results[[from]]$coef[terms]) - 1
shared <- intersect(names(results$offset$coef), names(results$offset_long$coef))
long_shift_all <- shift("offset", "offset_long", shared)
long_shift_key <- shift("offset", "offset_long", key_terms)

out <- c(
  "# Exposure in the frequency model",
  "",
  "Generated by `R/frequency_exposure.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf("Computed from `model.frequency_frame`, %s rows, frame md5 `%s`. %s.",
          fmt(nrow(frame), 0), frame_md5, R.version.string),
  "",
  "Every fit uses the same rating terms and differs only in how exposure enters:",
  "",
  paste0("`", rhs, "`"),
  "",
  sprintf(paste(
    "Area is not among them. It is a banding of density, with a rank correlation of",
    "%s between the two, so fitting both would be near-collinear."),
    fmt(area_density_rank_cor, 3)),
  "",
  sprintf("Observed: %s claims over %s policy-years, %s claims per policy-year.",
          fmt(sum(frame$claim_nb), 0), fmt(sum(frame$exposure), 1), fmt(a$observed_annual, 4)),
  "",
  "## Specifications",
  "",
  "*Weighted annual frequency* is the prediction for a full policy-year, averaged",
  "with exposure as the weight. Under an offset, fitted claims are annual frequency",
  "times exposure, so this average reproduces the observed frequency exactly. That",
  "is the property a rate level is built on, since premium is charged per",
  "policy-year of exposure. Of the specifications below, only the offset and the",
  "rate-with-weights model identical to it have that property.",
  "",
  md_table(
    c("Exposure enters as", "Role", "Deviance", "AIC", "Pearson dispersion",
      "Weighted annual frequency", "Observed"),
    lapply(results, function(r) c(
      r$label, r$role,
      if (is.na(r$deviance)) "" else fmt(r$deviance, 0),
      if (is.na(r$aic)) "" else fmt(r$aic, 0),
      fmt(r$dispersion, 3), fmt(r$weighted_annual, 4), fmt(r$observed_annual, 4)))
  ),
  "",
  "Every fit converged. The last two are on the policy-years not flagged as short",
  sprintf("exposure, %s rows, so they balance to that subset's own frequency.",
          fmt(nrow(long), 0)),
  "",
  "## Relativities",
  "",
  md_table(
    c("Term", vapply(names(results), function(k) paste0("`", k, "`"), "")),
    lapply(names(relativity_terms), function(term) c(
      term, vapply(names(results), function(k) fmt(relativity(k, term), 4), "")))
  ),
  "",
  "## A weight is not the error. A weight on the count is.",
  "",
  sprintf(paste(
    "With the claim rate as the response and exposure as a prior weight, every",
    "coefficient matches the offset model to within %s. The two are the same model.",
    "The error is keeping the count as the response and adding the weight, which",
    "fits claims per policy rather than per policy-year: the weighted annual",
    "frequency falls to %s against an observed %s, %s%% low; the regular-fuel",
    "relativity moves from %s to %s, changing sides; and brand B12 goes from %s to",
    "%s. It runs without a warning."),
    format(signif(equivalence_gap, 2)), fmt(b$weighted_annual, 4), fmt(a$observed_annual, 4),
    fmt(100 * (1 - b$weighted_annual / a$observed_annual), 0),
    fmt(relativity("offset", "regular fuel against diesel"), 3),
    fmt(relativity("weight_count", "regular fuel against diesel"), 3),
    fmt(relativity("offset", "brand B12 against B1"), 3),
    fmt(relativity("weight_count", "brand B12 against B1"), 3)),
  "",
  "## Proportionality",
  "",
  "An offset fixes the coefficient on log(exposure) at 1: twice the exposure, twice",
  "the expected claims. Estimating that coefficient tests the assumption.",
  "",
  md_table(
    c("Fit", "Coefficient", "Std. error", "95% interval", "Std. errors below 1",
      "Below 1, dispersion-scaled", "Claims for double the exposure"),
    list(
      with(proportionality$all, c("all policy-years", fmt(b, 4), fmt(se, 4),
        paste0("[", fmt(lo, 4), ", ", fmt(hi, 4), "]"), fmt(z, 0), fmt(z_scaled, 0),
        paste0(fmt(doubling, 3), "x"))),
      with(proportionality$long, c("short exposure removed", fmt(b, 4), fmt(se, 4),
        paste0("[", fmt(lo, 4), ", ", fmt(hi, 4), "]"), fmt(z, 0), fmt(z_scaled, 0),
        paste0(fmt(doubling, 3), "x"))))
  ),
  "",
  sprintf(paste(
    "Removing the short-exposure rows moves the coefficient from %s to %s. The",
    "departure from proportionality runs through the whole range of exposure; it",
    "is not produced by the rows flagged in transform 003."),
    fmt(proportionality$all$b, 3), fmt(proportionality$long$b, 3)),
  "",
  "## Where the offset misfits",
  "",
  "Fitted claims summed within each exposure band.",
  "",
  md_table(
    c("Exposure", "Observed", "Offset", "log(exposure) estimated", "Weight on count"),
    lapply(names(observed_band), function(band) c(
      band, fmt(observed_band[[band]], 0), fmt(a$by_band[[band]], 0),
      fmt(results$covariate$by_band[[band]], 0), fmt(b$by_band[[band]], 0)))
  ),
  "",
  "## What the short-exposure rows actually do",
  "",
  sprintf(paste(
    "Refitting the offset model without the %s short-exposure rows changes the",
    "Pearson dispersion from %s to %s, and changes no key relativity by more than",
    "%s%%. The largest movement of any coefficient is %s%%, on `%s`."),
    fmt(sum(frame$is_short_exposure), 0),
    fmt(a$dispersion, 3), fmt(results$offset_long$dispersion, 3),
    fmt(100 * max(abs(long_shift_key)), 2),
    fmt(100 * max(abs(long_shift_all)), 1), names(which.max(abs(long_shift_all)))),
  "",
  "They are high-residual and low-influence. Under an offset, the working weight",
  "a Poisson fit gives a row is its fitted mean, which is proportional to its",
  "exposure, so a one-week policy carries about a fiftieth of the weight of a",
  "full-year policy of the same risk. Their residuals dominate the dispersion",
  "statistic; they barely move the coefficients."
)

target <- file.path(find_repo_root(), "docs", "frequency-exposure.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message(sprintf("wrote %s in %.0f s", target, proc.time()[["elapsed"]] - started))
