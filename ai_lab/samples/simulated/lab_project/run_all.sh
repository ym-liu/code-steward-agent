#!/usr/bin/env bash
# Train 3 seeds then evaluate everything. Run from the project folder.
set -e
CONFIG=${1:-configs/base.yaml}
for seed in 1 2 3; do
  python train.py --config "$CONFIG" --seed "$seed"
done
python evaluate.py checkpoints/model_seed*.pt --config "$CONFIG" --out results/metrics.json
python plot_results.py results/metrics.json
