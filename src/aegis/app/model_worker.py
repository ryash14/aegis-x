"""One cancellable local inference request; stdin/stdout carry bounded JSON only."""

import json
import sys
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def main():
    task = json.loads(sys.stdin.buffer.read(65537))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(task["url"] + "/api/tags", timeout=5) as response:
        tags = json.loads(response.read(262145))
    selected = [model for model in tags.get("models", []) if model["name"] == task["model"]]
    if len(selected) != 1 or selected[0]["digest"] != task["digest"]:
        raise ValueError("Local model identity does not match the pinned digest")
    request = urllib.request.Request(
        task["url"] + "/api/chat",
        data=json.dumps(task["request"]).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    content, size, value = "", 0, {}
    with opener.open(request, timeout=task["timeout"]) as response:
        while True:
            raw = response.readline(16385)
            if not raw:
                break
            size += len(raw)
            if len(raw) > 16384 or size > 262144:
                raise ValueError("Local model response exceeded its byte limit")
            value = json.loads(raw)
            if value.get("error"):
                raise ValueError("Local model returned an error")
            part = value.get("message", {}).get("content", "")
            if not isinstance(part, str):
                raise ValueError("Invalid model content")
            content += part
            if len(content.encode()) > 32768:
                raise ValueError("Structured output exceeded its byte limit")
            if value.get("done"):
                break
    if not value.get("done") or value.get("done_reason") == "length":
        raise ValueError("Local model output was incomplete")
    if not isinstance(content, str) or len(content.encode()) > 32768:
        raise ValueError("Invalid or oversized structured output")
    result = {
        "output": json.loads(content),
        "prompt_tokens": value.get("prompt_eval_count"),
        "output_tokens": value.get("eval_count"),
        "model": task["model"],
        "digest": task["digest"],
    }
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}), flush=True)
        sys.exit(1)
