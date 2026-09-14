# What the severity model is fitted on, and what that does to pure premium.
# Writes docs/severity-population.md.
#
#   Rscript R/severity_population.R
#
# A couple of minutes. Deterministic for a given frame.
#
# Four questions, in order:
#   1. Every way zero-claim and unpriced policies can reach a Gamma fit, and
#      whether each one fails loudly or quietly.
#   2. Whether frequency times severity reproduces the losses actually recorded.
#   3. Whether the claims with no amount look like the claims with one.
#   4. Whether a holdout can tell a rated severity model from a constant yet.

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

rhs <- paste(FREQUENCY_TERMS, collapse = " + ")

# ---------------------------------------------------------------------------
# 1. The pitfall, every way
# ---------------------------------------------------------------------------

per_policy <- rowsum(claims$claim_amount, claims$idpol, reorder = FALSE)
priced_count <- tabulate(match(claims$idpol, policies$idpol), nbins = nrow(policies))
total_na <- rep(NA_real_, nrow(policies))
total_na[match(as.integer(rownames(per_policy)), policies$idpol)] <- per_policy[, 1]
policies$total_na <- total_na
policies$total_zero <- ifelse(is.na(total_na), 0, total_na)
policies$average_na <- policies$total_na / policies$claim_nb
policies$average_zero <- policies$total_zero / policies$claim_nb
reporting <- policies[policies$claim_nb > 0, ]

attempt <- function(label, response, data) {
  outcome <- tryCatch({
    fit <- suppressWarnings(stats::glm(stats::as.formula(paste(response, "~", rhs)),
                                       family = stats::Gamma(link = "log"), data = data,
                                       control = stats::glm.control(maxit = SEVERITY_MAX_ITERATIONS)))
    used <- stats::nobs(fit)
    rm(fit)
    c(label, fmt(nrow(data), 0), "runs", fmt(used, 0), fmt(nrow(data) - used, 0))
  }, error = function(e) c(label, fmt(nrow(data), 0), paste0("fails: ", conditionMessage(e)), "", ""))
  invisible(gc())
  outcome
}

fmt <- function(x, digits) formatC(x, format = "f", digits = digits, big.mark = ",")

pitfall_rows <- list(
  attempt("all policies; total amount; unpriced as 0", "total_zero", policies),
  attempt("all policies; total amount; unpriced left missing", "total_na", policies),
  attempt("all policies; amount per claim; unpriced as 0", "average_zero", policies),
  attempt("all policies; amount per claim; unpriced left missing", "average_na", policies),
  attempt("claim_nb > 0; amount per claim; unpriced as 0", "average_zero", reporting),
  attempt("claim_nb > 0; amount per claim; unpriced left missing", "average_na", reporting)
)

guard_messages <- list(
  missing = tryCatch(fit_severity(data.frame(claim_amount = c(100, NA)), character(0)),
                     error = function(e) conditionMessage(e)),
  zero = tryCatch(fit_severity(data.frame(claim_amount = c(100, 0)), character(0)),
                  error = function(e) conditionMessage(e))
)

# ---------------------------------------------------------------------------
# 2. Balance to recorded losses
# ---------------------------------------------------------------------------

exposure <- sum(policies$exposure)
losses <- sum(claims$claim_amount)
reported <- sum(policies$claim_nb)
priced <- nrow(claims)
mean_severity <- losses / priced
balance <- c(
  observed = losses / exposure,
  reported = reported / exposure * mean_severity,
  priced   = priced / exposure * mean_severity
)
unpriced_policies <- sum(policies$claim_nb > 0 & priced_count == 0)
partial_policies <- sum(priced_count > 0 & priced_count < policies$claim_nb)
over_priced <- sum(priced_count > policies$claim_nb)

# ---------------------------------------------------------------------------
# 3. Who has unpriced claims
# ---------------------------------------------------------------------------

has_unpriced <- policies$claim_nb > priced_count
all_priced <- policies$claim_nb > 0 & !has_unpriced
profile_row <- function(label, x, digits) {
  c(label, fmt(mean(x[has_unpriced]), digits), fmt(mean(x[all_priced]), digits))
}
profile_rows <- list(
  profile_row("mean exposure, years", policies$exposure, 3),
  profile_row("share flagged short exposure", policies$is_short_exposure, 3),
  profile_row("mean reported claims", policies$claim_nb, 3),
  profile_row("mean driver age", policies$driv_age, 1),
  profile_row("mean vehicle age", policies$veh_age, 1),
  profile_row("mean bonus-malus", policies$bonus_malus, 1),
  profile_row("mean log density", log(policies$density), 2)
)
reporting_mask <- policies$claim_nb > 0
unpriced_share_by_region <- tapply(has_unpriced[reporting_mask], policies$region[reporting_mask], mean)

# ---------------------------------------------------------------------------
# 4. The fit, and whether a holdout can choose its terms yet
# ---------------------------------------------------------------------------

default_iterations <- suppressWarnings(stats::glm(
  stats::as.formula(paste("claim_amount ~", rhs)), family = stats::Gamma(link = "log"), data = claims))
default_limit <- c(converged = default_iterations$converged, iterations = default_iterations$iter)
rm(default_iterations); invisible(gc())

fit <- fit_severity(claims, FREQUENCY_TERMS)
fitted_amount <- stats::fitted(fit)
severity_fit <- c(
  iterations = fit$iter,
  parameters = length(stats::coef(fit)),
  fitted_over_observed = sum(fitted_amount) / losses,
  mean_ratio = mean(claims$claim_amount / fitted_amount),
  dispersion = sum(stats::residuals(fit, type = "pearson")^2) / fit$df.residual
)
rm(fit, fitted_amount); invisible(gc())

sorted_amounts <- sort(claims$claim_amount, decreasing = TRUE)
concentration <- vapply(c(1, 10, 100, round(0.01 * priced)), function(k) sum(sorted_amounts[seq_len(k)]) / losses, 0)
amount_quantiles <- stats::quantile(claims$claim_amount, c(0.5, 0.9, 0.99, 0.995))

claims$holdout_fold <- claims$risk_group %% 5
fold_rows <- list()
fold_iterations <- list()
fold0 <- NULL
for (k in 0:4) {
  test <- claims[claims$holdout_fold == k, ]
  train <- claims[claims$holdout_fold != k, ]
  mu_constant <- stats::predict(fit_severity(train, character(0)), newdata = test, type = "response")
  rated <- fit_severity(train, FREQUENCY_TERMS)
  mu_rated <- stats::predict(rated, newdata = test, type = "response")
  default_fit <- suppressWarnings(stats::glm(stats::as.formula(paste("claim_amount ~", rhs)),
                                             family = stats::Gamma(link = "log"), data = train))
  fold_iterations[[k + 1]] <- c(needed = rated$iter, default_converged = default_fit$converged)
  rm(rated, default_fit)
  d_constant <- gamma_unit_deviance(test$claim_amount, mu_constant)
  d_rated <- gamma_unit_deviance(test$claim_amount, mu_rated)
  fold_rows[[k + 1]] <- c(k, nrow(test), sum(d_constant), sum(d_rated), sum(d_rated) - sum(d_constant))
  if (k == 0) {
    difference <- d_rated - d_constant
    largest <- which.max(abs(difference))
    by_size <- order(test$claim_amount, decreasing = TRUE)
    fold0 <- list(
      total = sum(difference), claims = nrow(test),
      largest_amount = test$claim_amount[largest], largest_share = difference[largest],
      without_largest = sum(difference[-largest]),
      without_ten = sum(difference[-by_size[1:10]]),
      below_median = sum(difference[test$claim_amount <= stats::median(test$claim_amount)]),
      above_p99 = sum(difference[test$claim_amount > stats::quantile(claims$claim_amount, 0.99)])
    )
  }
  invisible(gc())
}
folds <- do.call(rbind, fold_rows)
fold_iterations <- do.call(rbind, fold_iterations)
colnames(folds) <- c("fold", "claims", "constant", "rated", "difference")

if (!(abs(fold0$largest_share) > abs(fold0$without_largest))) {
  stop("the fold-0 result is no longer dominated by one claim; the specification section needs rewriting",
       call. = FALSE)
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

md_table <- function(header, rows) {
  c(paste0("| ", paste(header, collapse = " | "), " |"),
    paste0("|", paste(rep("---", length(header)), collapse = "|"), "|"),
    vapply(rows, function(r) paste0("| ", paste(r, collapse = " | "), " |"), ""))
}

out <- c(
  "# The severity population",
  "",
  "Generated by `R/severity_population.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf(paste(
    "Computed from `model.severity_frame`, %s priced claims totalling %s, joined to",
    "`model.frequency_frame`, %s rows, frame md5 `%s`. %s."),
    fmt(priced, 0), fmt(losses, 2), fmt(nrow(policies), 0), frame_md5, R.version.string),
  "",
  paste0("Rating terms where a fit needs them: `", rhs, "`"),
  "",
  "## Every way a zero can reach a Gamma fit",
  "",
  "A Gamma model cannot take a zero. Whether a policy without an amount makes the fit",
  "fail or disappears from it depends on how its missing amount was written.",
  "",
  md_table(c("Response built from", "Rows given", "Outcome", "Rows used", "Rows silently dropped"), pitfall_rows),
  "",
  sprintf(paste(
    "Written as 0, every version fails. Left missing, every version runs, because",
    "`glm()` removes rows with a missing response before fitting and reports nothing.",
    "The silent versions land on %s policies, the right population, by accident. What",
    "they never report is that restricting to `claim_nb > 0`, the usual advice, still",
    "handed the fit %s policies whose reported claims have no amount."),
    fmt(sum(priced_count > 0), 0), fmt(unpriced_policies, 0)),
  "",
  "`fit_severity()` refuses both cases by name instead of relying on either behaviour:",
  "",
  paste0("- missing amount: *", guard_messages$missing, "*"),
  paste0("- zero amount: *", guard_messages$zero, "*"),
  "",
  "## Does frequency times severity reproduce recorded losses?",
  "",
  md_table(
    c("Pure premium per policy-year", "Value", "Against recorded"),
    list(c("recorded losses over exposure", fmt(balance[["observed"]], 2), ""),
         c("frequency of reported claims times mean severity", fmt(balance[["reported"]], 2),
           paste0(fmt(100 * (balance[["reported"]] / balance[["observed"]] - 1), 1), "% high")),
         c("frequency of priced claims times mean severity", fmt(balance[["priced"]], 2),
           paste0(fmt(100 * (balance[["priced"]] / balance[["observed"]] - 1), 1), "%")))
  ),
  "",
  sprintf(paste(
    "The last row is an identity, not a finding: priced claims times their mean amount",
    "is recorded losses by definition, and it is there to anchor the comparison. The",
    "%s%% is simply the ratio of reported claims, %s, to priced claims, %s."),
    fmt(100 * (balance[["reported"]] / balance[["observed"]] - 1), 1), fmt(reported, 0), fmt(priced, 0)),
  "",
  sprintf(paste(
    "Whether it is an overstatement depends on what an unpriced claim is, and this data",
    "cannot say. If unpriced claims cost nothing, closed without payment, then a",
    "frequency model of reported claims overprices the book by that margin. If they",
    "are claims whose amounts are missing from the file, recorded losses understate",
    "the true cost instead. %s policies report claims with no amount at all, %s %s",
    "some claims priced and some not, and no policy has more priced claims than it",
    "reported."),
    fmt(unpriced_policies, 0), fmt(partial_policies, 0), if (partial_policies == 1) "has" else "have"),
  "",
  "## Do unpriced claims look like priced ones?",
  "",
  "Policies reporting at least one claim, split by whether every reported claim has an",
  "amount.",
  "",
  md_table(c("", "Some claims unpriced", "All claims priced"), profile_rows),
  "",
  sprintf(paste(
    "By region, the share of claim-reporting policies with an unpriced claim runs from",
    "%s%% in %s to %s%% in %s."),
    fmt(100 * min(unpriced_share_by_region), 0), names(which.min(unpriced_share_by_region)),
    fmt(100 * max(unpriced_share_by_region), 0), names(which.max(unpriced_share_by_region))),
  "",
  "They are not a random subset. Unpriced claims sit on newer vehicles, shorter",
  "exposures and particular regions, so no single scaling factor applied to the",
  "frequency model can correct for them. Whichever claim count frequency is fitted on",
  "changes relativities, not only the level, and that is what makes the choice matter",
  "even though the data cannot settle it.",
  "",
  "## The Gamma fit",
  "",
  md_table(
    c("", "Value"),
    list(c("claims", fmt(priced, 0)),
         c("parameters, frequency terms", fmt(severity_fit[["parameters"]], 0)),
         c("iterations to converge, all claims", fmt(severity_fit[["iterations"]], 0)),
         c("converged within the glm() default of 25, all claims",
           if (default_limit[["converged"]]) "yes" else "no"),
         c("iterations to converge, the five training folds",
           paste(fmt(fold_iterations[, "needed"], 0), collapse = ", ")),
         c("training folds that converge within the default of 25",
           paste0(sum(fold_iterations[, "default_converged"] == 1), " of 5")),
         c("fitted total over recorded total", fmt(severity_fit[["fitted_over_observed"]], 4)),
         c("mean of amount over fitted", fmt(severity_fit[["mean_ratio"]], 4)),
         c("Pearson dispersion", fmt(severity_fit[["dispersion"]], 2)))
  ),
  "",
  sprintf(paste(
    "A Gamma model with a log link balances the mean ratio of amount to fitted amount,",
    "which comes out at exactly 1, and not the total. Fitted amounts sum to %s%% of",
    "recorded ones, so pure premium built on this fit carries that gap into its level."),
    fmt(100 * severity_fit[["fitted_over_observed"]], 2)),
  "",
  sprintf(paste(
    "On all claims the fit happens to converge inside glm()'s default limit of 25",
    "iterations. On training subsets it needs up to %s, and a fit that runs out of",
    "iterations still returns coefficients, with only a warning. `fit_severity()`",
    "allows %s and refuses a fit that has not converged."),
    fmt(max(fold_iterations[, "needed"]), 0), SEVERITY_MAX_ITERATIONS),
  "",
  md_table(
    c("Largest claims", "Share of recorded losses"),
    list(c("1", paste0(fmt(100 * concentration[1], 1), "%")),
         c("10", paste0(fmt(100 * concentration[2], 1), "%")),
         c("100", paste0(fmt(100 * concentration[3], 1), "%")),
         c(paste0(fmt(round(0.01 * priced), 0), ", the top 1%"), paste0(fmt(100 * concentration[4], 1), "%")))
  ),
  "",
  sprintf("Median claim %s; 90th percentile %s; 99th %s; 99.5th %s; largest %s.",
          fmt(amount_quantiles[[1]], 0), fmt(amount_quantiles[[2]], 0), fmt(amount_quantiles[[3]], 0),
          fmt(amount_quantiles[[4]], 0), fmt(max(claims$claim_amount), 0)),
  "",
  "## Can a holdout choose the severity terms yet?",
  "",
  "A rule was fixed before fitting: on the risk-group holdout used for frequency, the",
  "frequency terms against a constant, lower Gamma deviance wins. It picked the",
  sprintf("constant, by %s. One claim decides that.", fmt(fold0$total, 1)),
  "",
  md_table(
    c("Fold-0 holdout, rated minus constant deviance", "Value"),
    list(c(sprintf("all %s claims", fmt(fold0$claims, 0)), fmt(fold0$total, 1)),
         c(sprintf("the single claim contributing most, an amount of %s", fmt(fold0$largest_amount, 0)), fmt(fold0$largest_share, 1)),
         c("without that claim", fmt(fold0$without_largest, 1)),
         c("without the ten largest claims", fmt(fold0$without_ten, 1)),
         c("claims at or below the median amount", fmt(fold0$below_median, 1)),
         c("claims above the 99th percentile", fmt(fold0$above_p99, 1)))
  ),
  "",
  md_table(
    c("Holdout fold", "Claims", "Constant", "Rated", "Rated minus constant"),
    lapply(seq_len(nrow(folds)), function(i) c(
      fmt(folds[i, "fold"], 0), fmt(folds[i, "claims"], 0), fmt(folds[i, "constant"], 1),
      fmt(folds[i, "rated"], 1), fmt(folds[i, "difference"], 1))))
  ,
  "",
  sprintf(paste(
    "The rated model predicts the bulk of claims better and a handful of very large",
    "ones worse, and which of those wins a fold depends on which large claims land in",
    "it. Across the five folds the difference changes sign, from %s to %s. On amounts",
    "this heavy-tailed, a single holdout cannot choose between these specifications,",
    "so the rule's verdict is not taken."),
    fmt(min(folds[, "difference"]), 1), fmt(max(folds[, "difference"]), 1)),
  "",
  "The specification is left to D2-6, which treats large losses first. Disclosed so",
  "that its choice is not presented as blind: while writing this analysis, the same",
  "single-fold comparison was also run with claims capped at their 99.5th percentile,",
  "and the rated model came out slightly ahead.",
  "",
  "## Handed on",
  "",
  "- **D2-6:** choose the severity terms after large losses are capped, with the",
  "  candidates and a multi-fold rule stated before fitting.",
  "- **D3-1:** decide which claim count the frequency side of pure premium uses,",
  "  reported or priced, knowing the data cannot say what an unpriced claim costs and",
  "  that the choice moves relativities. Decide as well whether the severity level is",
  "  rebalanced to recorded totals."
)

target <- file.path(find_repo_root(), "docs", "severity-population.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message("wrote ", target)
