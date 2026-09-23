import argparse
import csv

parser = argparse.ArgumentParser(description="Keep rows matching a date")
parser.add_argument("input_csv")
parser.add_argument("output_csv")
parser.add_argument("--date", required=True)
args = parser.parse_args()

with open(args.input_csv, newline="", encoding="utf-8") as source:
    reader = csv.DictReader(source)
    columns = reader.fieldnames
    selected = [row for row in reader if row["date"] == args.date]

with open(args.output_csv, "w", newline="", encoding="utf-8") as target:
    writer = csv.DictWriter(target, fieldnames=columns)
    writer.writeheader()
    writer.writerows(selected)
