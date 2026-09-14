#   Rscript R/export_predictions.R
#
# Writes model.glm_run, model.glm_prediction and model.glm_coefficient for scripts/pure_premium.py.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  for (file in c("db.R", "model_frame.R", "bands.R", "frequency.R", "severity.R")) {
    source(file.path(here, file))
  }
})

RUNS <- list(
  priced_claims = fit_pricing_frequency,
  reported_claims = function(frame) fit_frequency(frame, FREQUENCY_TERMS, "claim_nb")
)

con <- connect()
policies <- load_frequency_frame(con)
frame_md5 <- canonical_md5(policies)
policies <- apply_bands(add_policy_losses(con, policies))
claims <- load_severity_frame(con, policies)

severity <- fit_pricing_severity(claims)
capped_amount_per_claim <- unname(stats::predict(severity, newdata = policies, type = "response"))
severity_coefficients <- stats::coef(severity)
load <- large_loss_load(claims)
rm(severity); invisible(gc())

coefficient_rows <- function(run, model, estimates) {
  data.frame(run = run, model = model, term = names(estimates), estimate = unname(estimates))
}

for (run in names(RUNS)) {
  fit <- RUNS[[run]](policies)
  response <- all.vars(stats::formula(fit))[1]
  claims_per_year <- unname(annual_frequency(fit, policies))
  coefficients <- rbind(coefficient_rows(run, "frequency", stats::coef(fit)),
                        coefficient_rows(run, "severity", severity_coefficients))
  rm(fit); invisible(gc())

  predictions <- data.frame(run = run, idpol = policies$idpol, claims_per_year = claims_per_year,
                            capped_amount_per_claim = capped_amount_per_claim)
  DBI::dbWithTransaction(con, {
    DBI::dbExecute(con, "DELETE FROM model.glm_run WHERE run = $1", params = list(run))
    DBI::dbExecute(con, "INSERT INTO model.glm_run VALUES ($1, $2, $3, $4, $5)",
                   params = list(run, frame_md5, response, LARGE_LOSS_CAP, load))
    DBI::dbWriteTable(con, DBI::Id(schema = "model", table = "glm_prediction"), predictions,
                      append = TRUE, copy = TRUE)
    DBI::dbWriteTable(con, DBI::Id(schema = "model", table = "glm_coefficient"), coefficients,
                      append = TRUE, copy = TRUE)
  })

  stored <- DBI::dbGetQuery(con, paste(
    "SELECT claims_per_year, capped_amount_per_claim FROM model.glm_prediction",
    "WHERE run = $1 ORDER BY idpol"), params = list(run))
  if (!identical(stored$claims_per_year, claims_per_year) ||
      !identical(stored$capped_amount_per_claim, capped_amount_per_claim)) {
    stop("predictions for ", run, " did not survive the round trip to Postgres bit for bit", call. = FALSE)
  }
  message(sprintf("%s: %d predictions, exposure-weighted claims %.3f against %d observed",
                  run, nrow(stored), sum(policies$exposure * claims_per_year), sum(policies[[response]])))
}

DBI::dbDisconnect(con)
