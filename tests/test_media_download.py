from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor as RealExecutor
from io import BytesIO
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from kelma.kelma_sync_v2 import media_download


class Response(BytesIO):
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


def tar_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for index, (_filename, data) in enumerate(entries):
            info = tarfile.TarInfo(str(index))
            info.size = len(data)
            archive.addfile(info, BytesIO(data))
    return output.getvalue()


class ProxyClient:
    def open_media_stream(self, filename: str):
        return Response(filename.encode("utf-8"))


class PreparedClient:
    def __init__(self, delays: dict[str, float] | None = None, fail_first_get: bool = False) -> None:
        self.delays = delays or {}
        self.fail_first_get = fail_first_get
        self.jobs: dict[str, list[str]] = {}
        self.prepared: list[list[str]] = []
        self.opened: list[str] = []
        self.deleted: list[str] = []
        self.active_gets = 0
        self.max_gets = 0
        self.failed_get = False
        self.lock = threading.Lock()

    def prepare_media_tar(self, filenames: list[str]):
        time.sleep(self.delays.get(filenames[0], 0))
        with self.lock:
            job = f"job-{len(self.jobs)}"
            self.jobs[job] = list(filenames)
            self.prepared.append(list(filenames))
        payload = sum(len(name.encode()) for name in filenames)
        archive = tar_bytes([(name, name.encode()) for name in filenames])
        return {
            "job_id": job,
            "archive_bytes": len(archive),
            "payload_bytes": payload,
            "files": len(filenames),
        }

    def open_prepared_media_tar(self, job_id: str):
        with self.lock:
            self.opened.append(job_id)
            self.active_gets += 1
            self.max_gets = max(self.max_gets, self.active_gets)
        filenames = self.jobs[job_id]
        response_filenames = filenames
        if self.fail_first_get and not self.failed_get:
            self.failed_get = True
            response_filenames = filenames[:1]
        data = tar_bytes([(name, name.encode()) for name in response_filenames])
        owner = self

        class TrackedResponse(Response):
            def close(self) -> None:
                if not self.closed:
                    with owner.lock:
                        owner.active_gets -= 1
                super().close()

        return TrackedResponse(
            data,
            {
                "X-Kelma-Media-Files": str(len(filenames)),
                "X-Kelma-Media-Bytes": str(sum(len(name.encode()) for name in filenames)),
            },
        )

    def delete_prepared_media_tar(self, job_id: str) -> None:
        self.deleted.append(job_id)


class MediaDownloadProtocolTest(unittest.TestCase):
    def test_ordinary_download_uses_exactly_64_proxy_workers(self) -> None:
        used_workers: list[int] = []

        def executor(*args, **kwargs):
            used_workers.append(int(kwargs["max_workers"]))
            return RealExecutor(*args, **kwargs)

        entries = [
            {"filename": chr(65 + index % 26) + f"{index}", "size_bytes": len(chr(65 + index % 26) + f"{index}")}
            for index in range(65)
        ]
        with tempfile.TemporaryDirectory() as root, patch.object(
            media_download,
            "ThreadPoolExecutor",
            side_effect=executor,
        ):
            result = media_download.download_media(
                ProxyClient(),
                entries,
                Path(root) / "collection.media",
            )

        self.assertEqual(used_workers, [64])
        self.assertEqual(result.downloaded, 65)

    def test_prepared_tar_consumes_first_ready_and_only_one_get(self) -> None:
        client = PreparedClient({"a": 0.15, "c": 0.01})
        entries = [
            {"filename": name, "size_bytes": 1, "modified_at": "v1"}
            for name in ("a", "b", "c", "d")
        ]
        with tempfile.TemporaryDirectory() as root, patch.multiple(
            media_download,
            MIN_TAR_FILES=1,
            MAX_TAR_BYTES=2,
        ):
            result = media_download.download_media(
                client,
                entries,
                Path(root) / "collection.media",
            )

        first_opened_names = client.jobs[client.opened[0]]
        self.assertEqual(first_opened_names, ["c", "d"])
        self.assertEqual(client.max_gets, 1)
        self.assertEqual(result.downloaded, 4)
        self.assertCountEqual(client.deleted, client.opened)

    def test_consumed_interrupted_tar_is_replaced_with_only_remaining_entries(self) -> None:
        client = PreparedClient(fail_first_get=True)
        entries = [
            {"filename": name, "size_bytes": 1, "modified_at": "v1"}
            for name in ("a", "b", "c")
        ]
        with tempfile.TemporaryDirectory() as root, patch.object(
            media_download,
            "MIN_TAR_FILES",
            1,
        ):
            result = media_download.download_media(
                client,
                entries,
                Path(root) / "collection.media",
            )

        self.assertEqual(client.prepared, [["a", "b", "c"], ["b", "c"]])
        self.assertEqual(len(client.opened), 2)
        self.assertEqual(result.downloaded, 3)

    def test_tar_batches_obey_128_mib_and_10000_file_limits(self) -> None:
        entries = [media_download.MediaEntry(str(index), "v", 16 * 1024) for index in range(10_001)]
        batches = media_download._tar_batches(entries)

        self.assertEqual([len(batch) for batch in batches], [8_192, 1_809])
        self.assertTrue(all(len(batch) <= 10_000 for batch in batches))
        self.assertTrue(all(sum(item.size_bytes for item in batch) <= 128 * 1024 * 1024 for batch in batches))


if __name__ == "__main__":
    unittest.main()
