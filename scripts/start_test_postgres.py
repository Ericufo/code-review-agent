"""Start a dedicated disposable PostgreSQL container without native installation.

Writes random test credentials only to the ignored .env.pgtest file. Refuses to
overwrite an existing configuration or container. This is not production Compose.
"""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'postgres@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6'
NAME = 'code-review-agent-pgtest'


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], text=True, capture_output=True, check=True, **kwargs)


def write_test_config(config, password, port):
    # Apply owner-only permissions at creation on POSIX, with no readable window
    # before chmod. Exclusive creation also refuses existing files/symlinks.
    # Windows access remains governed by the directory's inherited ACL.
    with open(config, 'x', encoding='utf-8',
              opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
        handle.write(f'REVIEW_TEST_DATABASE_URL=postgresql+psycopg://review_test:{password}@127.0.0.1:{port}/review_test_admin\n')
        handle.write(f'REVIEW_TEST_CONTAINER={NAME}\n')


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    config = ROOT / '.env.pgtest'
    if config.exists():
        raise SystemExit('.env.pgtest already exists; use its existing test container and credentials.')
    # Check BEFORE creating anything. Never replace or delete another container.
    names = docker('ps', '-a', '--format', '{{.Names}}', timeout=15).stdout.splitlines()
    if NAME in names:
        raise SystemExit(f'{NAME} already exists; inspect it instead of recreating it.')
    password = secrets.token_hex(24)
    env = {**os.environ, 'POSTGRES_PASSWORD': password}
    docker('run', '--detach', '--name', NAME, '--label', 'code-review-agent.test=v442',
           '--tmpfs', '/var/lib/postgresql:rw', '-e', 'POSTGRES_USER=review_test',
           '-e', 'POSTGRES_DB=review_test_admin', '-e', 'POSTGRES_PASSWORD',
           '-p', '127.0.0.1::5432', IMAGE, env=env, timeout=300)
    # Save the credentials even if readiness later fails, so diagnosis/restart
    # can reuse the created container without inspecting its secret environment.
    port = None
    for _ in range(30):
        binding = docker('port', NAME, '5432/tcp', timeout=10).stdout.strip()
        if binding:
            port = int(binding.rsplit(':', 1)[1])
            break
        time.sleep(1)
    if port is None:
        raise SystemExit(f'No port binding for {NAME}; inspect Docker before retrying.')
    write_test_config(config, password, port)
    for _ in range(30):
        probe = subprocess.run(['docker', 'exec', NAME, 'pg_isready', '-h', '127.0.0.1', '-U', 'review_test', '-d', 'review_test_admin'],
                               capture_output=True, timeout=10)
        if probe.returncode == 0:
            print(f'PostgreSQL test container ready: {NAME}, local port {port}; credentials in .env.pgtest')
            return 0
        time.sleep(1)
    raise SystemExit(f'{NAME} did not become ready; inspect its logs. Credentials were saved in .env.pgtest.')


if __name__ == '__main__':
    raise SystemExit(main())
