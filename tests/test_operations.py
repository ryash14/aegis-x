"""A real backup/restore drill, corruption rejection and active-worker exclusion."""

import hashlib
import io
import json
from zipfile import ZipFile

import pytest

from aegis.app.operations import backup, restore
from aegis.app.store import Store
from aegis.workspace.service import Workspace

from .test_private_app import PASSWORD, pdf_bytes


def test_backup_restores_accounts_documents_and_revokes_sessions(tmp_path):
    root = tmp_path / "source"
    store = Store(root)
    owner = store.add_user("owner@example.test", "Owner", PASSWORD)
    store.new_session(owner, 3600)
    project = store.create_project(owner, "Original")
    workspace = Workspace(root / "documents")
    payload = pdf_bytes()
    job = workspace.upload(io.BytesIO(payload), len(payload), "source.pdf")
    store.link(project["id"], job["id"], "A", "reference")
    with pytest.raises(ValueError, match="Stop the app"):
        backup(root, tmp_path / "running.zip")
    workspace.close()
    archive = tmp_path / "backup.zip"
    result = backup(root, archive)
    assert result["files"] >= 4
    destination = tmp_path / "restored"
    assert restore(archive, destination)["sessions_revoked"]
    restored = Store(destination)
    assert restored.project(owner, project["id"])["name"] == "Original"
    assert (destination / "documents" / job["id"] / "source.pdf").read_bytes() == payload
    with restored.connection() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    with pytest.raises(ValueError, match="new storage"):
        restore(archive, destination)
    with ZipFile(archive) as original, ZipFile(tmp_path / "corrupt.zip", "w") as changed:
        for member in original.infolist():
            content = original.read(member)
            changed.writestr(
                member.filename, b"corrupt" if member.filename == "session.key" else content
            )
    with pytest.raises(ValueError, match="mismatch"):
        restore(tmp_path / "corrupt.zip", tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_restore_rejects_path_escape(tmp_path):
    archive = tmp_path / "escape.zip"
    payload = b"outside"
    with ZipFile(archive, "w") as zipfile:
        zipfile.writestr("../outside", payload)
        zipfile.writestr(
            "backup-manifest.json",
            json.dumps(
                {
                    "version": 1,
                    "files": {
                        "../outside": {
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    },
                }
            ),
        )
    with pytest.raises(ValueError, match="Unsafe"):
        restore(archive, tmp_path / "new")
    assert not (tmp_path / "outside").exists()
