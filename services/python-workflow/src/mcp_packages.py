"""MCP 安装包边界；不执行上传内容，不要求 Skill 入口。"""

import io
import json
import stat
import zipfile
import zlib
from pathlib import PureWindowsPath

from skill_packages import SkillError, digest, remote_root, safe_relative

ZIP_LIMIT = 20 * 1024 * 1024
TOTAL_LIMIT = 200 * 1024 * 1024
FILE_LIMIT = 32 * 1024 * 1024
FILE_COUNT = 1000


def windows_path(value):
    path = remote_root(value)
    if not isinstance(path, PureWindowsPath):
        raise SkillError("MCP 首版仅支持 Windows 绝对目录。")
    return path


def within(value, root):
    path, parent = windows_path(value), windows_path(root)
    return path != parent and parent in path.parents


def parse_package(content):
    if not isinstance(content, bytes) or not content or len(content) > ZIP_LIMIT:
        raise SkillError("请上传不超过 20 MiB 的 ZIP。", 413)
    files, nodes, total = {}, {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > FILE_COUNT * 3:
                raise SkillError("压缩包条目过多。", 413)
            seen = set()
            for entry in archive.infolist():
                name = safe_relative(entry.orig_filename.rstrip("/"))
                if name.split("/")[-1].casefold() in {".mcp-install-owner.json", ".mcp-package.zip"}:
                    raise SkillError("压缩包包含平台保留文件名。")
                if any(part.casefold() == ".mcp-transfer" for part in name.split("/")):
                    raise SkillError("压缩包包含平台保留的分块缓存目录名。")
                if name.casefold() in seen:
                    raise SkillError("压缩包存在重复路径。")
                seen.add(name.casefold())
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if entry.flag_bits & 1 or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise SkillError("压缩包不允许加密、链接或特殊文件。")
                parts = name.split("/")
                for i in range(1, len(parts) + 1):
                    node = "/".join(parts[:i])
                    kind = "directory" if i < len(parts) or entry.is_dir() else "file"
                    old = nodes.get(node.casefold())
                    if old and old != (node, kind):
                        raise SkillError("压缩包存在大小写或文件目录冲突。")
                    nodes[node.casefold()] = (node, kind)
                if entry.is_dir():
                    continue
                if entry.file_size > FILE_LIMIT or len(files) >= FILE_COUNT:
                    raise SkillError("单文件不能超过 32 MiB，最多 1000 个文件。", 413)
                with archive.open(entry) as stream:
                    data = stream.read(min(FILE_LIMIT, TOTAL_LIMIT - total) + 1)
                total += len(data)
                if len(data) > FILE_LIMIT or total > TOTAL_LIMIT:
                    raise SkillError("解压内容超过容量限制。", 413)
                files[name] = data
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError,
            zlib.error, EOFError, UnicodeError) as error:
        raise SkillError("ZIP 损坏或格式不受支持。") from error
    if not files:
        raise SkillError("压缩包不能为空。")
    manifest = [{"path": name, "size": len(data), "sha256": digest(data)}
                for name, data in sorted(files.items())]
    identity = digest(json.dumps(manifest, sort_keys=True).encode())
    return {"id": identity, "fileCount": len(files), "size": total,
            "manifest": manifest}, files


def installation_settings(value):
    defaults = {"enabled": False, "platform": "windows", "temporaryRoot": "",
                "programRoot": "", "runtimes": [], "installRoot": ""}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise SkillError("MCP 安装设置包含未知字段。")
    result = {**defaults, **value}
    if not isinstance(result["enabled"], bool) or result["platform"] != "windows":
        raise SkillError("MCP 安装设置须指定 Windows 和布尔授权开关。")
    if "installRoot" in value and (value["installRoot"] or not value.get("programRoot")):
        if not isinstance(value["installRoot"], str):
            raise SkillError("安装目录必须为字符串。")
        root = str(windows_path(value["installRoot"])) if value["installRoot"] or result["enabled"] else ""
        return {"enabled": result["enabled"], "platform": "windows", "installRoot": root,
                "temporaryRoot": root, "programRoot": root, "runtimes": []}
    # 已有机器和任务保留原目录；用户保存新设置时才切换到统一目录。
    result.pop("installRoot")
    roots = []
    for key in ("temporaryRoot", "programRoot"):
        if not isinstance(result[key], str):
            raise SkillError("安装目录必须为字符串。")
        if result[key] or result["enabled"]:
            result[key] = str(windows_path(result[key]))
            roots.append(windows_path(result[key]))
    if len(roots) == 2 and (roots[0] == roots[1] or roots[0] in roots[1].parents
                            or roots[1] in roots[0].parents):
        raise SkillError("临时目录和程序目录不能相同或互相包含。")
    runtimes = result["runtimes"]
    if not isinstance(runtimes, list) or len(runtimes) > 10:
        raise SkillError("最多配置 10 个运行时绝对路径。")
    result["runtimes"] = [str(windows_path(path)) for path in runtimes]
    return result
