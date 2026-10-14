import argparse
import json
from pathlib import Path

import torch
import yaml
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

from train import SmallCNN
from utils.metrics import accuracy, per_class_accuracy


def evaluate(checkpoint, cfg):
    tf = transforms.Compose([transforms.Resize((64, 64)), transforms.ToTensor()])
    test_set = datasets.ImageFolder(cfg["data_dir"] + "/test", transform=tf)
    model = SmallCNN(len(test_set.classes))
    model.load_state_dict(torch.load(checkpoint, map_location="cpu"))
    model.eval()
    preds, truth = [], []
    with torch.no_grad():
        for images, labels in DataLoader(test_set, batch_size=64):
            preds += model(images).argmax(1).tolist()
            truth += labels.tolist()
    return {"accuracy": accuracy(truth, preds),
            "per_class": per_class_accuracy(truth, preds, test_set.classes)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Evaluate saved checkpoints on the test split")
    p.add_argument("checkpoints", nargs="+")
    p.add_argument("--config", default="configs/base.yaml")
    p.add_argument("--out", default="results/metrics.json")
    a = p.parse_args()
    cfg = yaml.safe_load(open(a.config))
    results = {Path(c).name: evaluate(c, cfg) for c in a.checkpoints}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=2))
    for name, r in results.items():
        print(f"{name}: {r['accuracy']:.3f}")
