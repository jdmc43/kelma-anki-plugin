"""UTC timestamp policy for unambiguous newest-wins reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

Winner = Literal["local", "server"]

# Anki stores source modification times as integer Unix seconds. Treat
# sub-second differences as equal instead of repeatedly pulling a timestamp
# that the local collection cannot represent exactly.
_TIMESTAMP_RESOLUTION_SECONDS = 1.0
# The authenticated KelmaSync manifest supplies the UTC reference clock. A
# source timestamp farther in the future is clock-skewed and cannot be allowed
# to overwrite a valid upstream copy.
_MAX_FUTURE_SKEW_SECONDS = 300.0


def _timestamp(value: Any) -> float:
    if value in (None, "") or isinstance(value, bool):
        return 0.0
    try:
        if isinstance(value, (int, float)):
            parsed = float(value)
        else:
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            # Legacy offset-less values are UTC, never host-local time.
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            parsed = stamp.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if parsed > 0 else 0.0


def modified_timestamp(item: dict[str, Any] | None) -> float:
    """Return an item's UTC source modification epoch, or 0 when unknown.

    Server records prefer ``client_modified_at`` because ``modified_at`` is the
    time the server accepted the write. Older manifests do not expose the
    client timestamp, so the server timestamp remains a compatibility fallback.
    Local manifests only contain ``modified_at``.
    """
    if not item:
        return 0.0
    for key in ("client_modified_at", "modified_at"):
        parsed = _timestamp(item.get(key))
        if parsed > 0:
            return parsed
    return 0.0


def server_wins_anki_timestamp_truncation(
    local: dict[str, Any] | None,
    server: dict[str, Any] | None,
) -> bool:
    """Whether a server subsecond was truncated by Anki's integer mtime.

    A freshly applied server row can only retain whole Unix seconds locally.
    Treat the matching server fraction in that same second as the same source,
    not as a new concurrent edit that requires another user decision.
    """
    local_time = modified_timestamp(local)
    server_time = modified_timestamp(server)
    return (
        local_time > 0
        and server_time > local_time
        and local_time.is_integer()
        and int(server_time) == int(local_time)
    )


def newest_side(
    local: dict[str, Any] | None,
    server: dict[str, Any] | None,
    *,
    utc_now: Any = None,
) -> Winner | None:
    """Return the uniquely newer side, using KelmaSync's UTC clock.

    Ties, unknown times, and sub-second differences remain ambiguous. If the
    local clock is implausibly ahead of KelmaSync, the valid upstream copy wins
    instead of allowing a future timestamp to poison newest-wins forever. A
    future-skewed server source timestamp falls back to the server's trusted
    receipt timestamp.
    """
    local_time = modified_timestamp(local)
    server_time = modified_timestamp(server)
    reference_time = _timestamp(utc_now)

    local_is_future = False
    if reference_time > 0:
        ceiling = reference_time + _MAX_FUTURE_SKEW_SECONDS
        if local_time > ceiling:
            local_time = 0.0
            local_is_future = True
        if server_time > ceiling:
            receipt_time = _timestamp((server or {}).get("modified_at"))
            server_time = receipt_time if 0 < receipt_time <= ceiling else 0.0

    if local_is_future and server_time > 0:
        return "server"
    if local_time <= 0 or server_time <= 0:
        return None
    if abs(local_time - server_time) < _TIMESTAMP_RESOLUTION_SECONDS:
        return None
    return "local" if local_time > server_time else "server"
