"""Offline, checksummed backup and fail-closed restore into a new storage directory."""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

LIMIT = 20 * 1024**3


def backup(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if not (root / "app.sqlite3").is_file():
        raise ValueError("Application storage does not exist")
    if destination.is_relative_to(root):
        raise ValueError("Write the backup outside application storage")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (root / "documents/.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Stop the app before taking a consistent backup") from error
        with tempfile.TemporaryDirectory(prefix="aegis-backup-") as temporary:
            snapshots = {}
            for relative in ("app.sqlite3", "documents/workspace.sqlite3"):
                original = root / relative
                if not original.is_file():
                    raise ValueError("Backup requires both application databases")
                snapshot = Path(temporary) / original.name
                with sqlite3.connect(original) as source, sqlite3.connect(snapshot) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Database integrity check failed")
                snapshots[relative] = snapshot
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with (
                    os.fdopen(descriptor, "wb") as output,
                    ZipFile(output, "w", ZIP_DEFLATED) as archive,
                ):
                    manifest = {}
                    for original in sorted(root.rglob("*")):
                        if original.is_symlink():
                            raise ValueError("Storage symlinks are not supported in backups")
                        if (
                            not original.is_file()
                            or original.name == ".lock"
                            or original.name.endswith(("-wal", "-shm"))
                        ):
                            continue
                        relative = original.relative_to(root).as_posix()
                        path = snapshots.get(relative, original)
                        digest = hashlib.sha256()
                        with path.open("rb") as source, archive.open(relative, "w") as target:
                            while block := source.read(1024**2):
                                digest.update(block)
                                target.write(block)
                        manifest[relative] = {
                            "sha256": digest.hexdigest(),
                            "bytes": path.stat().st_size,
                        }
                    archive.writestr(
                        "backup-manifest.json",
                        json.dumps({"version": 1, "files": manifest}, sort_keys=True),
                    )
                return {
                    "files": len(manifest),
                    "bytes": destination.stat().st_size,
                    "path": str(destination),
                }
            except BaseException:
                destination.unlink(missing_ok=True)
                raise


def restore(archive_path, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            "Restore requires a new storage directory; existing data is never overwritten"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".aegis-restore-", dir=destination.parent))
    try:
        with ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > 100000 or sum(item.file_size for item in infos) > LIMIT:
                raise ValueError("Backup exceeds restore limits")
            if len({item.filename for item in infos}) != len(infos):
                raise ValueError("Duplicate archive entries")
            manifest_info = archive.getinfo("backup-manifest.json")
            if manifest_info.file_size > 32 * 1024**2:
                raise ValueError("Oversized backup manifest")
            manifest = json.loads(archive.read(manifest_info))
            if (
                not isinstance(manifest, dict)
                or manifest.get("version") != 1
                or not isinstance(manifest.get("files"), dict)
            ):
                raise ValueError("Unsupported backup manifest")
            expected = manifest["files"]
            if set(expected) != {
                item.filename for item in infos if item.filename != "backup-manifest.json"
            }:
                raise ValueError("Archive does not match its manifest")
            for name, metadata in expected.items():
                if (
                    not isinstance(metadata, dict)
                    or type(metadata.get("bytes")) is not int
                    or metadata["bytes"] < 0
                    or not isinstance(metadata.get("sha256"), str)
                    or len(metadata["sha256"]) != 64
                ):
                    raise ValueError("Invalid backup file metadata")
                relative = PurePosixPath(name)
                info = archive.getinfo(name)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in name
                    or not relative.parts
                    or stat.S_ISLNK(info.external_attr >> 16)
                ):
                    raise ValueError("Unsafe backup path")
                target = temporary.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest, count = hashlib.sha256(), 0
                with archive.open(info) as source, target.open("xb") as output:
                    target.chmod(0o600)
                    while block := source.read(1024**2):
                        count += len(block)
                        if count > metadata["bytes"] or count > LIMIT:
                            raise ValueError("Backup size mismatch")
                        digest.update(block)
                        output.write(block)
                if count != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
                    raise ValueError("Backup checksum mismatch")
        for relative in ("app.sqlite3", "documents/workspace.sqlite3"):
            if not (temporary / relative).is_file():
                raise ValueError("Backup is missing a database")
            with sqlite3.connect(temporary / relative) as db:
                db.execute("PRAGMA trusted_schema=OFF")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored database failed integrity check")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Restored database contains broken references")
                if relative == "app.sqlite3":
                    db.execute("DELETE FROM sessions")
        if (
            not (temporary / "session.key").is_file()
            or (temporary / "session.key").stat().st_size != 32
        ):
            raise ValueError("Backup has no valid application key")
        os.rename(temporary, destination)
        return {"files": len(expected), "path": str(destination), "sessions_revoked": True}
    except (BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise ValueError("Invalid or incomplete backup archive") from error
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
