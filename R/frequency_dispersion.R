# Assess dispersion in the frequency model and decide which standard errors it
# should use. Writes docs/frequency-dispersion.md.
#
#   Rscript R/frequency_dispersion.R
#
# Six fits of the 76-parameter model and 100 simulations; a few minutes.
# Deterministic for a given frame: the simulations are seeded, and nothing that
# varies between runs is written into the document.
#
# How the statistics are judged is fixed here, before any is computed: each one
# against its own distribution under the fitted Poisson model, simulated, and not
# against 1. With a mean of about 0.05 claims per row, the reference value of
# neither standard estimator is 1, and reading them against 1 is the mistake this
# document exists to avoid.

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

frame <- apply_bands(frame)
frame$risk_group <- risk_groups(frame)

SIMULATIONS <- 100
set.seed(20260913)

poisson_deviance <- function(y, mu) 2 * sum(ifelse(y > 0, y * log(y / mu), 0) - (y - mu))
statistics <- function(y, mu, df) c(
  pearson   = sum((y - mu)^2 / mu) / df,
  deviance  = poisson_deviance(y, mu) / df,
  ratio     = sum((y - mu)^2) / sum(mu)
)
pearson_of <- function(fit) sum(stats::residuals(fit, type = "pearson")^2) / fit$df.residual

# ---------------------------------------------------------------------------
# The pricing model
# ---------------------------------------------------------------------------

fit <- fit_frequency(frame)
y <- frame$claim_nb
mu <- stats::fitted(fit)
n <- nrow(frame)
p <- length(stats::coef(fit))
df <- n - p
observed <- statistics(y, mu, df)

pearson_terms <- (y - mu)^2 / mu
ranked <- order(pearson_terms, decreasing = TRUE)
top_counts <- round(n * c(0.0001, 0.001, 0.01))
concentration <- vapply(top_counts, function(k) sum(pearson_terms[ranked[seq_len(k)]]) / sum(pearson_terms), 0)
top <- ranked[seq_len(top_counts[2])]
top_profile <- list(
  short    = mean(frame$is_short_exposure[top]),
  exposure = stats::median(frame$exposure[top]),
  mu       = stats::median(mu[top]),
  one      = sum(y[top] == 1),
  more     = sum(y[top] >= 2)
)
pearson_without_top <- sum(pearson_terms[-top]) / (df - length(top))

coefficients <- stats::coef(fit)
se <- list(poisson = sqrt(diag(stats::vcov(fit))))
se$quasi <- se$poisson * sqrt(observed[["pearson"]])
se$row <- sqrt(diag(frequency_vcov(fit)))
se$cluster <- sqrt(diag(frequency_vcov(fit, cluster = frame$risk_group)))
rm(fit, pearson_terms); invisible(gc())

if (!(stats::median(se$cluster / se$poisson) < sqrt(observed[["pearson"]]))) {
  stop("clustered sandwich errors are not smaller than the quasi-Poisson rescaling; ",
       "the conclusion against quasi-Poisson no longer holds", call. = FALSE)
}

# ---------------------------------------------------------------------------
# The same statistics when the Poisson model is true
# ---------------------------------------------------------------------------

null <- t(replicate(SIMULATIONS, statistics(stats::rpois(n, mu), mu, df)))

# The sandwich code is hand-written, so check it where the answer is known: on
# claims simulated from the fitted model, every robust standard error should
# match the Poisson one.
simulated <- frame
simulated$claim_nb <- stats::rpois(n, mu)
fit <- fit_frequency(simulated)
se_sim_poisson <- sqrt(diag(stats::vcov(fit)))
validation <- c(
  row     = stats::median(sqrt(diag(frequency_vcov(fit))) / se_sim_poisson),
  cluster = stats::median(sqrt(diag(frequency_vcov(fit, cluster = simulated$risk_group))) / se_sim_poisson)
)
rm(fit, simulated); invisible(gc())

if (any(abs(validation - 1) > 0.03)) {
  stop("robust standard errors do not reproduce Poisson errors on Poisson data: ",
       paste(names(validation), round(validation, 3), collapse = ", "), call. = FALSE)
}

outside <- vapply(names(observed), function(s) observed[[s]] > max(null[, s]) || observed[[s]] < min(null[, s]), TRUE)
if (!all(outside)) {
  stop("an observed statistic falls inside its simulated range: ",
       paste(names(observed)[!outside], collapse = ", "), call. = FALSE)
}

# The document argues from three things. If any stops being true of the data,
# refuse to write it rather than publish prose the numbers no longer support.
if (!(observed[["pearson"]] > 1 && observed[["deviance"]] < 1)) {
  stop("the two estimators no longer point in opposite directions against 1", call. = FALSE)
}

# ---------------------------------------------------------------------------
# How much depends on the exposure specification
# ---------------------------------------------------------------------------

estimated_exposure <- function(d) {
  stats::glm(stats::as.formula(paste("claim_nb ~", paste(FREQUENCY_TERMS, collapse = " + "), "+ log(exposure)")),
             family = stats::poisson(), data = d)
}

sensitivity <- list()

long <- frame[!frame$is_short_exposure, ]
fit <- fit_frequency(long)
sensitivity$long <- c(rows = nrow(long), pearson = pearson_of(fit))
rm(fit, long); invisible(gc())

fit <- estimated_exposure(frame)
sensitivity$estimated <- c(rows = n, pearson = pearson_of(fit))
exposure_coef_rows <- c(b = stats::coef(fit)[["log(exposure)"]],
                        se = sqrt(stats::vcov(fit)["log(exposure)", "log(exposure)"]))
rm(fit); invisible(gc())

# Reassemble each risk group into one row: claims and exposure summed, rating
# factors identical within a group by construction.
first <- which(!duplicated(frame$risk_group))
groups <- frame[first, ]
groups$claim_nb <- as.integer(rowsum(frame$claim_nb, frame$risk_group, reorder = FALSE)[, 1])
groups$exposure <- rowsum(frame$exposure, frame$risk_group, reorder = FALSE)[, 1]
group_sizes <- tabulate(frame$risk_group)

fit <- fit_frequency(groups)
sensitivity$groups <- c(rows = nrow(groups), pearson = pearson_of(fit))
rm(fit); invisible(gc())

fit <- estimated_exposure(groups)
sensitivity$groups_estimated <- c(rows = nrow(groups), pearson = pearson_of(fit))
exposure_coef_groups <- c(b = stats::coef(fit)[["log(exposure)"]],
                          se = sqrt(stats::vcov(fit)["log(exposure)", "log(exposure)"]))
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

significant <- function(s) sum(abs(coefficients / s) > 1.96)
ratio_row <- function(label, s) {
  r <- s / se$poisson
  c(label, fmt(stats::median(r), 3), fmt(max(r), 3), paste0(significant(s), " of ", p))
}

read_against_one <- function(x) {
  if (x > 1.05) "overdispersed" else if (x < 0.95) "underdispersed" else "close to Poisson"
}
against_one <- vapply(observed, read_against_one, "")
read_against_null <- function(s) {
  if (observed[[s]] > max(null[, s])) "above every simulation" else "below every simulation"
}
statistic_labels <- c(pearson = "Pearson chi-square / df",
                      deviance = "deviance / df",
                      ratio = "sum of (y - mu)^2 / sum of mu")

most_affected <- order(se$cluster / se$poisson, decreasing = TRUE)[1:5]
gap_closed <- (exposure_coef_groups[["b"]] - exposure_coef_rows[["b"]]) / (1 - exposure_coef_rows[["b"]])

out <- c(
  "# Dispersion in the frequency model",
  "",
  "Generated by `R/frequency_dispersion.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf("Computed from `model.frequency_frame`, %s rows, frame md5 `%s`. %s.",
          fmt(n, 0), frame_md5, R.version.string),
  "",
  paste0("Pricing model, ", p, " parameters: `", paste(FREQUENCY_TERMS, collapse = " + "), "`"),
  "",
  "**How the statistics are judged, fixed before computing them:** each against its",
  sprintf("own distribution under the fitted Poisson model, from %d simulations, and", SIMULATIONS),
  "not against 1.",
  "",
  "## Three statistics",
  "",
  md_table(
    c("Statistic", "Observed", "Simulated mean", "Simulated range", "Read against 1", "Read against the simulation"),
    lapply(names(observed), function(s) c(
      statistic_labels[[s]], fmt(observed[[s]], 3), fmt(mean(null[, s]), 3),
      paste0(fmt(min(null[, s]), 3), " to ", fmt(max(null[, s]), 3)),
      against_one[[s]], read_against_null(s)))
  ),
  "",
  "Read against 1, the two standard estimators contradict each other: one says the",
  "model is overdispersed and the other that it is underdispersed. Neither",
  "has 1 as its reference here. With a mean of about 0.05 claims per row, deviance",
  sprintf("over degrees of freedom averages %s when the Poisson model is exactly true.",
          fmt(mean(null[, "deviance"]), 3)),
  "",
  "Read against the simulation, all three agree that the Poisson variance does not",
  "hold. They disagree only about how much, and the next section is why.",
  "",
  "## Where the Pearson statistic comes from",
  "",
  md_table(
    c("Largest contributions", "Share of rows", "Share of the Pearson sum"),
    lapply(seq_along(top_counts), function(i) c(
      paste(fmt(top_counts[i], 0), "rows"), paste0(c("0.01", "0.1", "1")[i], "%"),
      paste0(fmt(100 * concentration[i], 1), "%")))
  ),
  "",
  sprintf(paste(
    "The top 0.1%% of rows carry %s%% of the statistic. Their median exposure is %s of",
    "a year, %s%% are flagged as short exposure, their median fitted claim count is",
    "%s, and %s of them had one claim and %s had more. A claim where the model expected",
    "a few thousandths of one adds hundreds to the sum by itself. This is the exposure",
    "misfit D2-2 measured, showing up as variance. Without those rows the Pearson",
    "dispersion is %s."),
    fmt(100 * concentration[2], 1), fmt(top_profile$exposure, 3), fmt(100 * top_profile$short, 0),
    formatC(top_profile$mu, format = "f", digits = 5), fmt(top_profile$one, 0), fmt(top_profile$more, 0),
    fmt(pearson_without_top, 3)),
  "",
  sprintf(paste(
    "The ratio of summed squared residuals to summed fitted claims weights each row",
    "by its fitted mean instead, so a handful of tiny-mean rows cannot dominate it. It",
    "reads %s: across the bulk of the book, variance exceeds the Poisson mean by",
    "about %s%%."),
    fmt(observed[["ratio"]], 3), fmt(100 * (observed[["ratio"]] - 1), 0)),
  "",
  "## What it does to standard errors",
  "",
  md_table(
    c("Standard errors", "Median against Poisson", "Largest against Poisson", "Significant at 5%"),
    list(ratio_row("Poisson", se$poisson),
         ratio_row("quasi-Poisson, scaled by the Pearson dispersion", se$quasi),
         ratio_row("sandwich, rows independent", se$row),
         ratio_row("sandwich, clustered by risk group", se$cluster))
  ),
  "",
  sprintf(paste(
    "Quasi-Poisson multiplies every standard error by %s, the square root of the",
    "Pearson dispersion, and would take %s coefficients out of significance. The",
    "sandwich estimate, which assumes no variance function and is not dominated by",
    "tiny-mean rows, puts the understatement at %s%% in the median with rows independent",
    "and %s%% once pieces of the same policy are allowed to be correlated."),
    fmt(sqrt(observed[["pearson"]]), 3), significant(se$poisson) - significant(se$quasi),
    fmt(100 * (stats::median(se$row / se$poisson) - 1), 1),
    fmt(100 * (stats::median(se$cluster / se$poisson) - 1), 1)),
  "",
  "Most affected under clustering:",
  "",
  md_table(c("Coefficient", "Clustered against Poisson"),
           lapply(most_affected, function(i) c(paste0("`", names(coefficients)[i], "`"),
                                              fmt(se$cluster[i] / se$poisson[i], 3)))),
  "",
  sprintf(paste(
    "Checked where the answer is known: refitted on claims simulated from the fitted",
    "model, the median sandwich standard error is %s of the Poisson one with rows",
    "independent and %s clustered."),
    fmt(validation[["row"]], 3), fmt(validation[["cluster"]], 3)),
  "",
  "## How much depends on the exposure specification",
  "",
  md_table(
    c("Specification", "Rows", "Pearson dispersion"),
    list(c("offset, all policy-years (pricing model)", fmt(n, 0), fmt(observed[["pearson"]], 3)),
         c("offset, short exposure removed", fmt(sensitivity$long[["rows"]], 0), fmt(sensitivity$long[["pearson"]], 3)),
         c("log(exposure) estimated", fmt(n, 0), fmt(sensitivity$estimated[["pearson"]], 3)),
         c("offset, risk groups reassembled", fmt(sensitivity$groups[["rows"]], 0), fmt(sensitivity$groups[["pearson"]], 3)),
         c("log(exposure) estimated, risk groups reassembled", fmt(sensitivity$groups_estimated[["rows"]], 0),
           fmt(sensitivity$groups_estimated[["pearson"]], 3)))
  ),
  "",
  "## Are the fragments behind the non-proportionality?",
  "",
  sprintf(paste(
    "D2-3 raised the possibility that pieces of one policy-year recorded as separate",
    "short rows produce the non-proportionality D2-2 found. Reassembling each risk group",
    "into one row, %s rows become %s groups, %s of them made of two or more pieces.",
    "Only %s reassembled groups exceed one year of exposure, which is what pieces of a",
    "single policy-year would do."),
    fmt(n, 0), fmt(nrow(groups), 0), fmt(sum(group_sizes > 1), 0), fmt(sum(groups$exposure > 1), 0)),
  "",
  md_table(
    c("Estimated coefficient on log(exposure)", "Coefficient", "Std. error"),
    list(c("policy-years as recorded", fmt(exposure_coef_rows[["b"]], 3), fmt(exposure_coef_rows[["se"]], 4)),
         c("risk groups reassembled", fmt(exposure_coef_groups[["b"]], 3), fmt(exposure_coef_groups[["se"]], 4)))
  ),
  "",
  sprintf(paste(
    "Reassembly moves the coefficient towards 1 and closes %s%% of the distance. The",
    "fragmentation accounts for part of the non-proportionality, and most of it remains."),
    fmt(100 * gap_closed, 0)),
  "",
  "## Not done",
  "",
  sprintf(paste(
    "No negative binomial model was fitted. It would replace the Poisson variance with",
    "mu + alpha mu^2 and so reweight the rows, moving the point estimates. The evidence",
    "above is that the variance problem is concentrated in a small set of short-exposure",
    "rows, with about %s%% excess variance across the rest of the book. The sandwich",
    "estimator gives valid standard errors for the Poisson estimates without committing",
    "to a variance function, which is the question that needed answering."),
    fmt(100 * (observed[["ratio"]] - 1), 0)),
  "",
  "## Conclusion",
  "",
  "- The Poisson point estimates stand. Overdispersion on its own does not bias them;",
  "  what bias they carry comes from the exposure relationship documented in D2-2, not",
  "  from the variance.",
  "- Standard errors and confidence intervals for the frequency model use",
  "  `frequency_vcov(fit, cluster = risk_groups(frame))`.",
  sprintf(paste(
    "- Quasi-Poisson rescaling is not used. It would read a misfit concentrated in a",
    "small fraction of rows as uncertainty in every coefficient, widening every",
    "standard error by %s%% where the clustered sandwich finds %s%% in the median."),
    fmt(100 * (sqrt(observed[["pearson"]]) - 1), 0),
    fmt(100 * (stats::median(se$cluster / se$poisson) - 1), 0))
)

target <- file.path(find_repo_root(), "docs", "frequency-dispersion.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message("wrote ", target)
