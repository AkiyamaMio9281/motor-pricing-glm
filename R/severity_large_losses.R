# Cap large claims, choose the severity terms on capped amounts, and test whether
# a flat large-loss load is defensible. Writes docs/severity-large-losses.md.
#
#   Rscript R/severity_large_losses.R
#
# A minute or two. Deterministic for a given frame.
#
# Two rules, written here before anything below is computed:
#
#   Terms. On capped amounts, over the five risk-group folds used throughout, the
#   frequency terms against a constant: lower total out-of-fold Gamma deviance wins.
#   These are the two candidates D2-5 handed on, and no third one is added, because
#   anything added now would be informed by D2-5's drop-one tests.
#
#   Flat load. Adding the capped-off losses back as one flat factor assumes large
#   claims fall evenly across the book. The assumption is rejected if the 95%
#   interval for the slope of logit P(claim above the cap) on log out-of-fold
#   predicted capped severity, from the rated model, excludes zero.
#
# The threshold itself is the plan's 99.5th percentile, not chosen by a rule. The
# document shows what it costs next to the 99th and 99.9th.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  source(file.path(here, "db.R"))
  source(file.path(here, "model_frame.R"))
  source(file.path(here, "bands.R"))
  source(file.path(here, "frequency.R"))
  source(file.path(here, "severity.R"))
})

con <- connect()
policies <- load_frequency_frame(con)
frame_md5 <- canonical_md5(policies)
policies <- apply_bands(policies)
policies$risk_group <- risk_groups(policies)
claims <- load_severity_frame(con, policies)
DBI::dbDisconnect(con)

amount <- claims$claim_amount
losses <- sum(amount)
exposure <- sum(policies$exposure)

observed_cap <- round(unname(stats::quantile(amount, 0.995, type = 7)))
if (observed_cap != LARGE_LOSS_CAP) {
  stop(sprintf("LARGE_LOSS_CAP is %s but the 99.5th percentile of the loaded claims is %s",
               LARGE_LOSS_CAP, observed_cap), call. = FALSE)
}

fmt <- function(x, digits) formatC(x, format = "f", digits = digits, big.mark = ",")
md_table <- function(header, rows) {
  c(paste0("| ", paste(header, collapse = " | "), " |"),
    paste0("|", paste(rep("---", length(header)), collapse = "|"), "|"),
    vapply(rows, function(r) paste0("| ", paste(r, collapse = " | "), " |"), ""))
}

# ---------------------------------------------------------------------------
# Thresholds, and capping against dropping
# ---------------------------------------------------------------------------

threshold_rows <- lapply(c(0.99, 0.995, 0.999), function(p) {
  cap <- unname(stats::quantile(amount, p, type = 7))
  excess <- sum(pmax(amount - cap, 0))
  c(paste0(format(100 * p), "th percentile", if (p == 0.995) ", used" else ""),
    fmt(cap, 0), fmt(sum(amount > cap), 0),
    paste0(fmt(100 * excess / losses, 1), "%"),
    paste0(fmt(100 * sum(amount[amount > cap]) / losses, 1), "%"),
    fmt(losses / (losses - excess), 4))
})

capped <- cap_claims(claims)
above <- capped$claim_excess > 0
excess_total <- sum(capped$claim_excess)
load <- large_loss_load(capped)
sorted_excess <- sort(capped$claim_excess, decreasing = TRUE)
load_without_largest <- (losses - max(amount)) / (sum(capped$claim_amount) - LARGE_LOSS_CAP)

pure_premium <- c(
  recorded = losses / exposure,
  capped = sum(capped$claim_amount) / exposure,
  dropped = sum(amount[!above]) / exposure
)

# ---------------------------------------------------------------------------
# Terms: out-of-fold comparison on capped amounts
# ---------------------------------------------------------------------------

capped$fold <- capped$risk_group %% 5
oof_rated <- rep(NA_real_, nrow(capped))
fold_rows <- list()
for (k in 0:4) {
  test <- capped$fold == k
  train <- capped[!test, ]
  constant <- fit_severity(train, character(0))
  rated <- fit_severity(train, FREQUENCY_TERMS)
  mu_constant <- stats::predict(constant, newdata = capped[test, ], type = "response")
  mu_rated <- stats::predict(rated, newdata = capped[test, ], type = "response")
  oof_rated[test] <- mu_rated
  y <- capped$claim_amount[test]
  fold_rows[[k + 1]] <- c(fold = k, claims = sum(test),
                          constant = sum(gamma_unit_deviance(y, mu_constant)),
                          rated = sum(gamma_unit_deviance(y, mu_rated)))
  rm(constant, rated, train); invisible(gc())
}
folds <- do.call(rbind, fold_rows)
totals <- colSums(folds[, c("constant", "rated")])
chosen <- if (totals[["rated"]] < totals[["constant"]]) "rated" else "constant"
chosen_terms <- if (chosen == "rated") FREQUENCY_TERMS else character(0)
folds_favouring_chosen <- sum(if (chosen == "rated") folds[, "rated"] < folds[, "constant"]
                              else folds[, "constant"] < folds[, "rated"])

# The same comparison on uncapped amounts, from D2-5, for contrast.
uncapped_fold_differences <- vapply(0:4, function(k) {
  test <- capped$fold == k
  train <- claims[!test, ]
  y <- claims$claim_amount[test]
  mu_c <- stats::predict(fit_severity(train, character(0)), newdata = claims[test, ], type = "response")
  mu_r <- stats::predict(fit_severity(train, FREQUENCY_TERMS), newdata = claims[test, ], type = "response")
  sum(gamma_unit_deviance(y, mu_r)) - sum(gamma_unit_deviance(y, mu_c))
}, numeric(1))

# ---------------------------------------------------------------------------
# The chosen model on all capped claims
# ---------------------------------------------------------------------------

fit <- fit_severity(capped, chosen_terms)
chosen_fit <- c(parameters = length(stats::coef(fit)), iterations = fit$iter,
                fitted_over_capped = sum(stats::fitted(fit)) / sum(capped$claim_amount))
rm(fit); invisible(gc())

# ---------------------------------------------------------------------------
# Flat load: do large claims fall where rated severity is higher?
# ---------------------------------------------------------------------------

logistic <- stats::glm(above ~ log(oof_rated), family = stats::binomial())
slope <- stats::coef(logistic)[["log(oof_rated)"]]
slope_se <- sqrt(stats::vcov(logistic)["log(oof_rated)", "log(oof_rated)"])
slope_interval <- slope + c(-1.96, 1.96) * slope_se
flat_load_rejected <- slope_interval[1] > 0 || slope_interval[2] < 0

quintile <- cut(oof_rated, unique(stats::quantile(oof_rated, seq(0, 1, 0.2))), include.lowest = TRUE,
                labels = FALSE)
largest_claim_quintile <- quintile[which.max(amount)]
quintile_rows <- lapply(sort(unique(quintile)), function(q) {
  in_q <- quintile == q
  c(q, fmt(stats::median(oof_rated[in_q]), 0), fmt(sum(in_q), 0), fmt(sum(above[in_q]), 0),
    paste0(fmt(100 * mean(above[in_q]), 2), "%"),
    paste0(fmt(100 * sum(capped$claim_excess[in_q]) / sum(capped$claim_amount[in_q]), 1), "%"))
})

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

out <- c(
  "# Large losses and the severity terms",
  "",
  "Generated by `R/severity_large_losses.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf(paste(
    "Computed from `model.severity_frame`, %s priced claims totalling %s, and",
    "`model.frequency_frame`, frame md5 `%s`. %s."),
    fmt(nrow(claims), 0), fmt(losses, 2), frame_md5, R.version.string),
  "",
  "**Rules, fixed before computing.** *Terms:* on capped amounts over the five",
  "risk-group folds, the frequency terms against a constant, lower total out-of-fold",
  "Gamma deviance wins. *Flat load:* rejected if the 95% interval for the slope of",
  "logit P(claim above the cap) on log out-of-fold predicted capped severity excludes",
  "zero.",
  "",
  "## The threshold",
  "",
  md_table(c("Threshold", "Cap", "Claims above", "Capped off", "Lost if dropped", "Load factor"), threshold_rows),
  "",
  sprintf(paste(
    "The cap is the plan's 99.5th percentile, %s. %s claims exceed it, and capping",
    "sets aside %s%% of recorded losses, added back as a load of %s on capped severity."),
    fmt(LARGE_LOSS_CAP, 0), fmt(sum(above), 0), fmt(100 * excess_total / losses, 1), fmt(load, 4)),
  "",
  md_table(
    c("Pure premium per policy-year", "Value"),
    list(c("recorded losses", fmt(pure_premium[["recorded"]], 2)),
         c("capped losses", fmt(pure_premium[["capped"]], 2)),
         c("capped losses times the load", fmt(pure_premium[["capped"]] * load, 2)),
         c("claims above the cap dropped instead", fmt(pure_premium[["dropped"]], 2))))
  ,
  "",
  sprintf(paste(
    "Capped losses times the load equal recorded losses by construction. Dropping the",
    "large claims instead removes the part of each below the cap as well, and leaves",
    "pure premium %s%% below recorded."),
    fmt(100 * (1 - pure_premium[["dropped"]] / pure_premium[["recorded"]]), 1)),
  "",
  sprintf(paste(
    "The load rests on few claims. The single largest supplies %s%% of everything",
    "capped off and the ten largest %s%%. Without the largest claim the load would be",
    "%s rather than %s."),
    fmt(100 * sorted_excess[1] / excess_total, 1), fmt(100 * sum(sorted_excess[1:10]) / excess_total, 1),
    fmt(load_without_largest, 4), fmt(load, 4)),
  "",
  "## The severity terms",
  "",
  md_table(
    c("Fold", "Claims", "Constant", "Frequency terms", "Rated minus constant"),
    c(lapply(seq_len(nrow(folds)), function(i) c(
        fmt(folds[i, "fold"], 0), fmt(folds[i, "claims"], 0), fmt(folds[i, "constant"], 1),
        fmt(folds[i, "rated"], 1), fmt(folds[i, "rated"] - folds[i, "constant"], 1))),
      list(c("**total**", fmt(sum(folds[, "claims"]), 0), fmt(totals[["constant"]], 1),
             fmt(totals[["rated"]], 1), fmt(totals[["rated"]] - totals[["constant"]], 1)))))
  ,
  "",
  sprintf("Chosen by the rule: **%s**, favoured in %d of 5 folds.",
          if (chosen == "rated") "the frequency terms" else "a constant", folds_favouring_chosen),
  "",
  paste0("Chosen terms: `", if (length(chosen_terms)) paste(chosen_terms, collapse = " + ") else "1", "`"),
  "",
  sprintf(paste(
    "On uncapped amounts the same folds gave rated-minus-constant differences of %s:",
    "the sign changed with whichever large claims a fold held. Capped, the differences",
    "are %s."),
    paste(fmt(uncapped_fold_differences, 1), collapse = ", "),
    paste(fmt(folds[, "rated"] - folds[, "constant"], 1), collapse = ", ")),
  "",
  sprintf(paste(
    "Fitted on all capped claims, the chosen model has %s parameters, converges in %s",
    "iterations, and its fitted amounts sum to %s of capped losses."),
    fmt(chosen_fit[["parameters"]], 0), fmt(chosen_fit[["iterations"]], 0),
    fmt(chosen_fit[["fitted_over_capped"]], 4)),
  "",
  "Not blind, as D2-5 recorded: a single-fold comparison on capped amounts had",
  "already been run while D2-5's analysis was written, with the rated model slightly",
  "ahead. The five-fold result above was first computed by this script, under the rule.",
  "",
  "## Is a flat load defensible?",
  "",
  "Large-claim incidence by quintile of out-of-fold predicted capped severity from the",
  "rated model:",
  "",
  md_table(c("Quintile", "Median predicted", "Claims", "Above the cap", "Rate", "Capped off, share of capped"),
           quintile_rows),
  "",
  sprintf(paste(
    "The last column is dominated by single claims: the largest claim, %s, falls in",
    "quintile %s. The rate column is the one to read."),
    fmt(max(amount), 0), largest_claim_quintile),
  "",
  sprintf(paste(
    "Logistic slope of P(claim above the cap) on log predicted capped severity: %s,",
    "95%% interval %s to %s, an odds ratio of %s for each doubling of predicted",
    "severity. With %s claims above the cap the test has little power, so the interval",
    "is the result rather than the p-value."),
    fmt(slope, 3), fmt(slope_interval[1], 3), fmt(slope_interval[2], 3),
    fmt(exp(slope * log(2)), 2), fmt(sum(above), 0)),
  "",
  if (flat_load_rejected) {
    paste(
      "**The flat load is rejected by the rule.** The interval excludes zero: claims",
      "above the cap are not spread evenly across predicted severity, so one factor",
      "applied to every policy misallocates the capped-off losses between segments.",
      "The cap stands for fitting the severity model; how the large-loss load is",
      "distributed across the rate table is left to the rate-table layer.")
  } else {
    sprintf(paste(
      "**The flat load is not rejected by the rule, narrowly.** The interval includes",
      "zero, but its lower end is %s, and the point estimate doubles the odds of a large",
      "claim for each doubling of predicted severity. The rule's verdict stands and a",
      "flat load is kept. The evidence leans the other way, and with %s large claims a",
      "concentration of that size could easily escape the test, so the flat load is a",
      "provisional simplification for the rate table to revisit, not a finding that",
      "large claims fall evenly across the book."),
      fmt(slope_interval[1], 3), fmt(sum(above), 0))
  }
)

target <- file.path(find_repo_root(), "docs", "severity-large-losses.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message("wrote ", target, "; chosen: ", chosen, "; flat load rejected: ", flat_load_rejected)
