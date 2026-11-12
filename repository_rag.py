"""Exact pgvector retrieval over complete, immutable revision manifests."""
from dataclasses import asdict
import json
import math
from time import monotonic

from sqlalchemy import text

from rag_embedding import Embedder
from repository_chunks import ChunkConfig, SourceChunk, build_manifest, identity, read_chunk
from repository_context import ContextError

RAG_CHUNKS = ChunkConfig(max_lines=40, max_chars=1200)


def vector_literal(values, dimensions):
    if (len(values) != dimensions or any(not isinstance(v, (float, int))
            or not math.isfinite(v) for v in values) or not any(values)):
        raise ContextError('Embedding dimensions or values are invalid')
    return '[' + ','.join(str(float(v)) for v in values) + ']'


class VectorStore:
    def __init__(self, engine):
        self.engine = engine

    def initialize(self):
        """Explicit opt-in schema; does not alter the existing review-task migration."""
        with self.engine.begin() as conn:
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            conn.execute(text('''CREATE TABLE IF NOT EXISTS review_rag_indexes (
                id text PRIMARY KEY, descriptor jsonb NOT NULL,
                chunks integer NOT NULL, build_ms double precision NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now())'''))
            conn.execute(text('''CREATE TABLE IF NOT EXISTS review_rag_chunks (
                index_id text NOT NULL REFERENCES review_rag_indexes(id) ON DELETE CASCADE,
                ordinal integer NOT NULL, chunk jsonb NOT NULL, embedding vector NOT NULL,
                PRIMARY KEY(index_id, ordinal))'''))

    def exists(self, key, descriptor):
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT descriptor, chunks FROM review_rag_indexes WHERE id=:id'),
                               {'id': key}).first()
            if row is None:
                return False
            count = conn.execute(text('SELECT count(*) FROM review_rag_chunks WHERE index_id=:id'),
                                 {'id': key}).scalar_one()
            if row.descriptor != descriptor or count != row.chunks:
                raise ContextError('Stored index is inconsistent; rebuild in a new database')
            return True

    def publish(self, key, descriptor, chunks, vectors, elapsed):
        if len(chunks) != len(vectors):
            raise ContextError('Embedding batch returned the wrong number of vectors')
        dimensions = descriptor['embedding']['dimensions']
        rows = [{'id': key, 'ordinal': i, 'chunk': json.dumps(asdict(chunk)),
                 'embedding': vector_literal(vector, dimensions)}
                for i, (chunk, vector) in enumerate(zip(chunks, vectors))]
        with self.engine.begin() as conn:
            # Serialize publication of one immutable identity; never expose half an index.
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(key[:15], 16)})
            existing = conn.execute(text('SELECT descriptor FROM review_rag_indexes WHERE id=:id'),
                                    {'id': key}).scalar_one_or_none()
            if existing is not None:
                if existing != descriptor:
                    raise ContextError('Index identity collision')
                return
            conn.execute(text('''INSERT INTO review_rag_indexes(id,descriptor,chunks,build_ms)
                VALUES (:id,CAST(:descriptor AS jsonb),:chunks,:elapsed)'''),
                {'id': key, 'descriptor': json.dumps(descriptor), 'chunks': len(rows), 'elapsed': elapsed})
            if rows:
                conn.execute(text('''INSERT INTO review_rag_chunks(index_id,ordinal,chunk,embedding)
                    VALUES (:id,:ordinal,CAST(:chunk AS jsonb),CAST(:embedding AS vector))'''), rows)

    def search(self, key, vector, dimensions, limit):
        with self.engine.connect() as conn:
            return conn.execute(text('''SELECT chunk, embedding <=> CAST(:vector AS vector) AS distance
                FROM review_rag_chunks WHERE index_id=:id
                ORDER BY distance, ordinal LIMIT :limit'''),
                {'id': key, 'vector': vector_literal(vector, dimensions), 'limit': limit}).all()


class RepositoryRetriever:
    def __init__(self, context, embedder: Embedder, store: VectorStore, *,
                 before_work=lambda: None, timeout=120, max_chunks=4000):
        self.context, self.embedder, self.store = context, embedder, store
        self.before_work, self.timeout, self.max_chunks = before_work, timeout, max_chunks
        self.indexes = {}

    def prepare(self, version, check=lambda: None):
        started = monotonic()
        def boundary():
            self.before_work()
            check()
            if monotonic() - started >= self.timeout:
                raise ContextError('Index time budget exhausted')
        boundary()
        manifest = build_manifest(self.context, version, config=RAG_CHUNKS)
        descriptor = {'manifest_id': manifest.manifest_id, 'repository': manifest.repository,
                      'version': version, 'revision': manifest.revision,
                      'embedding': asdict(self.embedder.spec), 'schema': 1}
        key = identity(descriptor)
        if version in self.indexes and self.indexes[version] != key:
            raise ContextError('Repository index changed during review')
        chunks = tuple(c for c in manifest.chunks if c.text.strip())
        if len(chunks) > self.max_chunks:
            raise ContextError('Index chunk budget exhausted')
        boundary()
        cached = self.store.exists(key, descriptor)
        if not cached:
            vectors = []
            for start in range(0, len(chunks), 16):
                boundary()
                batch = chunks[start:start + 16]
                encoded = self.embedder.embed([c.text for c in batch])
                if len(encoded) != len(batch):
                    raise ContextError('Embedding batch returned the wrong number of vectors')
                vectors.extend(encoded)
            boundary()
            self.store.publish(key, descriptor, chunks, vectors, (monotonic() - started) * 1000)
        self.indexes[version] = key
        return key, {'index_cache_hit': int(cached), 'indexed_chunks': len(chunks)}

    def search(self, query, version='head', top_k=5, check=lambda: None):
        if not query.strip() or len(query) > 512 or not 1 <= top_k <= 10:
            raise ContextError('Retrieval query or candidate budget is invalid')
        key, stats = self.prepare(version, check)
        check()
        self.before_work()
        vectors = self.embedder.embed([query])
        if len(vectors) != 1:
            raise ContextError('Embedding batch returned the wrong number of vectors')
        rows = self.store.search(key, vectors[0], self.embedder.spec.dimensions, top_k + 1)
        hits = []
        for row in rows[:top_k]:
            chunk = SourceChunk(**row.chunk)
            source = read_chunk(self.context, chunk, version=version)
            hits.append((chunk, source))
        check()
        return hits, len(rows) > top_k, key, stats
