"""KelmaSync v2 HTTP client.

This module is intentionally standalone: it only knows how to talk to the v2
REST API. It does not touch Anki collections or Qt. Higher-level sync code will
build manifests from the local collection and call this client.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


class V2Error(RuntimeError):
    """Base error raised by the v2 client."""

    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class V2Conflict(V2Error):
    """Raised for HTTP 409 conflict responses."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(409, "conflict", payload)
        self.server = payload.get("server")
        self.client = payload.get("client")


@dataclass(frozen=True)
class V2Auth:
    token: str
    client_id: str


class V2Client:
    def __init__(self, endpoint: str, token: str = "", timeout: int = 30) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------------ auth --

    def register(self, username: str, password: str) -> dict[str, Any]:
        return self._json("POST", "/v2/auth/register", {
            "username": username,
            "password": password,
        }, auth=False)

    def login(self, username: str, password: str, client_label: str) -> V2Auth:
        data = self._json("POST", "/v2/auth/login", {
            "username": username,
            "password": password,
            "client_label": client_label,
        }, auth=False)
        auth = V2Auth(token=data["token"], client_id=data["client_id"])
        self.token = auth.token
        return auth

    def logout(self) -> None:
        self._json("POST", "/v2/auth/logout", None)
        self.token = ""

    # -------------------------------------------------------------- manifest --

    def manifest(self, since: Optional[str] = None) -> dict[str, Any]:
        path = "/v2/sync/manifest"
        if since:
            path += "?" + urllib.parse.urlencode({"since": since})
        return self._json("GET", path, None)

    def usage(self) -> dict[str, int]:
        """Return exact account-wide bytes currently stored by KelmaSync."""
        data = self._json("GET", "/v2/usage", None)
        return {
            "used_bytes": int(data.get("used_bytes", 0) or 0),
            "media_bytes": int(data.get("media_bytes", 0) or 0),
            "content_bytes": int(data.get("content_bytes", 0) or 0),
        }

    # ---------------------------------------------------------------- notes --

    def get_note(self, guid: str) -> dict[str, Any]:
        return self._json("GET", f"/v2/notes/{_quote(guid)}", None)

    def put_note(
        self,
        guid: str,
        *,
        notetype_id: int,
        fields: list[str],
        tags: list[str],
        client_modified_at: str,
        base_checksum: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        return self._json("PUT", f"/v2/notes/{_quote(guid)}", {
            "notetype_id": notetype_id,
            "fields": fields,
            "tags": tags,
            "client_modified_at": client_modified_at,
            "base_checksum": base_checksum,
        }, force=force)

    def delete_note(self, guid: str) -> None:
        self._request("DELETE", f"/v2/notes/{_quote(guid)}", None)

    # ---------------------------------------------------------------- cards --

    def get_card(self, card_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v2/cards/{card_id}", None)

    def put_card(
        self,
        card_id: int,
        *,
        note_guid: str,
        deck_name: str,
        ord: int,
        scheduling: dict[str, Any],
        client_modified_at: str,
        study_state: str | None = None,
        study_state_client_modified_at: str | None = None,
        schedule_reset_through_review_id: int | None = None,
        schedule_reset_client_modified_at: str | None = None,
        due_date_override_ms: int | None = None,
        due_date_override_client_modified_at: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "note_guid": note_guid,
            "deck_name": deck_name,
            "ord": ord,
            "scheduling": scheduling,
            "client_modified_at": client_modified_at,
        }
        if study_state is not None:
            body["study_state"] = study_state
            body["study_state_client_modified_at"] = (
                study_state_client_modified_at or client_modified_at
            )
        if schedule_reset_through_review_id is not None:
            body["schedule_reset_through_review_id"] = schedule_reset_through_review_id
            body["schedule_reset_client_modified_at"] = (
                schedule_reset_client_modified_at or client_modified_at
            )
        if due_date_override_ms is not None:
            body["due_date_override_ms"] = due_date_override_ms
            body["due_date_override_client_modified_at"] = (
                due_date_override_client_modified_at or client_modified_at
            )
        return self._json("PUT", f"/v2/cards/{card_id}", body)

    def delete_card(self, card_id: int) -> None:
        self._request("DELETE", f"/v2/cards/{card_id}", None)

    # ------------------------------------------------------------- notetypes --

    def get_notetype(self, notetype_id: int) -> dict[str, Any]:
        return self._json("GET", f"/v2/notetypes/{notetype_id}", None)

    def put_notetype(
        self,
        notetype_id: int,
        *,
        name: str,
        definition: dict[str, Any],
        client_modified_at: str,
        base_checksum: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        return self._json("PUT", f"/v2/notetypes/{notetype_id}", {
            "name": name,
            "definition": definition,
            "client_modified_at": client_modified_at,
            "base_checksum": base_checksum,
        }, force=force)

    def delete_notetype(self, notetype_id: int) -> None:
        self._request("DELETE", f"/v2/notetypes/{notetype_id}", None)

    # ---------------------------------------------------------------- decks --

    def get_deck(self, name: str) -> dict[str, Any]:
        return self._json("GET", f"/v2/decks/{_quote(name)}", None)

    def put_deck(
        self,
        name: str,
        *,
        config: dict[str, Any],
        client_modified_at: str,
        base_checksum: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        return self._json("PUT", f"/v2/decks/{_quote(name)}", {
            "config": config,
            "client_modified_at": client_modified_at,
            "base_checksum": base_checksum,
        }, force=force)

    def delete_deck(self, name: str) -> None:
        self._request("DELETE", f"/v2/decks/{_quote(name)}", None)

    # ---------------------------------------------------------------- media --

    def has_media(self, filename: str) -> bool:
        try:
            self._request("HEAD", f"/v2/media/{_quote(filename)}", None)
            return True
        except V2Error as err:
            if err.status == 404:
                return False
            raise

    def get_media(self, filename: str) -> bytes:
        return self._request("GET", f"/v2/media/{_quote(filename)}", None)

    def put_media(self, filename: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
        return self._json("PUT", f"/v2/media/{_quote(filename)}", data, content_type=content_type)

    def delete_media(self, filename: str) -> None:
        self._request("DELETE", f"/v2/media/{_quote(filename)}", None)

    def prepare_media_tar(self, filenames: list[str]) -> dict[str, Any]:
        return self._json(
            "POST",
            "/v2/media/prepare-tar",
            {"filenames": filenames},
            timeout=30 * 60,
        )

    def open_prepared_media_tar(self, job_id: str):
        return self._open_stream(
            "GET",
            f"/v2/media/prepared-tar/{_quote(job_id)}",
            timeout=30 * 60,
        )

    def delete_prepared_media_tar(self, job_id: str) -> None:
        try:
            self._request("DELETE", f"/v2/media/prepared-tar/{_quote(job_id)}", None)
        except V2Error as err:
            if err.status != 404:
                raise

    def open_media_stream(self, filename: str):
        return self._open_stream(
            "GET",
            f"/v2/media/{_quote(filename)}",
            timeout=120,
        )

    # ---------------------------------------------------------------- batch --

    def batch_pull(
        self,
        *,
        notes: list[str] | None = None,
        cards: list[int] | None = None,
        reviews: list[int] | None = None,
        notetypes: list[int] | None = None,
        decks: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "notes": notes or [],
            "cards": cards or [],
            "notetypes": notetypes or [],
            "decks": decks or [],
        }
        # Only upgraded servers accept this field. Review sync calls with an
        # explicit list after seeing the manifest capability marker; ordinary
        # content pulls retain the legacy four-field request shape.
        if reviews is not None:
            payload["reviews"] = reviews
        return self._json("POST", "/v2/batch/pull", payload)

    def batch_push(self, payload: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        return self._json("POST", "/v2/batch/push", payload, force=force)

    def batch_delete(
        self,
        *,
        notes: list[str] | None = None,
        cards: list[int] | None = None,
        notetypes: list[int] | None = None,
        decks: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._json("POST", "/v2/batch/delete", {
            "notes": notes or [],
            "cards": cards or [],
            "notetypes": notetypes or [],
            "decks": decks or [],
        })

    # -------------------------------------------------------------- internals --

    def _json(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        auth: bool = True,
        force: bool = False,
        content_type: str = "application/json",
        timeout: int | None = None,
    ) -> Any:
        raw = self._request(
            method,
            path,
            body,
            auth=auth,
            force=force,
            content_type=content_type,
            timeout=timeout,
        )
        if raw == b"" or raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as err:
            raise V2Error(0, f"invalid JSON response: {err}") from err

    def _request(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        auth: bool = True,
        force: bool = False,
        content_type: str = "application/json",
        timeout: int | None = None,
    ) -> bytes:
        url = self.endpoint + path
        data: bytes | None
        if body is None:
            data = None
        elif isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")

        # Full manifests are several megabytes uncompressed. Request gzip so a
        # transient tunnel stream cannot leave Desktop waiting on a large JSON
        # response after the server has already completed the request.
        headers = {
            "User-Agent": "KelmaSync-v2 Python client",
            "Accept-Encoding": "gzip",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        if auth:
            if not self.token:
                raise V2Error(401, "missing token")
            headers["Authorization"] = f"Bearer {self.token}"
        if force:
            headers["Force-Override"] = "true"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    return gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as err:
            payload: Any = None
            raw = err.read()
            if raw and err.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = raw.decode("utf-8", errors="replace")
            if err.code == 409 and isinstance(payload, dict):
                raise V2Conflict(payload) from err
            message = payload.get("message") if isinstance(payload, dict) else str(payload or err)
            raise V2Error(err.code, message, payload) from err
        except urllib.error.URLError as err:
            raise V2Error(0, "connection failed") from err

    def _open_stream(self, method: str, path: str, *, timeout: int):
        headers = {
            "User-Agent": "KelmaSync-v2 Python client",
            "Accept-Encoding": "identity",
        }
        if not self.token:
            raise V2Error(401, "missing token")
        headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.endpoint + path,
            headers=headers,
            method=method,
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as err:
            # Never include response URLs or storage credentials in generic
            # media errors. The status is sufficient for retry policy.
            try:
                err.read(64 * 1024)
            finally:
                err.close()
            raise V2Error(err.code, "media request failed") from err
        except urllib.error.URLError as err:
            raise V2Error(0, "media connection failed") from err


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
