"""Prove critical acceptance tests reject faults, in a disposable test DB.

Run via scripts/verify_rag.py --probe-guards. Mutations exist only in this
process and are restored after each probe. No production source files change.
"""
import inspect
import json
import os
from pathlib import Path
import sys
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import rag_embedding
import repository_rag
import test_rag
from review_tools import ToolSession
from review_api.db import create_database_engine
from sqlalchemy import text
from test_rag import TestRag


def changed(function, old, new):
    source = textwrap.dedent(inspect.getsource(function))
    if source.count(old) != 1:
        raise RuntimeError('Mutation target changed; update this explicit probe')
    namespace = {}
    exec(compile(source.replace(old, new), '<intentional guard probe>', 'exec'), function.__globals__, namespace)
    return namespace[function.__name__]


def main():
    if os.getenv('REVIEW_RAG_ACCEPTANCE') != '1':
        raise RuntimeError('Use the isolated acceptance runner')
    store = repository_rag.VectorStore
    truncated = changed(rag_embedding.LocalEmbedder.embed,
        'tokens = self.tokenizer.encode(text, add_special_tokens=False).ids',
        'tokens = self.tokenizer.encode(text, add_special_tokens=False).ids[:254]')
    unfiltered = changed(store.search, 'WHERE index_id=:id', '')
    early_commit = changed(store.publish,
        "'chunks': len(rows), 'elapsed': elapsed})",
        "'chunks': len(rows), 'elapsed': elapsed})\n        conn.commit()")
    hide_error = changed(test_rag.review_with_context, "if state['errors']:", 'if False:')
    # A committed header must survive if the transaction is mistakenly split.
    # Subsequent operations on the closed transaction fail, exposing that header.
    probes = [
        ('tail-truncation', 'test_long_input_is_windowed_and_unicode_lines_match_read_tool',
         patch.object(rag_embedding.LocalEmbedder, 'embed', truncated)),
        ('unscoped-query', 'test_search_returns_only_requested_repository_revision_and_model_index',
         patch.object(store, 'search', unfiltered)),
        ('partial-publication', 'test_database_failure_rolls_back_index_and_all_chunks_then_retry_succeeds',
         patch.object(store, 'publish', early_commit)),
        ('trust-cached-source', 'test_stale_or_tampered_index_is_not_returned_as_evidence',
         patch.object(repository_rag, 'read_chunk', lambda context, chunk, **kwargs: chunk.text)),
        ('accept-unseen-evidence', 'test_budgeted_tool_registers_only_returned_evidence',
         patch.object(ToolSession, 'validate_evidence', lambda self, result: None)),
        ('hide-tool-error', 'test_tool_error_remains_incomplete_despite_clean_final_answer',
         patch.object(test_rag, 'review_with_context', hide_error)),
    ]
    results = []
    expected = {
        'tail-truncation': 'not greater than 0.1',
        'unscoped-query': 'Chunk repository or revision does not match requested snapshot',
        'partial-publication': '1 != 0',
        'trust-cached-source': 'ContextError not raised',
        'accept-unseen-evidence': 'ContextError not raised',
        'hide-tool-error': 'ContextError not raised',
    }
    for label, method, mutation in probes:
        engine = create_database_engine(os.environ['REVIEW_RAG_DATABASE_URL'])
        try:
            with engine.begin() as conn:
                conn.execute(text('TRUNCATE review_rag_indexes CASCADE'))
        finally:
            engine.dispose()
        print('INTENTIONAL FAULT:', label, flush=True)
        with mutation:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([TestRag(method)]))
        failures = result.failures + result.errors
        detected = (result.testsRun == 1 and len(failures) == 1
                    and expected[label] in failures[0][1])
        results.append({'fault': label, 'detected': detected})
        if not detected:
            print(json.dumps(results), flush=True)
            return 1
    print('RESTORED IMPLEMENTATION: full real RAG suite', flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRag)
    expected_count = suite.countTestCases()
    restored = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = restored.wasSuccessful() and restored.testsRun == expected_count > 0
    print(json.dumps({'all_faults_detected': True, 'probes': results,
                      'restored_tests': restored.testsRun, 'restored_passed': passed}), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
