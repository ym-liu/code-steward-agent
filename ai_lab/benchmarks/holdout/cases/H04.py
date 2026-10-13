import csv
import random
import sys

rng = random.Random(7)
writer = csv.writer(sys.stdout)
writer.writerow(["id", "value"])
for identifier in range(3):
    writer.writerow([identifier, rng.randint(1, 10)])
