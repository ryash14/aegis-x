"""Authenticated single-instance application with project-scoped document access."""

import argparse
import asyncio
import hmac
import json
import math
import os
import tempfile
import threading
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from aegis.chunking import ChunkConfig
from aegis.workspace.server import DOCX_MIME
from aegis.workspace.service import Workspace

from .investigation import MODEL_DIGEST, Investigations, LocalModel
from .model_worker import NoRedirect
from .research import Research
from .reviews import Reviews, printable
from .store import Store

STATIC = Path(__file__).parent / "static"
WORKSPACE_STATIC = Path(__file__).parent.parent / "workspace/static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; frame-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


@dataclass(frozen=True)
class Settings:
    storage: Path = field(default_factory=lambda: Path(os.getenv("AEGIS_STORAGE", "data/app")))
    workers: int = field(default_factory=lambda: int(os.getenv("AEGIS_WORKERS", "2")))
    max_file_bytes: int = field(
        default_factory=lambda: int(os.getenv("AEGIS_MAX_FILE_MIB", "100")) * 1024**2
    )
    job_timeout: float = field(default_factory=lambda: float(os.getenv("AEGIS_JOB_TIMEOUT", "300")))
    session_seconds: int = field(
        default_factory=lambda: int(os.getenv("AEGIS_SESSION_SECONDS", "28800"))
    )
    secure_cookies: bool = field(
        default_factory=lambda: os.getenv("AEGIS_COOKIE_SECURE", "0") == "1"
    )
    allowed_hosts: tuple = field(
        default_factory=lambda: tuple(
            os.getenv("AEGIS_ALLOWED_HOSTS", "127.0.0.1,localhost,::1").split(",")
        )
    )
    embedding_model: Path = field(
        default_factory=lambda: Path(
            os.getenv("AEGIS_EMBEDDING_MODEL", "data/models/bge-small-en-v1.5")
        )
    )
    model_url: str = field(
        default_factory=lambda: os.getenv("AEGIS_MODEL_URL", "http://127.0.0.1:11435")
    )
    research_timeout: int = field(
        default_factory=lambda: int(os.getenv("AEGIS_RESEARCH_TIMEOUT", "180"))
    )
    public_origin: str = field(default_factory=lambda: os.getenv("AEGIS_PUBLIC_ORIGIN", ""))

    def __post_init__(self):
        if (
            self.session_seconds < 60
            or self.workers < 1
            or self.max_file_bytes < 1
            or not math.isfinite(self.job_timeout)
            or self.job_timeout <= 0
            or not self.allowed_hosts
            or "*" in self.allowed_hosts
        ):
            raise ValueError("Use positive session limits and explicit trusted hosts")
        local = urlsplit(self.model_url)
        if (
            local.scheme != "http"
            or local.hostname not in {"127.0.0.1", "localhost", "::1"}
            or local.path
            or local.query
            or local.fragment
            or local.username
            or not 10 <= self.research_timeout <= 600
        ):
            raise ValueError("Use a loopback model endpoint and a 10–600 second research timeout")
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in self.allowed_hosts:
                raise ValueError("Public origin must use a configured trusted host")
            if parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("Public origin must contain only scheme, host and optional port")
            if parsed.scheme == "https" and not self.secure_cookies:
                raise ValueError("HTTPS deployments require secure cookies")

    @property
    def cookie(self):
        return "__Host-aegis_session" if self.secure_cookies else "aegis_session"

    @property
    def login_cookie(self):
        return "__Host-aegis_login" if self.secure_cookies else "aegis_login"


async def json_body(request):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "Use a JSON request")
    data = bytearray()
    try:
        async with asyncio.timeout(15):
            async for part in request.stream():
                if len(data) + len(part) > 16384:
                    raise HTTPException(413, "Request is too large")
                data.extend(part)
    except TimeoutError as exc:
        raise HTTPException(408, "Request timed out") from exc
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON request") from exc


def create_app(settings=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        store = await run_in_threadpool(Store, settings.storage)
        workspace = await run_in_threadpool(
            Workspace,
            settings.storage / "documents",
            workers=settings.workers,
            max_file_bytes=settings.max_file_bytes,
            timeout=settings.job_timeout,
        )
        app.state.store = store
        app.state.workspace = workspace
        app.state.mutations = threading.RLock()
        app.state.upload_slots = threading.BoundedSemaphore(4)
        try:
            await run_in_threadpool(store.reconcile, workspace)
            app.state.research = await run_in_threadpool(
                Research, workspace, store, app.state.mutations, settings.embedding_model
            )
            app.state.investigations = Investigations(
                store,
                app.state.research,
                app.state.mutations,
                LocalModel(settings.model_url),
                timeout=settings.research_timeout,
            )
            app.state.reviews = Reviews(store, workspace, app.state.mutations)
            yield
        finally:
            if hasattr(app.state, "reviews"):
                await run_in_threadpool(app.state.reviews.close)
            if hasattr(app.state, "investigations"):
                await run_in_threadpool(app.state.investigations.close)
            if hasattr(app.state, "research"):
                await run_in_threadpool(app.state.research.close)
            await run_in_threadpool(workspace.close)

    app = FastAPI(title="AEGIS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.middleware("http")
    async def boundary(request, call_next):
        try:
            parsed = urlsplit("http://" + request.headers.get("host", ""))
            hostname = parsed.hostname
            _ = parsed.port
            trusted = (
                hostname in settings.allowed_hosts
                and not parsed.username
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            trusted = False
        if not trusted:
            response = JSONResponse({"error": "Untrusted host"}, 400)
        elif request.url.path.startswith("/api/") and (
            request.headers.get("sec-fetch-site") == "cross-site"
            or (
                request.headers.get("origin")
                and request.headers["origin"]
                != (settings.public_origin or str(request.base_url).rstrip("/"))
            )
        ):
            response = JSONResponse({"error": "Cross-origin requests are unavailable"}, 403)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
                    if request.url.path.endswith("/original")
                    else CSP
                ),
                "X-Frame-Options": (
                    "SAMEORIGIN" if request.url.path.endswith("/original") else "DENY"
                ),
            }
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, error.status_code, headers=error.headers)

    @app.exception_handler(KeyError)
    async def not_found(request, error):
        return JSONResponse({"error": "Project or document not found"}, 404)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"error": str(error)}, 400)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, error):
        return JSONResponse({"error": "Document artifact is unavailable"}, 404)

    def current_user(request: Request):
        token = request.cookies.get(settings.cookie, "")
        user = app.state.store.session(token)
        if not user:
            raise HTTPException(401, "Sign in to continue")
        if request.method not in {"GET", "HEAD"}:
            supplied = request.headers.get("x-aegis-token", "")
            expected = app.state.store.signed("csrf:" + token)
            if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                raise HTTPException(403, "Invalid session request token")
        return user

    user_dependency = Depends(current_user)

    def project_id(request, user):
        identity = request.headers.get("x-aegis-project") or request.query_params.get("project_id")
        if not identity:
            raise HTTPException(400, "Choose a project")
        app.state.store.project(user["id"], identity)
        return identity

    def document(request, user, identity):
        metadata = app.state.store.document(
            user["id"], identity, request.headers.get("x-aegis-project")
        )
        return {**app.state.workspace.get(identity), **metadata}

    def set_cookie(response, name, value, seconds):
        response.set_cookie(
            name,
            value,
            max_age=seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            path="/",
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    def readiness():
        checks = {
            "storage": False,
            "retrieval": False,
            "research_worker": False,
            "review_worker": False,
            "model": False,
        }
        try:
            with app.state.store.connection() as db:
                checks["storage"] = db.execute("SELECT 1").fetchone()[0] == 1
            checks["retrieval"] = (
                app.state.research.thread.is_alive()
                and app.state.research.dense is not None
                and not app.state.research.index_error
            )
            checks["research_worker"] = app.state.investigations.thread.is_alive()
            checks["review_worker"] = app.state.reviews.thread.is_alive()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(settings.model_url + "/api/tags", timeout=2) as response:
                payload = response.read(262145)
                if len(payload) <= 262144:
                    models = json.loads(payload).get("models", [])
                    checks["model"] = any(
                        model.get("name") == "qwen3:8b" and model.get("digest") == MODEL_DIGEST
                        for model in models
                    )
        except (OSError, ValueError, AttributeError):
            pass
        return {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks}

    @app.get("/health/ready")
    def ready():
        report = readiness()
        return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503)

    @app.get("/api/system")
    def system_status(user=user_dependency):
        report = readiness()
        report.update(
            model="qwen3:8b",
            model_digest=MODEL_DIGEST,
            retrieval_model=app.state.research.dense.encoder.key
            if app.state.research.dense
            else None,
            research_timeout_seconds=settings.research_timeout,
            per_file_limit_bytes=settings.max_file_bytes,
            document_workers=settings.workers,
        )
        return report

    @app.get("/api/session")
    def session(request: Request, user=user_dependency):
        return {
            "user": user,
            "token": app.state.store.signed("csrf:" + request.cookies[settings.cookie]),
            "max_file_bytes": settings.max_file_bytes,
            "workers": settings.workers,
            "timeout_seconds": settings.job_timeout,
            "engine": app.state.workspace.engine,
            "samples": False,
        }

    @app.get("/api/auth/challenge")
    def challenge():
        value = app.state.store.challenge()
        response = JSONResponse({"token": value})
        set_cookie(response, settings.login_cookie, value, 600)
        return response

    @app.post("/api/auth/login")
    async def login(request: Request):
        supplied = request.headers.get("x-aegis-login", "")
        stored = request.cookies.get(settings.login_cookie, "")
        if not hmac.compare_digest(
            supplied.encode("utf-8"), stored.encode("utf-8")
        ) or not app.state.store.valid_challenge(stored):
            raise HTTPException(403, "Refresh the sign-in page and try again")
        body = await json_body(request)
        password = body.get("password")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise HTTPException(400, "Invalid sign-in details")
        try:
            password.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(400, "Invalid sign-in details") from exc
        status, user = await run_in_threadpool(
            app.state.store.authenticate,
            body.get("email"),
            password,
            request.client.host if request.client else "unknown",
        )
        if status == "limited":
            raise HTTPException(
                429,
                "Too many sign-in attempts. Try again in 15 minutes.",
                headers={"Retry-After": "900"},
            )
        if status != "ok":
            raise HTTPException(401, "Email or password is incorrect")
        old = request.cookies.get(settings.cookie)
        if old:
            await run_in_threadpool(app.state.store.revoke, old)
        token = await run_in_threadpool(
            app.state.store.new_session, user["id"], settings.session_seconds
        )
        response = JSONResponse({"signed_in": True})
        set_cookie(response, settings.cookie, token, settings.session_seconds)
        response.delete_cookie(
            settings.login_cookie,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request, user=user_dependency):
        app.state.store.revoke(request.cookies[settings.cookie])
        response = JSONResponse({"signed_out": True})
        response.delete_cookie(
            settings.cookie,
            path="/",
            secure=settings.secure_cookies,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/projects")
    def projects(user=user_dependency):
        return {"projects": app.state.store.projects(user["id"])}

    @app.post("/api/projects", status_code=201)
    async def add_project(request: Request, user=user_dependency):
        body = await json_body(request)
        return await run_in_threadpool(
            app.state.store.create_project,
            user["id"],
            body.get("name"),
            body.get("description", ""),
        )

    @app.patch("/api/projects/{identity}")
    async def rename_project(identity: str, request: Request, user=user_dependency):
        body = await json_body(request)

        def rename():
            with app.state.mutations:
                return app.state.store.rename(
                    user["id"], identity, body.get("name"), body.get("description", "")
                )

        return await run_in_threadpool(rename)

    @app.delete("/api/projects/{identity}")
    def delete_project(identity: str, user=user_dependency):
        with app.state.mutations:
            identities = app.state.store.begin_delete(user["id"], identity)
            for document_id in identities:
                app.state.workspace.purge(document_id)
            app.state.store.finish_delete(identity)
        return {"deleted": True}

    @app.get("/api/documents")
    def documents(
        request: Request, offset: int = 0, limit: int = 50, q: str = "", user=user_dependency
    ):
        identity = project_id(request, user)
        offset, limit = max(0, offset), max(1, min(100, limit))
        with app.state.workspace.connection() as db:
            db.execute("ATTACH DATABASE ? AS account", (str(app.state.store.path),))
            where = "l.project_id=? AND d.status NOT IN ('removed','deleting') "
            params = [identity]
            base = "FROM documents d JOIN account.project_documents l ON l.document_id=d.id "
            states = dict(
                db.execute(
                    "SELECT d.status,count(*) " + base + "WHERE " + where + "GROUP BY d.status",
                    params,
                )
            )
            where += "AND instr(lower(d.name),lower(?))>0 "
            params.append(q[:240])
            count = db.execute("SELECT count(*) " + base + "WHERE " + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT d.*,l.project_id,l.revision,l.role "
                + base
                + "WHERE "
                + where
                + "ORDER BY d.created_at DESC,d.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        results = [dict(row) for row in rows]
        for row in results:
            row["config"] = json.loads(row["config"])
        return {
            "documents": results,
            "total": count,
            "states": states,
            "offset": offset,
            "limit": limit,
        }

    @app.post("/api/documents", status_code=201)
    async def upload(request: Request, user=user_dependency):
        identity = await run_in_threadpool(project_id, request, user)
        if request.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Upload a PDF or DOCX as binary content")
        try:
            name = unquote(request.headers.get("x-filename", ""), errors="strict")
            name = name.replace("\\", "/").split("/")[-1]
            if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
                raise ValueError("Invalid filename")
            options = json.loads(request.headers.get("x-aegis-config", "{}"))
            if not isinstance(options, dict) or not options.keys() <= {
                "max_chars",
                "overlap_chars",
            }:
                raise ValueError("Invalid chunk settings")
            if any(type(value) is not int for value in options.values()):
                raise ValueError("Chunk settings must be integers")
            config = ChunkConfig(**options)
            if not 32 <= config.max_chars <= 16000:
                raise ValueError("Use a chunk size from 32 to 16,000 characters")
            if Path(name.replace("\\", "/")).suffix.lower() not in {".pdf", ".docx"}:
                raise ValueError("Upload a PDF or DOCX file")
            revision = Store.text(
                unquote(request.headers.get("x-aegis-revision", ""), errors="strict"),
                "Revision",
                64,
                empty=True,
            )
            role = request.headers.get("x-aegis-role", "reference")
            if not isinstance(role, str) or role not in {"reference", "baseline", "candidate"}:
                raise ValueError("Invalid document role")
            declared = request.headers.get("content-length")
            if declared is not None and not 0 < int(declared) <= settings.max_file_bytes:
                raise HTTPException(413, "File exceeds the upload limit or is empty")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid document name or processing settings") from exc
        if not app.state.upload_slots.acquire(blocking=False):
            raise HTTPException(429, "Four uploads are already in progress; try again shortly")
        try:
            with tempfile.TemporaryFile(dir=settings.storage) as spool:
                size = 0
                try:
                    async with asyncio.timeout(30):
                        async for part in request.stream():
                            size += len(part)
                            if size > settings.max_file_bytes:
                                raise HTTPException(413, "File exceeds the upload limit")
                            await run_in_threadpool(spool.write, part)
                except TimeoutError as exc:
                    raise HTTPException(408, "Upload timed out") from exc
                if not size or (declared is not None and size != int(declared)):
                    raise HTTPException(400, "Upload is empty or incomplete")
                spool.seek(0)

                def commit():
                    with app.state.mutations:
                        app.state.store.project(user["id"], identity)
                        job = app.state.workspace.upload(spool, size, name, config)
                        try:
                            app.state.store.link(identity, job["id"], revision, role)
                        except Exception:
                            app.state.workspace.purge(job["id"])
                            raise
                        return document(request, user, job["id"])

                return await run_in_threadpool(commit)
        finally:
            app.state.upload_slots.release()

    @app.get("/api/documents/{identity}")
    def get_document(identity: str, request: Request, user=user_dependency):
        return document(request, user, identity)

    @app.post("/api/documents/{identity}/cancel")
    def cancel_document(identity: str, request: Request, user=user_dependency):
        with app.state.mutations:
            document(request, user, identity)
            app.state.workspace.cancel(identity)
            return document(request, user, identity)

    @app.post("/api/documents/{identity}/retry")
    def retry(identity: str, request: Request, user=user_dependency):
        with app.state.mutations:
            document(request, user, identity)
            app.state.workspace.retry(identity)
            return document(request, user, identity)

    @app.post("/api/documents/{identity}/remove")
    @app.delete("/api/documents/{identity}")
    def remove(identity: str, request: Request, user=user_dependency):
        with app.state.mutations:
            document(request, user, identity)
            app.state.store.forget_research_document(identity)
            app.state.workspace.purge(identity)
            app.state.store.unlink(identity)
        return {"deleted": True}

    @app.patch("/api/documents/{identity}")
    async def edit_document(identity: str, request: Request, user=user_dependency):
        body = await json_body(request)

        def edit():
            with app.state.mutations:
                existing = document(request, user, identity)
                revision = Store.text(
                    body.get("revision", existing["revision"]), "Revision", 64, empty=True
                )
                role = body.get("role", existing["role"])
                if not isinstance(role, str) or role not in {"reference", "baseline", "candidate"}:
                    raise ValueError("Invalid document role")
                with app.state.store.connection() as db:
                    db.execute(
                        "UPDATE project_documents SET revision=?,role=? WHERE document_id=?",
                        (revision, role, identity),
                    )
                return document(request, user, identity)

        return await run_in_threadpool(edit)

    @app.get("/api/documents/{identity}/{view:path}")
    def artifact(
        identity: str,
        view: str,
        request: Request,
        offset: int = 0,
        limit: int = 50,
        page: int | None = None,
        focus: int | None = None,
        user=user_dependency,
    ):
        job = document(request, user, identity)
        workspace = app.state.workspace
        if view == "original":
            return FileResponse(
                workspace.root / identity / ("source." + job["format"]),
                media_type="application/pdf" if job["format"] == "pdf" else DOCX_MIME,
                filename=job["name"],
                content_disposition_type="inline" if job["format"] == "pdf" else "attachment",
            )
        if view in {"summary", "structure"}:
            return workspace.read_json(identity, view + ".json")
        if view == "chunks":
            index = workspace.read_json(identity, "chunk-index.json")
            if page is not None:
                index = [item for item in index if page in item["pages"]]
            if focus is not None:
                positions = [n for n, item in enumerate(index) if item["index"] == focus]
                if not positions:
                    raise KeyError(focus)
                offset = (positions[0] // 50) * 50
            offset, limit = max(0, offset), min(100, max(1, limit))
            return {"chunks": index[offset : offset + limit], "total": len(index), "offset": offset}
        parts = view.split("/")
        if parts[0] == "pages" and len(parts) in {2, 3} and parts[1].isdigit():
            number = int(parts[1])
            if job["format"] != "pdf" or not 1 <= number <= job["pages"]:
                raise HTTPException(404, "Page not found")
            if len(parts) == 3 and parts[2] == "image":
                return FileResponse(workspace.image(identity, number), media_type="image/png")
            if len(parts) == 2:
                return workspace.read_json(identity, f"page-{number}.json")
        if parts[0] == "chunks" and len(parts) == 2 and parts[1].isdigit():
            number = int(parts[1])
            if 0 <= number < job["chunks"]:
                return workspace.read_json(identity, f"chunk-{number}.json")
        raise HTTPException(404, "Artifact not found")

    @app.get("/api/research")
    def research_history(request: Request, offset: int = 0, limit: int = 20, user=user_dependency):
        identity = project_id(request, user)
        return {"runs": app.state.store.runs(user["id"], identity, offset, limit)}

    @app.post("/api/research", status_code=202)
    async def start_research(request: Request, user=user_dependency):
        identity = await run_in_threadpool(project_id, request, user)
        body = await json_body(request)
        try:
            return await run_in_threadpool(
                app.state.investigations.create,
                user["id"],
                identity,
                body.get("question"),
                body.get("mode", "answer"),
                body.get("parent_id"),
                body.get("retrieval", "hybrid"),
                body.get("document_id"),
                body.get("review_id"),
                body.get("review_row_id"),
            )
        except BlockingIOError as exc:
            raise HTTPException(429, str(exc)) from exc

    def owned_run(user, identity):
        return app.state.store.run(user["id"], identity)

    @app.get("/api/reviews")
    def list_reviews(request: Request, user=user_dependency):
        return {"reviews": app.state.reviews.list(user["id"], project_id(request, user))}

    @app.post("/api/reviews", status_code=202)
    async def create_review(request: Request, user=user_dependency):
        body = await json_body(request)
        try:
            return await run_in_threadpool(
                app.state.reviews.create,
                user["id"],
                project_id(request, user),
                body.get("baseline_id"),
                body.get("candidate_id"),
            )
        except BlockingIOError as error:
            raise HTTPException(429, str(error)) from error

    @app.get("/api/reviews/{identity}")
    def read_review(identity: str, user=user_dependency):
        return app.state.reviews.get(user["id"], identity)

    @app.post("/api/reviews/{identity}/cancel")
    def cancel_review(identity: str, user=user_dependency):
        return app.state.reviews.cancel(user["id"], identity)

    @app.post("/api/reviews/{identity}/retry", status_code=202)
    def retry_review(identity: str, user=user_dependency):
        old = app.state.reviews.get(user["id"], identity)
        if old["status"] not in {"failed", "cancelled"}:
            raise ValueError("Only failed or cancelled reviews can be retried")
        try:
            return app.state.reviews.create(
                user["id"], old["project_id"], old["baseline_id"], old["candidate_id"], reuse=False
            )
        except BlockingIOError as error:
            raise HTTPException(429, str(error)) from error

    @app.post("/api/reviews/{identity}/decisions")
    async def decide_review(identity: str, request: Request, user=user_dependency):
        body = await json_body(request)
        return await run_in_threadpool(
            app.state.reviews.decide,
            user["id"],
            identity,
            body.get("row_id"),
            body.get("decision"),
            body.get("note", ""),
        )

    @app.get("/api/reviews/{identity}/report")
    def review_report(identity: str, format: str = "json", user=user_dependency):
        review = app.state.reviews.get(user["id"], identity)
        if review["status"] != "completed":
            raise ValueError("The review has not completed")
        if format == "html":
            return HTMLResponse(
                printable(review),
                headers={
                    "Content-Disposition": f'attachment; filename="aegis-review-{identity}.html"'
                },
            )
        if format != "json":
            raise ValueError("Use json or html report format")
        return JSONResponse(
            review,
            headers={"Content-Disposition": f'attachment; filename="aegis-review-{identity}.json"'},
        )

    @app.get("/api/research/{identity}")
    def read_research(identity: str, user=user_dependency):
        return owned_run(user, identity)

    @app.post("/api/research/{identity}/cancel")
    def cancel_research(identity: str, user=user_dependency):
        return app.state.investigations.cancel(user["id"], identity)

    @app.post("/api/research/{identity}/retry", status_code=202)
    def retry_research(identity: str, user=user_dependency):
        existing = owned_run(user, identity)
        if existing["status"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "Only failed or cancelled research can be retried")
        try:
            return app.state.investigations.create(
                user["id"],
                existing["project_id"],
                existing["question"],
                existing["mode"],
                existing["parent_id"],
                existing["config"]["retrieval"],
                existing["config"].get("document_id"),
                existing["config"].get("review_reference", {}).get("id"),
                existing["config"].get("review_reference", {}).get("row_id"),
            )
        except BlockingIOError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.get("/api/research/{identity}/evidence/{evidence_id}")
    def saved_evidence(identity: str, evidence_id: str, user=user_dependency):
        run = owned_run(user, identity)
        items = [item for item in run["evidence"] if item["id"] == evidence_id]
        if not items:
            raise KeyError("Evidence not found")
        return items[0]

    @app.get("/api/research/{identity}/report")
    def research_report(identity: str, user=user_dependency):
        run = owned_run(user, identity)
        if run["status"] != "completed":
            raise HTTPException(409, "Research is not complete")
        response = JSONResponse({"report_version": 1, "research": run})
        response.headers["Content-Disposition"] = f'attachment; filename="research-{identity}.json"'
        return response

    @app.get("/api/search/status")
    def search_status(request: Request, user=user_dependency):
        identity = project_id(request, user)
        return app.state.research.status(user["id"], identity)

    @app.get("/api/search")
    def search_evidence(
        request: Request,
        q: str,
        mode: str = "hybrid",
        limit: int = 10,
        budget: int = 12000,
        document_id: str | None = None,
        revision: str | None = None,
        role: str | None = None,
        section: str | None = None,
        kind: str | None = None,
        format: str | None = None,
        page: int | None = None,
        user=user_dependency,
    ):
        identity = project_id(request, user)
        if role and role not in {"reference", "baseline", "candidate"}:
            raise ValueError("Invalid role")
        if format and format not in {"pdf", "docx"}:
            raise ValueError("Invalid document format")
        if kind and kind not in {"text", "table"}:
            raise ValueError("Invalid evidence kind")
        for value in (revision, section):
            if value:
                Store.text(value, "Search filter", 160)
        try:
            return app.state.research.search(
                user["id"],
                identity,
                q,
                mode=mode,
                limit=limit,
                budget=budget,
                document_id=document_id,
                revision=revision,
                role=role,
                section=section,
                kind=kind,
                format=format,
                page=page,
            )
        except BlockingIOError as exc:
            raise HTTPException(429, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/")
    def home(request: Request):
        if not app.state.store.session(request.cookies.get(settings.cookie, "")):
            return RedirectResponse("/login", 303)
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html", media_type="text/html")

    @app.get("/{asset}")
    def asset(asset: str):
        if asset in {"app.js", "login.js", "private.css", "reviews.js"}:
            return FileResponse(STATIC / asset)
        if asset == "style.css":
            return FileResponse(WORKSPACE_STATIC / asset)
        raise HTTPException(404, "Page not found")

    @app.get("/assets/{asset}")
    def font(asset: str):
        if asset not in {"InterVariable.woff2", "Inter-LICENSE.txt"}:
            raise HTTPException(404, "Asset not found")
        return FileResponse(WORKSPACE_STATIC / "assets" / asset)

    return app


def main():
    import getpass
    import sqlite3

    parser = argparse.ArgumentParser(description="AEGIS private document application")
    parser.add_argument("--storage", type=Path, default=Settings().storage)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("user-add", help="Provision an invited user's account")
    add.add_argument("email")
    add.add_argument("--name", default="Workspace owner")
    reset = commands.add_parser("user-password", help="Change a password and revoke sessions")
    reset.add_argument("email")
    disable = commands.add_parser("user-disable", help="Disable a user and revoke their sessions")
    disable.add_argument("email")
    backup_command = commands.add_parser("backup", help="Back up stopped application storage")
    backup_command.add_argument("archive", type=Path)
    restore_command = commands.add_parser("restore", help="Restore a backup into new storage")
    restore_command.add_argument("archive", type=Path)
    serve = commands.add_parser("serve", help="Start the private application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    try:
        if args.command in {"user-add", "user-password"}:
            password = getpass.getpass("New password (12+ characters): ")
            if password != getpass.getpass("Confirm password: "):
                parser.error("Passwords do not match")
            store = Store(args.storage)
            if args.command == "user-add":
                store.add_user(args.email, args.name, password)
                print("Account created. Start the app and sign in with your email.")
            else:
                store.set_password(args.email, password)
                print("Password changed and existing sessions revoked.")
        elif args.command in {"backup", "restore"}:
            from .operations import backup, restore

            result = (
                backup(args.storage, args.archive)
                if args.command == "backup"
                else restore(args.archive, args.storage)
            )
            print(json.dumps(result))
        elif args.command == "user-disable":
            Store(args.storage).disable_user(args.email)
            print("Account disabled and sessions revoked.")
        else:
            settings = Settings(storage=args.storage)
            if args.host not in {"127.0.0.1", "localhost", "::1"} and (
                not settings.secure_cookies or not settings.public_origin.startswith("https://")
            ):
                parser.error("Network serving requires AEGIS_COOKIE_SECURE=1 and a TLS proxy")
            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                access_log=False,
                proxy_headers=False,
            )
    except (ValueError, sqlite3.IntegrityError) as exc:
        parser.error(
            "Account already exists" if isinstance(exc, sqlite3.IntegrityError) else str(exc)
        )


if __name__ == "__main__":
    main()
