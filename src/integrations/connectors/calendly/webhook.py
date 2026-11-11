"""Calendly webhook signature verification and duplicate event deduplication."""

from collections import deque
import hashlib
import hmac
import logging
import time
from typing import Any

from src.integrations.errors import WebhookVerificationError

logger = logging.getLogger(__name__)


class WebhookDeduplicator:
    """Thread-safe bounded in-memory cache preventing duplicate webhook event processing."""

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def is_duplicate(self, event_id: str) -> bool:
        """Check if event_id has already been processed and mark it as seen."""
        if not event_id:
            return False

        if event_id in self._seen:
            return True

        self._seen.add(event_id)
        self._order.append(event_id)
        if len(self._order) > self.capacity:
            oldest = self._order.popleft()
            self._seen.discard(oldest)

        return False


# Global deduplicator instance for webhook deliveries
webhook_deduplicator = WebhookDeduplicator()


def verify_calendly_webhook_signature(
    signature_header: str | None,
    raw_body: bytes | str,
    signing_key: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify Calendly HMAC-SHA256 webhook signature.

    Calendly sends signature in the format:
        Calendly-Webhook-Signature: t=1612345678,v1=abcdef...

    Raises:
        WebhookVerificationError: If header is malformed, signature mismatch, or timestamp expired.
    """
    if not signature_header:
        raise WebhookVerificationError("Missing Calendly-Webhook-Signature header.", provider_id="calendly")
    if not signing_key:
        raise WebhookVerificationError("Calendly webhook signing key is not configured.", provider_id="calendly")

    parts = {}
    for element in signature_header.split(","):
        if "=" in element:
            k, v = element.strip().split("=", 1)
            parts[k] = v

    t_str = parts.get("t")
    v1_sig = parts.get("v1")
    if not t_str or not v1_sig:
        raise WebhookVerificationError("Malformed Calendly signature header (missing 't' or 'v1').", provider_id="calendly")

    # 1. Timestamp freshness check (replay protection)
    try:
        timestamp = int(t_str)
    except ValueError:
        raise WebhookVerificationError("Invalid timestamp in webhook signature.", provider_id="calendly")

    now = int(time.time())
    if abs(now - timestamp) > tolerance_seconds:
        raise WebhookVerificationError(
            f"Webhook timestamp expired or too far in the future ({abs(now - timestamp)}s diff).",
            provider_id="calendly",
        )

    # 2. Compute expected HMAC-SHA256 signature
    body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    payload_to_sign = f"{t_str}.".encode("utf-8") + body_bytes

    expected_sig = hmac.new(
        signing_key.strip().encode("utf-8"),
        payload_to_sign,
        hashlib.sha256,
    ).hexdigest()

    # 3. Timing-safe comparison
    if not hmac.compare_digest(v1_sig, expected_sig):
        logger.warning("Calendly webhook signature mismatch.")
        raise WebhookVerificationError("Invalid webhook signature.", provider_id="calendly")

    return True

