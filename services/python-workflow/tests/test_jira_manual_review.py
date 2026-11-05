"""人工无提交分类：使用真实补充存储和结果投影，不依赖执行服务。"""
import json
import sqlite3
import unittest

from jira_history_review import apply_reviews, candidate, initialize, source
from workflow_jira import execution_result


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE workflows(workflow_id TEXT PRIMARY KEY,status TEXT,termination_json TEXT,
              jira_context_json TEXT,finished_at TEXT,response TEXT,error TEXT,spec_json TEXT,spec_zlib BLOB);
            CREATE TABLE workflow_jira_runs(workflow_id TEXT,delivery_version INTEGER);
            CREATE TABLE workflow_jira_deliveries(workflow_id TEXT,node_id TEXT,attempt INTEGER,status TEXT,
              reference TEXT,summary TEXT,submission_type TEXT,commits_json TEXT,recorded_at TEXT);
            CREATE TABLE workflow_nodes(workflow_id TEXT,node_id TEXT,status TEXT,response TEXT,error TEXT,position INTEGER);
            CREATE TABLE workflow_events(workflow_id TEXT,event_type TEXT,payload_json TEXT,payload_zlib BLOB,sequence INTEGER);
            INSERT INTO workflows VALUES('run','completed',NULL,'{}','today','完成',NULL,'{}',NULL);
            INSERT INTO workflow_jira_runs VALUES('run',2);
            INSERT INTO workflow_jira_deliveries VALUES('run','one',1,'completed','','已有修复',NULL,'[]','today');
        ''')
        initialize(self.db)
        self.db.commit()

    def review(self):
        review = candidate(source(self.db, 'run', allow_manual_completion=True))
        review.update(status='completed_no_submission', note='人工确认正常结束，无新增提交；不代表验证通过。')
        return review

    def test_explicit_idempotent_projection_preserves_original(self):
        before = tuple(self.db.execute('SELECT * FROM workflow_jira_deliveries').fetchone())
        self.assertEqual(execution_result(self.db, 'run')['status'], 'abnormal')
        review = self.review()
        self.assertEqual(apply_reviews(self.db, [review]), 1)
        self.assertEqual(apply_reviews(self.db, [review]), 0)
        result = execution_result(self.db, 'run')
        self.assertEqual((result['status'], result['source'], result['commits']),
                         ('completed_no_submission', 'manual_review', []))
        self.assertEqual(tuple(self.db.execute('SELECT * FROM workflow_jira_deliveries').fetchone()), before)

    def test_rejects_missing_or_contradictory_evidence(self):
        for state in ('submitted', 'unknown', 'not_submitted', 'blocked'):
            self.db.execute('UPDATE workflow_jira_deliveries SET status=?', (state,))
            with self.assertRaises(ValueError):
                apply_reviews(self.db, [self.review()])
        self.db.execute("UPDATE workflow_jira_deliveries SET status='completed',commits_json=?",
                        (json.dumps([{'repository':'repo','revision':'42'}]),))
        with self.assertRaises(ValueError):
            apply_reviews(self.db, [self.review()])
        self.db.execute("UPDATE workflow_jira_deliveries SET commits_json='[]'")
        for state in ('failed', 'cancelled', 'running'):
            self.db.execute('UPDATE workflows SET status=?', (state,))
            with self.assertRaises(ValueError):
                apply_reviews(self.db, [self.review()])

    def test_new_protocol_other_override_stale_and_blank_note_rejected(self):
        review = self.review()
        review['status'] = 'fully_submitted'
        with self.assertRaises(ValueError):
            apply_reviews(self.db, [review])
        review = self.review()
        review['note'] = ' '
        with self.assertRaises(ValueError):
            apply_reviews(self.db, [review])
        review = self.review()
        self.db.execute("UPDATE workflow_jira_deliveries SET summary='变化'")
        with self.assertRaisesRegex(ValueError, '证据已变化'):
            apply_reviews(self.db, [review])

    def test_batch_failure_rolls_back(self):
        review = self.review()
        with self.assertRaises(ValueError):
            with self.db:
                apply_reviews(self.db, [review, {**review, 'sourceHash':'stale'}])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM workflow_jira_history_reviews').fetchone()[0], 0)

    def test_manual_rework_preserves_failed_review_and_does_not_count_as_pass(self):
        review = self.review()
        review.update(status='rework_required', note='审核不通过，必要界面回归失败，已转交返工。')
        before = tuple(self.db.execute('SELECT * FROM workflow_jira_deliveries').fetchone())
        self.assertEqual(apply_reviews(self.db, [review]), 1)
        self.assertEqual(apply_reviews(self.db, [review]), 0)
        result = execution_result(self.db, 'run')
        self.assertEqual((result['status'], result['source']), ('rework_required', 'manual_review'))
        self.assertEqual(tuple(self.db.execute('SELECT * FROM workflow_jira_deliveries').fetchone()), before)
