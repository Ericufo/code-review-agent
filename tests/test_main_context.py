import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import main
from diff_parser import FileDiff
from github_review import PullRequestRevision
from repository_context import ContextError
from review_models import ReviewResult
from review_pipeline import ReviewBatch


class TestMainContext(unittest.TestCase):
    def run_cli(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['main.py', *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main.main()
        return code, output.getvalue()

    def batch(self):
        return ReviewBatch(ReviewResult(summary='', issues=[]), [], [], 1, 1)

    def test_local_context_capture_and_final_check(self):
        context = Mock(revisions={'head': 'workspace:test'})
        files = [FileDiff('app.py', 'diff')]
        with patch('review_service.capture_local_context', return_value=context), patch('review_service.get_local_files', return_value=files) as read, \
             patch('review_service.review_files', return_value=self.batch()) as review:
            code, _ = self.run_cli('--context', 'repo')
        self.assertEqual(code, 0)
        self.assertIs(review.call_args.kwargs['context'], context)
        self.assertEqual(context.ensure_unchanged.call_count, 2)
        self.assertEqual(read.call_count, 2)

    def test_mutated_local_context_or_diff_discards_final_result(self):
        for kind in ('snapshot', 'diff'):
            context = Mock(revisions={'head': 'workspace:test'})
            if kind == 'snapshot':
                context.ensure_unchanged.side_effect = [None, ContextError('snapshot changed')]
            files = [FileDiff('app.py', 'diff')]
            with patch('review_service.capture_local_context', return_value=context), \
                 patch('review_service.get_local_files', side_effect=[files, [FileDiff('app.py', 'changed')]]), \
                 patch('review_service.review_files', return_value=self.batch()), patch('main.post_github_comment') as publish:
                code, output = self.run_cli('--context', 'repo')
            self.assertEqual(code, 1)
            self.assertIn('changed', output)
            self.assertNotIn('=== Review Result ===', output)
            publish.assert_not_called()

    def test_pr_context_uses_recorded_revision_and_original_publication_checks(self):
        client = Mock()
        revision = PullRequestRevision('a' * 40, 'b' * 40)
        context = Mock(revisions={'head': revision.head_sha, 'base': revision.base_sha})
        with patch('main.GitHubClient', return_value=client), \
             patch('review_service.get_review_revision', return_value=revision), \
             patch('review_service.get_github_files', return_value=[FileDiff('app.py', 'diff')]), \
             patch('review_service.ensure_current_revision') as check, \
             patch('review_service.GitHubRepositoryContext', return_value=context) as factory, \
             patch('review_service.review_files', return_value=self.batch()) as review, \
             patch('main.post_github_comment', return_value='created') as publish:
            code, _ = self.run_cli('--github-pr', '7', '--context', 'repo')
        self.assertEqual(code, 0)
        factory.assert_called_once_with(client, revision)
        check.assert_called_once_with(client, 7, revision)
        self.assertIs(review.call_args.kwargs['context'], context)
        self.assertEqual(publish.call_args.args[-1], revision)
        context.ensure_unchanged.assert_not_called()

    def test_context_setup_failure_never_reviews_or_publishes(self):
        with patch('review_service.capture_local_context', side_effect=ContextError('snapshot too big')), \
             patch('review_service.review_files') as review, patch('main.post_github_comment') as publish:
            code, output = self.run_cli('--context', 'repo')
        self.assertEqual(code, 1)
        self.assertIn('Input error', output)
        review.assert_not_called()
        publish.assert_not_called()

    def test_classic_cannot_silently_ignore_repo_context(self):
        with patch('review_service.capture_local_context') as capture, self.assertRaises(SystemExit):
            self.run_cli('--context', 'repo', '--engine', 'classic')
        capture.assert_not_called()

    def test_model_budget_option_is_validated_and_forwarded(self):
        for args in [('--max-model-calls', '60'),
                     ('--context', 'repo', '--max-model-calls', '0')]:
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_cli(*args)
        context = Mock(revisions={'head': 'workspace:test'})
        with patch('review_service.capture_local_context', return_value=context), \
             patch('review_service.get_local_files', return_value=[FileDiff('app.py', 'diff')]), \
             patch('review_service.review_files', return_value=self.batch()) as review:
            code, _ = self.run_cli('--context', 'repo', '--max-model-calls', '60')
        self.assertEqual(code, 0)
        self.assertEqual(review.call_args.kwargs['max_model_calls'], 60)

    def test_character_and_tool_budgets_are_validated_and_forwarded(self):
        options = {'max_input_chars': 3_000_000, 'max_context_chars': 300_000, 'max_tool_calls': 180}
        for name in options:
            for args in [('--' + name.replace('_', '-'), '1'),
                         ('--context', 'repo', '--' + name.replace('_', '-'), '-1')]:
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    self.run_cli(*args)
        context = Mock(revisions={'head': 'workspace:test'})
        argv = ['--context', 'repo']
        for name, value in options.items():
            argv.extend(['--' + name.replace('_', '-'), str(value)])
        with patch('review_service.capture_local_context', return_value=context), \
             patch('review_service.get_local_files', return_value=[FileDiff('app.py', 'diff')]), \
             patch('review_service.review_files', return_value=self.batch()) as review:
            code, output = self.run_cli(*argv)
        self.assertEqual(code, 0)
        for name, value in options.items():
            self.assertEqual(review.call_args.kwargs[name], value)
            self.assertIn(f'{name}={value}', output)
