# Connection to the mart for the R side (L2 frequency, L3 severity).
#
# Reads the same .env as scripts/db.py, with the same precedence: built-in
# defaults, then the file, then real environment variables. There is one place
# the database address is decided, and R does not keep its own copy of it.

find_repo_root <- function(start = getwd()) {
  dir <- normalizePath(start, winslash = "/", mustWork = TRUE)
  repeat {
    if (dir.exists(file.path(dir, "migrations")) &&
        file.exists(file.path(dir, ".env.example"))) {
      return(dir)
    }
    parent <- dirname(dir)
    if (identical(parent, dir)) {
      stop("could not find the repository root above ", start, call. = FALSE)
    }
    dir <- parent
  }
}

load_env <- function(env_file = file.path(find_repo_root(), ".env")) {
  values <- c(
    POSTGRES_DB   = "motor_pricing",
    POSTGRES_USER = "pricing",
    POSTGRES_HOST = "localhost",
    POSTGRES_PORT = "5432"
  )

  if (file.exists(env_file)) {
    for (line in trimws(readLines(env_file, warn = FALSE))) {
      if (!nzchar(line) || startsWith(line, "#") || !grepl("=", line, fixed = TRUE)) {
        next
      }
      key <- trimws(sub("=.*$", "", line))
      values[key] <- trimws(sub("^[^=]*=", "", line))
    }
  }

  for (key in union(names(values), "POSTGRES_PASSWORD")) {
    from_environment <- Sys.getenv(key, unset = "")
    if (nzchar(from_environment)) values[key] <- from_environment
  }

  password <- unname(values["POSTGRES_PASSWORD"])
  if (is.na(password) || !nzchar(password)) {
    stop("POSTGRES_PASSWORD is not set. Copy .env.example to .env (expected at ",
         env_file, ") or export it.", call. = FALSE)
  }

  values
}

# bigint = "numeric" is a decision, and the other two options are both wrong in
# ways that do not raise anything. Measured against this database:
#
#   default, integer64    idpol * 1.5 on ids 1, 3, 5 gives 2, 5, 8.
#                         Multiplying by a non-integer rounds back to an
#                         integer64, silently, and is.numeric() is still TRUE,
#                         so an ordinary type check lets it through.
#   bigint = "integer"    3000000000::bigint arrives as NA, with no warning.
#   bigint = "numeric"    3000000000 exactly, and arithmetic behaves.
#
# A double represents every integer up to 2^53 exactly, and nothing in this
# project comes within nine orders of magnitude of that. Postgres returns bigint
# for count(*) and for sum() over integers, so this matters for ad-hoc
# aggregates as much as for the one bigint column in the model frame.
connect <- function(env_file = file.path(find_repo_root(), ".env")) {
  v <- load_env(env_file)
  DBI::dbConnect(
    RPostgres::Postgres(),
    host     = v[["POSTGRES_HOST"]],
    port     = as.integer(v[["POSTGRES_PORT"]]),
    dbname   = v[["POSTGRES_DB"]],
    user     = v[["POSTGRES_USER"]],
    password = v[["POSTGRES_PASSWORD"]],
    bigint   = "numeric"
  )
}
