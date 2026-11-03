"""Checksum computation using the Rust kelma-hash binary.

This avoids cross-language JSON normalization bugs (Unicode escaping, HTML
escaping, key ordering) by computing checksums with the canonical Rust
implementation — the same one KelmaMobile and the server use. The binary is a
tiny compiled CLI that reads JSON from stdin and writes hex checksums to stdout.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

# The binaries are shipped in clients/rust/kelma-hash/bin/.
_BIN_DIR = Path(__file__).resolve().parent.parent.parent / "rust" / "kelma-hash" / "bin"
_BIN_DIR_ALT = Path(__file__).resolve().parent.parent / "bin"


def _binary_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        if machine in ("arm64", "aarch64"):
            return "kelma-hash-darwin-arm64"
        return "kelma-hash-darwin-amd64"
    if system == "linux":
        return "kelma-hash-linux-amd64"
    return "kelma-hash-darwin-amd64"


def _binary_path() -> Path | None:
    name = _binary_name()
    for d in (_BIN_DIR, _BIN_DIR_ALT):
        p = d / name
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None


def _run_hash(args: list[str], payload: bytes, timeout: int) -> str:
    binary = _binary_path()
    if binary is None:
        raise FileNotFoundError("kelma-hash binary not found")
    result = subprocess.run(
        [str(binary), *args],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"kelma-hash failed: {result.stderr.decode('utf-8', errors='replace')}"
        )
    return result.stdout.decode("utf-8")


def _checksum_parts_py(parts: list) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(
            json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        h.update(b"\n")
    return h.hexdigest()


def note_checksum(fields: list[str], tags: list[str]) -> str:
    """Compute the checksum for a note via the Rust binary."""
    try:
        payload = json.dumps({"fields": fields, "tags": tags}).encode("utf-8")
        return _run_hash([], payload, timeout=5).strip()
    except FileNotFoundError:
        return _checksum_parts_py([fields, tags])


def note_checksums_batch(items: list[tuple[list[str], list[str]]]) -> list[str]:
    """Compute checksums for many notes.

    Tries the Rust binary (one spawn for the whole collection); on any failure
    falls back to the pure-Python implementation, which is verified byte-
    identical to Rust. Pure Python avoids subprocess entirely.
    """
    if not items:
        return []
    if _binary_path() is not None:
        try:
            payload = json.dumps([{"fields": f, "tags": t} for f, t in items]).encode("utf-8")
            lines = _run_hash(["-batch"], payload, timeout=120).splitlines()
            if len(lines) == len(items):
                return lines
        except Exception:
            pass
    # Fallback: pure Python (byte-identical, no subprocess).
    return [_checksum_parts_py([f, t]) for f, t in items]


def notetype_checksum(name: str, definition: dict) -> str:
    """Checksum for a notetype. Pure-Python (verified byte-identical to Rust);
    avoids per-item subprocess spawns."""
    return _checksum_parts_py([name, definition])


def deck_checksum(config: dict) -> str:
    """Checksum for a deck config. Pure-Python (verified byte-identical to Rust)."""
    return _checksum_parts_py([config])


def review_checksum(
    note_guid: str,
    card_ord: int,
    ease: int,
    interval: int,
    last_interval: int,
    factor: int,
    taken_millis: int,
    review_kind: int,
) -> str:
    """Stable checksum for an immutable review-history row.

    The collection-local card id and current deck name are deliberately absent;
    another collection remaps the card id through ``(note_guid, card_ord)``.
    """
    return _checksum_parts_py([
        note_guid,
        int(card_ord),
        int(ease),
        int(interval),
        int(last_interval),
        int(factor),
        int(taken_millis),
        int(review_kind),
    ])


def card_checksums_batch(items: list[tuple[str, str, int]]) -> list[str]:
    """Hash card structural identities in one Rust invocation.

    The fallback remains pure Python and byte-identical. Batching avoids tens of
    thousands of Python JSON encoder/hash object setups on large collections.
    """
    if not items:
        return []
    if _binary_path() is not None:
        try:
            payload = json.dumps([
                {"parts": [guid, deck_name, int(ord_ or 0)]}
                for guid, deck_name, ord_ in items
            ]).encode("utf-8")
            lines = _run_hash(["-batch"], payload, timeout=120).splitlines()
            if len(lines) == len(items):
                return lines
        except Exception:
            pass
    return [_checksum_parts_py([guid, deck_name, int(ord_ or 0)]) for guid, deck_name, ord_ in items]


def card_checksum(note_guid: str, deck_name: str, ord_: int, scheduling: dict) -> str:
    """Structural checksum for a card: note, deck, and template ordinal only.

    Scheduling is intentionally excluded. The `due` field (and others) is
    collection-relative — it is measured in days since each collection's
    creation date, so two collections with different creation dates always have
    different scheduling even for identical review state. Including scheduling
    here produced thousands of false conflicts. Scheduling propagates separately
    via newest-wins on the server.
    """
    return _checksum_parts_py([note_guid, deck_name, int(ord_ or 0)])
