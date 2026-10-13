"""Rename photos to their EXIF date, e.g. IMG_1234.jpg -> 2024-07-14_153012.jpg"""
import argparse
from datetime import datetime
from pathlib import Path

from PIL import Image

DATE_TAG = 36867  # DateTimeOriginal


def taken_at(path):
    with Image.open(path) as img:
        raw = img.getexif().get(DATE_TAG)
    return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S") if raw else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recursive", action="store_true", help="also look in subfolders")
    args = ap.parse_args()
    pattern = "**/*.jpg" if args.recursive else "*.jpg"
    for photo in sorted(Path(args.folder).glob(pattern)):
        when = taken_at(photo)
        if when is None:
            print(f"skip {photo.name}: no date")
            continue
        new = photo.with_name(when.strftime("%Y-%m-%d_%H%M%S") + photo.suffix.lower())
        print(f"{photo.name} -> {new.name}")
        if not args.dry_run:
            photo.rename(new)


if __name__ == "__main__":
    main()
