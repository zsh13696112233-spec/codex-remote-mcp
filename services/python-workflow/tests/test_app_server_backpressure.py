import asyncio
import json
import threading
import unittest
from unittest.mock import Mock

from websockets.asyncio.server import serve

from codex_orchestrator_mcp import AppServerClient, AppServerDisconnected
from workflow_event_batcher import AsyncEventBatcher


class AppServerBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_and_close_preserve_queued_events_and_receive_times(self):
        for consume in (True, False):
            with self.subTest(consume=consume):
                received = []
                recorded = []

                async def handler(socket):
                    async for raw in socket:
                        request = json.loads(raw)
                        if "id" not in request:
                            continue
                        await socket.send(json.dumps({"id": request["id"], "result": {}}))
                        if request["method"] == "burst":
                            for index in range(3):
                                await socket.send(json.dumps({"method": "test", "params": {"index": index}}))
                            await socket.close(code=1011, reason="test disconnect")
                            return

                async def record(message, received_at):
                    await asyncio.sleep(0)
                    recorded.append((message["params"]["index"], received_at))

                async with serve(handler, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    client = AppServerClient(f"ws://127.0.0.1:{port}", on_message=record,
                        on_notification=lambda method, timestamp: received.append(timestamp))
                    await client.open()
                    try:
                        await client.request("burst")
                        await asyncio.wait_for(client._closed.wait(), 2)
                        if consume:
                            for index in range(3):
                                message = await client.next_notification(1)
                                self.assertEqual(message["params"]["index"], index)
                            with self.assertRaises(AppServerDisconnected):
                                await client.next_notification(1)
                    finally:
                        await client.close()
                    await client.close()
                    self.assertEqual(recorded, list(enumerate(received)))
                    self.assertEqual(len(recorded), 3)

    async def test_slow_event_store_does_not_block_pong_or_rpc(self):
        """SVN-sized output burst must not stop protocol reads during a batch write."""
        count = 8000
        entered = asyncio.Event()
        sent = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        persisted = []

        def write(events):
            if not persisted:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("test store was not released")
            persisted.extend(events)
            return []

        store = Mock()
        store.add_events.side_effect = write
        batcher = AsyncEventBatcher(store)

        async def record(message, received_at):
            await batcher.add("test", node_id="step", source="worker",
                event_type=message["method"], payload=message, created_at=received_at)

        async def handler(socket):
            async for raw in socket:
                request = json.loads(raw)
                if "id" not in request:
                    continue
                await socket.send(json.dumps({"id": request["id"], "result": {}}))
                if request["method"] == "burst":
                    for index in range(count):
                        await socket.send(json.dumps({
                            "method": "item/commandExecution/outputDelta",
                            "params": {"itemId": "svn", "index": index, "delta": "x" * 45},
                        }))
                        if index % 64 == 0:
                            await asyncio.sleep(0)
                    sent.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = AppServerClient(f"ws://127.0.0.1:{port}", on_message=record)
            await client.open()

            async def consume():
                return [await client.next_notification(5) for _ in range(count)]

            consuming = asyncio.create_task(consume())
            try:
                await client.request("burst")
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(sent.wait(), 2)
                pong = await client._socket.ping()
                await asyncio.wait_for(asyncio.shield(pong), 0.5)
                self.assertEqual(await client.request("check", timeout_sec=0.5), {})
                self.assertFalse(consuming.done())
            finally:
                release.set()
                messages = await asyncio.wait_for(consuming, 5)
                await client.close()
                await batcher.close()
            self.assertEqual([m["params"]["index"] for m in messages], list(range(count)))
            self.assertEqual([e["payload"]["params"]["index"] for e in persisted], list(range(count)))


if __name__ == "__main__":
    unittest.main()
