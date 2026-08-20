from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYTHON_SOURCE = REPOSITORY_ROOT / "services" / "python-workflow" / "src"
sys.path.insert(0, str(PYTHON_SOURCE))

from workflow_store import WorkflowStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="复制失败工作流并以新 ID 重新提交。")
    parser.add_argument("workflow_id", help="需要重试的原工作流 ID。")
    parser.add_argument("--db", required=True, help="工作流 SQLite 数据库路径。")
    parser.add_argument(
        "--gateway-url", default="http://127.0.0.1:8080", help="Python 网关地址。"
    )
    parser.add_argument("--new-id", help="新工作流 ID；默认添加时间戳后缀。")
    args = parser.parse_args()

    spec = WorkflowStore(Path(args.db)).get_spec(args.workflow_id)
    retry_suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    new_id = args.new_id or f"{args.workflow_id}-retry-{retry_suffix}"
    spec["workflowId"] = new_id

    body = json.dumps(spec, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{args.gateway_url.rstrip('/')}/workflows",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"网关返回 HTTP {error.code}：{detail}") from error

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
