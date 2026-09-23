import ipaddress
import sys

for line in sys.stdin:
    try:
        address = ipaddress.ip_address(line.strip())
    except ValueError:
        continue
    if address.version == 4:
        print(address)
