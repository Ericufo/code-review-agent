import base64
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from github_review import PullRequestRevision
from repository_context import (ContextError, GitHubRepositoryContext,
                                capture_local_context, validate_path)


class TestRepositoryContext(unittest.TestCase):
    def test_rejects_absolute_traversal_hidden_secret_and_non_source_paths(self):
        for path in ('../a.py', '/a.py', 'C:/a.py', 'a\\b.py', 'a//b.py', './a.py',
                     '.env', '.git/config.py', 'secrets/key.py', 'credentials.py',
                     'a.py:stream', 'notes.md', 'a\x00.py', 'x/../a.py'):
            with self.subTest(path=path), self.assertRaises(ContextError):
                validate_path(path)
        self.assertEqual(validate_path('src/你好.py'), 'src/你好.py')

    def test_local_snapshot_copies_worktree_and_base_and_detects_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', '-C', directory, *args], capture_output=True, check=True)
            git('init')
            (root / 'app.py').write_text('old = 1\n', encoding='utf-8', newline='\n')
            (root / '.gitignore').write_text('.env\nignored.py\n', encoding='utf-8')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
            (root / 'app.py').write_text('new = 2\n', encoding='utf-8', newline='\n')
            (root / 'new.py').write_text('added = 3\n', encoding='utf-8', newline='\n')
            (root / '.env').write_text('SECRET', encoding='utf-8')
            (root / 'ignored.py').write_text('SECRET', encoding='utf-8')
            context = capture_local_context(root)
            self.assertEqual(context.read('app.py', 'head'), 'new = 2\n')
            self.assertEqual(context.read('app.py', 'base'), 'old = 1\n')
            self.assertIn('new.py', context.paths('head'))
            self.assertNotIn('ignored.py', context.paths('head'))
            context.ensure_unchanged()
            (root / 'new.py').write_text('changed = 4\n', encoding='utf-8')
            self.assertEqual(context.read('new.py', 'head'), 'added = 3\n')
            with self.assertRaisesRegex(ContextError, 'changed'):
                context.ensure_unchanged()

    def test_local_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', '-C', directory, 'init'], capture_output=True, check=True)
            (root / 'a.py').write_text('safe = True\n')
            subprocess.run(['git', '-C', directory, 'add', '.'], capture_output=True, check=True)
            subprocess.run(['git', '-C', directory, '-c', 'user.name=Test', '-c',
                            'user.email=test@example.invalid', 'commit', '-m', 'base'], capture_output=True, check=True)
            try:
                (root / 'link.py').symlink_to(root / 'a.py')
            except OSError:
                self.skipTest('OS does not permit creating symbolic links')
            with self.assertRaisesRegex(ContextError, 'Links'):
                capture_local_context(root)

    def client(self):
        return SimpleNamespace(owner='owner', repo='repo', base_url='https://api.github.com', _get=Mock())

    def test_github_reads_exact_head_base_blobs_and_caches(self):
        client = self.client()
        texts = [b'new = 2\n', b'old = 1\n']
        oids = [hashlib.sha1(f'blob {len(text)}\0'.encode() + text).hexdigest() for text in texts]
        def get(url):
            if '/trees/' in url:
                i = 0 if 'a' * 40 in url else 1
                return {'truncated': False, 'tree': [
                    {'path': 'app.py', 'mode': '100644', 'type': 'blob', 'size': len(texts[i]), 'sha': oids[i]},
                    {'path': 'link.py', 'mode': '120000', 'type': 'blob', 'size': 6, 'sha': 'c' * 40},
                    {'path': '.env', 'mode': '100644', 'type': 'blob', 'size': 6, 'sha': 'c' * 40},
                ]}
            i = oids.index(url.rsplit('/', 1)[-1])
            return {'encoding': 'base64', 'size': len(texts[i]), 'content': base64.b64encode(texts[i]).decode()}
        client._get.side_effect = get
        context = GitHubRepositoryContext(client, PullRequestRevision('a' * 40, 'b' * 40))
        self.assertEqual(context.read('app.py', 'head'), texts[0].decode())
        self.assertEqual(context.read('app.py', 'base'), texts[1].decode())
        context.read('app.py', 'head')
        self.assertEqual(client._get.call_count, 4)
        self.assertEqual(context.paths('head'), ['app.py'])
        self.assertIn('/trees/' + 'a' * 40, client._get.call_args_list[0].args[0])
        self.assertIn('/trees/' + 'b' * 40, client._get.call_args_list[1].args[0])

    def test_truncated_or_oversized_tree_is_not_a_complete_index(self):
        for tree in ({'truncated': True, 'tree': []}, {'tree': []},
                     {'truncated': False, 'tree': [{'path': 'a.py', 'mode': '100644',
                         'type': 'blob', 'size': 200_000, 'sha': 'a' * 40}]}):
            client = self.client()
            client._get.return_value = tree
            with self.subTest(tree=tree), self.assertRaises(ContextError):
                GitHubRepositoryContext(client, PullRequestRevision('a' * 40, 'b' * 40))

    def test_blob_substitution_is_rejected(self):
        client = self.client()
        tree = {'truncated': False, 'tree': [{'path': 'a.py', 'mode': '100644',
                'type': 'blob', 'size': 1, 'sha': 'c' * 40}]}
        client._get.side_effect = [tree, tree, {'encoding': 'base64', 'size': 1, 'content': 'eA=='}]
        context = GitHubRepositoryContext(client, PullRequestRevision('a' * 40, 'b' * 40))
        with self.assertRaisesRegex(ContextError, 'pinned'):
            context.read('a.py', 'head')
