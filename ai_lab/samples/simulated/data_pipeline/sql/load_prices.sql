-- Run with: psql -d prices -v csv_path=/data/prices.csv -f sql/load_prices.sql
CREATE TABLE IF NOT EXISTS price_history (
    url         TEXT NOT NULL,
    title       TEXT,
    price       NUMERIC(10, 2) NOT NULL,
    scraped_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (url, scraped_at)
);

CREATE TEMP TABLE staging (LIKE price_history);
\copy staging FROM :'csv_path' WITH (FORMAT csv, HEADER true)

INSERT INTO price_history
SELECT * FROM staging
ON CONFLICT DO NOTHING;
