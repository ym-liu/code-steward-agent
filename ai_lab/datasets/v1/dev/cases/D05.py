import pandas as pd
import sys

data = pd.read_csv(sys.argv[1])
minimum = float(sys.argv[3])
data.loc[data["amount"] >= minimum].to_csv(sys.argv[2], index=False)
