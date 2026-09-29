"""工作流异步调用中的数据库线程与取消处理。"""
import asyncio
from typing import Any, Callable


async def database_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """取消协程时先等待已经开始的数据库操作结束，避免旧写入污染下一次尝试。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # 下方统一取出异常；取消优先，但必须消费线程任务的异常。
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError()
    return task.result()
