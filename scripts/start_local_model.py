"""Start the local runtime with the verified setup configuration."""

import os

from prepare_local_runtime import RUNTIME, runtime_environment

if __name__ == "__main__":
    binary = RUNTIME / "bin/ollama"
    if not binary.is_file():
        raise SystemExit("Run bash scripts/prepare_environment.sh first")
    os.execve(str(binary), [str(binary), "serve"], runtime_environment())
