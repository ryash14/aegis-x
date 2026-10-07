"""Pinned, local-only BGE ONNX inference with explicit token-window coverage."""

import hashlib
import json
import threading
from importlib.metadata import version
from pathlib import Path

MODEL = "BAAI/bge-small-en-v1.5"
REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
INSTRUCTION = "Represent this sentence for searching relevant passages: "
WINDOW = 510  # Two additional special tokens give the model's 512-token limit.
OVERLAP = 64


class LocalEncoder:
    dimension = 384

    def __init__(self, directory):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        root = Path(directory)
        manifest = json.loads((root / "manifest.json").read_text())
        expected = json.loads(Path(__file__).with_name("model.json").read_text())
        if manifest != expected:
            raise ValueError("Unexpected embedding model revision")
        for name in ("model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json"):
            with (root / name).open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != manifest["files"][name]["sha256"]:
                raise ValueError("Embedding model checksum mismatch")
        identity = {
            "manifest": manifest,
            "runtime": ort.__version__,
            "tokenizers": version("tokenizers"),
            "numpy": np.__version__,
            "algorithm": "cls-l2-window510-overlap64-max-v1",
        }
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(root / "model.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls = self.tokenizer.token_to_id("[CLS]")
        self.sep = self.tokenizer.token_to_id("[SEP]")
        self.pad = self.tokenizer.token_to_id("[PAD]")
        self.np = np
        self.lock = threading.Lock()

    def _run(self, sequences):
        np = self.np
        size = max(map(len, sequences))
        ids = np.full((len(sequences), size), self.pad, dtype=np.int64)
        mask = np.zeros_like(ids)
        for position, sequence in enumerate(sequences):
            ids[position, : len(sequence)] = sequence
            mask[position, : len(sequence)] = 1
        values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)}
        inputs = {item.name: values[item.name] for item in self.session.get_inputs()}
        with self.lock:
            output = self.session.run(None, inputs)[0]
        vectors = output[:, 0, :] if output.ndim == 3 else output
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (
            vectors.shape != (len(sequences), self.dimension)
            or not np.isfinite(vectors).all()
            or (norms <= 0).any()
        ):
            raise ValueError("Invalid embedding output")
        return (vectors / norms).astype("<f4")

    def passages(self, text):
        encoding = self.tokenizer.encode(text, add_special_tokens=False)
        if not encoding.ids:
            raise ValueError("Passage contains no model tokens")
        windows = []
        start = 0
        while start < len(encoding.ids):
            end = min(start + WINDOW, len(encoding.ids))
            windows.append(
                {
                    "token_start": start,
                    "token_end": end,
                    "char_start": encoding.offsets[start][0],
                    "char_end": encoding.offsets[end - 1][1],
                }
            )
            if end == len(encoding.ids):
                break
            start = end - OVERLAP
        vectors = []
        for offset in range(0, len(windows), 8):
            batch = windows[offset : offset + 8]
            sequences = [
                [self.cls, *encoding.ids[w["token_start"] : w["token_end"]], self.sep]
                for w in batch
            ]
            vectors.extend(self._run(sequences))
        return windows, self.np.asarray(vectors, dtype="<f4")

    def query(self, text):
        ids = self.tokenizer.encode(INSTRUCTION + text, add_special_tokens=False).ids
        if len(ids) > WINDOW:
            raise ValueError("Query exceeds the model's 510-token content budget")
        return self._run([[self.cls, *ids, self.sep]])[0]
