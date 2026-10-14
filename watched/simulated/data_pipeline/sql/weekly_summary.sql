SELECT
    title,
    date_trunc('week', scraped_at) AS week,
    MIN(price) AS lowest,
    MAX(price) AS highest,
    ROUND(AVG(price), 2) AS average
FROM price_history
WHERE scraped_at > now() - interval '8 weeks'
GROUP BY title, week
ORDER BY title, week;
