"""本机存储与远程 API 共用的有界异步事件批处理。"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol


LOGGER = logging.getLogger(__name__)


class EventStore(Protocol):
    def add_events(self, events: list[dict[str, Any]]) -> list[int]: ...


class AsyncEventBatcher:
    """在事件循环外批量提交高频监控事件。"""

    def __init__(
        self,
        store: EventStore,
        *,
        batch_size: int = 64,
        flush_interval: float = 0.05,
        max_pending: int = 4096,
    ) -> None:
        if not 1 <= batch_size <= 64 or max_pending < 1 or flush_interval <= 0:
            raise ValueError("事件批次大小须为 1–64，缓冲容量和刷新间隔须大于零。")
        self.store = store
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_pending = max_pending
        self._pending: list[dict[str, Any]] = []
        self._flush_lock = asyncio.Lock()
        self._timer_handle: asyncio.TimerHandle | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    async def add(
        self,
        workflow_id: str,
        *,
        node_id: str | None,
        source: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("事件批量写入器已经关闭。")
        while len(self._pending) >= self.max_pending:
            await self.flush()
            if self._closed:
                raise RuntimeError("事件批量写入器已经关闭。")
        self._pending.append(
            {
                "workflow_id": workflow_id,
                "node_id": node_id,
                "source": source,
                "event_type": event_type,
                "payload": payload,
                "created_at": created_at or datetime.now(timezone.utc).isoformat(),
                "external_event_id": str(uuid.uuid4()),
            }
        )
        if len(self._pending) >= self.batch_size:
            await self.flush()
        else:
            self._schedule_flush()

    async def flush(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None
        try:
            task = self._flush_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.shield(task)
            await self._flush_batch()
        finally:
            self._schedule_flush()

    async def close(self) -> None:
        self._closed = True
        await self.flush()

    def _schedule_flush(self) -> None:
        if (
            self._closed
            or not self._pending
            or self._timer_handle is not None
            or (self._flush_task is not None and not self._flush_task.done())
        ):
            return
        self._timer_handle = asyncio.get_running_loop().call_later(
            self.flush_interval, self._start_scheduled_flush
        )

    def _start_scheduled_flush(self) -> None:
        self._timer_handle = None
        if self._closed or not self._pending:
            return
        task = asyncio.create_task(
            self._run_scheduled_flush(), name="workflow-event-flush"
        )
        self._flush_task = task
        task.add_done_callback(self._log_background_failure)

    async def _run_scheduled_flush(self) -> None:
        current = asyncio.current_task()
        cancelled = False
        try:
            await self._flush_batch()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if self._flush_task is current:
                self._flush_task = None
            if not cancelled:
                self._schedule_flush()

    @staticmethod
    def _log_background_failure(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            LOGGER.error(
                "批量写入工作流事件失败。",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _flush_batch(self) -> None:
        async with self._flush_lock:
            # 只处理本轮已入队的事件，持续到来的事件留给下一轮。
            remaining = len(self._pending)
            while remaining:
                size = 1
                workflow_id = self._pending[0]["workflow_id"]
                while (
                    size < min(remaining, self.batch_size)
                    and self._pending[size]["workflow_id"] == workflow_id
                ):
                    size += 1
                batch, self._pending = self._pending[:size], self._pending[size:]
                write = asyncio.create_task(asyncio.to_thread(self.store.add_events, batch))
                cancelled = False
                # 取消等待不能停止线程；保留写锁直到结果确定，成功不回队。
                while not write.done():
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        break
                try:
                    write.result()
                except BaseException:
                    self._pending = batch + self._pending
                    raise
                remaining -= size
                if cancelled:
                    raise asyncio.CancelledError()
