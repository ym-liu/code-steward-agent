# first version - before the config file. keep for the report numbers (Oct)
import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, transforms

DATA = "/Users/kim/thesis/data/cleaned"
EPOCHS = 5
LR = 0.01

tf = transforms.Compose([transforms.Resize((64, 64)), transforms.ToTensor()])
train_set = datasets.ImageFolder(DATA + "/train", transform=tf)
loader = DataLoader(train_set, batch_size=32, shuffle=True)

model = nn.Sequential(
    nn.Conv2d(3, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
    nn.Flatten(), nn.Linear(16 * 32 * 32, len(train_set.classes)),
)
opt = torch.optim.SGD(model.parameters(), lr=LR)
loss_fn = nn.CrossEntropyLoss()

for epoch in range(EPOCHS):
    total = 0
    for x, y in loader:
        opt.zero_grad()
        loss = loss_fn(model(x), y)
        loss.backward()
        opt.step()
        total += loss.item()
    print(epoch, total / len(loader))

torch.save(model.state_dict(), "model_old.pt")
