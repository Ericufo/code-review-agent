"""Real CPU model + isolated pgvector/API acceptance. Never reads .env files."""
import json
import argparse
import os
from pathlib import Path
import secrets
import re
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'pgvector/pgvector@sha256:9d9c930220cb9bf2f956d10a8f909cf9973d9672ca0278aef2c1e6facccad2e0'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-plan', type=Path, help='Explicitly spend generation API calls after offline checks pass')
    parser.add_argument('--key-env-file', type=Path, help='Explicit credential file for live only; never logged')
    parser.add_argument('--live-only', action='store_true', help='Use only after successful offline acceptance; fresh isolated vector DB')
    parser.add_argument('--probe-guards', action='store_true', help='Verify key tests detect deliberate in-process faults; no source files are changed')
    args = parser.parse_args()
    if args.key_env_file and not args.live_plan:
        parser.error('--key-env-file requires --live-plan')
    if args.live_only and not args.live_plan:
        parser.error('--live-only requires --live-plan')
    if args.probe_guards and args.live_plan:
        parser.error('--probe-guards is offline-only; do not combine with live generation')
    name = 'review-v45-test-' + uuid4().hex[:12]
    output = ROOT / 'reports' / name
    output.mkdir(parents=True)
    password = secrets.token_hex(24)
    env = {**os.environ, 'POSTGRES_PASSWORD': password}
    created = subprocess.run(['docker', 'run', '-d', '--name', name, '--label', 'review.rag.test=' + name,
        '--tmpfs', '/var/lib/postgresql:rw', '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_USER=review_test',
        '-e', 'POSTGRES_DB=review_test_admin', '-p', '127.0.0.1::5432', IMAGE], env=env,
        capture_output=True, text=True, check=True, timeout=60)
    container = created.stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container):
        raise RuntimeError('Docker returned an invalid container identity; inspect the test container')
    results = []
    try:
        for _ in range(40):
            ready = subprocess.run(['docker', 'exec', container, 'pg_isready', '-U', 'review_test',
                                    '-d', 'review_test_admin'], capture_output=True, timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Isolated pgvector database did not become ready')
        binding = subprocess.run(['docker', 'port', container, '5432/tcp'],
                                 capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        port = int(binding.rsplit(':', 1)[1])
        url = f'postgresql+psycopg://review_test:{password}@127.0.0.1:{port}/review_test_admin'
        env.update(REVIEW_TEST_DATABASE_URL=url, REVIEW_RAG_DATABASE_URL=url,
                   REVIEW_RAG_ACCEPTANCE='1', GITHUB_TOKEN='', DEEPSEEK_API_KEY='', REVIEW_MODEL_MODE='mock')
        commands = [('postgres', ['scripts/run_postgres_tests.py']),
                    ('rag', ['-m', 'unittest', 'discover', '-s', 'tests/rag', '-v']),
                    ('retrieval', ['-m', 'evals.retrieval', '--output', str(output / 'retrieval.json')])]
        if args.live_only:
            commands = []
        if args.probe_guards:
            commands.append(('guard-probes', ['tests/rag/probe_guards.py']))
        for label, command_args in commands:
            with (output / (label + '.log')).open('w', encoding='utf-8') as log:
                result = subprocess.run([sys.executable, *command_args], cwd=ROOT, env=env,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=600)
            results.append({'check': label, 'exit_code': result.returncode})
            print(f'{label}: exit {result.returncode}', flush=True)
        if args.live_plan and not any(r['exit_code'] for r in results):
            from dotenv import dotenv_values
            live_key = (dotenv_values(args.key_env_file).get('DEEPSEEK_API_KEY', '')
                        if args.key_env_file else os.getenv('DEEPSEEK_API_KEY', ''))
            if not live_key:
                raise RuntimeError('Live key unavailable; no generation requests sent')
            with (output / 'live.log').open('w', encoding='utf-8') as log:
                result = subprocess.run([sys.executable, '-m', 'evals.live', '--plan', str(args.live_plan),
                    '--live', '--output', str(output / 'live')], cwd=ROOT,
                    env={**env, 'DEEPSEEK_API_KEY': live_key}, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            results.append({'check': 'live', 'exit_code': result.returncode})
            print(f'live: exit {result.returncode}', flush=True)
        (output / 'summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(f'Evidence: {output}', flush=True)
        return int(any(r['exit_code'] for r in results))
    finally:
        # Only remove the exact container created above after confirming its label.
        owner = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "review.rag.test"}}',
                                container], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        if owner != name:
            raise RuntimeError('Test container ownership changed; refusing cleanup')
        subprocess.run(['docker', 'rm', '-f', container], capture_output=True, check=True, timeout=30)


if __name__ == '__main__':
    raise SystemExit(main())
