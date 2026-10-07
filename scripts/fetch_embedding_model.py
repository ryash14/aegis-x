"""Fetch checksum-pinned public model files once; inference never downloads anything."""

import hashlib
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "data/models/bge-small-en-v1.5"
SPEC = PROJECT / "src/aegis/retrieval/model.json"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    manifest = json.loads(SPEC.read_text())
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, record in manifest["files"].items():
        target = ROOT / name
        if target.is_file() and digest(target) == record["sha256"]:
            print(name, "verified; reused", flush=True)
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--retry",
                    "2",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    "120",
                    "--max-filesize",
                    str(record["bytes"] + 1024),
                    "--output",
                    str(temporary),
                    record["url"],
                ],
                check=True,
            )
            if temporary.stat().st_size != record["bytes"] or digest(temporary) != record["sha256"]:
                raise ValueError("Embedding model download checksum mismatch")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(name, "downloaded and verified", flush=True)
    temporary = ROOT / "manifest.json.part"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(ROOT / "manifest.json")
    print("Ready:", ROOT)


if __name__ == "__main__":
    main()
