"""User- and purpose-bound HMAC cursors. Positions contain only public metadata.

Local list callers use (created_at, id) keyset predicates with matching ORDER BY;
the codec also supports upstream page positions without imposing a DB schema.
"""

import base64
import binascii
import hashlib
import hmac
import json
from uuid import UUID

from pydantic import BaseModel

from devflow.errors import AppError


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def _invalid_cursor() -> AppError:
    return AppError("invalid_cursor", 400, "Invalid cursor")


class CursorCodec:
    """Bind an explicit APP_SIGNING_KEY (at least 32 bytes) to this instance."""

    def __init__(self, signing_key: bytes):
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("Cursor signing key must contain at least 32 bytes")
        self._key = signing_key

    def encode_cursor(
        self, actor_id: UUID, purpose: str, position: dict, limit: int, *, max_limit=100
    ) -> str:
        if (
            type(limit) is not int
            or type(max_limit) is not int
            or not 1 <= max_limit <= 200
            or not 1 <= limit <= max_limit
            or not purpose
            or not isinstance(position, dict)
        ):
            raise _invalid_cursor()
        payload = json.dumps(
            {
                "v": 1,
                "actor": str(actor_id),
                "purpose": purpose,
                "limit": limit,
                "position": position,
            },
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
        signature = hmac.digest(
            self._key, b"devflow:cursor:v1\0" + payload, hashlib.sha256
        )
        token = (
            base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")
        )
        if len(token) > 8192:
            raise _invalid_cursor()
        return token

    def decode_cursor(
        self, token: str, actor_id: UUID, purpose: str, limit: int, *, max_limit=100
    ) -> dict:
        try:
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 8192
                or type(limit) is not int
                or type(max_limit) is not int
                or not 1 <= max_limit <= 200
                or not 1 <= limit <= max_limit
            ):
                raise ValueError
            raw = base64.b64decode(
                token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
            )
            # Refuse noncanonical encodings as well as invalid signatures.
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
                raise ValueError
            payload, signature = raw[:-32], raw[-32:]
            expected = hmac.digest(
                self._key, b"devflow:cursor:v1\0" + payload, hashlib.sha256
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            data = json.loads(payload)
            if (
                data["v"] != 1
                or data["actor"] != str(actor_id)
                or data["purpose"] != purpose
                or data["limit"] != limit
                or not isinstance(data["position"], dict)
            ):
                raise ValueError
            return data["position"]
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
            raise _invalid_cursor() from None
