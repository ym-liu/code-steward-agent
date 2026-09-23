import csv
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    rows = json.load(source)
with open(sys.argv[2], "w", newline="", encoding="utf-8") as target:
    writer = csv.DictWriter(target, fieldnames=["name", "score"])
    writer.writeheader()
    writer.writerows(rows)
