"""Offline checks for one explicit recovery after a user confirms no submission."""
import copy
import json
import unittest

import test_jobs
from xhs_publisher.jobs import (
    ATTEMPT_FILENAME, REPREPARING, DuplicateSubmission, authorize_repreparation,
    begin_upload, event, preparation_guard, write_json,
)


class RepreparationTests(unittest.TestCase):
    # Reuse filesystem/network-isolation setup without inheriting test methods.
    def setUp(self):
        test_jobs.JobTests.setUp(self)
        with preparation_guard(self.profile, self.job) as record:
            begin_upload(self.job, self.profile)
            record['status'] = 'manual_publication_unverified'
        self.job['state'] = 'manual_publication_unverified'
        self.ledger_path = self.profile.parent / (self.profile.name + '-handoff-ledger.json')
        self.verification = self.output / 'publication-verification.json'
        write_json(self.verification, {
            'result': 'not_found_in_current_manager', 'source_sha256': self.job['source']['sha256'],
            'target_title': self.job['title'], 'publication_verified': False,
            'original_prepared_tab_closed': True,
        })
        self.confirmation = {'source': 'user_reply', 'quote': '小红书点错了未发布',
                             'confirmed_not_published': True, 'confirmed_at': '2026-10-19T12:30:00-07:00'}
        self.output.joinpath('ready-to-publish.png').write_bytes(b'old screenshot')

    def authorize(self):
        return authorize_repreparation(self.profile, self.job, user_confirmation=self.confirmation,
                                       verification_path=self.verification)

    def test_explicit_authorization_archives_and_allows_exactly_one_upload(self):
        authorization = self.authorize()
        archive = self.output / 'archive' / ('before-reprepare-' + authorization['authorization_id'])
        self.assertTrue((archive / ATTEMPT_FILENAME).is_file())
        self.assertEqual((archive / 'ready-to-publish.png').read_bytes(), b'old screenshot')
        self.assertFalse((self.output / ATTEMPT_FILENAME).exists())
        self.assertEqual(self.job['state'], REPREPARING)
        self.assertEqual(json.loads(self.ledger_path.read_text())[self.job['source']['sha256']]['status'], REPREPARING)
        with preparation_guard(self.profile, self.job) as record:
            event(self.job, 'preparing')  # Same ordering as the normal CLI.
            begin_upload(self.job, self.profile)
            with self.assertRaises(DuplicateSubmission):
                begin_upload(self.job, self.profile)
            record['status'] = 'awaiting_user_publish'
        with self.assertRaises(DuplicateSubmission):
            with preparation_guard(self.profile, self.job):
                self.fail('authorization must be consumed')
        row = json.loads(self.ledger_path.read_text())[self.job['source']['sha256']]
        self.assertEqual(row['repreparation_authorization']['authorization_id'], authorization['authorization_id'])

    def test_missing_user_confirmation_never_clears_marker(self):
        self.confirmation['confirmed_not_published'] = False
        with self.assertRaises(ValueError):
            self.authorize()
        self.assertTrue((self.output / ATTEMPT_FILENAME).exists())

    def test_mismatched_video_proof_rejected(self):
        proof = json.loads(self.verification.read_text())
        proof['source_sha256'] = '0' * 64
        write_json(self.verification, proof)
        with self.assertRaises(ValueError):
            self.authorize()
        self.assertTrue((self.output / ATTEMPT_FILENAME).exists())

    def test_known_published_row_is_never_authorized(self):
        ledger = json.loads(self.ledger_path.read_text())
        ledger[self.job['source']['sha256']].update(status='published', publication_verified=True)
        write_json(self.ledger_path, ledger)
        with self.assertRaises(DuplicateSubmission):
            self.authorize()

    def test_authorization_cannot_be_replayed_in_another_directory(self):
        self.authorize()
        other = copy.deepcopy(self.job)
        other['output_dir'] = str(self.root / 'different-output')
        with self.assertRaises(DuplicateSubmission):
            with preparation_guard(self.profile, other):
                self.fail('one task only')


if __name__ == '__main__':
    unittest.main()
