from datetime import datetime, timezone
import sys

value = datetime.fromisoformat(sys.argv[1])
print(value.astimezone(timezone.utc).isoformat())
