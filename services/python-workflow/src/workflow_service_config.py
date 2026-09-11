"""读取统一部署配置；机器清单始终由中央登记提供。"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SERVICE_CONFIG_PATH = REPOSITORY_ROOT / "config" / "workflow-service.json"

FIELDS = {
    name: tuple(name.split("."))
    for name in (
        "workflow_db", "machine_defaults.cwd", "machine_defaults.protocol",
        "machine_defaults.model", "machine_defaults.allow_write", "machine_defaults.allow_full_access",
        "machine_defaults.token_env", "machine_defaults.token_file",
        "machine_defaults.sidecar_token_template", "machine_defaults.orchestration_mode",
        "machine_defaults.artifact_root", "sidecar.host", "sidecar.port",
        "sidecar.gateway_url", "sidecar.token_env", "sidecar.token_file",
    )
}


@lru_cache(maxsize=4)
def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65_536:
            raise ValueError("服务配置文件不能超过 64 KiB。")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("无法读取服务配置，请检查 workflow-service.json 的编码和 JSON 格式。") from error
    if not isinstance(value, dict):
        raise ValueError("服务配置必须是 JSON 对象。")
    allowed = {parts[0] for parts in FIELDS.values()}
    if set(value) - allowed:
        raise ValueError("服务配置包含未知字段，请对照示例；实际令牌不能写入配置文件。")
    for section in ("machine_defaults", "sidecar"):
        block = value.get(section, {})
        if not isinstance(block, dict):
            raise ValueError(f"{section} 必须是 JSON 对象。")
        known = {parts[1] for parts in FIELDS.values() if len(parts) == 2 and parts[0] == section}
        if set(block) - known:
            raise ValueError(f"{section} 包含未知字段；只允许凭据引用，不允许实际令牌。")
        if block.get("token_env") and block.get("token_file"):
            raise ValueError(f"{section} 的 token_env 和 token_file 只能选一个。")
    for parts in FIELDS.values():
        item = value.get(parts[0]) if len(parts) == 1 else value.get(parts[0], {}).get(parts[1])
        if item is None:
            continue
        key = parts[-1]
        if key in {"allow_write", "allow_full_access"}:
            if not isinstance(item, bool):
                raise ValueError(f"{key} 必须是布尔值。")
        elif key == "port":
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65535:
                raise ValueError("Sidecar 端口必须为 1–65535 的整数。")
        elif not isinstance(item, str) or not item.strip():
            raise ValueError(f"{key} 必须是非空字符串。")
        if key in {"token_file", "sidecar_token_template", "artifact_root"} and not Path(item).is_absolute():
            raise ValueError(f"{key} 必须是服务所在机器上的绝对路径。")
    if value.get("machine_defaults", {}).get("protocol", "ws") not in {"ws", "wss"}:
        raise ValueError("protocol 只能为 ws 或 wss。")
    if value.get("machine_defaults", {}).get("orchestration_mode", "remote_sidecar") not in {"local_db", "remote_sidecar"}:
        raise ValueError("orchestration_mode 只能为 local_db 或 remote_sidecar。")
    defaults = value.get("machine_defaults", {})
    if defaults.get("allow_full_access") and not defaults.get("allow_write"):
        raise ValueError("allow_full_access=true 时必须同时启用 allow_write。")
    return value


def setting(name: str, default: Any = None) -> Any:
    config = _load(SERVICE_CONFIG_PATH)
    parts = FIELDS[name]
    result = config.get(parts[0], default) if len(parts) == 1 else config.get(parts[0], {}).get(parts[1], default)
    if result is None:
        return default
    if name == "workflow_db" and result:
        path = Path(result).expanduser()
        return str(path if path.is_absolute() else (REPOSITORY_ROOT / path).resolve())
    return result
