"""Helpers for the App-image data-URL ↔ bytes round-trip.

The API exposes ``apps.image`` as a single ``data:<mime>;base64,<...>``
string. Bytes are stored in ``apps.image`` and the mime in
``apps.image_mime``.

* ``parse_image_data_url`` decodes an incoming data-URL into
  ``(bytes, mime)``, validates size and mime, and raises an
  ``HTTPException`` on failure.
* ``build_image_data_url`` does the reverse for the response payload.
"""

from __future__ import annotations

import base64
import re

from fastapi import HTTPException, status

# 2 MiB on the decoded byte payload; enforced on the decoded length so
# the limit is independent of base64 encoding overhead.
MAX_IMAGE_BYTES = 2 * 1024 * 1024

# Permissive on the mime side — anything an HTML5 ``<img>`` can render
# is accepted. Only the obviously-not-an-image case is rejected.
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<payload>[A-Za-z0-9+/=\s]+)$"
)


def parse_image_data_url(data_url: str | None) -> tuple[bytes | None, str | None]:
    """Decode a data-URL into ``(bytes, mime)``.

    Returns ``(None, None)`` for ``None`` or empty string — the empty
    string is a useful sentinel from the update endpoint meaning
    "clear the image". Otherwise raises 422 if the input doesn't
    parse, or 413 if the decoded payload is larger than ``MAX_IMAGE_BYTES``.
    """
    if data_url is None or data_url == "":
        return None, None
    match = _DATA_URL_RE.match(data_url)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_image_format",
                "message": (
                    "image must be a data-URL like "
                    "'data:image/png;base64,<...>'"
                ),
            },
        )
    mime = match.group("mime").lower()
    try:
        payload = base64.b64decode(match.group("payload"), validate=True)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_base64", "message": "image payload is not valid base64"},
        )
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "reason": "image_too_large",
                "max_bytes": MAX_IMAGE_BYTES,
                "actual_bytes": len(payload),
            },
        )
    return payload, mime


def build_image_data_url(image_bytes: bytes | None, image_mime: str | None) -> str | None:
    """Build a data-URL for the API response.

    Returns ``None`` if either side is missing. The mime is trusted
    from the DB (validated at write time); it is not re-validated here.
    """
    if not image_bytes or not image_mime:
        return None
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{image_mime};base64,{encoded}"
