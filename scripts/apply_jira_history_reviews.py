"""应用已经人工核对的历史结果清单；默认只检查，--apply 才写补充表。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/python-workflow/src'))
from jira_history_review import apply_reviews, fingerprint, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    reviews = json.loads(args.reviews.read_text(encoding='utf-8'))
    if not isinstance(reviews, list) or len({r['workflowId'] for r in reviews}) != len(reviews):
        raise ValueError('核对清单格式无效或包含重复运行。')
    db = sqlite3.connect(args.database.resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    with db:
        db.execute('BEGIN IMMEDIATE' if args.apply else 'BEGIN')
        for review in reviews:
            if fingerprint(source(db, review['workflowId'], allow_manual_completion=review['status'] in ('completed_no_submission', 'rework_required'))) != review['sourceHash']:
                raise ValueError('历史记录已变化，拒绝应用过期结果。')
        inserted = apply_reviews(db, reviews) if args.apply else 0
    print(json.dumps({'mode': 'applied' if args.apply else 'checked', 'records': len(reviews),
                      'inserted': inserted, 'statuses': dict(Counter(r['status'] or 'special' for r in reviews))}, ensure_ascii=False))
    db.close()


if __name__ == '__main__':
    main()
