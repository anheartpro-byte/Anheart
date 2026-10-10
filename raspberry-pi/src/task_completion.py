import asyncio


async def complete_owned[T](task: asyncio.Task[T]) -> T:
    """Join shielded work before propagating cancellation, including repeated cancels."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
