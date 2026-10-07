"""Provision one pinned local model and verify real structured GPU inference."""

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "data/setup"
RUNTIME = ROOT / "data/runtime/ollama"
VERSION = "0.40.0"
ARCHIVE_SHA = "c94aa4156b3d13e64ebc2efe5ea53f015384c882be776e6695cfb37fb180d5ad"
MODEL = "qwen3:8b"
MODEL_MANIFEST_SHA = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
MODEL_BLOB_SHA = "a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f"
ADDRESS = "http://127.0.0.1:11435"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cached_model_verified():
    manifest = ROOT / "data/models/ollama/manifests/registry.ollama.ai/library/qwen3/8b"
    if not manifest.is_file() or digest(manifest) != MODEL_MANIFEST_SHA:
        return False
    spec = json.loads(manifest.read_text())
    for asset in [spec["config"], *spec["layers"]]:
        expected = asset["digest"].removeprefix("sha256:")
        path = ROOT / "data/models/ollama/blobs" / ("sha256-" + expected)
        if not path.is_file() or path.stat().st_size != asset["size"]:
            return False
        if digest(path) != expected:
            return False
    return True


def fetch_model_assets():
    """Use IPv4 for the pinned public registry; reuse verified large blobs."""
    manifest = ROOT / "data/models/ollama/manifests/registry.ollama.ai/library/qwen3/8b"
    temporary_manifest = SETUP / "qwen3-8b-manifest.part"
    subprocess.run(
        [
            "curl",
            "-4",
            "-fsSL",
            "--retry",
            "3",
            "--connect-timeout",
            "10",
            "--max-time",
            "120",
            "--max-filesize",
            "1048576",
            "--output",
            str(temporary_manifest),
            "https://registry.ollama.ai/v2/library/qwen3/manifests/8b",
        ],
        check=True,
    )
    if digest(temporary_manifest) != MODEL_MANIFEST_SHA:
        raise ValueError("Upstream model manifest differs from the selected pinned package")
    spec = json.loads(temporary_manifest.read_text())
    for asset in [spec["config"], *spec["layers"]]:
        expected = asset["digest"].removeprefix("sha256:")
        target = ROOT / "data/models/ollama/blobs" / ("sha256-" + expected)
        if target.is_file() and target.stat().st_size == asset["size"]:
            if digest(target) == expected:
                print("Verified and reused blob", expected[:12], flush=True)
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".aegis-part")
        subprocess.run(
            [
                "curl",
                "-4",
                "-fsSL",
                "--retry",
                "3",
                "--connect-timeout",
                "10",
                "--max-time",
                "3600",
                "--max-filesize",
                str(asset["size"]),
                "--output",
                str(temporary),
                "https://registry.ollama.ai/v2/library/qwen3/blobs/" + asset["digest"],
            ],
            check=True,
        )
        if temporary.stat().st_size != asset["size"] or digest(temporary) != expected:
            raise ValueError("Model asset checksum mismatch")
        temporary.replace(target)
        print("Downloaded and verified blob", expected[:12], flush=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest.replace(manifest)


def runtime_environment():
    env = os.environ.copy()
    env.update(
        OLLAMA_HOST="127.0.0.1:11435",
        OLLAMA_MODELS=str(ROOT / "data/models/ollama"),
        OLLAMA_NO_CLOUD="1",
        OLLAMA_NUM_PARALLEL="1",
        OLLAMA_MAX_LOADED_MODELS="1",
        OLLAMA_CONTEXT_LENGTH="8192",
        OLLAMA_KV_CACHE_TYPE="q8_0",
        OLLAMA_FLASH_ATTENTION="1",
    )
    return env


def api(route, payload=None, timeout=10):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(ADDRESS + route, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def install_runtime():
    archive = SETUP / "ollama-linux-amd64.tar.zst"
    temporary = archive.with_suffix(archive.suffix + ".part")
    if not archive.exists():
        if not temporary.exists() or digest(temporary) != ARCHIVE_SHA:
            subprocess.run(
                [
                    "curl",
                    "-fL",
                    "--retry",
                    "3",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    "3600",
                    "--output",
                    str(temporary),
                    f"https://github.com/ollama/ollama/releases/download/v{VERSION}/"
                    "ollama-linux-amd64.tar.zst",
                ],
                check=True,
            )
        if digest(temporary) != ARCHIVE_SHA:
            raise ValueError("Runtime archive checksum mismatch")
        temporary.replace(archive)
    if digest(archive) != ARCHIVE_SHA:
        raise ValueError("Runtime archive checksum mismatch")
    marker = RUNTIME / ".release-sha256"
    if not marker.exists() or marker.read_text().strip() != ARCHIVE_SHA:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(RUNTIME)], check=True)
        marker.write_text(ARCHIVE_SHA + "\n")
    print("Runtime SHA-256 verified; installed locally", flush=True)


def smoke_test(requirement_id="REQ-THERM-001", limit_celsius=85):
    started = time.monotonic()
    result = api(
        "/api/chat",
        {
            "model": MODEL,
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "limit_celsius": {"type": "number"},
                    "citation": {"type": "string"},
                },
                "required": ["requirement_id", "limit_celsius", "citation"],
                "additionalProperties": False,
            },
            "messages": [
                {"role": "system", "content": "Extract only the supplied facts as JSON."},
                {
                    "role": "user",
                    "content": f"Source E1: {requirement_id}: The controller shall operate "
                    f"at temperatures up to {limit_celsius} degrees Celsius. Return its "
                    "requirement_id, limit_celsius and citation E1.",
                },
            ],
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 128},
            "keep_alive": "2m",
        },
        timeout=300,
    )
    parsed = json.loads(result["message"]["content"])
    expected = {
        "requirement_id": requirement_id,
        "limit_celsius": limit_celsius,
        "citation": "E1",
    }
    if not result.get("done") or parsed != expected:
        raise ValueError(f"Structured extraction smoke test failed: {parsed}")
    running = api("/api/ps")
    models = running.get("models", [])
    gpu_bytes = sum(item.get("size_vram", 0) for item in models)
    if not gpu_bytes:
        raise RuntimeError("Inference completed but GPU placement was not confirmed")
    return {
        "structured_output": parsed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "gpu_bytes": gpu_bytes,
        "running_models": models,
        "prompt_tokens": result.get("prompt_eval_count"),
        "output_tokens": result.get("eval_count"),
        "load_seconds": round(result.get("load_duration", 0) / 1e9, 3),
        "prompt_seconds": round(result.get("prompt_eval_duration", 0) / 1e9, 3),
        "generation_seconds": round(result.get("eval_duration", 0) / 1e9, 3),
    }


def main():
    SETUP.mkdir(parents=True, exist_ok=True)
    (SETUP / "readiness.json").unlink(missing_ok=True)
    install_runtime()
    # Fail on a busy port rather than using or stopping another user's runtime.
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 11435))
    env = runtime_environment()
    binary = RUNTIME / "bin/ollama"
    with (SETUP / "ollama-server.log").open("w") as log:
        process = subprocess.Popen([str(binary), "serve"], env=env, stdout=log, stderr=log)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError("Runtime exited; inspect data/setup/ollama-server.log")
                try:
                    api("/api/version")
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.5)
            else:
                raise TimeoutError("Runtime startup timed out")
            cached = cached_model_verified()
            if cached:
                print("Verified cached model; no network pull required", flush=True)
            else:
                fetch_model_assets()
            if not cached and not cached_model_verified():
                raise ValueError("Generation model assets do not match the selected manifest")
            tags = api("/api/tags")
            selected = [item for item in tags.get("models", []) if item["name"] == MODEL]
            if len(selected) != 1 or selected[0]["digest"] != MODEL_MANIFEST_SHA:
                raise ValueError("Model manifest differs from the reviewed pinned package")
            print("Model weights verified; testing structured inference on GPU", flush=True)
            report = {
                "status": "passed",
                "runtime_version": VERSION,
                "runtime_sha256": ARCHIVE_SHA,
                "model": MODEL,
                "weights_sha256": MODEL_BLOB_SHA,
                "manifest_sha256": MODEL_MANIFEST_SHA,
                "installed_models": tags,
                "cloud_disabled": True,
                "smoke_test": smoke_test(),
                "warm_smoke_test": smoke_test("REQ-THERM-002", 80),
                "scope": "Environment smoke test, not a review quality benchmark",
            }
            (SETUP / "readiness.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
