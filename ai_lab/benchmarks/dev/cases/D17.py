# Securely encrypt any text.
import base64

def encode_text(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")
