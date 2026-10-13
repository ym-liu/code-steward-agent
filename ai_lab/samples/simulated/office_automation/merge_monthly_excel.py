"""Merge every branch's monthly sales sheet into one workbook.

Each branch sends sales_<branch>.xlsx with the same columns.
"""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

DEFAULT_FOLDER = r"C:\Users\amy.chen\Documents\Reports\incoming"


def merge_folder(folder):
    frames = []
    for path in sorted(Path(folder).glob("sales_*.xlsx")):
        frame = pd.read_excel(path, sheet_name="Sales")
        frame["Branch"] = path.stem.replace("sales_", "")
        frames.append(frame)
    if not frames:
        raise SystemExit(f"No sales_*.xlsx files in {folder}")
    merged = pd.concat(frames, ignore_index=True)
    merged["Total"] = merged["Quantity"] * merged["UnitPrice"]
    return merged


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER
    merged = merge_folder(folder)
    target = Path(folder).parent / f"merged_{date.today():%Y%m}.xlsx"
    with pd.ExcelWriter(target) as writer:
        merged.to_excel(writer, sheet_name="All", index=False)
        merged.groupby("Branch")["Total"].sum().to_excel(writer, sheet_name="ByBranch")
    print(f"Wrote {target} ({len(merged)} rows)")
