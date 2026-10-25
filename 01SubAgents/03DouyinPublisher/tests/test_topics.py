"""Offline topic-search fixtures; all browser requests are intercepted."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from douyin_agent.topics import (TopicSelectionError, editor_snapshot, parse_displayed_count,
                                 rank_candidates, select_topics, validate_settings)
from douyin_agent.browser import _Publisher, _description
from douyin_agent.cli import parser, prepare

FIXTURE = Path(__file__).parent / 'fixtures' / 'topics.html'


class TopicRankingTests(unittest.TestCase):
    def test_counts_preserve_unknown_metric(self):
        for value, expected in [('8千', 8000), ('2.3万', 23000), ('4216.1亿', 421610000000), ('1,234', 1234)]:
            with self.subTest(value=value):
                number, label = parse_displayed_count(value)
                self.assertAlmostEqual(number, expected)
                self.assertIsNone(label)
        self.assertEqual(parse_displayed_count('播放量：2.3万'), (23000, '播放量'))
        self.assertEqual(parse_displayed_count('无数据显示'), (None, None))
        self.assertEqual(parse_displayed_count('200集'), (None, None))

    def test_relevance_precedes_counts_and_duplicate_existing_excluded(self):
        rows = [{'name': '美食', 'count_value': 1e15}, {'name': '职场穿搭', 'count_value': 1e14},
                {'name': '职场沟通', 'count_value': 8000}, {'name': '职场', 'count_value': 421610000000}]
        selected = rank_candidates(rows, {'enabled': True, 'queries': ['职场'], 'exclude_keywords': ['穿搭']}, ['职场'])
        self.assertEqual([item['name'] for item in selected], ['职场沟通'])

    def test_allowlist_and_mixed_metrics(self):
        rows = [{'name': '职场', 'count_value': 100, 'metric_label': '热度'},
                {'name': '职场沟通', 'count_value': 9999, 'metric_label': '播放量'},
                {'name': '职场日常', 'count_value': None}]
        settings = {'enabled': True, 'queries': ['职场'], 'allowed_names': ['职场', '职场沟通']}
        result = rank_candidates(rows, settings, [])
        self.assertEqual([item['name'] for item in result], ['职场', '职场沟通'])
        self.assertTrue(all('没有可比较' in item['selection_basis'] for item in result))

    def test_cli_enabled_by_repeated_queries(self):
        args = parser().parse_args(['prepare', 'clip.mp4', '--topic-query', '职场', '--topic-query', '情绪'])
        self.assertEqual(args.topic_query, ['职场', '情绪'])
        self.assertIsNone(args.topic_count)  # prepare resolves imported/default 5
        self.assertEqual(parser().parse_args(['run', 'clip.mp4']).topic_query, [])
        with self.assertRaises(ValueError):
            validate_settings({'enabled': True, 'queries': ['职场'], 'target_count': 0})

    def test_prepare_persists_settings_without_unrelated_default_tag_seeds(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'clip.mp4'
            video.write_bytes(b'offline fixture')
            args = parser().parse_args(['prepare', str(video), '--title', '本次标题', '--description', '本次正文',
                                        '--topic-query', '职场', '--output', str(Path(folder) / 'output')])
            with patch('douyin_agent.media.prepare_covers', return_value={'vertical': 'a.jpg', 'horizontal': 'b.jpg'}), patch('builtins.print'):
                path = prepare(args)
            manifest = json.loads(path.read_text())
            self.assertEqual(manifest['hashtags'], [])
            self.assertTrue(manifest['topic_selection']['enabled'])
            self.assertEqual(manifest['topic_selection']['queries'], ['职场'])
            self.assertEqual(manifest['topic_selection']['target_count'], 5)


class TopicBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.fulfill(body=FIXTURE.read_text(), content_type='text/html'))
        self.page = self.context.new_page()
        self.page.goto('https://creator.douyin.com/offline-fixture')
        self.page.set_default_timeout(1000)
        self.editor = self.page.locator('#editor')
        self.before = editor_snapshot(self.editor)

    def tearDown(self):
        self.context.close()
        self.folder.cleanup()

    def settings(self, **overrides):
        return dict({'enabled': True, 'queries': ['职场', '情绪'], 'target_count': 4,
                     'exclude_keywords': ['穿搭']}, **overrides)

    def test_real_entities_added_by_rank_without_publish_or_body_reset(self):
        with patch.object(self.editor, 'fill', side_effect=AssertionError('Never reset editor')):
            report = select_topics(self.page, self.settings(), timeout_seconds=.5)
        after = editor_snapshot(self.editor)
        self.assertTrue(after['text'].startswith(self.before['text']))
        self.assertEqual(after['entities'], ['用户已有', '职场', '情绪管理', '职场日常'])
        self.assertEqual(self.page.evaluate('window.published'), 0)
        self.assertEqual(self.page.evaluate('window.clickedTopics'), ['职场', '情绪管理', '职场日常'])
        self.assertNotIn('pending_query', report)
        self.assertEqual(report['selected'][0]['platform_displayed_count'], '4216.1亿')
        self.assertIsNone(report['selected'][0]['metric_label'])
        self.assertIsNone(report['selected'][0]['time_window'])
        self.assertTrue(report['selected'][0]['collected_at'])
        self.assertIn('零宽字\u200b符', after['text'])

    def test_no_results_roll_back_exact_query_and_preserve_original(self):
        result = select_topics(self.page, self.settings(queries=['空']), timeout_seconds=.2)
        self.assertEqual(editor_snapshot(self.editor), self.before)
        self.assertEqual(result['selected'], [])
        self.assertIn('相关候选不足', result['warning'])
        self.assertEqual(self.page.evaluate('window.published'), 0)

    def test_existing_entities_and_user_added_prose_preserved(self):
        self.page.evaluate('''() => {const s=document.createElement('span');s.dataset.string='true';s.textContent=' 用户刚加的一句。';document.querySelector('#editor').insertBefore(s,document.querySelector('[data-enter]'));}''')
        current = editor_snapshot(self.editor)
        result = select_topics(self.page, self.settings(queries=['用户', '职场'], target_count=2), timeout_seconds=.4)
        self.assertEqual(result['actual_topics'], ['用户已有', '职场'])
        self.assertTrue(editor_snapshot(self.editor)['text'].startswith(current['text']))
        self.assertNotIn('用户已有', self.page.evaluate('window.clickedTopics'))

    def test_visible_limit_stops_at_actual_total(self):
        self.page.locator('#limit').evaluate("el => el.textContent='最多可添加2个话题'")
        result = select_topics(self.page, self.settings(), timeout_seconds=.4)
        self.assertEqual(result['platform_visible_limit'], 2)
        self.assertEqual(result['actual_topics'], ['用户已有', '职场'])

    def test_plain_tags_do_not_count_as_entities_or_duplicate(self):
        self.page.evaluate('''() => {const s=document.createElement('span');s.dataset.string='true';s.textContent=' #职场 ';document.querySelector('#editor').insertBefore(s,document.querySelector('[data-enter]'));}''')
        result = select_topics(self.page, self.settings(target_count=3), timeout_seconds=.4)
        self.assertNotIn('职场', result['actual_topics'])
        self.assertNotIn('职场', self.page.evaluate('window.clickedTopics'))

    def test_click_without_entity_raises_and_never_claims_success(self):
        self.page.evaluate('window.noEntity=true')
        report = {}
        with self.assertRaises(TopicSelectionError) as caught:
            select_topics(self.page, self.settings(), report=report, timeout_seconds=.3)
        self.assertEqual(caught.exception.code, 'topic_entity_not_confirmed')
        self.assertEqual(report['selected'], [])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(editor_snapshot(self.editor), self.before)
        self.assertEqual(self.page.evaluate('window.published'), 0)

    def test_concurrent_edit_stops_without_deleting_any_user_text(self):
        self.page.evaluate('window.mutateDuringSearch=true')
        with self.assertRaises(TopicSelectionError) as caught:
            select_topics(self.page, self.settings(), timeout_seconds=.2)
        self.assertEqual(caught.exception.code, 'topic_query_changed')
        actual = editor_snapshot(self.editor)
        self.assertIn('用户新加的文字', actual['text'])
        self.assertEqual(actual['entities'], self.before['entities'])
        self.assertTrue(actual['text'].startswith(self.before['text']))

    def test_disabled_publisher_does_not_change_page(self):
        publisher = _Publisher(self.page, {'output_dir': self.folder.name}, {}, 1)
        publisher.add_topics()
        self.assertEqual(editor_snapshot(self.editor), self.before)
        self.assertNotIn('topic_selection', publisher.report)

    def test_publisher_persists_actual_caption_and_real_tags_without_duplication(self):
        manifest = {'output_dir': self.folder.name, 'description': '原文不重写', 'hashtags': ['旧文字标签'],
                    'topic_selection': self.settings(target_count=2)}
        path = Path(self.folder.name) / 'manifest.json'
        publisher = _Publisher(self.page, manifest, {'_manifest_path': str(path)}, 1)
        publisher.add_topics()
        saved = json.loads(path.read_text())
        self.assertEqual(saved['hashtags'], ['用户已有', '职场'])
        self.assertEqual(saved['description'], '原文不重写')
        self.assertEqual(_description(saved), editor_snapshot(self.editor)['text'])
        self.assertEqual(_description(saved).count('#职场'), 1)
        self.assertTrue(publisher.report['checks']['topics'])

    def test_manual_topic_seed_is_relevant_with_nonmatching_keywords_and_keeps_constraints(self):
        self.page.evaluate('''() => {window.options['主动复盘'] = [
            ['主动复盘直播','999999亿'], ['主动复盘穿搭','99999亿'], ['主动复盘','9000亿']
        ]}''')
        settings = self.settings(queries=['职场'], keywords=['职场'], target_count=3,
                                 allowed_names=['职场', '主动复盘', '主动复盘穿搭'])
        manifest = {'output_dir': self.folder.name, 'description': '原文不重写',
                    'hashtags': ['主动复盘'], 'topic_selection': settings}
        publisher = _Publisher(self.page, manifest, {'topic_timeout_seconds': .4}, 1)
        publisher.add_topics()
        actual = editor_snapshot(self.editor)
        self.assertEqual(actual['entities'], ['用户已有', '主动复盘', '职场'])
        self.assertTrue(actual['text'].startswith(self.before['text']))
        self.assertEqual(actual['text'].count('#主动复盘'), 1)
        self.assertEqual(self.page.evaluate('window.clickedTopics'), ['主动复盘', '职场'])
        self.assertEqual(publisher.report['topic_selection']['selected'][0]['matched_keywords'], ['主动复盘'])
        self.assertEqual(settings['keywords'], ['职场'])
        self.assertEqual(manifest['description'], '原文不重写')
        self.assertEqual(self.page.evaluate('window.published'), 0)

    def test_run_fills_only_body_then_selects_manual_tag_seeds_as_entities(self):
        video = Path(self.folder.name) / 'video.mp4'
        video.write_bytes(b'offline fixture')
        manifest = {'output_dir': self.folder.name, 'video': str(video), 'title': '本次标题',
                    'description': '本次正文，保留内容。', 'hashtags': ['职场'],
                    'topic_selection': self.settings(queries=['情绪'], target_count=2)}
        publisher = _Publisher(self.page, manifest, {'review': False, 'topic_timeout_seconds': .4}, 1)
        def check_fresh_body():
            self.assertEqual(editor_snapshot(self.editor)['text'], manifest['description'])
            self.assertEqual(editor_snapshot(self.editor)['entities'], [])
            publisher.report['checks']['video_processed'] = True
        original_add = publisher.add_topics
        def checked_add():
            check_fresh_body()
            original_add()
        with patch.object(publisher, 'add_topics', side_effect=checked_add), \
             patch.object(publisher, 'wait_upload_ready'), patch.object(publisher, 'set_cover'), \
             patch.object(publisher, 'choose_collection'), patch.object(publisher, 'fill_declaration'), \
             patch.object(publisher, 'preflight'), patch.object(publisher, 'screenshot', return_value=None):
            result = publisher.run(publish=False)
        self.assertEqual(result['hashtags'], ['职场', '情绪管理'])
        self.assertEqual(_description(manifest).count('#职场'), 1)
        self.assertEqual(_description(manifest).count('#情绪管理'), 1)
        self.assertTrue(_description(manifest).startswith('本次正文，保留内容。'))
        self.assertEqual(self.page.evaluate('window.published'), 0)


if __name__ == '__main__':
    unittest.main()
