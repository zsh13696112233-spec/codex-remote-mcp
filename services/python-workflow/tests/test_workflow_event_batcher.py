import asyncio
import threading
import unittest
from unittest.mock import Mock

from workflow_event_batcher import AsyncEventBatcher


class AsyncEventBatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = Mock()
        self.store.add_events.return_value = []
        self.batcher = AsyncEventBatcher(self.store, flush_interval=60)
        self.addAsyncCleanup(self.batcher.close)

    async def add(self, index: int, workflow_id: str = "demo") -> None:
        await self.batcher.add(
            workflow_id,
            node_id=None,
            source="test",
            event_type="test.delta",
            payload={"index": index},
        )

    def block_first_write(self, *, fail: bool = False):
        entered = asyncio.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        loop = asyncio.get_running_loop()

        def write(events):
            if self.store.add_events.call_count == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(timeout=5):
                    raise TimeoutError("test write was not released")
                if fail:
                    raise RuntimeError("temporary failure")
            return []

        self.store.add_events.side_effect = write
        return entered, release

    def written_indices(self):
        return [
            event["payload"]["index"]
            for call in self.store.add_events.call_args_list
            for event in call.args[0]
        ]

    async def test_backlog_is_split_into_bounded_batches_in_order(self) -> None:
        entered, release = self.block_first_write()
        await self.add(0)
        first = asyncio.create_task(self.batcher.flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        producers = [asyncio.create_task(self.add(index)) for index in range(1, 150)]
        try:
            await asyncio.sleep(0)
        finally:
            release.set()
        await asyncio.gather(first, *producers)
        await self.batcher.close()
        self.assertEqual(self.written_indices(), list(range(150)))
        self.assertTrue(all(len(call.args[0]) <= 64 for call in self.store.add_events.call_args_list))

    async def test_each_batch_belongs_to_one_workflow_without_reordering(self) -> None:
        for index, workflow_id in enumerate(["a", "a", "b", "a"]):
            await self.add(index, workflow_id)
        await self.batcher.close()
        self.assertEqual(self.written_indices(), [0, 1, 2, 3])
        self.assertEqual(
            [[event["workflow_id"] for event in call.args[0]]
             for call in self.store.add_events.call_args_list],
            [["a", "a"], ["b"], ["a"]],
        )

    async def test_failed_write_precedes_events_accepted_during_write(self) -> None:
        entered, release = self.block_first_write(fail=True)
        await self.add(0)
        first = asyncio.create_task(self.batcher.flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            await self.add(1)
        finally:
            release.set()
        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            await first
        await self.batcher.close()
        calls = self.store.add_events.call_args_list
        self.assertEqual(self.written_indices(), [0, 0, 1])
        self.assertEqual(calls[0].args[0][0], calls[1].args[0][0])

    async def test_cancelled_write_waits_for_commit_without_requeuing(self) -> None:
        entered, release = self.block_first_write()
        await self.add(0)
        first = asyncio.create_task(self.batcher.flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            await self.add(1)
            closing = asyncio.create_task(self.batcher.close())
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertFalse(closing.done())
            self.assertEqual(self.store.add_events.call_count, 1)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await closing
        self.assertEqual(self.written_indices(), [0, 1])

    async def test_cancelled_failed_write_is_retained_and_error_is_visible(self) -> None:
        entered, release = self.block_first_write(fail=True)
        await self.add(0)
        first = asyncio.create_task(self.batcher.flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        first.cancel()
        release.set()
        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            await first
        await self.batcher.close()
        self.assertEqual(self.written_indices(), [0, 0])
        calls = self.store.add_events.call_args_list
        self.assertEqual(calls[0].args[0], calls[1].args[0])

    async def test_backpressure_rejects_waiting_add_when_closed(self) -> None:
        self.batcher.max_pending = 2
        entered, release = self.block_first_write()
        await self.add(0)
        first = asyncio.create_task(self.batcher.flush())
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            await self.add(1)
            await self.add(2)
            waiting = asyncio.create_task(self.add(3))
            await asyncio.sleep(0)
            self.assertFalse(waiting.done())
            closing = asyncio.create_task(self.batcher.close())
            await asyncio.sleep(0)
        finally:
            release.set()
        await first
        with self.assertRaisesRegex(RuntimeError, "已经关闭"):
            await waiting
        await closing
        self.assertEqual(self.written_indices(), [0, 1, 2])

    async def test_close_can_retry_after_failed_drain(self) -> None:
        self.store.add_events.side_effect = [RuntimeError("temporary failure"), []]
        await self.add(0)
        with self.assertRaisesRegex(RuntimeError, "temporary failure"):
            await self.batcher.close()
        with self.assertRaisesRegex(RuntimeError, "已经关闭"):
            await self.add(1)
        await self.batcher.close()
        await self.batcher.close()
        self.assertEqual(self.written_indices(), [0, 0])

    async def test_timer_retries_failed_write_without_another_add(self) -> None:
        self.batcher.flush_interval = 0.01
        completed = asyncio.Event()
        loop = asyncio.get_running_loop()

        def write(events):
            if self.store.add_events.call_count == 1:
                raise RuntimeError("temporary failure")
            loop.call_soon_threadsafe(completed.set)
            return []

        self.store.add_events.side_effect = write
        with self.assertLogs("workflow_event_batcher", level="ERROR"):
            await self.add(0)
            await asyncio.wait_for(completed.wait(), timeout=1)
            await self.batcher.close()
        self.assertEqual(self.written_indices(), [0, 0])


if __name__ == "__main__":
    unittest.main()
