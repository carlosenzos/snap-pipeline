from __future__ import annotations

import base64
import hashlib
import hmac

from config.settings import get_settings


def verify_trello_signature(
    body: bytes,
    signature: str,
    callback_url: str,
) -> bool:
    """Verify Trello webhook HMAC-SHA1 signature.

    Trello signs webhooks with HMAC-SHA1 using:
    - key = your API secret
    - message = request body + callback URL
    """
    s = get_settings()
    secret = s.trello_webhook_secret.encode()
    content = body + callback_url.encode()

    digest = hmac.new(secret, content, hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()

    return hmac.compare_digest(expected, signature)
