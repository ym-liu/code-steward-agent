from collections import Counter
import sys

from mylib import parse_log  # TODO: where did mylib go after the laptop reset?

counts = Counter()
with open(sys.argv[1]) as fh:
    for line in fh:
        entry = parse_log(line)
        if entry and entry.status >= 500:
            counts[entry.path] += 1

for path, n in counts.most_common(10):
    print(f"{n:6}  {path}")
