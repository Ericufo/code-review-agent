"""Real model and real pgvector; only invoked by the isolated acceptance runner."""
from dataclasses import replace
import json
import os
import time
import unittest
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from context_review import review_with_context
from evals.mocks import reply
from rag_embedding import LocalEmbedder
from repository_context import ContextError, RepositoryContext
from repository_rag import RepositoryRetriever, VectorStore
from review_api.db import create_database_engine
from review_api.storage_types import StoredEvent
from review_tools import ToolSession, ContextBudget


class TestRag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getenv('REVIEW_RAG_ACCEPTANCE') != '1':
            raise RuntimeError('Run scripts/verify_rag.py for an isolated database')
        cls.engine = create_database_engine(os.environ['REVIEW_RAG_DATABASE_URL'])
        cls.store = VectorStore(cls.engine)
        cls.store.initialize()
        cls.embedder = LocalEmbedder()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.ctx = RepositoryContext('test/' + uuid4().hex, {'head': 'a' * 40, 'base': 'b' * 40},
            {'head': {'cache.py': 'def expired(timestamp, now):\n    return now >= timestamp\n',
                      'paint.py': 'def paint_blue():\n    return "butterfly"\n'},
             'base': {'cache.py': 'def expired(timestamp, now):\n    return now > timestamp\n'}})
        self.ctx.retriever = RepositoryRetriever(self.ctx, self.embedder, self.store)

    def test_real_semantic_search_persists_and_reuses_versioned_index(self):
        hits, _, key, stats = self.ctx.retriever.search('cache timestamp expiration', top_k=1)
        self.assertEqual(hits[0][0].path, 'cache.py')
        self.assertEqual(stats['index_cache_hit'], 0)
        reopened = RepositoryRetriever(self.ctx, self.embedder, VectorStore(self.engine))
        again, _, same_key, stats = reopened.search('cache timestamp expiration', top_k=1)
        self.assertEqual(same_key, key)
        self.assertEqual(again, hits)
        self.assertEqual(stats['index_cache_hit'], 1)
        # A distinct query must change the result; constant/ignored embeddings
        # otherwise pass a test where the expected file is also the first row.
        insect, _, _, _ = reopened.search('colourful winged insect', top_k=1)
        self.assertEqual(insect[0][0].path, 'paint.py')
        old, _, old_key, _ = reopened.search('cache timestamp expiration', version='base', top_k=1)
        self.assertNotEqual(old_key, key)
        self.assertIn('now > timestamp', old[0][1])

    def test_model_identity_and_repository_are_isolated(self):
        key, _ = self.ctx.retriever.prepare('head')
        other = RepositoryContext('other/' + uuid4().hex, self.ctx.revisions, self.ctx.files)
        other_key, _ = RepositoryRetriever(other, self.embedder, self.store).prepare('head')
        self.assertNotEqual(key, other_key)
        # Change declared preprocessing to demonstrate the key changes even at the same dimension.
        class Alternate:
            spec = replace(self.embedder.spec, preprocessing='test-identity-only')
            embed = self.embedder.embed
        alternate, _ = RepositoryRetriever(self.ctx, Alternate(), self.store).prepare('head')
        self.assertNotEqual(key, alternate)

    def test_stale_or_tampered_index_is_not_returned_as_evidence(self):
        key, _ = self.ctx.retriever.prepare('head')
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE review_rag_chunks SET chunk=jsonb_set(chunk,'{text}','\"fake\"') WHERE index_id=:id"), {'id': key})
        with self.assertRaises(ContextError):
            self.ctx.retriever.search('timestamp', top_k=2)
        self.ctx.files['head']['cache.py'] = 'changed = True\n'
        with self.assertRaisesRegex(ContextError, 'changed'):
            self.ctx.retriever.search('timestamp')

    def test_embedding_failure_cannot_publish_partial_index(self):
        class Broken:
            spec = self.embedder.spec
            def embed(self, texts):
                raise ContextError('Injected embedding failure')
        with self.assertRaises(ContextError):
            RepositoryRetriever(self.ctx, Broken(), self.store).prepare('head')
        with self.engine.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM review_rag_indexes WHERE descriptor->>'repository'=:repo"),
                                 {'repo': self.ctx.repository}).scalar_one()
        self.assertEqual(count, 0)

    def test_database_failure_rolls_back_index_and_all_chunks_then_retry_succeeds(self):
        # Fail the second chunk in PostgreSQL, after the header and first chunk
        # were inserted. This exercises the real transaction, not a fake store.
        function = 'reject_rag_' + uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(text(f'''CREATE FUNCTION {function}() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                IF NEW.ordinal = 1 THEN RAISE EXCEPTION 'injected second chunk failure'; END IF;
                RETURN NEW; END $$'''))
            conn.execute(text(f'''CREATE TRIGGER {function} BEFORE INSERT ON review_rag_chunks
                FOR EACH ROW EXECUTE FUNCTION {function}()'''))
        try:
            with self.assertRaises(SQLAlchemyError):
                self.ctx.retriever.prepare('head')
            with self.engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT count(*) FROM review_rag_indexes WHERE descriptor->>'repository'=:repo"),
                    {'repo': self.ctx.repository}).scalar_one(), 0)
                self.assertEqual(conn.execute(text("SELECT count(*) FROM review_rag_chunks WHERE chunk->>'repository'=:repo"),
                    {'repo': self.ctx.repository}).scalar_one(), 0)
        finally:
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP TRIGGER {function} ON review_rag_chunks'))
                conn.execute(text(f'DROP FUNCTION {function}()'))
        hits, _, _, stats = self.ctx.retriever.search('colourful winged insect', top_k=2)
        self.assertEqual({chunk.path for chunk, _ in hits}, {'cache.py', 'paint.py'})
        self.assertEqual(stats['index_cache_hit'], 0)

    def test_search_returns_only_requested_repository_revision_and_model_index(self):
        self.ctx.retriever.prepare('head')
        self.ctx.retriever.prepare('base')
        foreign = RepositoryContext('foreign/' + uuid4().hex, self.ctx.revisions,
            {'head': {'foreign.py': 'def butterfly():\n    return "foreign repository data"\n'}})
        RepositoryRetriever(foreign, self.embedder, self.store).prepare('head')
        class Alternate:
            spec = replace(self.embedder.spec, preprocessing='isolation-test')
            embed = self.embedder.embed
        RepositoryRetriever(self.ctx, Alternate(), self.store).prepare('head')
        hits, more, _, _ = self.ctx.retriever.search('butterfly', top_k=10)
        self.assertFalse(more)
        self.assertEqual(len(hits), 2)
        self.assertEqual({chunk.path for chunk, _ in hits}, {'cache.py', 'paint.py'})
        self.assertTrue(all(chunk.repository == self.ctx.repository and chunk.version == 'head'
                            and chunk.revision == self.ctx.revisions['head'] for chunk, _ in hits))

    def test_budgeted_tool_registers_only_returned_evidence(self):
        session = ToolSession(self.ctx, ContextBudget(max_tool_calls=1))
        result = json.loads(session.execute('retrieve_code', '{"query":"cache timestamp","top_k":1}'))
        self.assertEqual(result['entries'][0]['evidence']['path'], 'cache.py')
        self.assertEqual(len(session.evidence), 1)
        self.assertEqual(session.budget.context_chars, session.used_chars)
        # Indexed but not returned source is not citable, even with a real path
        # and revision. Check the user-visible rejection, not just list length.
        from review_models import ContextReviewResult
        invented = ContextReviewResult(summary='', context_complete=True, context_reason='', issues=[{
            'file': 'cache.py', 'line': 2, 'severity': 'low', 'title': 'Unseen evidence',
            'description': 'Uses a source that was not supplied', 'evidence': [{
                'repository': self.ctx.repository, 'revision': self.ctx.revisions['head'],
                'path': 'paint.py', 'start_line': 1, 'end_line': 2}]}])
        with self.assertRaisesRegex(ContextError, 'actually returned'):
            session.validate_evidence(invented)
        with self.assertRaisesRegex(ContextError, 'budget'):
            session.execute('retrieve_code', '{"query":"paint"}')
        small = ToolSession(self.ctx, ContextBudget(max_context_chars=10))
        with self.assertRaisesRegex(ContextError, 'budget'):
            small.execute('retrieve_code', '{"query":"cache"}')
        self.assertEqual(small.evidence, [])

    def test_graph_uses_real_retrieval_and_records_source_free_index_metadata(self):
        events = []
        def request(inputs, instructions, *, tools_enabled=True, tools=None):
            self.assertIn('retrieve_code', [t['name'] for t in tools])
            outputs = [x for x in inputs if x.get('type') == 'function_call_output']
            if not outputs:
                return reply(calls=[{'type': 'function_call', 'call_id': 'rag1', 'name': 'retrieve_code',
                                     'arguments': '{"query":"cache timestamp expiration","top_k":1}'}])
            evidence = json.loads(outputs[0]['output'])['entries'][0]['evidence']
            return reply(issues=[{'file': 'cache.py', 'line': 2, 'severity': 'low', 'title': 'Fixture finding',
                                 'description': 'Transport exercise only', 'suggestion': '', 'evidence': [evidence]}])
        result = review_with_context('+return now >= timestamp', context=self.ctx, budget=ContextBudget(),
                                     request_fn=request, on_record=events.append)
        self.assertEqual(len(result.issues), 1)
        retrieval = next(e for e in events if e.node == 'retrieve_code')
        saved = StoredEvent.from_event(retrieval)
        self.assertEqual(saved.retrieval.returned_chunks, 1)
        self.assertNotIn('cache timestamp expiration', saved.model_dump_json())
        self.assertNotIn('return now', saved.model_dump_json())

    def test_tool_error_remains_incomplete_despite_clean_final_answer(self):
        def request(inputs, instructions, **kwargs):
            if any(x.get('type') == 'function_call_output' for x in inputs):
                return reply()
            return reply(calls=[{'type': 'function_call', 'call_id': 'rag1', 'name': 'retrieve_code',
                                 'arguments': '{"query":"   "}'}])
        with self.assertRaisesRegex(ContextError, 'incomplete'):
            review_with_context('+x', context=self.ctx, budget=ContextBudget(), request_fn=request)

    def test_long_input_is_windowed_and_unicode_lines_match_read_tool(self):
        vector = self.embedder.embed(['cache expiry ' * 400])[0]
        self.assertEqual(len(vector), 384)
        self.assertAlmostEqual(sum(v*v for v in vector), 1, places=5)
        # Identical prefixes beyond one window, semantically different suffixes.
        # Silent truncation would produce identical vectors and fail this check.
        prefix = 'cache ' * 300
        left, right = self.embedder.embed([prefix + 'violin melody orchestra ' * 200,
                                           prefix + 'rainforest wildlife habitat ' * 200])
        self.assertGreater(sum((a-b)**2 for a, b in zip(left, right)) ** .5, .1)
        self.ctx.files['head'] = {'a.py': 'x = "a\u2028b"\r\ndef f():\r\n    return x\r\n'}
        session = ToolSession(self.ctx, ContextBudget())
        literal = json.loads(session.execute('read_file', '{"path":"a.py","start_line":2,"end_line":3}'))
        self.assertEqual(literal['entries'][0]['text'], '2: def f():\n3:     return x')
        retrieved = json.loads(session.execute('retrieve_code', '{"query":"function f return x"}'))
        self.assertTrue(any(e['text'] == literal['entries'][0]['text'] for e in retrieved['entries']))

    def test_http_vector_task_persists_index_event_and_replays_after_restart(self):
        import base64
        import hashlib
        from unittest.mock import patch
        from alembic import command
        from fastapi.testclient import TestClient
        from rag_embedding import DEFAULT_MODEL_DIR
        from review_api.app import create_app
        from review_api.db import migration_config
        from review_api.settings import Settings
        from review_api.storage_types import StoredConfig
        with self.engine.begin() as conn:
            command.upgrade(migration_config(conn), 'head')
        # Only the external GitHub HTTP responses and generation are scripted.
        # The production provider must construct the retriever itself.
        source = b'def expired(now, timestamp):\n    return now >= timestamp\n'
        oid = hashlib.sha1(f'blob {len(source)}\0'.encode() + source).hexdigest()
        prefix = 'https://api.github.com/repos/test/repo/'
        fixtures = {
            prefix + 'pulls/1': {'state': 'open', 'draft': False, 'head': {'sha': 'a'*40}, 'base': {'sha': 'b'*40}},
            prefix + f'compare/{"b"*40}...{"a"*40}?per_page=1&page=1': {'files': [{
                'filename': 'a.py', 'patch': '@@ -0,0 +1,2 @@\n+def expired(now, timestamp):\n+    return now >= timestamp'}]},
            prefix + 'git/trees/' + 'a'*40 + '?recursive=1': {'truncated': False, 'tree': [{
                'path': 'a.py', 'type': 'blob', 'mode': '100644', 'sha': oid, 'size': len(source)}]},
            prefix + 'git/trees/' + 'b'*40 + '?recursive=1': {'truncated': False, 'tree': []},
            prefix + 'git/blobs/' + oid: {'encoding': 'base64', 'size': len(source),
                                         'content': base64.b64encode(source).decode()},
        }
        class Response:
            status_code = 200
            def __init__(self, data):
                self.data = data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def iter_content(self, **kwargs):
                yield json.dumps(self.data).encode()
        def github_response(url, **kwargs):
            return Response(fixtures[url])
        def request(inputs, instructions, **kwargs):
            if any(x.get('type') == 'function_call_output' for x in inputs):
                return reply()
            return reply(calls=[{'type': 'function_call', 'call_id': 'api-rag', 'name': 'retrieve_code',
                                 'arguments': '{"query":"timestamp expiry"}'}])
        settings = Settings(database_url=self.engine.url.render_as_string(hide_password=False),
            api_token='rag-test-token', allowed_repos={'test/repo'}, rag_model_dir=str(DEFAULT_MODEL_DIR),
            profiles={'default': StoredConfig(retrieval_mode='vector', embedding_id=self.embedder.spec.key)})
        key, headers = str(uuid4()), {'Authorization': 'Bearer rag-test-token'}
        body = {'owner': 'test', 'repo': 'repo', 'pr_number': 1}
        def client():
            return TestClient(create_app(settings, engine=self.engine, mock_request_fn=request),
                              headers=headers)
        with patch('review_api.providers.requests.get', side_effect=github_response), client() as http:
            submitted = http.post('/api/v1/reviews', json=body, headers={'Idempotency-Key': key})
            self.assertEqual(submitted.status_code, 202, submitted.text)
            task_id = submitted.json()['task_id']
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = http.get('/api/v1/reviews/' + task_id).json()
                if task['execution_status'] in ('finished', 'failed', 'interrupted'):
                    break
                time.sleep(.02)
            self.assertEqual(task['review_status'], 'COMPLETE', task)
            events = http.get(f'/api/v1/reviews/{task_id}/events').json()['items']
            saved = next(e for e in events if e['node'] == 'retrieve_code')
            self.assertEqual(saved['retrieval']['returned_chunks'], 1)
            with self.engine.connect() as conn:
                descriptor = conn.execute(text('SELECT descriptor FROM review_rag_indexes WHERE id=:id'),
                    {'id': saved['retrieval']['index_id']}).scalar_one()
            self.assertEqual(descriptor['repository'], 'test/repo')
            self.assertEqual(descriptor['revision'], 'a'*40)
        with patch('review_api.providers.requests.get', side_effect=AssertionError('Replay must not read upstream')), client() as http:
            replay = http.post('/api/v1/reviews', json=body, headers={'Idempotency-Key': key})
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json()['task_id'], task_id)
            self.assertEqual(http.get(f'/api/v1/reviews/{task_id}/events').json()['items'], events)
