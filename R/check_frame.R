# Load the frequency frame and print a fingerprint of it as JSON.
#
#   Rscript R/check_frame.R
#
# tests/test_model_frame.py runs this and compares the fingerprint with the one
# scripts/frame.py computes from the same view. The md5 is over a canonical text
# rendering of every row, so it catches a single changed value that aggregates
# would miss.
#
# canonical_md5() lives in R/model_frame.R so any R output can be stamped with
# the frame it was computed from. The rendering has to be identical in both
# languages, which is less trivial than it sounds:
#   - idpol is formatted with %d. The loader has already made it an integer; a
#     double 100000 would render as "1e+05" through as.character or paste,
#     which is exactly how one policy id is written in the upstream file.
#   - exposure uses %.17g, enough digits to identify a double uniquely, so both
#     languages print the same bits the same way.
#   - the file is opened in binary mode. writeLines on a text-mode connection on
#     Windows writes \r\n, and the hash would differ by 678,013 carriage returns.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  source(file.path(here, "db.R"))
  source(file.path(here, "model_frame.R"))
})

started <- Sys.time()
con <- connect()
frame <- load_frequency_frame(con)
DBI::dbDisconnect(con)
seconds <- as.numeric(difftime(Sys.time(), started, units = "secs"))

level_counts <- vapply(CATEGORICAL_COLUMNS, function(column) nlevels(frame[[column]]), 0L)

json <- paste0(
  "{",
  sprintf('"rows": %d, ', nrow(frame)),
  sprintf('"claims": %d, ', sum(frame$claim_nb)),
  sprintf('"exposure_sum": %.6f, ', sum(frame$exposure)),
  sprintf('"short_exposure_rows": %d, ', sum(frame$is_short_exposure)),
  '"levels": {',
  paste(sprintf('"%s": %d', names(level_counts), level_counts), collapse = ", "),
  "}, ",
  sprintf('"md5": "%s", ', canonical_md5(frame)),
  sprintf('"load_seconds": %.2f', seconds),
  "}"
)
cat(json, "\n", sep = "")
