"""Train the small CNN on the cleaned image folders.

Usage: python train.py --config configs/base.yaml [--seed 1] [--epochs N]
"""
import argparse
import json
import random
from pathlib import Path

import numpy as np
import torch
import yaml
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

from utils.metrics import accuracy


class SmallCNN(nn.Module):
    def __init__(self, num_classes):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(3, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        )
        self.head = nn.Linear(32 * 16 * 16, num_classes)

    def forward(self, x):
        return self.head(self.features(x).flatten(1))


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/base.yaml")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--epochs", type=int, help="overrides the value in the config file")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    set_seed(args.seed)
    epochs = args.epochs or cfg["epochs"]
    tf = transforms.Compose([transforms.Resize((64, 64)), transforms.ToTensor()])
    train_set = datasets.ImageFolder(cfg["data_dir"] + "/train", transform=tf)
    val_set = datasets.ImageFolder(cfg["data_dir"] + "/val", transform=tf)
    train_loader = DataLoader(train_set, batch_size=cfg["batch_size"], shuffle=True)
    val_loader = DataLoader(val_set, batch_size=cfg["batch_size"])

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SmallCNN(len(train_set.classes)).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=cfg["lr"])
    loss_fn = nn.CrossEntropyLoss()

    history = []
    for epoch in range(epochs):
        model.train()
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = loss_fn(model(images), labels)
            loss.backward()
            optimizer.step()
        model.eval()
        with torch.no_grad():
            preds, truth = [], []
            for images, labels in val_loader:
                preds += model(images.to(device)).argmax(1).cpu().tolist()
                truth += labels.tolist()
        acc = accuracy(truth, preds)
        history.append({"epoch": epoch + 1, "val_acc": acc})
        print(f"epoch {epoch + 1}/{epochs}  val_acc={acc:.3f}")

    out = Path(cfg["checkpoint_dir"])
    out.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out / f"model_seed{args.seed}.pt")
    (out / f"history_seed{args.seed}.json").write_text(json.dumps(history, indent=2))


if __name__ == "__main__":
    main()
