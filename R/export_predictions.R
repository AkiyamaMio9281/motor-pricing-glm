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

fit_reported_frequency <- function(frame) fit_frequency(frame, FREQUENCY_TERMS, "claim_nb")

TRAINING <- list(
  all = function(frame) rep(TRUE, nrow(frame)),
  risk_group_split = function(frame) !frame$risk_group_holdout,
  idpol_split = function(frame) !frame$idpol_holdout
)

RUNS <- list(
  priced_claims = list(frequency = fit_pricing_frequency, trained_on = "all"),
  reported_claims = list(frequency = fit_reported_frequency, trained_on = "all"),
  priced_claims_risk_group_split = list(frequency = fit_pricing_frequency, trained_on = "risk_group_split"),
  reported_claims_risk_group_split = list(frequency = fit_reported_frequency, trained_on = "risk_group_split"),
  priced_claims_idpol_split = list(frequency = fit_pricing_frequency, trained_on = "idpol_split"),
  reported_claims_idpol_split = list(frequency = fit_reported_frequency, trained_on = "idpol_split")
)

con <- connect()
policies <- load_frequency_frame(con)
frame_md5 <- canonical_md5(policies)
policies <- apply_bands(add_holdout(con, add_policy_losses(con, policies)))
claims <- load_severity_frame(con, policies)

coefficient_rows <- function(run, model, estimates) {
  data.frame(run = run, model = model, term = names(estimates), estimate = unname(estimates))
}

severity_for <- list()
for (trained_on in unique(vapply(RUNS, function(r) r$trained_on, ""))) {
  training_claims <- claims[TRAINING[[trained_on]](claims), ]
  severity <- fit_pricing_severity(training_claims)
  severity_for[[trained_on]] <- list(
    capped_amount_per_claim = unname(stats::predict(severity, newdata = policies, type = "response")),
    coefficients = stats::coef(severity),
    load = large_loss_load(training_claims)
  )
  rm(severity, training_claims); invisible(gc())
}

for (run in names(RUNS)) {
  trained_on <- RUNS[[run]]$trained_on
  training <- TRAINING[[trained_on]](policies)
  severity <- severity_for[[trained_on]]

  fit <- RUNS[[run]]$frequency(policies[training, ])
  response <- all.vars(stats::formula(fit))[1]
  claims_per_year <- unname(annual_frequency(fit, policies))
  coefficients <- rbind(coefficient_rows(run, "frequency", stats::coef(fit)),
                        coefficient_rows(run, "severity", severity$coefficients))
  rm(fit); invisible(gc())

  predictions <- data.frame(run = run, idpol = policies$idpol, claims_per_year = claims_per_year,
                            capped_amount_per_claim = severity$capped_amount_per_claim)
  DBI::dbWithTransaction(con, {
    DBI::dbExecute(con, "DELETE FROM model.glm_run WHERE run = $1", params = list(run))
    DBI::dbExecute(con, paste(
      "INSERT INTO model.glm_run (run, frame_md5, frequency_response, large_loss_cap, large_loss_load, trained_on)",
      "VALUES ($1, $2, $3, $4, $5, $6)"),
      params = list(run, frame_md5, response, LARGE_LOSS_CAP, severity$load, trained_on))
    DBI::dbWriteTable(con, DBI::Id(schema = "model", table = "glm_prediction"), predictions,
                      append = TRUE, copy = TRUE)
    DBI::dbWriteTable(con, DBI::Id(schema = "model", table = "glm_coefficient"), coefficients,
                      append = TRUE, copy = TRUE)
  })

  stored <- DBI::dbGetQuery(con, paste(
    "SELECT claims_per_year, capped_amount_per_claim FROM model.glm_prediction",
    "WHERE run = $1 ORDER BY idpol"), params = list(run))
  if (!identical(stored$claims_per_year, claims_per_year) ||
      !identical(stored$capped_amount_per_claim, severity$capped_amount_per_claim)) {
    stop("predictions for ", run, " did not survive the round trip to Postgres bit for bit", call. = FALSE)
  }
  message(sprintf("%s: trained on %d rows, exposure-weighted claims %.3f against %d observed",
                  run, sum(training), sum(policies$exposure[training] * claims_per_year[training]),
                  sum(policies[[response]][training])))
}

DBI::dbDisconnect(con)
