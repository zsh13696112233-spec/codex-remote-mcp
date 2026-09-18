import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from codex_orchestrator_mcp import AppServerClient, AppServerDisconnected


class DisconnectLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def check_disconnect(self, method, during_send, code):
        secret = "private-token-测试"
        client = AppServerClient("ws://private-host", token=secret)
        client._socket = object()
        error = AppServerDisconnected(code=code, reason=secret, detail=secret)
        sent = []

        async def send(message):
            sent.append(message)
            if during_send:
                raise error
            asyncio.get_running_loop().call_soon(client._signal_disconnect, error)

        client._send = send
        with self.assertLogs("codex_orchestrator_mcp", level="WARNING") as logs:
            with self.assertRaises(AppServerDisconnected) as caught:
                await client.request(method, {"dataBase64": secret, "path": secret})
        self.assertIs(caught.exception, error)
        self.assertEqual(client._pending, {})
        output = "\n".join(logs.output)
        size = len(json.dumps(sent[0], ensure_ascii=False).encode("utf-8"))
        self.assertIn(f"request_bytes={size}", output)
        self.assertIn(f"close_code={code}", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("private-host", output)
        return output

    async def test_send_failure_logs_size_and_close_code_without_payload(self):
        output = await self.check_disconnect("fs/writeFile", True, 1009)
        self.assertIn("method=fs/writeFile", output)

    async def test_response_disconnect_and_absent_code(self):
        output = await self.check_disconnect("fs/readFile", False, None)
        self.assertIn("method=fs/readFile", output)

    async def test_unrecognized_method_is_not_logged_verbatim(self):
        output = await self.check_disconnect("private-method-secret", True, 1006)
        self.assertIn("method=other", output)
        self.assertNotIn("private-method-secret", output)

    async def test_success_does_not_emit_disconnect_warning(self):
        client = AppServerClient("ws://unused")
        client._socket = object()

        async def send(message):
            client._handle_response({"id": message["id"], "result": {"ok": True}})

        client._send = AsyncMock(side_effect=send)
        with self.assertNoLogs("codex_orchestrator_mcp", level="WARNING"):
            self.assertEqual(await client.request("fs/writeFile", {}), {"ok": True})
        self.assertEqual(client._pending, {})
