#!/bin/bash
# backup thesis folder to the external drive (2019 laptop)
SRC=/home/alice/Documents/thesis
DEST=/media/alice/WD_Passport/backups/thesis

if [ ! -d "$DEST" ]; then
    echo "drive not mounted"
    exit 1
fi

rsync -av --delete --exclude '*.aux' --exclude '*.log' "$SRC/" "$DEST/"
# rsync -av "$SRC/figures/" /media/alice/USB/figures/
date >> "$DEST/last_backup.txt"
