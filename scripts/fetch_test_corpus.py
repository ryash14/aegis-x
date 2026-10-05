"""Download a fixed local test corpus using only the Python standard library."""

import argparse
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 200 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(entry: dict, output: Path, previous: dict) -> dict:
    target = output / entry["path"]
    record = dict(entry)
    saved = previous.get(entry["path"], {})
    if target.is_file() and saved.get("sha256") == digest(target):
        if saved.get("url") == entry["url"]:
            print(f"Cached: {entry['path']}", flush=True)
            return saved
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            request = Request(entry["url"], headers={"User-Agent": "AEGIS-X-test-corpus/1.0"})
            size = 0
            with urlopen(request, timeout=45) as response, temporary.open("wb") as stream:
                record["resolved_url"] = response.url
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Download exceeds 200 MiB limit")
                    stream.write(chunk)
            if not size:
                raise ValueError("Empty download")
            with temporary.open("rb") as stream:
                header = stream.read(512)
            if target.suffix == ".pdf" and not header.startswith(b"%PDF-"):
                raise ValueError("Expected PDF; received another format")
            if header.lstrip().startswith(b"version https://git-lfs.github.com/spec/"):
                raise ValueError("Received Git LFS pointer instead of document")
            if expected := entry.get("git_blob_sha1"):
                blob = hashlib.sha1(f"blob {size}\0".encode())
                with temporary.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        blob.update(chunk)
                if blob.hexdigest() != expected:
                    raise ValueError("Download differs from pinned Git blob")
            record.update(
                sha256=digest(temporary),
                size_bytes=size,
                downloaded_at=datetime.now(UTC).isoformat(),
                status="downloaded",
            )
            temporary.replace(target)
            print(f"Downloaded: {entry['path']} ({size:,} bytes)", flush=True)
            return record
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(attempt + 1)
            else:
                record.update(status="failed", error=str(exc))
                print(f"FAILED: {entry['path']}: {exc}", flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "test-corpus")
    args = parser.parse_args()
    source = json.loads((ROOT / "docs" / "test-corpus-sources.json").read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    previous = {}
    if manifest_path.exists():
        previous = {item["path"]: item for item in json.loads(manifest_path.read_text())["files"]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda entry: fetch(entry, output, previous), source["files"]))
    manifest = {"schema_version": 1, "files": records}
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(manifest_path)
    failures = sum(item["status"] == "failed" for item in records)
    print(
        f"Corpus: {len(records) - failures} available, {failures} failed. Manifest: {manifest_path}"
    )
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
