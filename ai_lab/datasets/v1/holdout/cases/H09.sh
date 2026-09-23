#!/bin/sh
for file in "$1"/*.log; do
    [ -f "$file" ] || continue
    printf '%s\n' "$file"
done
