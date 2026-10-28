"""macOS 测试服务启动、停止和状态查询，不操作正式服务。"""
import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / '.runtime'
PORTS = {'gateway': 18080, 'console': 18090, 'config-center': 18091}


def listeners(port):
    result = subprocess.run(
        ['lsof', '-nP', '-t', f'-iTCP:{port}', '-sTCP:LISTEN'],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError('无法查询监听进程：' + result.stderr.strip())
    return {int(value) for value in result.stdout.split()}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop(name):
    port = PORTS[name]
    path = BASE / 'run' / f'{name}.pid'
    if not path.exists():
        if listeners(port):
            raise RuntimeError(f'{name}: 端口 {port} 被占用，但没有启动记录；请在原启动窗口停止。')
        print(f'{name}: 已停止')
        return
    pid = int(path.read_text().strip())
    if pid <= 1:
        raise RuntimeError('进程编号无效，拒绝停止。')
    if not alive(pid):
        if listeners(port):
            raise RuntimeError(f'{name}: PID 记录已过期，端口由其他进程占用；拒绝停止。')
        path.unlink()
        print(f'{name}: 已停止，已清理过期 PID 记录')
        return
    cwd = subprocess.run(
        ['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    if 'n' + str(ROOT) not in cwd or listeners(port) != {pid}:
        raise RuntimeError(f'{name}: PID、测试端口或工作目录不匹配；拒绝停止。')
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while alive(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{name}: 已请求正常停止，但 30 秒后仍未退出；请检查日志，未强制结束。')
        time.sleep(0.2)
    path.unlink()
    print(f'{name}: 已停止 PID {pid}')


def status(name):
    pids = listeners(PORTS[name])
    if pids:
        print(f'{name}: 端口 {PORTS[name]} 正在监听，PID {", ".join(map(str, sorted(pids)))}')
    else:
        print(f'{name}: 已停止（端口 {PORTS[name]} 无监听）')


def check_port(port):
    with socket.socket() as probe:
        # 与服务监听套接字一致，允许重用已关闭连接的 TIME_WAIT 端口。
        # 不启用 SO_REUSEPORT，仍拒绝已有监听服务占用的端口。
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', port))


def start(name):
    port = PORTS[name]
    path = BASE / 'run' / f'{name}.pid'
    if path.exists():
        pid = int(path.read_text().strip())
        if pid <= 1 or alive(pid):
            raise RuntimeError(f'{name}: 已有活动进程记录，请先检查状态，拒绝重复启动。')
    for directory in (ROOT / 'logs', BASE / 'tmp', BASE / 'run'):
        directory.mkdir(parents=True, exist_ok=True)
    check_port(port)
    env = {key: os.environ[key] for key in ('HOME', 'PATH', 'JAVA_HOME', 'LANG') if key in os.environ}
    env.update(TMPDIR=str(BASE/'tmp'), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(ROOT/'services/python-workflow/src'))
    if name == 'gateway':
        if os.environ.get('CODEX_APP_SERVER_TOKEN'):
            env['CODEX_APP_SERVER_TOKEN'] = os.environ['CODEX_APP_SERVER_TOKEN']
        command = ['/Users/zhangshuhao/Documents/codex-remote-mcp/.venv/bin/python',
                   str(ROOT/'services/python-workflow/src/workflow_gateway.py'),
                   '--host', '127.0.0.1', '--port', str(port)]
    else:
        module = 'workflow-console' if name == 'console' else 'role-task-config-center'
        env.update(SERVER_ADDRESS='127.0.0.1', SERVER_PORT=str(port),
                   CODEX_GATEWAY_URL='http://127.0.0.1:18080',
                   WORKFLOW_MONITOR_URL='http://127.0.0.1:18090',
                   DINGTALK_ENABLED='false', DINGTALK_CLIENT_ID='', DINGTALK_CLIENT_SECRET='')
        if name == 'config-center':
            env['MYSQL_URL'] = 'jdbc:mysql://192.168.5.236:3306/codex_sop_dev?useUnicode=true&characterEncoding=utf8&serverTimezone=Asia/Shanghai'
        command = [shutil.which('java'), '-Djava.io.tmpdir='+str(BASE/'tmp'),
                   '-jar', str(ROOT/'services'/module/'target'/f'{module}-0.1.0.jar')]
    with (ROOT/'logs'/f'{name}.log').open('ab') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    (BASE/'run'/f'{name}.pid').write_text(str(process.pid)+'\n')
    print(f"{name}: 已启动 PID {process.pid}，端口 {port}；日志：{ROOT / 'logs' / (name + '.log')}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('service', choices=PORTS, help='测试服务名称')
    parser.add_argument('action', nargs='?', default='start', choices=('start', 'stop', 'status'))
    args = parser.parse_args()
    try:
        {'start': start, 'stop': stop, 'status': status}[args.action](args.service)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'操作失败：{error}\n')


if __name__ == '__main__':
    main()
