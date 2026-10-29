"""Offline Compose acceptance in a uniquely named disposable project.

Never loads .env/.env.compose/.env.pgtest. Creates random test-only credentials,
uses an internal network, mounts a fixed fixture, and deletes only its own volume.
Requires Docker Compose v2 and a host Python; no third-party host packages.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build', action='store_true', help='Use an already built current image')
    parser.add_argument('--image', default='code-review-agent:v4.4')
    args = parser.parse_args()
    project = 'review-v445-test-' + uuid4().hex[:12]
    assert re.fullmatch(r'review-v445-test-[0-9a-f]{12}', project)
    directory = ROOT / 'reports' / project
    directory.mkdir(parents=True, exist_ok=False)
    token, password = secrets.token_hex(32), secrets.token_hex(32)
    env_file = directory / '.env.test'
    env_file.write_text(f'POSTGRES_PASSWORD={password}\nREVIEW_API_TOKEN={token}\n'
        f'REVIEW_IMAGE={args.image}\nREVIEW_ALLOWED_REPOS=test/repo\nREVIEW_API_PORT=0\n'
        'REVIEW_MODEL_MODE=mock\nREVIEW_PROFILES_JSON={}\nGITHUB_TOKEN=\nDEEPSEEK_API_KEY=\n', encoding='utf-8')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COMPOSE_', 'REVIEW_', 'POSTGRES_'))
           and key not in {'DATABASE_URL', 'GITHUB_TOKEN', 'DEEPSEEK_API_KEY'}}
    override = directory / 'fixture.json'
    override.write_text(json.dumps({'services': {'api': {
        'command': ['python', '/testsupport/compose_fixture.py'],
        'environment': {'PYTHONPATH': '/app:/testsupport'},
        'volumes': [{'type': 'bind', 'source': str(ROOT / 'tests' / 'compose'),
                     'target': '/testsupport', 'read_only': True}]}},
        'networks': {'default': {'internal': True}}}), encoding='utf-8')
    failure_override = directory / 'migration-failure.json'
    failure_override.write_text(json.dumps({'services': {'migrate': {
        'environment': {'POSTGRES_PASSWORD': 'intentionally-wrong-test-password'}}}}), encoding='utf-8')
    normal_override = directory / 'normal.json'
    normal_override.write_text(json.dumps({'networks': {'default': {}}}), encoding='utf-8')
    log = directory / 'docker.log'
    checks = []
    owned = False
    normal_http = False

    def run(*arguments, expected=0, timeout=180, input_data=None):
        result = subprocess.run(['docker', *arguments], cwd=ROOT, env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout, input=input_data)
        output = result.stdout + result.stderr
        for secret in (token, password):
            output = output.replace(secret, '[REDACTED]')
        # Do not print docker inspect/config environments, even with random test secrets.
        if 'inspect' not in arguments and 'config' not in arguments:
            with log.open('a', encoding='utf-8') as handle:
                handle.write(output)
        if expected is not None and result.returncode != expected:
            raise RuntimeError(f'Docker command failed ({result.returncode}); see redacted docker.log')
        return result

    def compose(*arguments, extra=None, normal=False, **kwargs):
        files = ['-f', str(ROOT / 'compose.yaml'), '-f', str(normal_override if normal else override)]
        if extra is not None:
            files += ['-f', str(extra)]
        return run('compose', '--project-name', project, '--env-file', str(env_file), *files, *arguments, **kwargs)

    def check(name):
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def address():
        binding = compose('port', 'api', '8000').stdout.strip()
        if not binding.startswith('127.0.0.1:'):
            raise RuntimeError('Unexpected public API binding')
        return 'http://' + binding

    def http(path, *, body=None, key=None, base=None):
        if not normal_http:
            # Docker internal networks may suppress published ports. Exercise
            # real HTTP over container loopback while upstream egress is blocked.
            script = """import json, os, sys
from urllib.request import Request, urlopen
values=json.load(sys.stdin)
headers={'Authorization': 'Bearer '+os.environ['REVIEW_API_TOKEN']}
if values['key'] is not None: headers['Idempotency-Key']=values['key']
if values['body'] is not None: headers['Content-Type']='application/json'
request=Request('http://127.0.0.1:8000'+values['path'], headers=headers,
    data=json.dumps(values['body']).encode() if values['body'] is not None else None)
with urlopen(request, timeout=5) as response:
    print(json.dumps({'status': response.status, 'body': json.load(response)}))
"""
            reply = json.loads(compose('exec', '-T', 'api', 'python', '-c', script,
                input_data=json.dumps({'path': path, 'body': body, 'key': key})).stdout)
            return reply['status'], reply['body']
        headers = {'Authorization': 'Bearer ' + token}
        if key is not None:
            headers['Idempotency-Key'] = key
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request((base or address()) + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def wait_result(task_id, predicate, timeout=20):
        end, base = time.monotonic() + timeout, address() if normal_http else None
        while time.monotonic() < end:
            try:
                _, result = http(f'/api/v1/reviews/{task_id}/result', base=base)
                if predicate(result):
                    return result
            except (HTTPError, URLError, TimeoutError):
                pass
            time.sleep(.2)
        raise RuntimeError('Timed out waiting for the expected persisted result')

    def up(*, normal=False):
        compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '120', normal=normal)

    try:
        label = f'com.docker.compose.project={project}'
        if (run('ps', '-aq', '--filter', 'label=' + label).stdout.strip()
                or run('volume', 'ls', '-q', '--filter', 'label=' + label).stdout.strip()):
            raise RuntimeError('Refusing an existing test project')
        owned = True
        if not args.skip_build:
            print('Building application image...', flush=True)
            compose('build', 'api', timeout=1200)
        config = json.loads(compose('config', '--format', 'json').stdout)
        assert not config['services']['db'].get('ports')
        assert config['services']['api']['ports'][0]['host_ip'] == '127.0.0.1'
        assert config['services']['api']['depends_on']['migrate']['condition'] == 'service_completed_successfully'
        assert config['networks']['default']['internal'] is True
        assert config['services']['api']['environment']['REVIEW_MODEL_MODE'] == 'mock'
        assert not config['services']['api']['environment']['DEEPSEEK_API_KEY']
        assert json.loads(config['services']['api']['environment']['REVIEW_PROFILES_JSON']) == {}
        check('resolved configuration: private DB, loopback API, migration gate, mock and no egress')
        sources = [*ROOT.glob('*.py'), *(ROOT / 'review_api').glob('*.py'),
                   *(ROOT / 'migrations').rglob('*.py'), ROOT / 'alembic.ini',
                   ROOT / 'requirements.txt', ROOT / 'requirements-api.txt']
        hashes = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        audit = """from pathlib import Path
import os, hashlib
root=Path('/app')
assert os.getuid() != 0
assert not any(p.name.startswith('.env') or p.name in {'.git','.venv','docs','reports','experiments','evals','tests','__pycache__'} for p in root.rglob('*'))
assert (root/'alembic.ini').is_file() and (root/'review_api/app.py').is_file()
print('image filesystem and non-root audit passed')
"""
        audit += f'expected = {hashes!r}\n'
        audit += "assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in expected.items()), 'Image differs from current source; rebuild required'\n"
        run('run', '--rm', '--network', 'none', '--entrypoint', 'python', args.image, '-c', audit)
        image_config = json.loads(run('image', 'inspect', args.image).stdout)[0]['Config']
        assert not any(item.startswith(('POSTGRES_PASSWORD=', 'REVIEW_API_TOKEN=', 'GITHUB_TOKEN=', 'DEEPSEEK_API_KEY='))
                       for item in image_config['Env'])
        check('runtime image excludes credentials, personal material and fixtures; runs non-root')
        print('Starting fresh named volume...', flush=True)
        up()
        assert http('/health/ready')[0] == 200
        assert compose('ps', '-aq', 'migrate').stdout.strip()
        migration_id = compose('ps', '-aq', 'migrate').stdout.strip()
        assert run('inspect', '--format', '{{.State.ExitCode}}', migration_id).stdout.strip() == '0'
        check('empty-volume migration completes before healthy API')
        key = str(uuid4())
        status, task = http('/api/v1/reviews', key=key, body={'owner': 'test', 'repo': 'repo', 'pr_number': 1})
        assert status == 202 and task['model_mode'] == 'mock'
        task_id = task['task_id']
        saved = wait_result(task_id, lambda value: value['execution_status'] == 'finished')
        assert saved['review_status'] == 'COMPLETE' and len(saved['files']) == 1
        check('container HTTP submission and persisted mock result')
        compose('restart', 'api')
        up()
        assert wait_result(task_id, lambda value: not value['partial']) == saved
        check('ordinary API restart preserves exact result')
        _, crash_task = http('/api/v1/reviews', key=str(uuid4()), body={'owner': 'test', 'repo': 'repo', 'pr_number': 2})
        crash_id = crash_task['task_id']
        wait_result(crash_id, lambda value: value['execution_status'] == 'running' and len(value['files']) == 1)
        compose('kill', '-s', 'SIGKILL', 'api')
        up()
        partial = wait_result(crash_id, lambda value: value['execution_status'] == 'interrupted')
        assert len(partial['files']) == 1 and not partial['resource_records_complete']
        check('killed API preserves completed file and interrupts old work without resuming')
        compose('stop', 'api')
        compose('restart', 'db')
        up()
        assert wait_result(task_id, lambda value: not value['partial']) == saved
        check('database restart preserves persisted result')
        compose('down')  # Deliberately retain this project's named volume.
        volume = project + '_review-data'
        labels = json.loads(run('volume', 'inspect', volume).stdout)[0]['Labels']
        assert labels['com.docker.compose.project'] == project
        up()
        assert wait_result(task_id, lambda value: not value['partial']) == saved
        check('compose down/up retains named-volume data')
        compose('down')
        failed = compose('up', '-d', '--no-build', 'api', extra=failure_override, expected=None)
        assert failed.returncode != 0
        assert not compose('ps', '--status', 'running', '-q', 'api', extra=failure_override).stdout.strip()
        check('actual failed migration prevents API from running')
        # Restore real image entrypoint, with no fixture mount and no upstream calls.
        compose('down', extra=failure_override)
        up(normal=True)
        normal_http = True
        assert http('/health/ready')[0] == 200
        assert wait_result(task_id, lambda value: not value['partial']) == saved
        status, replay = http('/api/v1/reviews', key=key, body={'owner': 'test', 'repo': 'repo', 'pr_number': 1})
        assert status == 200 and replay['task_id'] == task_id
        check('production entrypoint is healthy and queries/replays old results without GitHub')
        (directory / 'result.json').write_text(json.dumps({'project': project, 'image': args.image,
            'checks': checks, 'passed': True}, indent=2), encoding='utf-8')
        print(f'Compose acceptance passed: {len(checks)} checks; report: {directory.relative_to(ROOT)}', flush=True)
        return 0
    except Exception:
        if owned:
            try:
                compose('logs', '--no-color', '--tail', '100', expected=None, timeout=30)
            except Exception:
                pass
        (directory / 'result.json').write_text(json.dumps({'project': project, 'image': args.image,
            'checks': checks, 'passed': False}, indent=2), encoding='utf-8')
        raise
    finally:
        if owned:
            try:
                # This random namespace was proven absent before any creation.
                volumes = run('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).stdout.splitlines()
                if any(name != project + '_review-data' for name in volumes):
                    raise RuntimeError('Unexpected volume in test namespace; refusing cleanup')
                compose('down', '--volumes', '--remove-orphans', timeout=120)
                assert not run('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).stdout.strip()
                assert not run('volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).stdout.strip()
                print('Removed only the generated test project and its volume: ' + project, flush=True)
            finally:
                env_file.unlink(missing_ok=True)
        else:
            env_file.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
