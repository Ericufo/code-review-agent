"""Exercise CLI -> pinned GitHub blobs -> graph -> comment lifecycle offline."""
import base64
import contextlib
import hashlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import main
from github_comment import BOT_MARKER
from test_context_review import call, response


class TestRepoPRLifecycle(unittest.TestCase):
    def setUp(self):
        self.head, self.base = 'a' * 40, 'b' * 40
        self.metadata = {'state': 'open', 'draft': False,
                         'head': {'sha': self.head}, 'base': {'sha': self.base}}
        self.client = Mock(owner='example', repo='repo', base_url='https://api.github.com')
        self.client.get_pull_request.return_value = self.metadata
        self.client.get_pull_request_files.return_value = [
            {'filename': 'app.py', 'patch': '@@ -1 +1 @@\n-old()\n+lookup().name'},
            {'filename': 'README.md', 'patch': '@@ -1 +1 @@\n-old\n+new'},
        ]
        self.client.get_comment_author_login.return_value = 'github-actions[bot]'
        self.client.get_issue_comments.return_value = []
        source = b'def lookup():\n    return None\n'
        oid = hashlib.sha1(f'blob {len(source)}\0'.encode() + source).hexdigest()
        self.blob = {'encoding': 'base64', 'size': len(source),
                     'content': base64.b64encode(source).decode()}
        self.tree = {'truncated': False, 'tree': [
            {'path': 'dep.py', 'type': 'blob', 'mode': '100644', 'sha': oid, 'size': len(source)},
        ]}
        self.oid = oid
        def get(url):
            if url.endswith(f'/trees/{self.head}?recursive=1') or url.endswith(f'/trees/{self.base}?recursive=1'):
                return self.tree
            if url.endswith('/blobs/' + self.oid):
                return self.blob
            raise AssertionError('Unexpected GitHub object request')
        self.client._get.side_effect = get

    def run_cli(self, model, *extra):
        out = io.StringIO()
        with patch('main.GitHubClient', return_value=self.client), \
             patch('context_review.request_context_model', side_effect=model), \
             patch('sys.argv', ['main.py', '--github-pr', '7', '--context', 'repo',
                                '--expected-head-sha', self.head, '--trace', *extra]), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main.main()
        return code, out.getvalue()

    def finding_model(self, inputs, instructions, **kwargs):
        outputs = [item for item in inputs if item.get('type') == 'function_call_output']
        if not outputs:
            return response(call())
        evidence = json.loads(outputs[-1]['output'])['entries'][0]['evidence']
        return response(result={'issues': [{
            'severity': 'high', 'file': 'app.py', 'title': 'Null access',
            'description': 'lookup returns None; accessing name raises AttributeError.',
            'evidence': [evidence],
        }]})

    def test_repo_review_creates_evidence_comment_then_updates_after_new_commit(self):
        code, output = self.run_cli(self.finding_model)
        self.assertEqual(code, 0, output)
        self.assertIn('model_calls=2/64', output)
        created = self.client.create_issue_comment.call_args.kwargs['body']
        self.assertTrue(created.startswith(BOT_MARKER))
        self.assertIn('Reviewed 1/1', created)
        self.assertIn('Excluded: 1', created)
        self.assertIn(f'example/repo@{self.head}', created)
        self.assertIn('dep.py:1-2', created)
        self.assertIn('Null access', created)
        self.client.get_issue_comments.return_value = [
            {'id': 42, 'body': created, 'user': {'login': 'github-actions[bot]'}},
        ]
        self.head = 'c' * 40
        self.metadata['head'] = {'sha': self.head}
        self.client.get_pull_request.return_value = self.metadata
        code, output = self.run_cli(lambda *args, **kwargs: response())
        self.assertEqual(code, 0, output)
        self.client.create_issue_comment.assert_called_once()
        updated = self.client.update_issue_comment.call_args.kwargs
        self.assertEqual(updated['comment_id'], 42)
        self.assertIn(f'Reviewed commit: `{self.head}`', updated['body'])
        self.assertNotIn('Null access', updated['body'])
        self.assertIn('Status: **COMPLETE**', updated['body'])

    def test_repo_stale_head_after_comment_fetch_never_writes(self):
        def comments(**kwargs):
            self.client.get_pull_request.return_value = {
                **self.metadata, 'head': {'sha': 'c' * 40}}
            return []
        self.client.get_issue_comments.side_effect = comments
        code, output = self.run_cli(self.finding_model)
        self.assertEqual(code, 0, output)
        self.assertIn('discarding outdated results', output)
        self.client.create_issue_comment.assert_not_called()
        self.client.update_issue_comment.assert_not_called()

    def test_repo_tiny_budget_publishes_incomplete_and_exits_nonzero(self):
        model = Mock()
        code, output = self.run_cli(model, '--max-input-chars', '1')
        self.assertEqual(code, 1, output)
        model.assert_not_called()
        body = self.client.create_issue_comment.call_args.kwargs['body']
        self.assertIn('Status: **INCOMPLETE**', body)
        self.assertIn('Reviewed 0/1', body)
        self.assertIn('input character budget', body)
        self.assertIn('Excluded: 1', body)

    def test_repo_only_readme_posts_no_eligible_without_calling_model(self):
        self.client.get_pull_request_files.return_value = [
            {'filename': 'README.md', 'patch': '+text'}]
        model = Mock()
        code, output = self.run_cli(model)
        self.assertEqual(code, 0, output)
        model.assert_not_called()
        body = self.client.create_issue_comment.call_args.kwargs['body']
        self.assertIn('Status: **NO_ELIGIBLE_FILES**', body)
        self.assertNotIn('No issues found', body)
