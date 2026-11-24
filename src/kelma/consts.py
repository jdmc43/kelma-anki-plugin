"""Shared constants for the Kelma add-on."""

KELMA_CLIENT_VERSION = "1.0.136"
UPDATE_MANIFEST_URL = "https://kelma.tech/updates/v1.json"

# Cloud providers. Each Anki profile is assigned to exactly one.
KELMA = "kelma"
ANKIWEB = "ankiweb"

SERVICE_LABEL = {
    KELMA: "KelmaSync",
    ANKIWEB: "AnkiWeb",
}

# Canonical KelmaSync v2 REST hostname. The old ankiai.tech alias remains live
# for already-installed clients, but all new/default traffic uses kelma.tech.
DEFAULT_V2_URL = "https://sync2.kelma.tech"
LEGACY_V2_URLS = {"https://sync2.ankiai.tech"}
