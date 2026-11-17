import contextlib
import asyncio
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workflow_gateway import main


class GatewayStartupTests(unittest.TestCase):
    def test_open_event_stream_does_not_block_application_shutdown(self):
        import uvicorn
        from starlette.applications import Starlette
        from starlette.responses import StreamingResponse
        from starlette.routing import Route

        events = []
        server = None
        client_task = None
        server_class = uvicorn.Server

        async def stream(request):
            async def generate():
                try:
                    yield ': keep-alive\n\n'
                    await asyncio.Event().wait()
                finally:
                    events.append('stream_closed')
            return StreamingResponse(generate(), media_type='text/event-stream')

        async def connect():
            while not server.started:
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                writer.write(b'GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n')
                await writer.drain()
                await reader.readuntil(b': keep-alive')
                server.should_exit = True
                await asyncio.wait_for(reader.read(), timeout=5)
            finally:
                writer.close()
                await writer.wait_closed()

        @contextlib.asynccontextmanager
        async def lifespan(app):
            nonlocal client_task
            client_task = asyncio.create_task(connect())
            yield
            await client_task
            events.append('stopped')

        def build_server(config):
            nonlocal server
            self.assertEqual(config.timeout_graceful_shutdown, 10)
            config.timeout_graceful_shutdown = 0.1
            server = server_class(config)
            return server

        with patch('sys.argv', ['gateway', '--host', '127.0.0.1', '--port', '0']), \
                patch('workflow_gateway.create_app', return_value=Starlette(
                    routes=[Route('/events', stream)], lifespan=lifespan)), \
                patch('workflow_gateway.uvicorn.Server', side_effect=build_server):
            main()
        self.assertEqual(events, ['stream_closed', 'stopped'])
        self.assertTrue(client_task.done())
        client_task.result()

    def test_duplicate_cli_exits_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            port = occupied.getsockname()[1]
            database = Path(directory, 'unused', 'workflow.db')
            script = Path(__file__).resolve().parents[1] / 'src' / 'workflow_gateway.py'
            result = subprocess.run(
                [sys.executable, str(script), '--host', '127.0.0.1', '--port', str(port), '--db', str(database)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f'端口 {port} 已被占用', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse(database.parent.exists())
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass

    def test_port_is_reserved_before_factory_and_closed_after_server(self):
        listeners = []
        application = object()

        def server(config):
            def run(*, sockets):
                listener = sockets[0]
                listeners.append(listener)
                with socket.create_connection(listener.getsockname(), timeout=1):
                    pass
                factory.assert_not_called()
                self.assertIs(config.app(), application)
                factory.assert_called_once_with(db_path='chosen.db')
            return SimpleNamespace(run=run, started=True)

        with patch('sys.argv', ['gateway', '--host', '127.0.0.1', '--port', '0', '--db', 'chosen.db']), \
                patch('workflow_gateway.create_app', return_value=application) as factory, \
                patch('workflow_gateway.uvicorn.Server', side_effect=server):
            main()
        self.assertEqual(len(listeners), 1)
        self.assertEqual(listeners[0].fileno(), -1)

    def test_failed_application_start_is_nonzero_and_releases_port(self):
        listeners = []

        def server(config):
            def run(*, sockets):
                listeners.extend(sockets)
            return SimpleNamespace(run=run, started=False)

        with patch('sys.argv', ['gateway', '--host', '127.0.0.1', '--port', '0']), \
                patch('workflow_gateway.uvicorn.Server', side_effect=server):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 3)
        self.assertEqual(listeners[0].fileno(), -1)

    def test_real_server_uses_prebound_socket_and_closes_lifespan(self):
        import uvicorn
        from starlette.applications import Starlette

        events = []
        server = None
        server_class = uvicorn.Server

        @contextlib.asynccontextmanager
        async def lifespan(app):
            events.append('started')
            server.should_exit = True
            yield
            events.append('stopped')

        def build_server(config):
            nonlocal server
            server = server_class(config)
            return server

        with patch('sys.argv', ['gateway', '--host', '127.0.0.1', '--port', '0']), \
                patch('workflow_gateway.create_app', return_value=Starlette(lifespan=lifespan)) as factory, \
                patch('workflow_gateway.uvicorn.Server', side_effect=build_server):
            main()
        factory.assert_called_once()
        self.assertEqual(events, ['started', 'stopped'])
        self.assertTrue(server.started)

    def test_unavailable_address_does_not_initialize_application(self):
        with patch('sys.argv', ['gateway', '--host', '127.0.0.1']), \
                patch('workflow_gateway.socket.socket') as sock, \
                patch('workflow_gateway.create_app') as factory, \
                contextlib.redirect_stderr(io.StringIO()) as output:
            sock.return_value.__enter__.return_value.bind.side_effect = OSError('unavailable')
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 1)
        self.assertIn('无法绑定监听地址', output.getvalue())
        factory.assert_not_called()
