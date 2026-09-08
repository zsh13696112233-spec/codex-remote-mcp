"""工作流对话图片：原始内容、消息引用和已确认返工范围。"""

import hashlib
import json
import sqlite3
import struct
import zlib
from typing import Any

IMAGE_BYTES_LIMIT = 20_000_000
IMAGE_COUNT_LIMIT = 5


def _jpeg_structure_valid(content: bytes) -> bool:
    offset = 2
    frame = False
    scan = False
    while offset < len(content):
        if content[offset] != 0xff:
            if not scan:
                return False
            offset += 1
            continue
        while offset < len(content) and content[offset] == 0xff:
            offset += 1
        if offset >= len(content):
            return False
        marker = content[offset]
        offset += 1
        if marker == 0xd9:
            return frame and scan and offset == len(content)
        if marker == 0x00 or 0xd0 <= marker <= 0xd7:
            if not scan:
                return False
            continue
        if marker in {0x01, 0xd8} or offset + 2 > len(content):
            return False
        size = int.from_bytes(content[offset:offset + 2], "big")
        if size < 2 or offset + size > len(content):
            return False
        if marker in {0xc0, 0xc1, 0xc2}:
            if size < 11 or not int.from_bytes(content[offset + 3:offset + 5], "big") or not int.from_bytes(content[offset + 5:offset + 7], "big"):
                return False
            frame = True
        if marker == 0xda:
            if not frame or size < 6:
                return False
            scan = True
        offset += size
    return False


def _webp_structure_valid(content: bytes, allow_animation: bool = True) -> bool:
    offset = 12
    image_found = False
    while offset + 8 <= len(content):
        tag = content[offset:offset + 4]
        size = int.from_bytes(content[offset + 4:offset + 8], "little")
        start = offset + 8
        end = start + size
        if end > len(content):
            return False
        if tag == b"VP8 ":
            if size < 10 or content[start + 3:start + 6] != b"\x9d\x01\x2a":
                return False
            image_found = True
        elif tag == b"VP8L":
            if size < 5 or content[start] != 0x2f:
                return False
            image_found = True
        elif tag == b"ANMF":
            if not allow_animation or size < 24:
                return False
            nested = content[start + 16:end]
            wrapped = b"RIFF" + struct.pack("<I", len(nested) + 4) + b"WEBP" + nested
            if not _webp_structure_valid(wrapped, allow_animation=False):
                return False
            image_found = True
        elif tag == b"VP8X" and size != 10:
            return False
        offset = end + (size % 2)
    return image_found and offset == len(content)


def image_media_type(content: bytes) -> str:
    if not content or len(content) > IMAGE_BYTES_LIMIT:
        raise ValueError("图片为空或超过 20 MB。")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        offset = 8
        first = True
        has_data = False
        while offset + 12 <= len(content):
            size = int.from_bytes(content[offset:offset + 4], "big")
            end = offset + size + 12
            if end > len(content):
                break
            tag = content[offset + 4:offset + 8]
            if first and (tag != b"IHDR" or size != 13):
                break
            if tag == b"IHDR" and (not int.from_bytes(content[offset + 8:offset + 12], "big")
                                  or not int.from_bytes(content[offset + 12:offset + 16], "big")):
                break
            first = False
            if zlib.crc32(content[offset + 4:end - 4]) != int.from_bytes(content[end - 4:end], "big"):
                break
            if tag == b"IDAT" and size:
                has_data = True
            if tag == b"IEND" and size == 0 and end == len(content) and has_data:
                return "image/png"
            offset = end
    elif content.startswith(b"\xff\xd8\xff") and _jpeg_structure_valid(content):
        return "image/jpeg"
    elif (len(content) >= 30 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
          and struct.unpack("<I", content[4:8])[0] + 8 == len(content)
          and content[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
          and _webp_structure_valid(content)):
        return "image/webp"
    raise ValueError("图片格式或内容无效，仅支持 PNG、JPEG、WebP。")


class InputImageStore:
    def initialize_input_images(self, connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_input_images (
                image_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
                media_type TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(workflow_id, image_id),
                FOREIGN KEY(workflow_id) REFERENCES workflows(workflow_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_revision_images (
                workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, image_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                PRIMARY KEY(workflow_id, node_id, image_id, action_id),
                FOREIGN KEY(workflow_id, image_id) REFERENCES workflow_input_images(workflow_id, image_id)
            );
        """)
        for table, fields in {
            "workflow_chat_messages": {"image_ids_json": "TEXT NOT NULL DEFAULT '[]'", "actor_id": "TEXT", "expected_action_id": "TEXT"},
            "workflow_control_actions": {"image_ids_json": "TEXT NOT NULL DEFAULT '[]'", "actor_id": "TEXT"},
        }.items():
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, definition in fields.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def save_input_image(self, workflow_id: str, content: bytes) -> dict[str, Any]:
        media_type = image_media_type(content)
        image_id = hashlib.sha256(content).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone():
                raise LookupError("找不到工作流。")
            exists = connection.execute("SELECT 1 FROM workflow_input_images WHERE workflow_id = ? AND image_id = ?", (workflow_id, image_id)).fetchone()
            if not exists:
                count = connection.execute("SELECT COUNT(*) FROM workflow_input_images WHERE workflow_id = ?", (workflow_id,)).fetchone()[0]
                count += connection.execute("SELECT COUNT(*) FROM workflow_artifacts WHERE workflow_id = ?", (workflow_id,)).fetchone()[0]
                count += connection.execute("SELECT COUNT(*) FROM workflow_attempt_artifacts WHERE workflow_id = ?", (workflow_id,)).fetchone()[0]
                if count >= 50:
                    raise ValueError("工作流文件数量已达到 50 个上限。")
                connection.execute("INSERT INTO workflow_input_images VALUES (?, ?, ?, ?)", (image_id, workflow_id, media_type, content))
        return {"imageId": image_id, "mediaType": media_type, "byteSize": len(content)}

    def get_input_image(self, workflow_id: str, image_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workflow_input_images WHERE workflow_id = ? AND image_id = ?", (workflow_id, image_id)).fetchone()
        if row is None:
            raise LookupError("找不到该任务的输入图片。")
        return {"imageId": image_id, "mediaType": row["media_type"], "content": bytes(row["content"])}

    def input_images(self, workflow_id: str, image_ids: list[str]) -> list[dict[str, Any]]:
        return [self.get_input_image(workflow_id, value) for value in image_ids]

    def node_input_images(self, workflow_id: str, node_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT image_id FROM workflow_revision_images WHERE workflow_id = ? AND node_id = ? GROUP BY image_id ORDER BY MIN(rowid)", (workflow_id, node_id))]
        return self.input_images(workflow_id, ids)

    @staticmethod
    def validate_control_actor(connection: sqlite3.Connection, workflow_id: str, action: sqlite3.Row, message: sqlite3.Row | None) -> None:
        if message is None:
            raise ValueError("找不到确认消息。")
        if action["actor_id"] != message["actor_id"]:
            raise ValueError("只能由提出该操作的人确认或取消。")
        expected = message["expected_action_id"]
        if (message["actor_id"] and expected is None) or (expected and expected != action["action_id"]):
            raise ValueError("待确认操作已变化，请引用对应确认消息重新操作。")

    @staticmethod
    def validate_message_images(connection: sqlite3.Connection, workflow_id: str, image_ids: list[str]) -> None:
        if not isinstance(image_ids, list) or len(image_ids) > IMAGE_COUNT_LIMIT:
            raise ValueError("每条消息最多 5 张图片。")
        total = 0
        for value in image_ids:
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("图片编号无效。")
            row = connection.execute("SELECT length(content) FROM workflow_input_images WHERE workflow_id = ? AND image_id = ?", (workflow_id, value)).fetchone()
            if row is None:
                raise ValueError("图片不属于该工作流或不存在。")
            total += row[0]
        if total > IMAGE_BYTES_LIMIT:
            raise ValueError("每条消息图片合计不能超过 20 MB。")
