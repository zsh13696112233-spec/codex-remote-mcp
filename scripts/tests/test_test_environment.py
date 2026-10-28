import importlib.util
from pathlib import Path
import signal
import socket
import errno
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'test_environment', Path(__file__).resolve().parents[1] / 'start_test_environment.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / 'run').mkdir()
        self.pid_file = self.base / 'run/gateway.pid'
        self.pid_file.write_text('12345\n')
        base_patch = patch.object(module, 'BASE', self.base)
        base_patch.start()
        self.addCleanup(base_patch.stop)

    def test_wrong_worktree_never_signalled(self):
        result = subprocess.CompletedProcess([], 0, 'p12345\nn/production\n')
        with patch.object(module, 'alive', return_value=True), patch.object(
            module.subprocess, 'run', return_value=result
        ), patch.object(module.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '不匹配'):
                module.stop('gateway')
            kill.assert_not_called()
        self.assertTrue(self.pid_file.exists())

    def test_wrong_listener_never_signalled(self):
        result = subprocess.CompletedProcess([], 0, 'p12345\nn' + str(module.ROOT) + '\n')
        with patch.object(module, 'alive', return_value=True), patch.object(
            module.subprocess, 'run', return_value=result
        ), patch.object(module, 'listeners', return_value={99999}), patch.object(module.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '不匹配'):
                module.stop('gateway')
            kill.assert_not_called()

    def test_stale_pid_with_occupied_port_is_preserved(self):
        with patch.object(module, 'alive', return_value=False), patch.object(
            module, 'listeners', return_value={99999}
        ), patch.object(module.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '过期'):
                module.stop('gateway')
            kill.assert_not_called()
        self.assertTrue(self.pid_file.exists())

    def test_stale_pid_empty_port_is_cleaned(self):
        with patch.object(module, 'alive', return_value=False), patch.object(
            module, 'listeners', return_value=set()
        ), patch.object(module.os, 'kill') as kill:
            module.stop('gateway')
            kill.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    def test_matching_process_receives_only_sigterm(self):
        result = subprocess.CompletedProcess([], 0, 'p12345\nn' + str(module.ROOT) + '\n')
        with patch.object(module, 'alive', side_effect=[True, False]), patch.object(
            module.subprocess, 'run', return_value=result
        ), patch.object(module, 'listeners', return_value={12345}), patch.object(module.os, 'kill') as kill:
            module.stop('gateway')
            kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertFalse(self.pid_file.exists())


class PortCheckTest(unittest.TestCase):
    def test_recently_closed_connection_can_be_reused(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            with socket.create_connection(('127.0.0.1', port)) as client:
                connection, _ = server.accept()
                connection.close()
                self.assertEqual(client.recv(1), b'')
        # 服务端主动关闭后的 TIME_WAIT 曾使普通 bind 预检失败。
        module.check_port(port)

    def test_live_listener_still_rejected(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            server.listen()
            with self.assertRaises(OSError) as error:
                module.check_port(server.getsockname()[1])
            self.assertEqual(error.exception.errno, errno.EADDRINUSE)


if __name__ == '__main__':
    unittest.main()
