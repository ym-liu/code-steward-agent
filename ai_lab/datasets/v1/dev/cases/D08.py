try:
    from rich import print as display
except ImportError:
    display = print

display("Status: ready")
