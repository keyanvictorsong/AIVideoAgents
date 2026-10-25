"""Stage 02 copy handoff tests; no browser or media renderer is started."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from douyin_agent.cli import parser, prepare


class CopyImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.video = self.root / 'edited.mp4'
        self.video.write_bytes(b'edited fixture media, different from the original')
        self.copy_path = self.root / 'douyin-copy.json'
        self.payload = {
            'title': '原定标题', 'description': '本期聊职场沟通与情绪反思。', 'hashtags': [],
            'topic_selection': {'enabled': True, 'queries': ['职场', '情绪管理'],
                                'keywords': ['职场', '情绪'], 'target_count': 7,
                                'allowed_names': ['职场沟通', '情绪管理'], 'exclude_keywords': ['穿搭']},
            'copy_source': 'assistant_reviewed', 'source_sha256': 'a' * 64,
        }

    def tearDown(self):
        self.folder.cleanup()

    def write_copy(self, payload=None):
        self.copy_path.write_text(json.dumps(self.payload if payload is None else payload, ensure_ascii=False), encoding='utf-8')

    def arguments(self, *extra, command='prepare', imported=True):
        argv = [command, str(self.video), '--output', str(self.root / 'output')]
        if imported:
            argv += ['--copy-file', str(self.copy_path)]
        return parser().parse_args([*argv, *extra])

    def prepared(self, args):
        with patch('douyin_agent.media.prepare_covers', return_value={'vertical': 'a.jpg', 'horizontal': 'b.jpg'}) as renderer, patch('builtins.print'):
            path = prepare(args)
        renderer.assert_called_once()
        return json.loads(path.read_text())

    def test_import_automatically_enables_queries_without_manual_flags(self):
        for command in ('prepare', 'run'):
            with self.subTest(command=command):
                self.write_copy()
                job = self.prepared(self.arguments(command=command))
                self.assertEqual(job['title'], self.payload['title'])
                self.assertEqual(job['description'], self.payload['description'])
                self.assertEqual(job['hashtags'], [])
                self.assertEqual(job['topic_selection'], self.payload['topic_selection'])
                self.assertEqual(job['topic_selection']['target_count'], 7)
                self.assertEqual(job['copy_source'], 'assistant_reviewed')

    def test_different_theme_is_imported_without_previous_subject_or_keywords(self):
        self.payload.update(title='相机与摄影', description='本期记录摄影练习。', hashtags=['摄影'])
        self.payload['topic_selection'] = {'enabled': True, 'queries': ['摄影'], 'keywords': ['摄影'], 'target_count': 5}
        self.write_copy()
        job = self.prepared(self.arguments())
        self.assertEqual(job['hashtags'], ['摄影'])
        self.assertEqual(job['topic_selection']['queries'], ['摄影'])
        self.assertEqual(job['topic_selection']['keywords'], ['摄影'])
        self.assertNotIn('示例', job['description'])
        self.assertNotIn('职场', json.dumps(job['topic_selection'], ensure_ascii=False))

    def test_explicit_overrides_include_empty_values_and_reset_old_query_constraints(self):
        self.write_copy()
        job = self.prepared(self.arguments('--title', '改为摄影标题', '--description', '', '--hashtags', '',
                                            '--topic-query', '摄影', '--topic-query', '构图', '--topic-count', '3'))
        self.assertEqual(job['title'], '改为摄影标题')
        self.assertEqual(job['description'], '')
        self.assertEqual(job['hashtags'], [])
        self.assertEqual(job['topic_selection'], {'enabled': True, 'queries': ['摄影', '构图'],
                                                 'keywords': ['摄影', '构图'], 'target_count': 3,
                                                 'allowed_names': [], 'exclude_keywords': []})
        self.assertEqual(job['copy_provenance']['overrides'], ['title', 'description', 'hashtags', 'topic_query', 'topic_count'])

    def test_explicit_count_only_keeps_imported_queries_and_constraints(self):
        self.write_copy()
        job = self.prepared(self.arguments('--topic-count', '5'))
        self.assertEqual(job['topic_selection'], {**self.payload['topic_selection'], 'target_count': 5})
        self.assertEqual(job['copy_provenance']['overrides'], ['topic_count'])

    def test_original_copy_hash_is_recorded_separately_from_edited_video(self):
        self.write_copy()
        job = self.prepared(self.arguments())
        self.assertEqual(job['source']['sha256'], hashlib.sha256(self.video.read_bytes()).hexdigest())
        self.assertEqual(job['copy_provenance']['source_sha256'], 'a' * 64)
        self.assertNotEqual(job['source']['sha256'], job['copy_provenance']['source_sha256'])
        self.assertEqual(job['copy_provenance']['path'], str(self.copy_path.resolve()))
        self.assertEqual(job['copy_provenance']['sha256'], hashlib.sha256(self.copy_path.read_bytes()).hexdigest())

    def test_disabled_import_and_reason_are_preserved_without_template_fallback(self):
        self.payload['topic_selection'] = {'enabled': False, 'queries': [], 'disabled_reason': 'missing_fact_keywords'}
        self.write_copy()
        job = self.prepared(self.arguments())
        self.assertFalse(job['topic_selection']['enabled'])
        self.assertEqual(job['topic_selection']['disabled_reason'], 'missing_fact_keywords')
        self.assertEqual(job['hashtags'], [])
        self.assertEqual(job['description'], self.payload['description'])

    def test_imported_file_is_fully_validated_even_when_cli_overrides_bad_field(self):
        self.payload['title'] = None
        self.write_copy()
        with patch('douyin_agent.media.prepare_covers') as renderer, patch('douyin_agent.cli.write_json') as writer:
            with self.assertRaises(ValueError):
                prepare(self.arguments('--title', '明确标题'))
        renderer.assert_not_called()
        writer.assert_not_called()
        self.assertFalse((self.root / 'output').exists())

    def test_invalid_imports_fail_before_render_or_output(self):
        invalid = [[], {}, {**self.payload, 'description': None}, {**self.payload, 'hashtags': '职场'},
                   {**self.payload, 'hashtags': [1]}, {**self.payload, 'topic_selection': None},
                   {**self.payload, 'topic_selection': {'enabled': 'false', 'queries': []}},
                   {**self.payload, 'topic_selection': {'enabled': True, 'queries': []}},
                   {**self.payload, 'topic_selection': {'enabled': True, 'queries': ['职场'], 'target_count': 0}},
                   {**self.payload, 'source_sha256': 'not-a-sha'}, {**self.payload, 'copy_source': ''},
                   {**self.payload, 'title': ''}, {**self.payload, 'hashtags': ['有 标点']},
                   {key: value for key, value in self.payload.items() if key != 'topic_selection'}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.write_copy(payload)
                with patch('douyin_agent.media.prepare_covers') as renderer, patch('douyin_agent.cli.write_json') as writer:
                    with self.assertRaises(ValueError):
                        prepare(self.arguments())
                renderer.assert_not_called()
                writer.assert_not_called()
                self.assertFalse((self.root / 'output').exists())
        self.copy_path.write_text('{"broken":', encoding='utf-8')
        with patch('douyin_agent.media.prepare_covers') as renderer:
            with self.assertRaises(ValueError):
                prepare(self.arguments())
        renderer.assert_not_called()
        self.copy_path.unlink()
        with self.assertRaises(ValueError):
            prepare(self.arguments())
        self.assertFalse((self.root / 'output').exists())

    def test_no_import_has_no_subject_or_copy_defaults_and_honors_manual_queries(self):
        old = self.prepared(self.arguments(imported=False))
        self.assertEqual(old['hashtags'], [])
        self.assertEqual(old['description'], '')
        self.assertEqual(old['collection']['keywords'], [])
        self.assertNotIn('topic_selection', old)
        self.assertEqual(old['copy_source'], 'user_fields_or_filename; not_transcribed')
        explicit = self.prepared(self.arguments('--topic-query', '摄影', '--description', '本期摄影。', imported=False))
        self.assertEqual(explicit['hashtags'], [])
        self.assertEqual(explicit['topic_selection']['queries'], ['摄影'])
        self.assertEqual(explicit['topic_selection']['target_count'], 5)
        self.assertNotIn('copy_provenance', explicit)


if __name__ == '__main__':
    unittest.main()
