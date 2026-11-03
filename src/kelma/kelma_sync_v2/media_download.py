"""Bounded-memory KelmaSync media downloads matching the Kotlin protocol."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import tarfile
import tempfile
import threading
import time
from typing import Callable, Iterable

from .client import V2Client, V2Error

MEDIA_WORKERS = 64
MAX_TAR_PREPARATIONS = 5
MAX_TAR_FILES = 10_000
MAX_TAR_BYTES = 128 * 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_ATTEMPTS = 4
MIN_TAR_FILES = 1_000
MIN_TAR_FILES_BY_BYTES = 128
MIN_TAR_BYTES = 128 * 1024 * 1024
_CHUNK = 256 * 1024


class TarUnsupported(Exception):
    pass


@dataclass(frozen=True)
class MediaEntry:
    filename: str
    modified_at: str
    size_bytes: int


@dataclass(frozen=True)
class MediaDownloadResult:
    downloaded: int
    missing: int
    bytes_downloaded: int
    transport: str


@dataclass(frozen=True)
class PreparedBatch:
    entries: tuple[MediaEntry, ...]
    job_id: str
    archive_bytes: int
    payload_bytes: int
    attempt: int = 1


class ProgressTracker:
    def __init__(
        self,
        entries: list[MediaEntry],
        callback: Callable[[str], None] | None,
    ) -> None:
        self.total_files = len(entries)
        self.total_bytes = sum(entry.size_bytes for entry in entries)
        self.callback = callback
        self.completed_files = 0
        self.completed_bytes = 0
        self.active_bytes: dict[str, int] = {}
        self.transport = ""
        self.last_report = 0.0
        self.last_files = 0
        self.lock = threading.Lock()

    def set_transport(self, detail: str) -> None:
        with self.lock:
            if detail == self.transport:
                return
            self.transport = detail
            self._report(True)

    def active(self, filename: str, count: int) -> None:
        with self.lock:
            self.active_bytes[filename] = max(0, count)
            self._report(False)

    def reset(self, filename: str) -> None:
        with self.lock:
            self.active_bytes.pop(filename, None)

    def complete(self, entry: MediaEntry) -> None:
        with self.lock:
            self.active_bytes.pop(entry.filename, None)
            self.completed_files += 1
            self.completed_bytes += entry.size_bytes
            self._report(
                self.completed_files == self.total_files
                or self.completed_files - self.last_files >= 500
            )

    def missing(self, entry: MediaEntry) -> None:
        with self.lock:
            self.active_bytes.pop(entry.filename, None)
            self.completed_files += 1
            self._report(self.completed_files == self.total_files)

    def _report(self, force: bool) -> None:
        if not self.callback:
            return
        now = time.monotonic()
        if not force and now - self.last_report < 0.5:
            return
        visible_bytes = min(
            self.total_bytes,
            self.completed_bytes + sum(self.active_bytes.values()),
        )
        self.last_report = now
        self.last_files = self.completed_files
        self.callback(
            f"Media download · {self.transport} · "
            f"{self.completed_files:,}/{self.total_files:,} files · "
            f"{_format_bytes(visible_bytes)}/{_format_bytes(self.total_bytes)}"
        )


def download_media(
    client: V2Client,
    raw_entries: list[dict],
    media_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> MediaDownloadResult:
    entries = [
        MediaEntry(
            filename=str(item.get("filename") or ""),
            modified_at=str(item.get("modified_at") or ""),
            size_bytes=int(item.get("size_bytes") or 0),
        )
        for item in raw_entries
        if item.get("filename")
    ]
    for entry in entries:
        if entry.size_bytes < 0 or entry.size_bytes > MAX_MEDIA_BYTES:
            raise ValueError("KelmaSync returned an invalid media size")
    media_dir.mkdir(parents=True, exist_ok=True)
    staging = media_dir.parent / ".kelma-media-staging"
    staging.mkdir(parents=True, exist_ok=True)
    tracker = ProgressTracker(entries, progress)
    total_bytes = sum(entry.size_bytes for entry in entries)
    if _use_tar(entries, total_bytes):
        try:
            return _download_tar_pipeline(client, entries, media_dir, staging, tracker)
        except TarUnsupported:
            # Compatibility only: current production servers support prepared
            # TARs, while older installations retain the proxy endpoint. Do not
            # redownload entries completed before another preparation reported
            # that the endpoint was unavailable.
            remaining = [
                entry for entry in entries
                if not _completed(media_dir, staging, entry)
            ]
            with tracker.lock:
                completed_files = tracker.completed_files
                completed_bytes = tracker.completed_bytes
            fallback = _download_ordinary(
                client, remaining, media_dir, staging, tracker,
            )
            return MediaDownloadResult(
                completed_files + fallback.downloaded,
                fallback.missing,
                completed_bytes + fallback.bytes_downloaded,
                fallback.transport,
            )
    return _download_ordinary(client, entries, media_dir, staging, tracker)


def _use_tar(entries: list[MediaEntry], total_bytes: int) -> bool:
    return len(entries) >= MIN_TAR_FILES or (
        len(entries) >= MIN_TAR_FILES_BY_BYTES and total_bytes >= MIN_TAR_BYTES
    )


def _tar_batches(entries: list[MediaEntry]) -> list[tuple[MediaEntry, ...]]:
    batches: list[tuple[MediaEntry, ...]] = []
    batch: list[MediaEntry] = []
    size = 0
    for entry in entries:
        if batch and (len(batch) >= MAX_TAR_FILES or size + entry.size_bytes > MAX_TAR_BYTES):
            batches.append(tuple(batch))
            batch = []
            size = 0
        batch.append(entry)
        size += entry.size_bytes
    if batch:
        batches.append(tuple(batch))
    return batches


def _prepare(client: V2Client, entries: tuple[MediaEntry, ...], attempt: int) -> PreparedBatch:
    last_error: Exception | None = None
    for retry in range(MAX_ATTEMPTS):
        try:
            response = client.prepare_media_tar([entry.filename for entry in entries])
            job_id = str(response.get("job_id") or "")
            archive_bytes = int(response.get("archive_bytes") or 0)
            payload_bytes = int(response.get("payload_bytes") or 0)
            files = int(response.get("files") or 0)
            if (
                not job_id
                or files != len(entries)
                or payload_bytes != sum(entry.size_bytes for entry in entries)
                or archive_bytes <= 0
            ):
                raise V2Error(0, "invalid prepared media metadata")
            return PreparedBatch(entries, job_id, archive_bytes, payload_bytes, attempt)
        except V2Error as error:
            if error.status in (404, 405):
                raise TarUnsupported() from error
            if error.status not in (0, 408, 429) and not 500 <= error.status <= 599:
                raise
            last_error = error
        if retry < MAX_ATTEMPTS - 1:
            time.sleep(_retry_delay(retry))
    raise V2Error(0, "media TAR preparation failed") from last_error


def _download_tar_pipeline(
    client: V2Client,
    entries: list[MediaEntry],
    media_dir: Path,
    staging: Path,
    tracker: ProgressTracker,
) -> MediaDownloadResult:
    initial = _tar_batches(entries)
    ready: Queue[tuple[PreparedBatch | None, Exception | None]] = Queue()
    prepared_jobs: set[str] = set()
    jobs_lock = threading.Lock()
    prepared_count = 0
    completed_batches = 0
    total_batches = len(initial)
    downloading = False
    state_lock = threading.Lock()

    def detail() -> str:
        with state_lock:
            active = "downloading 1" if downloading else "sequential"
            return (
                f"TAR · {completed_batches}/{total_batches} batches · "
                f"{min(prepared_count, total_batches)}/{total_batches} prepared · {active}"
            )

    def submit(pool: ThreadPoolExecutor, batch: tuple[MediaEntry, ...], attempt: int) -> None:
        def work() -> None:
            nonlocal prepared_count
            try:
                prepared = _prepare(client, batch, attempt)
                with jobs_lock:
                    prepared_jobs.add(prepared.job_id)
                with state_lock:
                    prepared_count += 1
                tracker.set_transport(detail())
                ready.put((prepared, None))
            except Exception as error:  # noqa: BLE001
                ready.put((None, error))
        pool.submit(work)

    pool = ThreadPoolExecutor(max_workers=MAX_TAR_PREPARATIONS, thread_name_prefix="kelma-tar-prepare")
    outstanding = 0
    try:
        for batch in initial:
            submit(pool, batch, 1)
            outstanding += 1
        tracker.set_transport(detail())
        while outstanding:
            prepared, error = ready.get()
            outstanding -= 1
            if error:
                raise error
            assert prepared is not None
            with state_lock:
                downloading = True
            tracker.set_transport(detail())
            try:
                _consume_tar(client, prepared, media_dir, staging, tracker)
                with state_lock:
                    completed_batches += 1
            except Exception as error:  # noqa: BLE001
                remaining = tuple(
                    entry
                    for entry in prepared.entries
                    if not _completed(media_dir, staging, entry)
                )
                if not remaining:
                    with state_lock:
                        completed_batches += 1
                elif prepared.attempt < MAX_ATTEMPTS:
                    submit(pool, remaining, prepared.attempt + 1)
                    outstanding += 1
                else:
                    raise V2Error(
                        0,
                        "prepared media connection failed; retry sync to resume completed downloads",
                    ) from error
            finally:
                with jobs_lock:
                    prepared_jobs.discard(prepared.job_id)
                _delete_best_effort(client, prepared.job_id)
            with state_lock:
                downloading = False
            tracker.set_transport(detail())
    finally:
        pool.shutdown(wait=True, cancel_futures=False)
        with jobs_lock:
            leftovers = list(prepared_jobs)
        for job_id in leftovers:
            _delete_best_effort(client, job_id)
    return MediaDownloadResult(len(entries), 0, sum(entry.size_bytes for entry in entries), "prepared TAR")


def _consume_tar(
    client: V2Client,
    prepared: PreparedBatch,
    media_dir: Path,
    staging: Path,
    tracker: ProgressTracker,
) -> tuple[int, int]:
    response = client.open_prepared_media_tar(prepared.job_id)
    try:
        if int(response.headers.get("X-Kelma-Media-Files", -1)) != len(prepared.entries):
            raise V2Error(0, "prepared media file count did not match")
        if int(response.headers.get("X-Kelma-Media-Bytes", -1)) != prepared.payload_bytes:
            raise V2Error(0, "prepared media byte count did not match")
        seen: set[int] = set()
        downloaded = 0
        downloaded_bytes = 0
        with tarfile.open(fileobj=response, mode="r|") as archive:
            for member in archive:
                if not member.isfile() or not member.name.isdecimal():
                    raise V2Error(0, "prepared media archive contained an invalid entry")
                index = int(member.name)
                if index not in range(len(prepared.entries)) or index in seen:
                    raise V2Error(0, "prepared media archive contained an unexpected entry")
                seen.add(index)
                entry = prepared.entries[index]
                if member.size != entry.size_bytes:
                    raise V2Error(0, "prepared media entry size did not match")
                source = archive.extractfile(member)
                if source is None:
                    raise V2Error(0, "prepared media entry was unavailable")
                _publish_stream(source, media_dir, staging, entry, tracker)
                downloaded += 1
                downloaded_bytes += entry.size_bytes
        if len(seen) != len(prepared.entries):
            raise V2Error(0, "prepared media archive ended early")
        return downloaded, downloaded_bytes
    finally:
        response.close()


def _download_ordinary(
    client: V2Client,
    entries: list[MediaEntry],
    media_dir: Path,
    staging: Path,
    tracker: ProgressTracker,
) -> MediaDownloadResult:
    tracker.set_transport("proxy · 64 concurrent")
    iterator = iter(entries)
    downloaded = 0
    missing = 0
    downloaded_bytes = 0

    def one(entry: MediaEntry) -> tuple[str, MediaEntry]:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = client.open_media_stream(entry.filename)
                try:
                    _publish_stream(response, media_dir, staging, entry, tracker)
                finally:
                    response.close()
                return "downloaded", entry
            except V2Error as error:
                tracker.reset(entry.filename)
                if error.status == 404:
                    return "missing", entry
                if error.status not in (0, 408, 429) and not 500 <= error.status <= 599:
                    raise
                last_error = error
            except Exception as error:  # noqa: BLE001
                tracker.reset(entry.filename)
                last_error = error
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt))
        raise V2Error(0, "media connection failed; retry sync to resume completed downloads") from last_error

    with ThreadPoolExecutor(max_workers=MEDIA_WORKERS, thread_name_prefix="kelma-media-download") as pool:
        pending = {}
        for entry in _take(iterator, MEDIA_WORKERS):
            pending[pool.submit(one, entry)] = entry
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                pending.pop(future)
                status, entry = future.result()
                if status == "downloaded":
                    downloaded += 1
                    downloaded_bytes += entry.size_bytes
                else:
                    missing += 1
                    tracker.missing(entry)
                try:
                    next_entry = next(iterator)
                except StopIteration:
                    continue
                pending[pool.submit(one, next_entry)] = next_entry
    return MediaDownloadResult(downloaded, missing, downloaded_bytes, "64-worker proxy")


def _publish_stream(source, media_dir: Path, staging: Path, entry: MediaEntry, tracker: ProgressTracker) -> None:
    destination = _safe_path(media_dir, entry.filename)
    descriptor, temporary = tempfile.mkstemp(prefix="media-", suffix=".part", dir=staging)
    count = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                if count > entry.size_bytes:
                    raise V2Error(0, "media response exceeded its manifest size")
                output.write(chunk)
                tracker.active(entry.filename, count)
            output.flush()
            os.fsync(output.fileno())
        if count != entry.size_bytes:
            raise V2Error(0, "media response did not match its manifest size")
        os.replace(temporary, destination)
        _write_marker(staging, entry)
        tracker.complete(entry)
    except Exception:
        tracker.reset(entry.filename)
        raise
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def delete_download_marker(media_dir: Path, filename: str) -> None:
    staging = media_dir.parent / ".kelma-media-staging"
    _marker_path(staging, filename).unlink(missing_ok=True)


def _marker_path(staging: Path, filename: str) -> Path:
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    return staging / f"{digest}.json"


def _write_marker(staging: Path, entry: MediaEntry) -> None:
    path = _marker_path(staging, entry.filename)
    descriptor, temporary = tempfile.mkstemp(prefix="marker-", dir=staging)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {
                    "filename": entry.filename,
                    "modified_at": entry.modified_at,
                    "size_bytes": entry.size_bytes,
                },
                output,
                separators=(",", ":"),
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _completed(media_dir: Path, staging: Path, entry: MediaEntry) -> bool:
    destination = _safe_path(media_dir, entry.filename)
    marker = _marker_path(staging, entry.filename)
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        return (
            destination.is_file()
            and destination.stat().st_size == entry.size_bytes
            and metadata.get("filename") == entry.filename
            and metadata.get("modified_at") == entry.modified_at
            and int(metadata.get("size_bytes", -1)) == entry.size_bytes
        )
    except (OSError, TypeError, ValueError):
        return False


def _safe_path(media_dir: Path, filename: str) -> Path:
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
        raise ValueError("KelmaSync returned an unsafe media filename")
    return media_dir / filename


def _delete_best_effort(client: V2Client, job_id: str) -> None:
    try:
        client.delete_prepared_media_tar(job_id)
    except Exception:  # noqa: BLE001
        pass


def _take(iterator: Iterable[MediaEntry], count: int) -> list[MediaEntry]:
    values = []
    iterator = iter(iterator)
    for _ in range(count):
        try:
            values.append(next(iterator))
        except StopIteration:
            break
    return values


def _retry_delay(attempt: int) -> float:
    return 0.25 * (2**attempt)


def _format_bytes(size: int) -> str:
    return f"{size / (1024 * 1024):,.1f} MiB"
