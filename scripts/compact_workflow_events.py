"""默认只报告旧事件容量；显式 --apply 才执行无损压缩，不删除运行、事件或附件。"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="中央 SQLite 的绝对路径")
    parser.add_argument("--before", required=True, help="只处理此前结束的运行，ISO 8601，必须包含时区")
    parser.add_argument("--apply", action="store_true", help="确认执行无损压缩，默认只读统计")
    args = parser.parse_args()
    if not args.db.is_absolute() or not args.db.is_file():
        parser.error("--db 必须是已经存在的数据库绝对路径。")
    try:
        before = datetime.fromisoformat(args.before)
        if before.tzinfo is None:
            raise ValueError()
        cutoff = before.astimezone(timezone.utc).isoformat()
    except ValueError:
        parser.error("--before 必须是包含时区的 ISO 8601 时间。")
    if not args.apply:
        with closing(sqlite3.connect(args.db.as_uri() + "?mode=ro", uri=True)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_events)")}
            uncompressed = "AND e.payload_zlib IS NULL" if "payload_zlib" in columns else ""
            count, size = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(e.payload_json AS BLOB))), 0) "
                "FROM workflow_events e JOIN workflows w USING(workflow_id) "
                "WHERE w.status IN ('completed', 'failed', 'cancelled') AND w.finished_at < ? "
                "AND LENGTH(e.payload_json) >= 1024 " + uncompressed, (cutoff,)
            ).fetchone()
        print(json.dumps({"dryRun": True, "candidateEvents": count, "uncompressedBytes": size}))
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/python-workflow/src"))
    from workflow_store import WorkflowStore
    store = WorkflowStore(args.db)
    totals = {"scanned": 0, "compacted": 0, "savedBytes": 0}
    cursor = 0
    while True:
        result = store.compact_terminal_events(cutoff, after=cursor)
        for key in totals:
            totals[key] += result[key]
        if not result["scanned"]:
            break
        cursor = result["nextCursor"]
    print(json.dumps({"dryRun": False, **totals}))


if __name__ == "__main__":
    main()
