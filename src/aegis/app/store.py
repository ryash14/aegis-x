"""Versioned SQLite application state; passwords and session tokens stay hashed."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "app.sqlite3"
        secret = self.root / "session.key"
        try:
            descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
        self.secret = secret.read_bytes()
        if len(self.secret) != 32:
            raise ValueError("Invalid application session key")
        self.hasher = PasswordHasher()
        self.hash_slots = threading.BoundedSemaphore(4)
        self.dummy_hash = self.hasher.hash(secrets.token_hex(32))
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Application database is newer than this code")
            db.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE users (
                        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL, password_hash TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
                    );
                    CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        expires_at INTEGER NOT NULL
                    );
                    CREATE INDEX session_expiry ON sessions(expires_at);
                    CREATE TABLE projects (
                        id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL REFERENCES users(id),
                        name TEXT NOT NULL, description TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL
                    );
                    CREATE INDEX project_owner ON projects(owner_id,created_at);
                    CREATE TABLE project_documents (
                        document_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        revision TEXT NOT NULL, role TEXT NOT NULL
                    );
                    CREATE INDEX document_project ON project_documents(project_id);
                    CREATE TABLE login_limits (
                        key TEXT PRIMARY KEY, started_at INTEGER NOT NULL, attempts INTEGER NOT NULL
                    );
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            if version < 2:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE research_runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        parent_id TEXT REFERENCES research_runs(id) ON DELETE SET NULL,
                        question TEXT NOT NULL, mode TEXT NOT NULL,
                        status TEXT NOT NULL, stage TEXT NOT NULL,
                        config TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '[]',
                        trace TEXT NOT NULL DEFAULT '[]', result TEXT,
                        error TEXT, cancel INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE INDEX research_project ON research_runs(project_id,created_at);
                    PRAGMA user_version=2;
                    COMMIT;
                """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def email(value):
        if not isinstance(value, str):
            raise ValueError("Enter a valid email address")
        value = value.strip().lower()
        if (
            len(value) > 254
            or any(ord(c) < 32 for c in value)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
        ):
            raise ValueError("Enter a valid email address")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Enter a valid email address") from exc
        return value

    def add_user(self, email, name, password):
        email = self.email(email)
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("Use a password between 12 and 1,024 characters")
        name = self.text(name, "Name", 100)
        user_id = uuid4().hex
        hashed = self.hasher.hash(password)
        with self.connection() as db:
            db.execute(
                "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
                (user_id, email, name, hashed, int(time.time())),
            )
        return user_id

    def set_password(self, email, password):
        email = self.email(email)
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("Use a password between 12 and 1,024 characters")
        hashed = self.hasher.hash(password)
        with self.connection() as db:
            user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if not user:
                raise ValueError("User not found")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))

    def disable_user(self, email):
        with self.connection() as db:
            user = db.execute("SELECT id FROM users WHERE email=?", (self.email(email),)).fetchone()
            if not user:
                raise ValueError("User not found")
            db.execute("UPDATE users SET active=0 WHERE id=?", (user["id"],))
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))

    def signed(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def challenge(self):
        value = f"{secrets.token_hex(24)}.{int(time.time())}"
        return value + "." + self.signed("login:" + value)

    def valid_challenge(self, value):
        try:
            nonce, timestamp, signature = value.split(".")
            age = time.time() - int(timestamp)
            return 0 <= age <= 600 and hmac.compare_digest(
                signature, self.signed("login:" + nonce + "." + timestamp)
            )
        except (ValueError, AttributeError, TypeError):
            return False

    def reserve_login(self, email, address):
        now = int(time.time())
        limits = [("account:" + email, 6), ("address:" + address, 40)]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_limits WHERE started_at<?", (now - 900,))
            rows = []
            for value, maximum in limits:
                key = self.signed(value)
                row = db.execute("SELECT attempts FROM login_limits WHERE key=?", (key,)).fetchone()
                if row and row["attempts"] >= maximum:
                    return False
                rows.append(key)
            for key in rows:
                db.execute(
                    "INSERT INTO login_limits VALUES(?,?,1) ON CONFLICT(key) "
                    "DO UPDATE SET attempts=attempts+1",
                    (key, now),
                )
        return True

    def authenticate(self, email, password, address):
        email = self.email(email)
        if not self.reserve_login(email, address):
            return "limited", None
        if not self.hash_slots.acquire(blocking=False):
            return "limited", None
        try:
            with self.connection() as db:
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            try:
                valid = self.hasher.verify(
                    user["password_hash"] if user else self.dummy_hash, password
                )
            except (VerificationError, InvalidHashError):
                valid = False
            if not valid or not user or not user["active"]:
                return "invalid", None
            with self.connection() as db:
                db.execute(
                    "DELETE FROM login_limits WHERE key=?", (self.signed("account:" + email),)
                )
                if self.hasher.check_needs_rehash(user["password_hash"]):
                    db.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (self.hasher.hash(password), user["id"]),
                    )
            return "ok", dict(user)
        finally:
            self.hash_slots.release()

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def new_session(self, user_id, seconds):
        token = secrets.token_urlsafe(48)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(time.time()),))
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (self.token_hash(token), user_id, int(time.time()) + seconds),
            )
        return token

    def session(self, token):
        if not token or len(token) > 200:
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT u.id,u.email,u.name,s.expires_at FROM sessions s JOIN users u "
                "ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                (self.token_hash(token), int(time.time())),
            ).fetchone()
        return dict(row) if row else None

    def revoke(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash(token),))

    @staticmethod
    def text(value, label, maximum, *, empty=False, multiline=False):
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text")
        value = value.strip()
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{label} must be valid Unicode text") from exc
        if (
            (not value and not empty)
            or len(value) > maximum
            or any(ord(c) < 32 and not (multiline and c in "\n\r\t") for c in value)
        ):
            raise ValueError(f"{label} must contain {'0' if empty else '1'}–{maximum} characters")
        return value

    def create_project(self, owner, name, description=""):
        name = self.text(name, "Project name", 120)
        description = self.text(description, "Description", 1000, empty=True, multiline=True)
        project_id = uuid4().hex
        with self.connection() as db:
            db.execute(
                "INSERT INTO projects VALUES(?,?,?,?,'active',?)",
                (project_id, owner, name, description, int(time.time())),
            )
        return self.project(owner, project_id)

    def project(self, owner, identity):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE id=? AND owner_id=? AND status='active'",
                (identity, owner),
            ).fetchone()
        if not row:
            raise KeyError("Project not found")
        return dict(row)

    def projects(self, owner):
        with self.connection() as db:
            rows = db.execute(
                "SELECT p.*,count(d.document_id) AS document_count FROM projects p "
                "LEFT JOIN project_documents d ON d.project_id=p.id "
                "WHERE p.owner_id=? AND p.status='active' GROUP BY p.id "
                "ORDER BY p.created_at DESC,p.id",
                (owner,),
            ).fetchall()
        return [dict(row) for row in rows]

    def rename(self, owner, identity, name, description):
        self.project(owner, identity)
        name = self.text(name, "Project name", 120)
        description = self.text(description, "Description", 1000, empty=True, multiline=True)
        with self.connection() as db:
            db.execute(
                "UPDATE projects SET name=?,description=? WHERE id=? AND owner_id=?",
                (name, description, identity, owner),
            )
        return self.project(owner, identity)

    def link(self, project_id, document_id, revision, role):
        revision = self.text(revision, "Revision", 64, empty=True)
        if not isinstance(role, str) or role not in {"reference", "baseline", "candidate"}:
            raise ValueError("Choose reference, baseline or candidate")
        with self.connection() as db:
            db.execute(
                "INSERT INTO project_documents VALUES(?,?,?,?)",
                (document_id, project_id, revision, role),
            )

    def document(self, owner, identity, project_id=None):
        with self.connection() as db:
            row = db.execute(
                "SELECT d.* FROM project_documents d JOIN projects p ON p.id=d.project_id "
                "WHERE d.document_id=? AND p.owner_id=? AND p.status='active'",
                (identity, owner),
            ).fetchone()
        if not row or (project_id is not None and row["project_id"] != project_id):
            raise KeyError("Document not found")
        return dict(row)

    def unlink(self, identity):
        with self.connection() as db:
            db.execute("DELETE FROM project_documents WHERE document_id=?", (identity,))

    @staticmethod
    def decode_run(row):
        value = dict(row)
        for key in ("config", "evidence", "trace", "result"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def run(self, owner, identity):
        with self.connection() as db:
            row = db.execute(
                "SELECT r.* FROM research_runs r JOIN projects p ON p.id=r.project_id "
                "WHERE r.id=? AND p.owner_id=? AND p.status='active'",
                (identity, owner),
            ).fetchone()
        if not row:
            raise KeyError("Research not found")
        return self.decode_run(row)

    def runs(self, owner, project, offset=0, limit=20):
        self.project(owner, project)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,parent_id,question,mode,status,stage,error,created_at,updated_at "
                "FROM research_runs WHERE project_id=? ORDER BY created_at DESC,id "
                "LIMIT ? OFFSET ?",
                (project, min(50, max(1, limit)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_research_document(self, identity):
        """Erase saved and follow-up research derived from a deleted source."""
        with self.connection() as db:
            rows = db.execute(
                "WITH RECURSIVE affected(id) AS ("
                "SELECT r.id FROM research_runs r WHERE EXISTS "
                "(SELECT 1 FROM json_each(r.evidence) WHERE json_extract(value,'$.document_id')=?) "
                "UNION SELECT r.id FROM research_runs r JOIN affected a ON r.parent_id=a.id) "
                "SELECT id FROM affected",
                (identity,),
            ).fetchall()
            for row in rows:
                db.execute("DELETE FROM research_runs WHERE id=?", (row[0],))

    def begin_delete(self, owner, identity):
        self.project(owner, identity)
        with self.connection() as db:
            db.execute("UPDATE projects SET status='deleting' WHERE id=?", (identity,))
            return [
                r[0]
                for r in db.execute(
                    "SELECT document_id FROM project_documents WHERE project_id=?", (identity,)
                )
            ]

    def finish_delete(self, identity):
        with self.connection() as db:
            db.execute("DELETE FROM projects WHERE id=? AND status='deleting'", (identity,))

    def reconcile(self, workspace):
        """Finish interrupted deletions and remove unmapped private-app uploads."""
        with self.connection() as db:
            deleting = db.execute(
                "SELECT id,owner_id FROM projects WHERE status='deleting'"
            ).fetchall()
            mapped = {r[0] for r in db.execute("SELECT document_id FROM project_documents")}
        for project in deleting:
            with self.connection() as db:
                documents = [
                    r[0]
                    for r in db.execute(
                        "SELECT document_id FROM project_documents WHERE project_id=?",
                        (project["id"],),
                    )
                ]
            for identity in documents:
                try:
                    workspace.purge(identity)
                except KeyError:
                    pass
            self.finish_delete(project["id"])
        with workspace.connection() as db:
            jobs = {r[0] for r in db.execute("SELECT id FROM documents")}
        for identity in jobs - mapped:
            workspace.purge(identity)
        for path in workspace.root.iterdir():
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{32}", path.name) and path.name not in jobs:
                import shutil

                shutil.rmtree(path)
        with self.connection() as db:
            for identity in mapped - jobs:
                db.execute("DELETE FROM project_documents WHERE document_id=?", (identity,))
