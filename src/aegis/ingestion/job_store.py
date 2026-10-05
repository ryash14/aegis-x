"""Durable job state; only the batch coordinator writes the SQLite manifest."""

import sqlite3
from pathlib import Path


class JobStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                source_path TEXT NOT NULL, pipeline_id TEXT NOT NULL,
                content_sha256 TEXT, status TEXT NOT NULL,
                output_path TEXT, output_sha256 TEXT, pages INTEGER,
                error_code TEXT, error TEXT,
                PRIMARY KEY (source_path, pipeline_id)
            )
        """)
        self.connection.commit()

    def get(self, source: str, pipeline_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE source_path=? AND pipeline_id=?", (source, pipeline_id)
        ).fetchone()
        return dict(row) if row is not None else None

    def save(self, job: dict) -> None:
        columns = (
            "source_path",
            "pipeline_id",
            "content_sha256",
            "status",
            "output_path",
            "output_sha256",
            "pages",
            "error_code",
            "error",
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(job.get(column) for column in columns),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
