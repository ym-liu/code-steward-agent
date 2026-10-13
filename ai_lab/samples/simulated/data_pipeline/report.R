# Weekly price report -> report/price_trends.png and report/summary.csv
library(readr)
library(dplyr)
library(ggplot2)

args <- commandArgs(trailingOnly = TRUE)
input <- if (length(args) > 0) args[1] else "exports/weekly_summary.csv"

prices <- read_csv(input, show_col_types = FALSE)

summary_tbl <- prices %>%
  group_by(title) %>%
  summarise(lowest = min(lowest), highest = max(highest), last_avg = last(average))

dir.create("report", showWarnings = FALSE)
write_csv(summary_tbl, "report/summary.csv")

p <- ggplot(prices, aes(x = week, y = average, colour = title)) +
  geom_line() +
  labs(title = "Average weekly price", x = NULL, y = "Price ($)")
ggsave("report/price_trends.png", p, width = 8, height = 4)
