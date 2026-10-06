"""半自动第一步主文档：受控读取与中央快照。"""
import asyncio
import base64
import hashlib
import logging
import sqlite3
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

FILE_LIMIT = 512 * 1024
METADATA_COLUMNS = "document_id,node_id,name,format,revision,updated_at,error,removed,content IS NOT NULL AS available"
ERROR = "主文档未同步，请检查是否生成文件及其格式、大小和读取权限。"
DOCUMENT_SCHEMA = {
    "anyOf": [{"type": "null"}, {
        "type": "object", "additionalProperties": False, "required": ["name", "path"],
        "properties": {"name": {"type": "string", "maxLength": 160},
                       "path": {"type": "string", "maxLength": 4096}},
    }],
}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "document"],
    "properties": {"summary": {"type": "string", "maxLength": 20000}, "document": DOCUMENT_SCHEMA},
}
INSTRUCTION = (
    "\n本步骤展示一份主文档。最终按结构返回 summary（完整交接总结）和 document。"
    "document 为 name（展示名称）和 path（实际工作目录内文件路径）对象；无交付文件时为 null。"
    "只报告本次明确交付的 UTF-8 .md/.markdown/.txt 主文档，最大512 KiB；"
    "不要列入参考资料、日志、敏感配置或凭据，不返回文档正文。"
)


def enabled(connection: sqlite3.Connection, workflow_id: str, node_id: str) -> bool:
    return connection.execute("""SELECT 1 FROM workflow_nodes n JOIN workflows w USING(workflow_id)
        WHERE n.workflow_id=? AND n.node_id=? AND n.position=0 AND w.advance_mode='semi_automatic'
        AND EXISTS (SELECT 1 FROM workflow_nodes next WHERE next.workflow_id=n.workflow_id AND next.position=1)""",
        (workflow_id, node_id)).fetchone() is not None


def failure() -> dict[str, Any]:
    return {"name": "主文档", "error": ERROR}


def path_for(cwd: str, value: str) -> tuple[PurePosixPath | PureWindowsPath, str]:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError(ERROR)
    cls = PureWindowsPath if PureWindowsPath(cwd).drive else PurePosixPath
    root, path = cls(cwd), cls(value)
    if not root.is_absolute() or '..' in path.parts:
        raise ValueError(ERROR)
    if path.drive and not path.is_absolute():
        raise ValueError(ERROR)
    if not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    if not relative.parts or path.suffix.lower() not in {'.md', '.markdown', '.txt'}:
        raise ValueError(ERROR)
    if any(p.startswith('.') or p.lower() in {'config', 'secrets', 'credentials', 'node_modules', 'target'}
           or ':' in p for p in relative.parts):
        raise ValueError(ERROR)
    if any(word in path.name.lower() for word in ('token', 'credential', 'password', 'secret')):
        raise ValueError(ERROR)
    return path, str(relative).casefold() if cls is PureWindowsPath else str(relative)


def valid_name(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 160
            and not any(ord(c) < 32 for c in value) and '/' not in value and '\\' not in value)


async def collect(client: Any, cwd: str, document: Any, denied_paths=()) -> dict[str, Any]:
    """只读取明确报告的主文档；无文件或读取失败不影响业务步骤完成。"""
    try:
        if not isinstance(document, dict) or set(document) != {'name', 'path'} or not valid_name(document['name']):
            raise ValueError(ERROR)
        path, _ = path_for(cwd, document['path'])
        if any(type(path)(str(denied)) == path for denied in denied_paths if denied):
            raise ValueError(ERROR)

        async def read():
            for parent in reversed(path.parents):
                meta = await client.request('fs/getMetadata', {'path': str(parent)})
                if meta.get('isSymlink') is not False or meta.get('isDirectory') is not True:
                    raise ValueError(ERROR)
            before = await client.request('fs/getMetadata', {'path': str(path)})
            if before.get('isSymlink') is not False or before.get('isFile') is not True:
                raise ValueError(ERROR)
            size = before.get('size', before.get('byteSize'))
            if isinstance(size, (int, float)) and size > FILE_LIMIT:
                raise ValueError(ERROR)
            response = await client.request('fs/readFile', {'path': str(path)})
            encoded = response.get('dataBase64')
            if not isinstance(encoded, str) or len(encoded) > ((FILE_LIMIT + 2) // 3) * 4:
                raise ValueError(ERROR)
            content = base64.b64decode(encoded, validate=True)
            if len(content) > FILE_LIMIT:
                raise ValueError(ERROR)
            after = await client.request('fs/getMetadata', {'path': str(path)})
            if before != after:
                raise ValueError(ERROR)
            text = content.decode('utf-8-sig')
            if '\0' in text:
                raise ValueError(ERROR)
            return text

        content = await asyncio.wait_for(read(), 10)
        return {"name": document['name'], "content": content,
                "format": "text" if path.suffix.lower() == '.txt' else "markdown"}
    except Exception:
        logging.getLogger(__name__).warning("主文档未同步，保留上次成功正文。")
        return failure()


def initialize(connection: sqlite3.Connection) -> None:
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(workflow_nodes)')}
    if 'display_documents' not in columns:
        connection.execute('ALTER TABLE workflow_nodes ADD COLUMN display_documents INTEGER NOT NULL DEFAULT 0')
    connection.execute('''CREATE TABLE IF NOT EXISTS workflow_documents (
        workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, document_id TEXT NOT NULL,
        source_key TEXT NOT NULL, name TEXT NOT NULL, content TEXT, format TEXT,
        checksum TEXT, revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
        error TEXT, removed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(workflow_id, document_id), UNIQUE(workflow_id,node_id,source_key))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS workflow_document_batches (
        workflow_id TEXT NOT NULL, node_id TEXT NOT NULL, source_id TEXT NOT NULL,
        PRIMARY KEY(workflow_id,node_id,source_id))''')


def validate_result(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or not valid_name(item.get('name')):
        raise ValueError('主文档名称无效。')
    if set(item) == {'name', 'error'} and item['error'] == ERROR:
        return item
    if set(item) != {'name', 'content', 'format'}:
        raise ValueError('主文档字段无效。')
    if not isinstance(item['content'], str) or '\0' in item['content'] or item['format'] not in {'text', 'markdown'}:
        raise ValueError('主文档正文无效。')
    if len(item['content'].encode('utf-8')) > FILE_LIMIT:
        raise ValueError('主文档超过容量限制。')
    return item


def save(connection: sqlite3.Connection, workflow_id: str, node_id: str, source_id: str,
         item: dict[str, Any], now: str) -> None:
    if not enabled(connection, workflow_id, node_id):
        raise ValueError('仅半自动第一步且存在下一步时可以保存主文档。')
    validate_result(item)
    if connection.execute('SELECT 1 FROM workflow_document_batches WHERE workflow_id=? AND node_id=? AND source_id=?',
                          (workflow_id, node_id, source_id)).fetchone():
        return
    # 首个历史文档沿用原链接；不清理其他历史正文，旧链接继续可读。
    old = connection.execute('''SELECT * FROM workflow_documents WHERE workflow_id=? AND node_id=?
        ORDER BY CASE WHEN source_key='@main' THEN 0 ELSE 1 END, removed, rowid LIMIT 1''',
        (workflow_id, node_id)).fetchone()
    document_id = old['document_id'] if old else str(uuid.uuid5(uuid.NAMESPACE_URL, f'{workflow_id}/{node_id}/@main'))
    content = item.get('content', old['content'] if old else None)
    name = old['name'] if old and item.get('error') else item['name']
    fmt = item.get('format', old['format'] if old else None)
    checksum = hashlib.sha256(content.encode()).hexdigest() if content is not None else None
    connection.execute('''INSERT INTO workflow_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(workflow_id,document_id) DO UPDATE SET source_key=excluded.source_key,
        name=excluded.name,content=excluded.content,format=excluded.format,checksum=excluded.checksum,
        revision=workflow_documents.revision+1,updated_at=excluded.updated_at,error=excluded.error,removed=0''',
        (workflow_id, node_id, document_id, '@main', name, content, fmt, checksum, 1, now, item.get('error'), 0))
    connection.execute('INSERT INTO workflow_document_batches VALUES(?,?,?)', (workflow_id, node_id, source_id))
    connection.execute('UPDATE workflows SET state_version=state_version+1 WHERE workflow_id=?', (workflow_id,))
    connection.execute('''INSERT INTO workflow_node_revisions VALUES(?,?,1)
        ON CONFLICT(workflow_id,node_id) DO UPDATE SET revision=revision+1''', (workflow_id, node_id))


def view(row: sqlite3.Row, body: bool = False) -> dict[str, Any]:
    result = {'id': row['document_id'], 'nodeId': row['node_id'], 'name': row['name'],
              'format': row['format'], 'revision': row['revision'], 'updatedAt': row['updated_at'],
              'error': row['error'], 'removed': bool(row['removed']), 'available': bool(row['available']) if 'available' in row.keys() else row['content'] is not None}
    if body:
        result['content'] = row['content']
    return result
