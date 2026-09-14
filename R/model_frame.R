# The frequency model frame, read from model.frequency_frame.
#
# No joins happen here. The view is the definition of a policy-year as a model
# sees it, and scripts/frame.py reads the same view; a test checks the two
# languages receive byte-identical frames. What this file adds is only what a
# data frame needs and a view cannot express: factor levels, and assertions that
# fail loudly on the ways a frame goes quietly wrong between a database and R.

CATEGORICAL_COLUMNS <- c("area", "veh_brand", "veh_gas", "region")

FLAG_COLUMNS <- c("is_short_exposure", "is_high_claim_count", "is_exposure_capped")

# Levels come from the dimension tables, not from the rows, so a subset of the
# frame keeps every declared level. They are put in natural order, B2 before
# B10, for readable output. That choice does not affect the base level, which is
# the first level either way and is chosen deliberately in the rate table.
dimension_levels <- function(con) {
  natural <- function(codes) codes[order(as.integer(gsub("\\D", "", codes)), codes)]
  list(
    area      = natural(DBI::dbGetQuery(con, "SELECT area_code FROM dim.area")[[1]]),
    veh_brand = natural(DBI::dbGetQuery(con, "SELECT DISTINCT veh_brand FROM dim.vehicle")[[1]]),
    veh_gas   = sort(DBI::dbGetQuery(con, "SELECT DISTINCT veh_gas FROM dim.vehicle")[[1]]),
    region    = natural(DBI::dbGetQuery(con, "SELECT region_code FROM dim.region")[[1]])
  )
}

load_frequency_frame <- function(con) {
  frame <- DBI::dbGetQuery(con, "SELECT * FROM model.frequency_frame ORDER BY idpol")
  expected_rows <- DBI::dbGetQuery(con, "SELECT count(*) FROM fact.exposure")[[1]]

  check <- function(ok, message) if (!isTRUE(ok)) stop(message, call. = FALSE)

  check(nrow(frame) == expected_rows,
        sprintf("frame has %d rows, fact.exposure has %d", nrow(frame), expected_rows))

  # The other two bigint settings fail as a silently rounded integer64 or a
  # silent NA. The first is caught by class, the second by the NA check below.
  classes <- vapply(frame, function(col) class(col)[1], "")
  check(!any(classes == "integer64"),
        paste("integer64 columns reached R:", paste(names(classes)[classes == "integer64"], collapse = ", "),
              "- connect() must use bigint = 'numeric'"))

  check(!anyNA(frame),
        paste("NA values in:", paste(names(frame)[vapply(frame, anyNA, TRUE)], collapse = ", ")))

  check(!any(grepl("_key$", names(frame))),
        "surrogate keys in the frame would be fitted as continuous covariates")

  # connect() reads bigint as double, which is right for aggregates and wrong for
  # an identifier: as.character(100000), paste() and write.csv() all render a
  # double 100000 as "1e+05". Every multiple of 100,000 in the upstream file is
  # written exactly the way write.csv writes it from a double, 1e+05 for 100000
  # and fixed notation for 2100000, 3200000 and 5100000, which makes an R export
  # the most likely origin of the malformed id repaired in transform 002. So idpol
  # becomes an integer here, after proving the conversion loses nothing, rather
  # than trusting that nobody will ever paste it.
  check(all(frame$idpol >= 1 & frame$idpol <= .Machine$integer.max &
            frame$idpol == trunc(frame$idpol)),
        "idpol values do not fit losslessly in an R integer")
  frame$idpol <- as.integer(frame$idpol)

  check(is.double(frame$exposure) && all(frame$exposure > 0 & frame$exposure <= 1),
        "exposure must be a double in (0, 1]")

  check(is.integer(frame$claim_nb) && all(frame$claim_nb >= 0),
        "claim_nb must be a non-negative integer")

  levels <- dimension_levels(con)
  for (column in CATEGORICAL_COLUMNS) {
    values <- frame[[column]]
    unknown <- setdiff(unique(values), levels[[column]])
    check(length(unknown) == 0,
          sprintf("%s has values not in its dimension: %s", column, paste(unknown, collapse = ", ")))
    frame[[column]] <- factor(values, levels = levels[[column]])
  }

  frame
}

# md5 over one canonical line per row. scripts/frame.py computes the same hash in
# Python, and R/check_frame.R documents why each column is formatted as it is.
canonical_md5 <- function(frame) {
  lines <- paste(
    sprintf("%d", frame$idpol),
    sprintf("%d", frame$claim_nb),
    sprintf("%.17g", frame$exposure),
    as.character(frame$area),
    sprintf("%d", frame$veh_power),
    sprintf("%d", frame$veh_age),
    sprintf("%d", frame$driv_age),
    sprintf("%d", frame$bonus_malus),
    as.character(frame$veh_brand),
    as.character(frame$veh_gas),
    sprintf("%d", frame$density),
    as.character(frame$region),
    as.integer(frame$is_short_exposure),
    as.integer(frame$is_high_claim_count),
    as.integer(frame$is_exposure_capped),
    sep = "|"
  )
  path <- tempfile(fileext = ".txt")
  on.exit(unlink(path))
  connection <- file(path, open = "wb")
  writeLines(lines, connection, sep = "\n", useBytes = TRUE)
  close(connection)
  unname(tools::md5sum(path))
}


# A risk group is a run of consecutive policy ids with identical rating factors.
#
# 14% of rows match the previous policy id on all nine factors, which is one
# policy recorded as several rows rather than chance. Reassembled, only 102 of
# 581,535 groups exceed one year of exposure, which is what pieces of a single
# policy-year would do. Anything that needs independent units uses these groups:
# the holdout split in R/frequency_banding.R and the cluster-robust standard
# errors in R/frequency_dispersion.R. Defined once, here.
RISK_PROFILE_COLUMNS <- c("area", "veh_power", "veh_age", "driv_age", "bonus_malus",
                          "veh_brand", "veh_gas", "density", "region")

risk_groups <- function(frame) {
  # "Consecutive" only means something in policy-id order, and a frame that has
  # been reordered would silently produce different groups.
  if (is.unsorted(frame$idpol, strictly = TRUE)) {
    stop("risk_groups() needs the frame in strictly increasing idpol order", call. = FALSE)
  }
  profile <- do.call(paste, c(lapply(frame[RISK_PROFILE_COLUMNS], as.character), sep = "|"))
  cumsum(c(TRUE, profile[-1] != profile[-length(profile)]))
}

add_policy_losses <- function(con, frame) {
  losses <- DBI::dbGetQuery(con, "SELECT idpol, priced_claim_nb, incurred_loss FROM model.policy_loss ORDER BY idpol")
  if (!identical(as.integer(losses$idpol), frame$idpol)) {
    stop("model.policy_loss does not match the frame row for row", call. = FALSE)
  }
  frame$priced_claim_nb <- losses$priced_claim_nb
  frame$incurred_loss <- losses$incurred_loss
  frame
}

add_holdout <- function(con, frame) {
  holdout <- DBI::dbGetQuery(con, "SELECT idpol, risk_group, risk_group_holdout, idpol_holdout FROM model.holdout ORDER BY idpol")
  if (!identical(as.integer(holdout$idpol), frame$idpol)) {
    stop("model.holdout does not match the frame row for row", call. = FALSE)
  }
  if (!identical(as.integer(holdout$risk_group), as.integer(risk_groups(frame)))) {
    stop("model.holdout's risk groups differ from risk_groups()", call. = FALSE)
  }
  frame$risk_group_holdout <- holdout$risk_group_holdout
  frame$idpol_holdout <- holdout$idpol_holdout
  frame
}
