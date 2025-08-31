import hmac
import hashlib
import base64
import os

# Example headers from the webhook
webhook_id = "msg_3212fYzJ6MpsPKmaSJMvDQI4ELn"
webhook_timestamp = "1756640650"
received_signature = "U1apls0iFao1t80ofxFbgnwxHvgk7MdnjJiFJRKyo6c="  # from webhook-signature header

# Your secret must be loaded from environment
secret = os.getenv("DODO_WEBHOOK_SECRET")
if not secret:
    raise RuntimeError("DODO_WEBHOOK_SECRET is not set!")

# Example raw body from webhook (bytes)
raw_body = b'{"type":"payment.succeeded","data":{"amount_cents":1000,"currency":"usd"}}'

# Build message: id + '.' + timestamp + '.' + raw body bytes
message = webhook_id.encode("utf-8") + b"." + webhook_timestamp.encode("utf-8") + b"." + raw_body

# Compute HMAC-SHA256
digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
expected_sig = base64.b64encode(digest).decode().strip()

print("Expected signature:", expected_sig)
print("Matches webhook?", hmac.compare_digest(expected_sig, received_signature))
