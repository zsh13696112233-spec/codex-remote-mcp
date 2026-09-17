"""检测信息只保存白名单状态和分类说明，不保存执行服务原始错误。"""

from skill_store import now

STATES = {"notStarted": "尚未启动", "starting": "正在连接", "connected": "已连接",
          "authenticationRequired": "需要认证", "failed": "连接失败", "cancelled": "连接已取消",
          "disabled": "服务被禁用"}


def tools_discovered(row):
    """旧执行服务可以不提供 runtimeStatus；显式失败状态仍必须拒绝。"""
    return bool(isinstance(row, dict) and isinstance(row.get('tools'), dict) and row['tools']
                and not row.get('toolsError') and row.get('runtimeStatus') in (None, 'connected'))


def safe_reason(error):
    if isinstance(error, InterruptedError):
        return '检测已中断，请重新检测。'
    text = str(error)[:16000].lower()
    if 'base-url' in text or 'jira_baseurl' in text or 'server address is required' in text:
        return "程序报告缺少服务地址，请核对执行服务启动程序时读取的配置。"
    if isinstance(error, TimeoutError) or 'timeout' in text or 'timed out' in text:
        return "等待执行服务或 MCP 响应超时。"
    if any(word in text for word in ('unauthorized', 'authentication', 'not logged in', '401')):
        return "认证未通过，请检查程序使用的账号配置。"
    if any(word in text for word in ('permission denied', 'access denied', '拒绝访问')):
        return "启动程序或访问文件时被拒绝。"
    if any(word in text for word in ('initialize', 'handshake', '握手')):
        return "MCP 初始化握手未完成，程序启动成功不代表协议连接成功。"
    if any(word in text for word in ('connection closed', 'broken pipe', 'eof')):
        return "协议连接提前关闭，程序可能退出或输出了非协议内容。"
    if isinstance(error, ConnectionError):
        return "与执行服务的连接中断。"
    return "执行服务返回错误，但无法安全分类；原始内容未保存或展示。"


def diagnostic(stage, found=None, error=None, pending=False):
    row = found if isinstance(found, dict) else None
    tools = row.get('tools') if row is not None else None
    result = {'checkedAt': now(), 'stage': stage, 'pending': pending,
              'found': None if pending or error is not None else row is not None,
              'connection': STATES.get(row.get('runtimeStatus'), '执行服务未提供状态') if row is not None else '尚未取得状态',
              'toolCount': len(tools) if isinstance(tools, dict) else None, 'reason': ''}
    if pending:
        return result
    if error is not None:
        result['reason'] = safe_reason(error)
    elif row is None:
        result['reason'] = '执行服务的工具状态列表中未找到这个 MCP；请核对配置加载情况。'
    elif row.get('toolsError'):
        result['reason'] = safe_reason(row['toolsError'])
    elif tools_discovered(row) and row.get('runtimeStatus') is None:
        result['reason'] = '工具发现成功；执行服务未提供连接状态，尚未执行任何业务工具。'
    elif row.get('runtimeStatus') != 'connected':
        result['reason'] = '尚未确认 MCP 已连接。' + result['connection'] + '。'
    elif not isinstance(tools, dict) or not tools:
        result['reason'] = 'MCP 已连接，但未返回可用工具。'
    else:
        result['reason'] = '已连接并发现工具；尚未执行任何业务工具。'
    return result
