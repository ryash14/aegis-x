"""One PDF per subprocess so native crashes and timeouts remain isolated."""

import json
import sys

from .batch import BatchConfig, _worker
from .models import IngestionLimits
from .ocr_models import OCRConfig


def main() -> None:
    request = json.load(sys.stdin)
    settings = request["config"]
    settings["ocr"] = OCRConfig(**settings["ocr"])
    settings["limits"] = IngestionLimits(**settings["limits"])
    result = _worker(
        request["source"],
        request["identity"],
        request["output"],
        request["pipeline"],
        BatchConfig(**settings),
    )
    print("AEGIS_RESULT:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
