import asyncio
import json
from contextlib import suppress
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed


class MockAppServer:
    """实现测试所需的最小 Codex App Server JSON-RPC/WebSocket 协议。"""

    def __init__(
        self,
        *,
        delay_sec: float = 0.05,
        turn_status: str = "completed",
        turn_error: Any = None,
        ignore_methods: set[str] | None = None,
        close_after_turn_start: tuple[int, str] | None = None,
        interrupt_error: str | None = None,
        send_message_delta: bool = False,
        steer_error: str | None = None,
        steer_commentary: bool = False,
        steer_completes_turn: bool = False,
        structured_reply: str | None = None,
        config_requirements: dict[str, Any] | None = None,
    ) -> None:
        self.delay_sec = delay_sec
        self.turn_status = turn_status
        self.turn_error = turn_error
        self.ignore_methods = ignore_methods or set()
        self.close_after_turn_start = close_after_turn_start
        self.interrupt_error = interrupt_error
        self.send_message_delta = send_message_delta
        self.steer_error = steer_error
        self.steer_commentary = steer_commentary
        self.steer_completes_turn = steer_completes_turn
        self.structured_reply = structured_reply or json.dumps({
            "kind": "answer",
            "text": "mock chat reply",
            "actionType": None,
            "nodeId": None,
            "revisionInstruction": None,
        }, ensure_ascii=False)
        self.config_requirements = config_requirements
        self.url = ""
        self.authorization: str | None = None
        self.requests: list[dict[str, Any]] = []
        self.interrupt_requests = 0
        self._server: Server | None = None
        self._completion_tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> "MockAppServer":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in self._completion_tasks:
            task.cancel()
        await asyncio.gather(*self._completion_tasks, return_exceptions=True)

    async def _handler(self, connection: ServerConnection) -> None:
        self.authorization = connection.request.headers.get("Authorization")
        try:
            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    continue
                message = json.loads(raw_message)
                if not isinstance(message, dict) or "id" not in message:
                    continue
                self.requests.append(message)
                method = str(message.get("method", ""))
                if method in self.ignore_methods:
                    continue
                request_id = message["id"]

                if method == "initialize":
                    await self._result(connection, request_id, {"serverInfo": {"name": "mock"}})
                elif method == "configRequirements/read":
                    await self._result(
                        connection,
                        request_id,
                        {"requirements": self.config_requirements},
                    )
                elif method in {"thread/start", "thread/resume"}:
                    await self._result(connection, request_id, {"thread": {"id": "thread-1"}})
                elif method == "turn/start":
                    await self._result(connection, request_id, {"turn": {"id": "turn-1"}})
                    if self.close_after_turn_start is not None:
                        code, reason = self.close_after_turn_start
                        await connection.close(code=code, reason=reason)
                    else:
                        reply = (
                            self.structured_reply
                            if "outputSchema" in (message.get("params") or {})
                            else "mock final reply"
                        )
                        task = asyncio.create_task(self._complete_turn(connection, reply))
                        self._completion_tasks.add(task)
                        task.add_done_callback(self._completion_tasks.discard)
                elif method == "turn/interrupt":
                    self.interrupt_requests += 1
                    if self.interrupt_error:
                        await connection.send(
                            json.dumps(
                                {
                                    "id": request_id,
                                    "error": {"code": -32000, "message": self.interrupt_error},
                                }
                            )
                        )
                    else:
                        await self._result(connection, request_id, {})
                elif method == "turn/steer":
                    if self.steer_error:
                        await connection.send(json.dumps({
                            "id": request_id,
                            "error": {"code": -32000, "message": self.steer_error},
                        }))
                    else:
                        await self._result(connection, request_id, {"turnId": "turn-1"})
                        task = asyncio.create_task(self._answer_steer(connection))
                        self._completion_tasks.add(task)
                        task.add_done_callback(self._completion_tasks.discard)
        except ConnectionClosed:
            pass

    async def _complete_turn(self, connection: ServerConnection, reply: str) -> None:
        await asyncio.sleep(self.delay_sec)
        try:
            if self.send_message_delta:
                await connection.send(
                    json.dumps(
                        {
                            "method": "item/agentMessage/delta",
                            "params": {"delta": "mock streaming reply"},
                        }
                    )
                )
            await connection.send(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": reply,
                            }
                        },
                    }
                )
            )
            turn: dict[str, Any] = {"id": "turn-1", "status": self.turn_status}
            if self.turn_error is not None:
                turn["error"] = self.turn_error
            await connection.send(
                json.dumps({"method": "turn/completed", "params": {"turn": turn}})
            )
        except ConnectionClosed:
            pass

    async def _answer_steer(self, connection: ServerConnection) -> None:
        await asyncio.sleep(0.001)
        try:
            if self.steer_commentary:
                await connection.send(json.dumps({
                    "method": "item/completed",
                    "params": {"item": {
                        "type": "agentMessage", "phase": "commentary",
                        "text": "我先查询最新状态。",
                    }},
                }))
            await connection.send(json.dumps({
                "method": "item/agentMessage/delta",
                "params": {"delta": "mock chat reply"},
            }))
            await connection.send(json.dumps({
                "method": "item/completed",
                "params": {"item": {
                    "type": "agentMessage", "phase": "final_answer",
                    "text": "mock chat reply",
                }},
            }))
            if self.steer_completes_turn:
                await connection.send(json.dumps({
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "completed"}},
                }))
        except ConnectionClosed:
            pass

    @staticmethod
    async def _result(
        connection: ServerConnection,
        request_id: Any,
        result: dict[str, Any],
    ) -> None:
        with suppress(ConnectionClosed):
            await connection.send(json.dumps({"id": request_id, "result": result}))
