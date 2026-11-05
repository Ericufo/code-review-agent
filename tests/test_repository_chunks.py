import dataclasses
import unittest

from repository_chunks import ChunkConfig, build_manifest, chunk_source, read_chunk
from repository_context import ContextError, RepositoryContext


def context(head, base=None, repository='owner/repo'):
    return RepositoryContext(repository, {'head': 'a' * 40, 'base': 'b' * 40},
                             {'head': head, 'base': base or {}})


class TestRepositoryChunks(unittest.TestCase):
    def test_decorators_classes_methods_and_module_lines_remain_exact(self):
        source = ('import os\n\n@decorate\nasync def work():\n    return 1\n\n'
                  'class Box:\n    @staticmethod\n    def get():\n        return 2\n'
                  '\n# trailing comment\n')
        ctx = context({'app.py': source})
        chunks = build_manifest(ctx).chunks
        work = next(c for c in chunks if c.symbol == 'work')
        method = next(c for c in chunks if c.symbol == 'Box.get')
        self.assertEqual((work.start_line, work.end_line), (3, 5))
        self.assertEqual((method.start_line, method.end_line), (8, 10))
        self.assertEqual(''.join(read_chunk(ctx, c, version='head') for c in chunks), source)
        self.assertEqual([n for c in chunks for n in range(c.start_line, c.end_line + 1)],
                         list(range(1, 13)))

    def test_nested_functions_remain_inside_outer_function(self):
        source = 'def outer():\n    def inner():\n        return 1\n    return inner()\n'
        chunks = build_manifest(context({'a.py': source})).chunks
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, source)

    def test_physical_lines_preserve_crlf_unicode_and_missing_final_newline(self):
        source = '# 中文\r\nx = "a\u2028b"\r\ndef f():\r\n    return x'
        chunks = build_manifest(context({'a.py': source})).chunks
        self.assertEqual(''.join(c.text for c in chunks), source)
        self.assertEqual((chunks[-1].start_line, chunks[-1].end_line), (3, 4))

    def test_invalid_python_falls_back_without_executing_source(self):
        source = 'raise RuntimeError("never execute")\ndef broken(:\n    pass\n'
        chunks = build_manifest(context({'a.py': source}), config=ChunkConfig(max_lines=2)).chunks
        self.assertEqual([(c.start_line, c.end_line) for c in chunks], [(1, 2), (3, 3)])
        self.assertEqual(''.join(c.text for c in chunks), source)

    def test_limits_never_truncate_a_line(self):
        source = '#123\n#456\n#789\n'
        chunks = build_manifest(context({'a.py': source}), config=ChunkConfig(max_chars=6)).chunks
        self.assertEqual([c.text for c in chunks], ['#123\n', '#456\n', '#789\n'])
        with self.assertRaisesRegex(ContextError, 'line exceeds'):
            build_manifest(context({'a.py': source}), config=ChunkConfig(max_chars=4))

    def test_empty_file_is_in_manifest_without_citable_lines(self):
        manifest = build_manifest(context({'empty.py': '', 'a.ts': 'let x = 1;'}))
        self.assertEqual([p for p, _ in manifest.files], ['empty.py'])
        self.assertEqual(manifest.chunks, ())

    def test_manifest_is_deterministic_and_binds_configuration(self):
        first = build_manifest(context({'b.py': 'b=2', 'a.py': 'a=1'}))
        second = build_manifest(context({'a.py': 'a=1', 'b.py': 'b=2'}))
        self.assertEqual(first.manifest_id, second.manifest_id)
        changed = build_manifest(context({'a.py': 'a=1', 'b.py': 'b=2'}),
                                 config=ChunkConfig(max_lines=2))
        self.assertNotEqual(first.manifest_id, changed.manifest_id)

    def test_identical_text_cannot_cross_repository_or_version(self):
        ctx = context({'a.py': 'x=1'}, {'a.py': 'x=1'})
        head = build_manifest(ctx)
        base = build_manifest(ctx, 'base')
        self.assertNotEqual(head.manifest_id, base.manifest_id)
        self.assertNotEqual(head.chunks[0].chunk_id, base.chunks[0].chunk_id)
        with self.assertRaisesRegex(ContextError, 'snapshot'):
            read_chunk(ctx, head.chunks[0], version='base')
        with self.assertRaisesRegex(ContextError, 'snapshot'):
            read_chunk(context({'a.py': 'x=1'}, repository='other/repo'), head.chunks[0], version='head')

    def test_old_revision_rejected_even_if_content_matches(self):
        ctx = context({'a.py': 'x=1'})
        chunk = build_manifest(ctx).chunks[0]
        ctx.revisions['head'] = 'c' * 40
        with self.assertRaisesRegex(ContextError, 'revision'):
            read_chunk(ctx, chunk, version='head')

    def test_same_revision_content_change_deleted_and_renamed_files(self):
        ctx = context({'a.py': 'x=1'})
        old = build_manifest(ctx)
        for files in ({'a.py': 'x=2'}, {}, {'renamed.py': 'x=1'}):
            ctx.files['head'] = files
            with self.subTest(files=files):
                self.assertNotEqual(old.manifest_id, build_manifest(ctx).manifest_id)
                with self.assertRaises(ContextError):
                    read_chunk(ctx, old.chunks[0], version='head')

    def test_tampered_candidate_text_range_or_hash_is_rejected(self):
        ctx = context({'a.py': 'x=1\n'})
        chunk = build_manifest(ctx).chunks[0]
        for changes in ({'text': 'fake'}, {'end_line': 20}, {'start_line': 0},
                        {'content_hash': '0' * 64}):
            with self.subTest(changes=changes), self.assertRaises(ContextError):
                read_chunk(ctx, dataclasses.replace(chunk, **changes), version='head')

    def test_source_policy_and_size_fail_closed(self):
        for path, source in (('.env.py', 'x=1'), ('secrets/key.py', 'x=1'),
                             ('../a.py', 'x=1'), ('a.py', '\x00'), ('a.py', 'x' * 128001)):
            with self.subTest(path=path), self.assertRaises(ContextError):
                build_manifest(context({path: source}))

    def test_read_failure_does_not_produce_partial_manifest(self):
        class DeniedContext(RepositoryContext):
            def read(self, path, version):
                if path == 'b.py':
                    raise ContextError('Read denied')
                return super().read(path, version)
        ctx = DeniedContext('repo', {'head': 'a' * 40}, {'head': {'a.py': 'a=1', 'b.py': 'b=2'}})
        with self.assertRaisesRegex(ContextError, 'denied'):
            build_manifest(ctx)

    def test_invalid_limits_and_missing_version_rejected(self):
        for kwargs in ({'max_lines': 0}, {'max_chars': 0}, {'max_lines': True}, {'max_chars': 12001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContextError):
                ChunkConfig(**kwargs)
        with self.assertRaises(ContextError):
            build_manifest(context({}), 'latest')


if __name__ == '__main__':
    unittest.main()
