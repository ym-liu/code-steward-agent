import hashlib
import sys
from pathlib import Path

data = Path(sys.argv[1]).read_bytes()
print(hashlib.sha256(data).hexdigest())
