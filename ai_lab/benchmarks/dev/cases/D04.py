import re
import sys

pattern = re.compile(r"^ERROR\s+(.+)$")
for line in sys.stdin:
    match = pattern.match(line.rstrip("\n"))
    if match:
        print(match.group(1))
