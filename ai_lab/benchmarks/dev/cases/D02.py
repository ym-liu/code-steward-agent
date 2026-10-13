import json
import sys
from collections import Counter

counts = Counter()
for line in sys.stdin:
    if line.strip():
        counts[json.loads(line)["level"]] += 1
print(json.dumps(dict(counts), sort_keys=True))
