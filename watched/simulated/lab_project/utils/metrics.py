"""Small metric helpers shared by train.py and evaluate.py."""
from collections import defaultdict


def accuracy(truth, preds):
    if not truth:
        return 0.0
    return sum(t == p for t, p in zip(truth, preds)) / len(truth)


def per_class_accuracy(truth, preds, class_names):
    hits, counts = defaultdict(int), defaultdict(int)
    for t, p in zip(truth, preds):
        counts[t] += 1
        hits[t] += t == p
    return {class_names[c]: hits[c] / counts[c] for c in counts}
