import csv
import json
import sys

fields = ["url", "title", "price", "scrapedAt"]
writer = csv.DictWriter(sys.stdout, fieldnames=fields)
writer.writeheader()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if row.get("price"):
        writer.writerow({k: row.get(k, "") for k in fields})
