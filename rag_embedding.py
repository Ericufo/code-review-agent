"""Local CPU embeddings behind a replaceable, versioned batch interface."""
from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path
from typing import Protocol

from repository_chunks import identity
from repository_context import ContextError

MODEL = 'sentence-transformers/all-MiniLM-L6-v2'
REVISION = '1110a243fdf4706b3f48f1d95db1a4f5529b4d41'
FILES = {
    'onnx/model.onnx': '6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452',
    'tokenizer.json': 'be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037',
}
DEFAULT_MODEL_DIR = Path(__file__).parent / '.rag-models' / 'minilm'


@dataclass(frozen=True)
class EmbeddingSpec:
    model: str
    revision: str
    dimensions: int
    preprocessing: str

    @property
    def key(self):
        return identity(asdict(self))


class Embedder(Protocol):
    spec: EmbeddingSpec

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedder:
    """Loads verified local files only. Long inputs use explicit token windows.

    Window means are weighted by attention-mask length then L2 normalized.
    This is a versioned baseline policy, not silent model truncation.
    """
    spec = EmbeddingSpec(MODEL, REVISION, 384, 'onnx-mean-mask-window254-weighted-l2-v1')

    def __init__(self, directory=DEFAULT_MODEL_DIR):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        directory = Path(directory)
        for name, expected in FILES.items():
            path = directory / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ContextError('Embedding files missing or changed; run scripts/download_embedding.py')
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(directory / 'onnx/model.onnx'), options,
                                           providers=['CPUExecutionProvider'])

    def embed(self, texts):
        np = self.np
        if (not 1 <= len(texts) <= 128 or any(not isinstance(t, str) or not t.strip()
                or len(t) > 12000 for t in texts)):
            raise ContextError('Embedding input exceeds batch/text budget or is empty')
        result = []
        for text in texts:
            tokens = self.tokenizer.encode(text, add_special_tokens=False).ids
            if len(tokens) > 8192:
                raise ContextError('Embedding token window budget exhausted')
            aggregate, count = np.zeros(384, dtype=np.float64), 0
            for offset in range(0, len(tokens), 254):
                ids = np.array([[101, *tokens[offset:offset + 254], 102]], dtype=np.int64)
                feed = {'input_ids': ids, 'attention_mask': np.ones_like(ids),
                        'token_type_ids': np.zeros_like(ids)}
                output = self.session.run(None, {x.name: feed[x.name] for x in self.session.get_inputs()})[0]
                aggregate += output[0].sum(axis=0)
                count += ids.shape[1]
            if not count:
                raise ContextError('Embedding tokenizer returned no tokens')
            aggregate /= count
            norm = np.linalg.norm(aggregate)
            if not np.isfinite(norm) or norm == 0:
                raise ContextError('Embedding returned an invalid vector')
            result.append((aggregate / norm).tolist())
        return result
