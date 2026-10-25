"""Offline checks for the 02-copy to 03-publisher handoff."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

AGENT_ROOT = Path(__file__).resolve().parents[1]
AGENTS_ROOT = AGENT_ROOT.parent
sys.path[:0] = [str(AGENTS_ROOT / '02DYXHSTitleTag/src'), str(AGENT_ROOT)]

from dyxhs_title_tag.pipeline import assemble_output, export_output, rank_collections
from douyin_agent.cli import parser, prepare


class DouyinHandoffTests(unittest.TestCase):
    def test_exported_copy_enables_topics_without_manual_queries_for_each_video(self):
        collections = {platform: {'samples': [], 'status': 'not_collected', 'reason': 'offline test'}
                       for platform in ('xiaohongshu', 'douyin')}
        for keywords in (['职场', '情绪管理'], ['示例', '团队配合']):
            with self.subTest(keywords=keywords), tempfile.TemporaryDirectory() as folder:
                task = Path(folder)
                source_sha = hashlib.sha256(('original:' + keywords[0]).encode()).hexdigest()
                facts = {'source_sha256': source_sha, 'summary': '作者分享自己的体会。',
                         'keywords': keywords,
                         'facts': [{'id': 'F1', 'text': '作者分享自己的体会。', 'evidence_ids': ['sub-1']}]}
                draft = {'source_sha256': source_sha, 'generation_mode': 'assistant_reviewed',
                         'xiaohongshu': {'variants': [{'title': '分享自己的体会',
                                                      'description': '聊聊自己的经历和想法。', 'fact_ids': ['F1']}],
                                         'tag_suggestions': keywords},
                         'douyin': {'variants': [{'title': '分享自己的体会',
                                                 'description': '聊聊自己的经历和想法。', 'fact_ids': ['F1']}]}}
                result = assemble_output(draft, facts, collections, rank_collections(collections, keywords))
                copy_dir = task / '02 TitleTagoutput'
                export_output(result, copy_dir)
                video = task / 'edited.mp4'
                video.write_bytes(('edited:' + keywords[0]).encode())
                args = parser().parse_args(['prepare', str(video), '--copy-file', str(copy_dir / 'douyin-copy.json'),
                                            '--title', '用户确认的标题', '--output', str(task / '03 AutoPublicoutput')])
                with patch('douyin_agent.media.prepare_covers', return_value={'vertical': 'vertical.png',
                                                                             'horizontal': 'horizontal.png'}), \
                     patch('builtins.print'):
                    manifest = json.loads(prepare(args).read_text(encoding='utf-8'))
                self.assertTrue(manifest['topic_selection']['enabled'])
                self.assertEqual(manifest['topic_selection']['queries'], keywords)
                self.assertEqual(manifest['topic_selection']['keywords'], keywords)
                self.assertEqual(manifest['topic_selection']['target_count'], 5)
                self.assertEqual(manifest['title'], '用户确认的标题')
                self.assertEqual(manifest['description'], result['douyin']['description'])
                self.assertEqual(manifest['hashtags'], [])
                self.assertNotEqual(manifest['source']['sha256'], source_sha)
                self.assertNotIn('topic_selection', draft['douyin'])
                self.assertFalse((task / '03 AutoPublicoutput/publish-attempt.json').exists())


if __name__ == '__main__':
    unittest.main()
