import statistics
import sys

values = [float(value) for value in sys.argv[1:]]
print(statistics.median(values))
