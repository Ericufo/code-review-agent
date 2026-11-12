"""Real local-embedding dev retrieval comparison. No generation-model calls.

Queries use changed source only. Labels are read after ranking to score coverage.
This is retrieval evidence, never precision/recall of generated review findings.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
from time import perf_counter

from evals.dataset import load_cases, dataset_manifest
from rag_embedding import LocalEmbedder
from repository_chunks import build_manifest
from repository_rag import RAG_CHUNKS, RepositoryRetriever, VectorStore
from review_api.db import create_database_engine


def query_from_diffs(diffs):
    # No case ID, label, rationale, trigger or gold evidence enters this query.
    added = '\n'.join(line[1:] for diff in diffs for line in diff.diff.splitlines()
                      if line.startswith('+') and not line.startswith('+++'))
    return added[:512].strip() or ' '.join(diff.path for diff in diffs)[:512]


def words(value):
    return set(re.findall(r'[a-zA-Z][a-zA-Z0-9]*', value.lower().replace('_', ' ')))


def run(engine, embedder):
    store = VectorStore(engine)
    store.initialize()
    cases = load_cases()
    records = []
    for case in cases:
        if case.split != 'dev':
            continue
        diffs, context = case.review_inputs()
        query = query_from_diffs(diffs)
        started = perf_counter()
        manifest = build_manifest(context, config=RAG_CHUNKS)
        terms = words(query)
        lexical = sorted(manifest.chunks,
                         key=lambda c: (-len(terms & words((c.symbol or '') + ' ' + c.text)), c.path, c.start_line))
        lexical = [c for c in lexical if terms & words((c.symbol or '') + ' ' + c.text)]
        lexical_ms = (perf_counter() - started) * 1000
        retriever = RepositoryRetriever(context, embedder, store)
        started = perf_counter()
        key, stats = retriever.prepare('head')
        build_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        hits, more, _, _ = retriever.search(query, top_k=10)
        retrieval_ms = (perf_counter() - started) * 1000
        # Ground truth enters ONLY after both rankings are fixed.
        relevant = {(e.path, e.start_line, e.end_line) for e in case.expectation.evidence if e.version == 'head'}
        for defect in case.expectation.defects:
            relevant.update((e.path, e.start_line, e.end_line) for e in defect.evidence if e.version == 'head')
        for mode, ranked, elapsed in [('lexical-symbol', lexical, lexical_ms),
                                      ('vector', [c for c, _ in hits], retrieval_ms)]:
            for k in (1, 3, 5):
                covered = sum(all(any(c.path == path and c.start_line <= line <= c.end_line
                                      for c in ranked[:k]) for line in range(start, end + 1))
                              for path, start, end in relevant)
                records.append({'case_id': case.case_id, 'mode': mode, 'k': k,
                    'relevant_ranges': len(relevant), 'covered_ranges': covered,
                    'recall': covered / len(relevant) if relevant else None,
                    'candidate_count': len(manifest.chunks), 'returned': len(ranked[:k]),
                    'duration_ms': elapsed, 'index_build_ms': build_ms if mode == 'vector' else 0,
                    'index_cache_hit': stats['index_cache_hit'] if mode == 'vector' else None,
                    'index_id': key if mode == 'vector' else None})
    summary = []
    for mode in ('lexical-symbol', 'vector'):
        for k in (1, 3, 5):
            subset = [r for r in records if r['mode'] == mode and r['k'] == k]
            denominator = sum(r['relevant_ranges'] for r in subset)
            summary.append({'mode': mode, 'k': k, 'cases': len(subset),
                'recall_micro': sum(r['covered_ranges'] for r in subset) / denominator if denominator else None,
                'no_positive_cases': sum(r['relevant_ranges'] == 0 for r in subset)})
    return {'schema_version': 1, 'dataset_hash': dataset_manifest(cases)['dataset_hash'],
            'embedding': asdict(embedder.spec), 'query_policy': 'added-lines-first512-v1',
            'split': 'dev', 'embedding_api_cost_usd': 0, 'generation_calls': 0,
            'review_quality': None, 'summary': summary, 'records': records,
            'limitations': ['Small synthetic repositories; k can cover most or all chunks.',
                           'Labels were AI-reviewed; not independent human ground truth.',
                           'Recall is full-range coverage; lexical baseline is token/symbol overlap, not BM25.',
                           'No generation model evaluated; no final-review quality claim.',
                           'Holdout was historically observed; this run uses dev only.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output must be new')
    engine = create_database_engine(os.environ['REVIEW_RAG_DATABASE_URL'])
    try:
        result = run(engine, LocalEmbedder())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as output:
            json.dump(result, output, indent=2)
        print(json.dumps(result['summary'], indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
