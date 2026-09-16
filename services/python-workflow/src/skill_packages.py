"""Skill ZIP 校验；上传内容始终作为数据，不执行脚本。"""

import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from pathlib import PurePosixPath, PureWindowsPath

import yaml

ZIP_LIMIT = 20 * 1024 * 1024
TOTAL_LIMIT = 50 * 1024 * 1024
FILE_LIMIT = 10 * 1024 * 1024
FILE_COUNT = 500
OWNER_FILE = ".codex-platform-owner.json"


class SkillError(ValueError):
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_relative(name: str) -> str:
    parts = name.split("/")
    if (not name or len(name) > 240 or len(parts) > 16 or "\\" in name
            or any(not part or part in {".", ".."} or part.endswith((".", " "))
                   or re.search(r'[<>:"|?*\x00-\x1f]', part)
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)
                   for part in parts)):
        raise SkillError("压缩包包含不安全或不兼容的文件路径。")
    return name


def remote_root(value: str):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise SkillError("Skill 安装目录必须为有效绝对路径。")
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise SkillError("Skill 安装目录必须为专用绝对目录。")
    # UNC/设备路径不接受，避免不同机器及设备命名空间的歧义。
    if isinstance(path, PureWindowsPath) and not re.fullmatch(r"[A-Za-z]:", path.drive):
        raise SkillError("Skill 安装目录不支持网络共享或设备路径。")
    for part in path.parts[1:]:
        safe_relative(part)
    return path


class MetadataLoader(yaml.SafeLoader):
    """限制别名和重复键，避免 YAML 资源放大及歧义。"""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SkillError("Skill 元数据不允许 YAML 别名。")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise SkillError("Skill 元数据包含重复或非法字段。")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def parse_package(content: bytes) -> tuple[dict, dict[str, bytes]]:
    if len(content) > ZIP_LIMIT:
        raise SkillError("ZIP 不能超过 20 MiB。", 413)
    files = {}
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > FILE_COUNT * 3:
                raise SkillError("压缩包条目过多。", 413)
            for info in archive.infolist():
                path = safe_relative(info.orig_filename.rstrip("/"))
                if path.casefold() in seen:
                    raise SkillError("压缩包包含重复或大小写冲突的路径。")
                seen.add(path.casefold())
                mode = info.external_attr >> 16
                if info.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise SkillError("压缩包不支持加密文件、链接或特殊文件。")
                if path.split("/")[0] == "__MACOSX" or path.split("/")[-1] == ".DS_Store":
                    continue
                if info.is_dir():
                    continue
                if info.file_size > FILE_LIMIT or len(files) >= FILE_COUNT:
                    raise SkillError("每个文件最多 10 MiB，最多 500 个文件。", 413)
                with archive.open(info) as source:
                    data = source.read(min(FILE_LIMIT, TOTAL_LIMIT - total) + 1)
                total += len(data)
                if len(data) > FILE_LIMIT or total > TOTAL_LIMIT:
                    raise SkillError("解压内容超过容量限制。", 413)
                files[path] = data
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error, EOFError, UnicodeError) as error:
        raise SkillError("ZIP 文件损坏或压缩格式不受支持。") from error
    entries = [p for p in files if p.split("/")[-1].casefold() == "skill.md"]
    if len(entries) != 1 or entries[0].split("/")[-1] != "SKILL.md":
        raise SkillError("ZIP 必须包含且只包含一个 SKILL.md。")
    entry = entries[0]
    if entry.count("/") > 1:
        raise SkillError("Skill 最多允许一层外部目录。")
    prefix = entry[:-len("SKILL.md")]
    if any(not p.startswith(prefix) for p in files):
        raise SkillError("所有文件必须位于同一个 Skill 目录。")
    files = {p[len(prefix):]: data for p, data in files.items()}
    nodes = {}
    for path in files:
        if path.casefold() == OWNER_FILE.casefold():
            raise SkillError("压缩包包含平台保留文件名。")
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            name = "/".join(parts[:i])
            kind = "file" if i == len(parts) else "directory"
            old = nodes.get(name.casefold())
            if old and old != (name, kind):
                raise SkillError("压缩包存在文件目录或大小写冲突。")
            nodes[name.casefold()] = (name, kind)
    try:
        text = files["SKILL.md"].decode("utf-8-sig")
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
        if not match or len(match[1]) > 16_384:
            raise SkillError("SKILL.md 缺少 YAML 元数据，或元数据超过 16 KiB。")
        meta = yaml.load(match[1], Loader=MetadataLoader)
        if not isinstance(meta, dict):
            raise SkillError("Skill 元数据必须是对象。")
        name, description = meta.get("name"), meta.get("description")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise SkillError("Skill 名称须为最多 64 字符的小写字母、数字和单连字符。")
        safe_relative(name)
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
            raise SkillError("Skill 说明须为 1–1024 个字符。")
    except (UnicodeError, yaml.YAMLError, RecursionError) as error:
        raise SkillError("SKILL.md 必须是 UTF-8 且包含合法、安全的 YAML 元数据。") from error
    manifest = [{"path": p, "size": len(files[p]), "sha256": digest(files[p])} for p in sorted(files)]
    key = digest(json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode())
    return {"id": key, "name": name, "description": description.strip(), "manifest": manifest,
            "fileCount": len(files), "size": total, "hasScripts": any(
                p.startswith("scripts/") or p.lower().endswith((".py", ".sh", ".ps1", ".js", ".bat", ".cmd"))
                for p in files)}, files
