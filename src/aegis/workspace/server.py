"""Loopback-only Phase 1 inspection server with streaming file uploads."""

import argparse
import hmac
import json
import mimetypes
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from aegis.chunking import ChunkConfig
from aegis.retrieval import SparseIndex

from .service import Workspace

STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'"
)
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SAMPLES = (
    "reports/nasa-systems-engineering-handbook.pdf",
    "docling/docx/word_tables.docx",
    "docling/ocr/ocr_test_rotated_90.pdf",
)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace, project):
        self.workspace = workspace
        self.project = Path(project).resolve()
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid logging filenames, extracted text, or upload tokens.
        pass

    def guard(self, mutation=False):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        if host not in allowed or (
            origin and origin not in {"http://" + value for value in allowed}
        ):
            raise PermissionError("This workspace accepts localhost requests only")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("Cross-site requests are unavailable")
        if mutation and not hmac.compare_digest(
            self.headers.get("X-Aegis-Token", ""), self.server.token
        ):
            raise PermissionError("Invalid workspace session")

    def respond_headers(self, status, content_type, length, extras=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            CSP,
        )
        for key, value in (extras or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def json(self, value, status=200):
        payload = json.dumps(value).encode()
        self.respond_headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def file(self, path, content_type=None, filename=None):
        if not path.is_file():
            raise KeyError("File unavailable")
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        extras = {"Accept-Ranges": "bytes"}
        if filename:
            disposition = "inline" if content_type == "application/pdf" else "attachment"
            extras["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
        value = self.headers.get("Range")
        if value:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not match or not any(match.groups()):
                self.respond_headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                return
            if match[1]:
                start = int(match[1])
                end = min(size - 1, int(match[2]) if match[2] else size - 1)
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.respond_headers(416, "text/plain", 0, {"Content-Range": f"bytes */{size}"})
                return
            status = 206
            extras["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        self.respond_headers(
            status,
            content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            length,
            extras,
        )
        with path.open("rb") as stream:
            stream.seek(start)
            while length:
                part = stream.read(min(length, 1024 * 1024))
                if not part:
                    break
                self.wfile.write(part)
                length -= len(part)

    def do_GET(self):
        try:
            self.guard()
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            path = unquote(route.path)
            workspace = self.server.workspace
            if path == "/api/session":
                self.json(
                    {
                        "token": self.server.token,
                        "max_file_bytes": workspace.max_file_bytes,
                        "workers": workspace.workers,
                        "engine": workspace.engine,
                        "samples": all(
                            (self.server.project / "data/test-corpus" / item).is_file()
                            for item in SAMPLES
                        ),
                    }
                )
            elif path == "/api/retrieval":
                self.json(SparseIndex(workspace.db).stats())
            elif path == "/api/search":
                self.json(
                    {
                        "results": SparseIndex(workspace.db).search(
                            query.get("q", [""])[0],
                            limit=int(query.get("limit", [10])[0]),
                            job_id=query.get("job_id", [None])[0],
                            format=query.get("format", [None])[0] or None,
                            kind=query.get("kind", [None])[0] or None,
                            page=int(query["page"][0]) if query.get("page", [""])[0] else None,
                            mode=query.get("mode", ["any"])[0],
                            live_workspace=True,
                        )
                    }
                )
            elif path == "/api/documents":
                self.json(
                    workspace.list(
                        offset=max(0, int(query.get("offset", [0])[0])),
                        limit=min(100, max(1, int(query.get("limit", [50])[0]))),
                        query=query.get("q", [""])[0][:240],
                    )
                )
            elif match := re.fullmatch(r"/api/documents/([0-9a-f]{32})(?:/(.*))?", path):
                identity, action = match.groups()
                job = workspace.get(identity)
                if not action:
                    self.json(job)
                elif action == "original":
                    self.file(
                        workspace.root / identity / ("source." + job["format"]),
                        "application/pdf" if job["format"] == "pdf" else DOCX_MIME,
                        job["name"],
                    )
                elif action in {"summary", "structure"}:
                    self.json(workspace.read_json(identity, action + ".json"))
                elif action == "chunks":
                    index = workspace.read_json(identity, "chunk-index.json")
                    if "page" in query:
                        number = int(query["page"][0])
                        index = [item for item in index if number in item["pages"]]
                    offset = max(0, int(query.get("offset", [0])[0]))
                    limit = min(100, max(1, int(query.get("limit", [50])[0])))
                    self.json(
                        {
                            "chunks": index[offset : offset + limit],
                            "total": len(index),
                            "offset": offset,
                        }
                    )
                elif page := re.fullmatch(r"pages/(\d+)(/image)?", action):
                    number = int(page[1])
                    if job["format"] != "pdf" or not 1 <= number <= job["pages"]:
                        raise KeyError("Page unavailable")
                    if page[2]:
                        self.file(workspace.image(identity, number), "image/png")
                    else:
                        self.json(workspace.read_json(identity, f"page-{number}.json"))
                elif chunk := re.fullmatch(r"chunks/(\d+)", action):
                    if not 0 <= int(chunk[1]) < job["chunks"]:
                        raise KeyError("Chunk unavailable")
                    self.json(workspace.read_json(identity, f"chunk-{int(chunk[1])}.json"))
                else:
                    raise KeyError("Route unavailable")
            elif path == "/":
                self.file(STATIC / "index.html", "text/html; charset=utf-8")
            elif path in {"/app.js", "/style.css"}:
                self.file(STATIC / path[1:])
            elif path in {"/assets/InterVariable.woff2", "/assets/Inter-LICENSE.txt"}:
                self.file(STATIC / "assets" / path.rsplit("/", 1)[1])
            elif path.startswith(("/docs/", "/data/")):
                relative = path.lstrip("/")
                if path.startswith("/data/") and not relative.startswith(
                    (
                        "data/experiments/",
                        "data/docx-experiments/",
                        "data/chunk-experiments/",
                        "data/ingestion/reports/",
                        "data/test-corpus/",
                    )
                ):
                    raise KeyError("Artifact unavailable")
                target = (self.server.project / relative).resolve()
                if not target.is_relative_to(self.server.project / relative.split("/")[0]):
                    raise KeyError("Artifact unavailable")
                self.file(target)
            else:
                raise KeyError("Route unavailable")
        except PermissionError as exc:
            self.json({"error": str(exc)}, 403)
        except (KeyError, FileNotFoundError):
            self.json({"error": "Document or artifact unavailable"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError):
            self.json({"error": "Requested document view is unavailable"}, 400)

    def do_POST(self):
        try:
            self.guard(mutation=True)
            path = urlsplit(self.path).path
            workspace = self.server.workspace
            if path == "/api/retrieval/index":
                self.json(SparseIndex(workspace.db).sync_workspace(workspace))
            elif path == "/api/documents":
                length = int(self.headers.get("Content-Length", "0"))
                if length > workspace.max_file_bytes:
                    self.json({"error": "File exceeds workspace upload limit"}, 413)
                    return
                if (
                    self.headers.get("Transfer-Encoding")
                    or self.headers.get("Content-Type") != "application/octet-stream"
                ):
                    raise ValueError("Use a fixed-length document upload")
                options = json.loads(self.headers.get("X-Aegis-Config", "{}"))
                if not isinstance(options, dict) or not options.keys() <= {
                    "max_chars",
                    "overlap_chars",
                }:
                    raise ValueError("Invalid chunk settings")
                if any(type(value) is not int for value in options.values()):
                    raise ValueError("Chunk settings must be integers")
                config = ChunkConfig(**options)
                if config.max_chars > 16000:
                    raise ValueError("Use at most 16,000 characters per chunk")
                self.connection.settimeout(30)
                job = workspace.upload(
                    self.rfile, length, unquote(self.headers.get("X-Filename", "")), config
                )
                self.json(job, 201)
            elif path == "/api/samples":
                paths = [self.server.project / "data/test-corpus" / item for item in SAMPLES]
                if not all(item.is_file() for item in paths):
                    raise ValueError("Local samples have not been fetched")
                self.json({"documents": [workspace.add_file(item) for item in paths]}, 201)
            elif match := re.fullmatch(r"/api/documents/([0-9a-f]{32})/(retry|remove)", path):
                if match[2] == "retry":
                    self.json(workspace.retry(match[1]))
                else:
                    workspace.remove(match[1])
                    self.json({"removed": True})
            else:
                raise KeyError("Route unavailable")
        except PermissionError as exc:
            self.json({"error": str(exc)}, 403)
        except (KeyError, FileNotFoundError):
            self.json({"error": "Document unavailable"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError) as exc:
            self.json({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage", type=Path, default=Path("data/workspace"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-file-mib", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    workspace = Workspace(
        args.storage,
        workers=args.workers,
        max_file_bytes=args.max_file_mib * 1024 * 1024,
        timeout=args.timeout_seconds,
    )
    server = LocalServer(("127.0.0.1", args.port), workspace, Path.cwd())
    print(f"AEGIS workspace: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        workspace.close()


if __name__ == "__main__":
    main()
