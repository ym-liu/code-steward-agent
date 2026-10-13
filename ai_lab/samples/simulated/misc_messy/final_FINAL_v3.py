import csv, json, sys

# usage: python final_FINAL_v3.py in.csv out.json
rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8")))
for r in rows:
    for k, v in r.items():
        if v.replace(".", "", 1).isdigit():
            r[k] = float(v) if "." in v else int(v)
json.dump(rows, open(sys.argv[2], "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print(len(rows), "rows")
