"""Offline, version-bound Python chunks; no embedding, storage or model calls."""
import ast
from dataclasses import asdict, dataclass
import hashlib
import json
import sys

from repository_context import (
    ContextError, RepositoryContext, MAX_SOURCE_FILES, MAX_SNAPSHOT_BYTES,
    decode_source, validate_path,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def identity(value) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')))


@dataclass(frozen=True)
class ChunkConfig:
    max_lines: int = 80
    max_chars: int = 6000

    def __post_init__(self):
        if (type(self.max_lines) is not int or not 1 <= self.max_lines <= 120
                or type(self.max_chars) is not int or not 1 <= self.max_chars <= 12000):
            raise ContextError('Chunk limits must be integers: lines 1..120, characters 1..12000')

    @property
    def version(self):
        return f'python-ast-lines-v1:py{sys.version_info.major}.{sys.version_info.minor}'


@dataclass(frozen=True)
class SourceChunk:
    repository: str
    version: str
    revision: str
    path: str
    start_line: int
    end_line: int
    source_hash: str
    content_hash: str
    chunker: str
    symbol: str | None
    text: str

    @property
    def chunk_id(self):
        return identity(asdict(self))


@dataclass(frozen=True)
class ChunkManifest:
    repository: str
    version: str
    revision: str
    config: ChunkConfig
    # Includes empty Python files. Non-Python sources remain on literal tools.
    files: tuple[tuple[str, str], ...]
    chunks: tuple[SourceChunk, ...]

    @property
    def manifest_id(self):
        return identity({'repository': self.repository, 'version': self.version,
                         'revision': self.revision, 'chunker': self.config.version,
                         'config': asdict(self.config), 'files': self.files,
                         'chunks': [chunk.chunk_id for chunk in self.chunks]})


def _regions(source):
    """Partition physical lines without unparse, duplicated lines or lost comments."""
    # Match Python's physical lines; str.splitlines also splits form feed and
    # Unicode separators inside strings, which are not AST line boundaries.
    lines = source.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    if lines[-1] == '':
        lines.pop()
    boundaries = {1: None, len(lines) + 1: None}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        tree = None

    def definitions(body, prefix=''):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                name = prefix + node.name
                boundaries[start] = name
                boundaries.setdefault(node.end_lineno + 1, None)
                if isinstance(node, ast.ClassDef):
                    definitions(node.body, name + '.')
    if tree is not None:
        definitions(tree.body)
    points = sorted(boundaries)
    return [(start, end - 1, boundaries[start]) for start, end in zip(points, points[1:])]


def chunk_source(repository: str, version: str, revision: str, path: str,
                 source: str, config: ChunkConfig = ChunkConfig()) -> tuple[SourceChunk, ...]:
    validate_path(path)
    if not path.lower().endswith('.py'):
        raise ContextError('Only Python sources are indexed in this stage')
    if version not in {'head', 'base'} or not repository or not revision:
        raise ContextError('Chunks require repository, head/base and pinned revision')
    decode_source(source.encode('utf-8'))
    # keepends preserves CRLF and trailing newline exactly in the stored text.
    import io
    lines = list(io.StringIO(source, newline=''))
    source_hash = digest(source)
    chunker = config.version + ':' + identity(asdict(config))
    chunks = []
    for region_start, region_end, symbol in _regions(source):
        start = region_start
        while start <= region_end:
            end, size = start, 0
            while end <= region_end and end - start < config.max_lines:
                next_size = len(lines[end - 1])
                if size + next_size > config.max_chars:
                    break
                size += next_size
                end += 1
            if end == start:
                raise ContextError('A source line exceeds the chunk character budget')
            text = ''.join(lines[start - 1:end - 1])
            chunks.append(SourceChunk(repository, version, revision, path, start, end - 1,
                                      source_hash, digest(text), chunker, symbol, text))
            start = end
    return tuple(chunks)


def build_manifest(context: RepositoryContext, version: str = 'head', *,
                   config: ChunkConfig = ChunkConfig()) -> ChunkManifest:
    """Build completely or raise; callers must never publish a partial manifest."""
    if version not in {'head', 'base'} or version not in context.revisions:
        raise ContextError('Requested pinned version is unavailable')
    context.ensure_unchanged()
    repository, revision = context.repository, context.revisions[version]
    paths = tuple(sorted(context.paths(version)))
    if len(paths) > MAX_SOURCE_FILES:
        raise ContextError('Repository exceeds snapshot file budget')
    files, chunks, total = [], [], 0
    for path in paths:
        validate_path(path)
        if not path.lower().endswith('.py'):
            continue
        source = context.read(path, version)
        total += len(source.encode('utf-8'))
        if total > MAX_SNAPSHOT_BYTES:
            raise ContextError('Repository exceeds snapshot byte budget')
        chunks.extend(chunk_source(repository, version, revision, path, source, config))
        files.append((path, digest(source)))
    context.ensure_unchanged()
    if (context.repository != repository or context.revisions.get(version) != revision
            or tuple(sorted(context.paths(version))) != paths):
        raise ContextError('Source identity changed during chunk construction')
    return ChunkManifest(repository, version, revision, config, tuple(files), tuple(chunks))


def read_chunk(context: RepositoryContext, chunk: SourceChunk, *, version: str) -> str:
    """Resolve candidates against pinned source; does not register review evidence."""
    if (version not in {'head', 'base'} or chunk.version != version
            or chunk.repository != context.repository
            or chunk.revision != context.revisions.get(version)):
        raise ContextError('Chunk repository or revision does not match requested snapshot')
    context.ensure_unchanged()
    source = context.read(chunk.path, version)
    if digest(source) != chunk.source_hash:
        raise ContextError('Chunk source is stale; rebuild the index')
    import io
    lines = list(io.StringIO(source, newline=''))
    if not 1 <= chunk.start_line <= chunk.end_line <= len(lines):
        raise ContextError('Chunk range is outside the pinned source')
    text = ''.join(lines[chunk.start_line - 1:chunk.end_line])
    if text != chunk.text or digest(text) != chunk.content_hash:
        raise ContextError('Chunk content does not match pinned source lines')
    return text
