#!/bin/bash
# nightly via cron:  15 2 * * * /opt/price-tracker/run_pipeline.sh >> /var/log/price-tracker.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
DAY=$(date +%F)
mkdir -p raw exports

node scrape_prices.js urls.txt > "raw/prices-$DAY.jsonl"
python3 prices_to_csv.py < "raw/prices-$DAY.jsonl" > "exports/prices-$DAY.csv"
psql -d prices -v csv_path="$PWD/exports/prices-$DAY.csv" -f sql/load_prices.sql
psql -d prices --csv -f sql/weekly_summary.sql > exports/weekly_summary.csv
Rscript report.R exports/weekly_summary.csv
echo "pipeline finished $DAY"
