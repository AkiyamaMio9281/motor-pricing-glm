# Residual diagnostics for the two pricing GLMs, as ggplot2 figures.
# Writes docs/model-diagnostics.md and two PNGs under docs/figures/.
#
#   Rscript R/model_diagnostics.R
#
# A couple of minutes. The figures are committed: R's png device writes identical
# bytes for identical input on this machine, and the randomised residuals are seeded,
# so regenerating on unchanged data produces no diff.
#
# Both models are checked with randomised quantile residuals (Dunn and Smyth, 1996).
# Under a correctly specified model they are standard normal whatever the
# distribution, which is what makes them usable here. Ordinary residuals from a
# Poisson model of mostly zeros plot as parallel stripes and show nothing, and a
# capped severity is not Gamma at the cap at all.

local({
  script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))
  here <- if (length(script)) dirname(normalizePath(script)) else "R"
  source(file.path(here, "db.R"))
  source(file.path(here, "model_frame.R"))
  source(file.path(here, "bands.R"))
  source(file.path(here, "frequency.R"))
  source(file.path(here, "severity.R"))
})
suppressMessages(library(ggplot2))

con <- connect()
policies <- load_frequency_frame(con)
frame_md5 <- canonical_md5(policies)
policies <- apply_bands(policies)
policies$risk_group <- risk_groups(policies)
claims <- load_severity_frame(con, policies)
DBI::dbDisconnect(con)

set.seed(20260913)
QQ_POINTS <- 2000
figure_dir <- file.path(find_repo_root(), "docs", "figures")
dir.create(figure_dir, showWarnings = FALSE, recursive = TRUE)

fmt <- function(x, digits) formatC(x, format = "f", digits = digits, big.mark = ",")
md_table <- function(header, rows) {
  c(paste0("| ", paste(header, collapse = " | "), " |"),
    paste0("|", paste(rep("---", length(header)), collapse = "|"), "|"),
    vapply(rows, function(r) paste0("| ", paste(r, collapse = " | "), " |"), ""))
}
clip <- function(u) pmin(pmax(u, 1e-12), 1 - 1e-12)

qq_frame <- function(r, group) {
  do.call(rbind, lapply(split(r, group), function(values) {
    probs <- stats::ppoints(QQ_POINTS)
    data.frame(theoretical = stats::qnorm(probs), sample = stats::quantile(values, probs, names = FALSE))
  })) |>
    transform(group = factor(rep(names(split(r, group)), each = QQ_POINTS), levels = names(split(r, group))))
}

summary_row <- function(label, r) {
  c(label, fmt(length(r), 0), paste0(fmt(100 * mean(abs(r) > 1.96), 2), "%"),
    fmt(stats::quantile(r, 0.001), 2), fmt(stats::quantile(r, 0.999), 2))
}

qq_plot <- function(data, title, subtitle) {
  ggplot(data, aes(theoretical, sample)) +
    geom_abline(slope = 1, intercept = 0, linetype = "dashed", colour = "grey45") +
    geom_point(size = 0.5, colour = "#1f4e79") +
    facet_wrap(~ group, nrow = 1) +
    coord_cartesian(ylim = c(-5, 5)) +
    labs(title = title, subtitle = subtitle,
         x = "standard normal quantile", y = "randomised quantile residual") +
    theme_minimal(base_size = 11) +
    theme(plot.title.position = "plot", panel.grid.minor = element_blank())
}

# ---------------------------------------------------------------------------
# Frequency
# ---------------------------------------------------------------------------

fit <- fit_frequency(policies)
mu <- stats::fitted(fit)
rm(fit); invisible(gc())
y <- policies$claim_nb
frequency_r <- stats::qnorm(clip(stats::runif(length(y),
                                              stats::ppois(y - 1, mu),
                                              stats::ppois(y, mu))))
exposure_group <- ifelse(policies$exposure < 0.1, "exposure under 0.1 of a year", "exposure 0.1 of a year or more")
exposure_group <- factor(exposure_group, levels = c("exposure under 0.1 of a year", "exposure 0.1 of a year or more"))

ggsave(file.path(figure_dir, "frequency-quantile-residuals.png"),
       qq_plot(qq_frame(frequency_r, exposure_group),
               "Frequency model: randomised quantile residuals",
               "Poisson GLM, offset log(exposure), banded terms. Points lie on the dashed line if the model is right."),
       width = 8, height = 4.2, dpi = 120, device = "png", bg = "white")

frequency_rows <- list(
  summary_row("all policy-years", frequency_r),
  summary_row(levels(exposure_group)[1], frequency_r[exposure_group == levels(exposure_group)[1]]),
  summary_row(levels(exposure_group)[2], frequency_r[exposure_group == levels(exposure_group)[2]])
)
short_share_beyond <- mean(abs(frequency_r[exposure_group == levels(exposure_group)[1]]) > 1.96)
long_share_beyond <- mean(abs(frequency_r[exposure_group == levels(exposure_group)[2]]) > 1.96)

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

gamma_residuals <- function(fit, amount, censored_at = NULL) {
  shape <- MASS::gamma.shape(fit)$alpha
  mu <- stats::fitted(fit)
  u <- stats::pgamma(amount, shape = shape, rate = shape / mu)
  if (!is.null(censored_at)) {
    # A capped claim is only known to be at least the cap, so its residual is drawn
    # from the part of the fitted distribution above the cap.
    at_cap <- amount >= censored_at
    lower <- stats::pgamma(censored_at, shape = shape, rate = shape / mu[at_cap])
    u[at_cap] <- stats::runif(sum(at_cap), lower, 1)
  }
  list(r = stats::qnorm(clip(u)), shape = shape)
}

uncapped_fit <- fit_severity(claims, SEVERITY_TERMS)
uncapped <- gamma_residuals(uncapped_fit, claims$claim_amount)
rm(uncapped_fit); invisible(gc())

capped_claims <- cap_claims(claims)
capped_fit <- fit_pricing_severity(claims)
capped <- gamma_residuals(capped_fit, capped_claims$claim_amount, censored_at = LARGE_LOSS_CAP)
rm(capped_fit); invisible(gc())

severity_group <- factor(rep(c("uncapped amounts", "capped at the large-loss cap"), each = nrow(claims)),
                         levels = c("uncapped amounts", "capped at the large-loss cap"))
ggsave(file.path(figure_dir, "severity-quantile-residuals.png"),
       qq_plot(qq_frame(c(uncapped$r, capped$r), severity_group),
               "Severity model: randomised quantile residuals",
               sprintf("Gamma GLM, log link, severity terms. Claims at the cap of %s treated as censored.",
                       fmt(LARGE_LOSS_CAP, 0))),
       width = 8, height = 4.2, dpi = 120, device = "png", bg = "white")

severity_rows <- list(
  summary_row("uncapped amounts", uncapped$r),
  summary_row("capped at the large-loss cap", capped$r)
)

# Why the residuals are S-shaped: a large share of claims sit on a few exact amounts.
amount_counts <- sort(table(claims$claim_amount), decreasing = TRUE)
top_amounts <- head(amount_counts, 5)
top_four_share <- sum(head(amount_counts, 4)) / nrow(claims)
near_median_share <- mean(claims$claim_amount >= 1100 & claims$claim_amount <= 1250)
upper <- c(uncapped = stats::quantile(uncapped$r, 0.999, names = FALSE),
           capped = stats::quantile(capped$r, 0.999, names = FALSE))

# The prose below rests on these; stop rather than publish text the numbers contradict.
if (!(short_share_beyond > long_share_beyond)) {
  stop("short exposures no longer depart more from the line than long ones", call. = FALSE)
}
if (!all(upper > 5)) {
  stop("an upper residual tail is no longer far above normal; the severity section needs rewriting", call. = FALSE)
}
if (!(top_four_share > 0.3)) {
  stop("the four most common amounts no longer hold a large share of claims", call. = FALSE)
}

# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

header <- c("Residuals", "Rows", "Beyond ±1.96 (5% if right)", "0.1st percentile (-3.09)", "99.9th percentile (3.09)")

out <- c(
  "# Model diagnostics",
  "",
  "Generated by `R/model_diagnostics.R`. Do not edit by hand; re-run the script.",
  "",
  sprintf("Computed from `model.frequency_frame`, frame md5 `%s`, and `model.severity_frame`, %s claims. %s.",
          frame_md5, fmt(nrow(claims), 0), R.version.string),
  "",
  "Both models are checked with randomised quantile residuals. For a correctly",
  "specified model they are standard normal, so each panel should follow its dashed",
  "line, about 5% should fall beyond ±1.96, and the 0.1st and 99.9th percentiles should",
  "sit near -3.09 and 3.09. Residuals are drawn with a fixed seed, and the plots are",
  "clipped at ±5, so the most extreme severity residuals run off the top edge.",
  "",
  "## Frequency",
  "",
  "![Frequency quantile residuals](figures/frequency-quantile-residuals.png)",
  "",
  md_table(header, frequency_rows),
  "",
  sprintf(paste(
    "On exposures of 0.1 of a year or more, %s%% of residuals fall beyond ±1.96. Under",
    "0.1 of a year it is %s%%, and the upper tail lifts away from the line. That is",
    "the non-proportionality D2-2 measured, visible as a shape: short policy-years",
    "carry more claims than an offset allows, so their claims land further into the",
    "upper tail of the fitted Poisson distribution than chance would put them."),
    fmt(100 * long_share_beyond, 2), fmt(100 * short_share_beyond, 2)),
  "",
  "## Severity",
  "",
  "![Severity quantile residuals](figures/severity-quantile-residuals.png)",
  "",
  sprintf(paste(
    "Both panels use the pricing severity terms. In the right-hand panel claims are",
    "capped at %s and the %s claims at the cap are treated as censored."),
    fmt(LARGE_LOSS_CAP, 0), fmt(sum(claims$claim_amount >= LARGE_LOSS_CAP), 0)),
  "",
  md_table(header, severity_rows),
  "",
  sprintf(paste(
    "Gamma shape estimated by maximum likelihood: %s on uncapped amounts and %s capped.",
    "A Pearson estimate is not used because a handful of very large claims dominate it."),
    fmt(uncapped$shape, 3), fmt(capped$shape, 3)),
  "",
  sprintf(paste(
    "The Gamma assumption does not hold, capped or not, and capping hardly changes that.",
    "Both panels are S-shaped. The lower tail sits above the line, so small claims are",
    "less extreme than the fitted Gamma expects. The middle is flatter than the line,",
    "so claims bunch together more tightly than a Gamma distribution does. The upper",
    "tail climbs far above it: the 99.9th percentile of the residuals is %s uncapped",
    "and %s capped, against 3.09."),
    fmt(upper[["uncapped"]], 2), fmt(upper[["capped"]], 2)),
  "",
  "The bunching in the middle has a plain cause. A large share of claims are exactly",
  "the same amount:",
  "",
  md_table(c("Claim amount", "Claims", "Share of claims"),
           lapply(seq_along(top_amounts), function(i) c(
             fmt(as.numeric(names(top_amounts)[i]), 2), fmt(top_amounts[[i]], 0),
             paste0(fmt(100 * top_amounts[[i]] / nrow(claims), 1), "%")))),
  "",
  sprintf(paste(
    "The four most common amounts hold %s%% of all claims, and %s%% lie between 1,100",
    "and 1,250. Amounts repeated that exactly look like standard settlement figures",
    "rather than individually assessed losses; the data does not say where they come",
    "from. No continuous distribution fitted to the whole range can put that much mass",
    "on a handful of points, which is why the middle of both panels is flat."),
    fmt(100 * top_four_share, 1), fmt(100 * near_median_share, 1)),
  "",
  sprintf(paste(
    "Capped, the upper tail stays high for a different reason. The fitted Gamma, with a",
    "shape near 1, gives a claim at the cap a probability so small that a capped",
    "claim's residual lands around 6, while %s%% of claims actually reach the cap."),
    fmt(100 * mean(claims$claim_amount >= LARGE_LOSS_CAP), 2)),
  "",
  "What this means for the model. A Gamma GLM estimates the mean consistently when the",
  "mean is right, whatever the true distribution, and pure premium needs only the mean:",
  "capped, the mean model converges quickly and its fitted amounts balance to capped",
  "losses (docs/severity-large-losses.md). Nothing else should be taken from its",
  "distribution. Tail probabilities, simulated claims, or anything that prices a limit",
  "or an excess from the fitted Gamma would be wrong."
)

target <- file.path(find_repo_root(), "docs", "model-diagnostics.md")
connection <- file(target, open = "wb")
writeLines(out, connection, sep = "\n", useBytes = TRUE)
close(connection)
message("wrote ", target)
