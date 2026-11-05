import json
import unittest

from jira_history_review import apply_reviews, candidate, source
from workflow_jira import execution_result
from tests import test_jira_delivery as fixtures
from tests.test_workflow_jira import outcome


class HistoryReviewTests(unittest.TestCase):
    setUp = fixtures.DeliveryTests.setUp
    create = fixtures.DeliveryTests.create
    ready = fixtures.DeliveryTests.ready
    claim = fixtures.DeliveryTests.claim

    def completed(self):
        self.create('old')
        self.ready('old')
        self.store.prepare_node_dispatch('old', 'one')
        self.store.sync_node_job('old', 'one', {'status': 'completed', 'businessResult': outcome(status='submitted', reference='r42')})
        self.store.finish_workflow('old', supervisor_status='completed', response='完成', error=None)

    def test_review_projection_preserves_protocol_and_original_receipt(self):
        self.completed()
        original = self.store.get_spec('old')
        with self.store._connect() as db:
            review = candidate(source(db, 'old'))
            review.update(status='limited_submitted', commits=[{'repository':'客户端','revision':'r42'}], note='审核明确验证受限，回执确认本轮编号。')
            db.execute('BEGIN IMMEDIATE')
            self.assertEqual(apply_reviews(db, [review]), 1)
            self.assertEqual(apply_reviews(db, [review]), 0)
            result = execution_result(db, 'old')
            self.assertEqual(result['status'], 'limited_submitted')
            self.assertEqual(result['source'], 'historical_review')
            self.assertEqual(db.execute('SELECT submission_type FROM workflow_jira_deliveries').fetchone()[0], None)
        self.assertEqual(self.store.get_spec('old'), original)

    def test_stale_evidence_and_conflicting_review_rejected(self):
        self.completed()
        with self.store._connect() as db:
            review = candidate(source(db, 'old'))
            apply_reviews(db, [review])
            review['status'] = 'abnormal'
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                apply_reviews(db, [review])
            db.execute("UPDATE workflow_nodes SET response='变化' WHERE workflow_id='old'")
            with self.assertRaisesRegex(ValueError, '证据已变化'):
                apply_reviews(db, [review])

    def test_does_not_guess_completed_and_never_changes_new_or_running(self):
        self.create('running')
        with self.store._connect() as db:
            with self.assertRaises(ValueError):
                source(db, 'running')
            db.execute("UPDATE workflows SET status='completed' WHERE workflow_id='running'")
            self.assertEqual(candidate(source(db, 'running'))['status'], 'unknown')
            db.execute("UPDATE workflow_jira_runs SET delivery_version=2 WHERE workflow_id='running'")
            with self.assertRaises(ValueError):
                source(db, 'running')

    def test_atomic_rollback_on_changed_record(self):
        self.completed()
        with self.store._connect() as db:
            review = candidate(source(db, 'old'))
        with self.assertRaises(ValueError):
            with self.store._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                apply_reviews(db, [review, {**review, 'sourceHash':'stale'}])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workflow_jira_history_reviews').fetchone()[0], 0)
