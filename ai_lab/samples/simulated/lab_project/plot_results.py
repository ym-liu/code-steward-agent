import json
import sys

import matplotlib.pyplot as plt

metrics_file = sys.argv[1] if len(sys.argv) > 1 else "results/metrics.json"
results = json.load(open(metrics_file))

names = list(results)
scores = [results[n]["accuracy"] for n in names]
plt.figure(figsize=(6, 4))
plt.bar(names, scores, color="steelblue")
plt.ylabel("test accuracy")
plt.ylim(0, 1)
plt.xticks(rotation=30, ha="right")
plt.tight_layout()
plt.savefig("figures/accuracy_by_seed.png", dpi=150)
print("saved figures/accuracy_by_seed.png")
