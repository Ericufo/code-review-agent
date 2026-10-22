"""Read-only, bounded source snapshots. No model-supplied shell commands or URLs."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from urllib.parse import quote

MAX_SOURCE_BYTES = 128_000
MAX_SNAPSHOT_BYTES = 2_000_000
MAX_SOURCE_FILES = 500
SOURCE_SUFFIXES = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.cs', '.go', '.rs',
                   '.c', '.h', '.cpp', '.hpp', '.rb', '.php', '.swift', '.kt', '.sql'}
BLOCKED_PARTS = {'.git', '.env', '.aws', '.ssh', '.venv', 'node_modules', 'vendor',
                 'secrets', 'credentials', '__pycache__'}


class ContextError(RuntimeError):
    pass


def validate_path(path: str) -> str:
    if (not path or '\\' in path or ':' in path or '\x00' in path
            or any(ord(c) < 32 for c in path) or path.startswith('/')
            or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ContextError('Only normalized repository-relative paths are allowed')
    if any(part.casefold() in BLOCKED_PARTS or part.startswith('.') for part in path.split('/')):
        raise ContextError('Path is outside the allowed source scope')
    name = PurePosixPath(path).name.casefold()
    if (PurePosixPath(path).suffix.casefold() not in SOURCE_SUFFIXES
            or any(word in name for word in ('secret', 'credential', 'private_key'))):
        raise ContextError('Only allowlisted source files may be read')
    return path


def allowed_path(path: str) -> bool:
    try:
        validate_path(path)
        return True
    except ContextError:
        return False


def decode_source(data: bytes) -> str:
    if len(data) > MAX_SOURCE_BYTES or b'\x00' in data:
        raise ContextError('Source is binary or exceeds the file byte limit')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ContextError('Source must be UTF-8 text') from exc


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=10)
    if result.returncode:
        raise ContextError('Cannot capture Git repository snapshot')
    return result.stdout


class RepositoryContext:
    def __init__(self, repository: str, revisions: dict[str, str],
                 files: dict[str, dict[str, str]], *, local_root: Path | None = None):
        self.repository = repository
        self.revisions = dict(revisions)
        self.files = {version: dict(contents) for version, contents in files.items()}
        self.local_root = local_root

    def paths(self, version: str) -> list[str]:
        if version not in self.files:
            raise ContextError('Requested version is unavailable')
        return sorted(self.files[version])

    def read(self, path: str, version: str) -> str:
        validate_path(path)
        if path not in self.paths(version):
            raise ContextError('File is unavailable in this version or excluded by source policy')
        return self.files[version][path]

    def ensure_unchanged(self) -> None:
        if self.local_root is not None:
            fresh = capture_local_context(self.local_root)
            if fresh.revisions != self.revisions:
                raise ContextError('Local source snapshot changed during review; rerun required')


def capture_local_context(root: Path | None = None) -> RepositoryContext:
    root = Path(git(root or Path.cwd(), 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    head = git(root, 'rev-parse', 'HEAD').decode().strip()
    entries = git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    names = sorted(set(entries.decode('utf-8').split('\0')) - {''})
    sources = {}
    total = 0
    for name in names:
        if not allowed_path(name):
            continue
        path = root / name
        # lstat every component: reject symlinks and Windows junction/reparse points.
        parts = [root.joinpath(*PurePosixPath(name).parts[:i])
                 for i in range(1, len(PurePosixPath(name).parts) + 1)]
        try:
            stats = [part.lstat() for part in parts]
        except FileNotFoundError:
            continue  # Deleted tracked file; HEAD remains available as base.
        if any(stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400 for s in stats):
            raise ContextError('Links/reparse points in source scope are not supported')
        before = stats[-1]
        if not stat.S_ISREG(before.st_mode) or not path.resolve().is_relative_to(root):
            raise ContextError('Source path is not a regular repository file')
        if before.st_size > MAX_SOURCE_BYTES:
            raise ContextError('Source file exceeds snapshot byte limit')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise ContextError('Source changed while snapshot was being captured')
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if any(part.is_symlink() or (getattr(part.lstat(), 'st_file_attributes', 0) & 0x400)
               for part in parts):
            raise ContextError('Source link changed during capture')
        after = path.stat()
        if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
            raise ContextError('Source changed while snapshot was being captured')
        sources[name] = decode_source(data)
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES or len(sources) > MAX_SOURCE_FILES:
            raise ContextError('Repository exceeds snapshot size/file budget')
    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    base = {}
    total = 0
    for record in git(root, 'ls-tree', '-r', '-z', '--full-tree', head).split(b'\0'):
        if not record:
            continue
        metadata, raw_name = record.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode('utf-8')
        if not allowed_path(name) or mode not in ('100644', '100755') or kind != 'blob':
            continue
        size = int(git(root, 'cat-file', '-s', oid))
        total += size
        if size > MAX_SOURCE_BYTES or total > MAX_SNAPSHOT_BYTES or len(base) >= MAX_SOURCE_FILES:
            raise ContextError('Base source exceeds snapshot budget')
        base[name] = decode_source(git(root, 'cat-file', 'blob', oid))
    if git(root, 'rev-parse', 'HEAD').decode().strip() != head:
        raise ContextError('HEAD changed while snapshot was being captured')
    return RepositoryContext(root.as_uri(), {'head': f'workspace:{head}:{fingerprint}', 'base': head},
                             {'head': sources, 'base': base}, local_root=root)


class GitHubRepositoryContext(RepositoryContext):
    """Tree metadata pins all blob reads to exact head/base objects; never to a branch."""
    def __init__(self, client, revision):
        for sha in (revision.head_sha, revision.base_sha):
            if re.fullmatch(r'[0-9a-f]{40}', sha) is None:
                raise ContextError('Invalid commit SHA')
        super().__init__(f'{client.owner}/{client.repo}',
                         {'head': revision.head_sha, 'base': revision.base_sha}, {})
        self.client = client
        self.api = f'{client.base_url}/repos/{quote(client.owner, safe="")}/{quote(client.repo, safe="")}/git'
        self.entries = {}
        self.cache = {}
        for version, sha in self.revisions.items():
            tree = client._get(f'{self.api}/trees/{sha}?recursive=1')
            if not isinstance(tree, dict) or tree.get('truncated') is not False or not isinstance(tree.get('tree'), list):
                raise ContextError('Repository tree is incomplete')
            entries = {}
            total = 0
            for entry in tree['tree']:
                path = entry.get('path', '')
                if not allowed_path(path) or entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755'):
                    continue
                size, oid = entry.get('size'), entry.get('sha')
                if type(size) is not int or size < 0 or not isinstance(oid, str) or re.fullmatch(r'[0-9a-f]{40}', oid) is None:
                    raise ContextError('Invalid source metadata')
                total += size
                if size > MAX_SOURCE_BYTES or total > MAX_SNAPSHOT_BYTES or len(entries) >= MAX_SOURCE_FILES:
                    raise ContextError('Repository exceeds snapshot size/file budget')
                entries[path] = (oid, size)
            self.entries[version] = entries

    def paths(self, version: str) -> list[str]:
        if version not in self.entries:
            raise ContextError('Requested version is unavailable')
        return sorted(self.entries[version])

    def read(self, path: str, version: str) -> str:
        validate_path(path)
        if path not in self.paths(version):
            raise ContextError('File is unavailable in this version or excluded by source policy')
        oid, expected_size = self.entries[version][path]
        if oid not in self.cache:
            blob = self.client._get(f'{self.api}/blobs/{oid}')
            if blob.get('encoding') != 'base64' or blob.get('size') != expected_size:
                raise ContextError('Invalid blob response')
            encoded = blob.get('content', '')
            if len(encoded) > MAX_SOURCE_BYTES * 2:
                raise ContextError('Blob response exceeds size budget')
            data = base64.b64decode(''.join(encoded.split()), validate=True)
            actual_oid = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            if len(data) != expected_size or actual_oid != oid:
                raise ContextError('Blob does not match pinned Git object')
            self.cache[oid] = decode_source(data)
        return self.cache[oid]
