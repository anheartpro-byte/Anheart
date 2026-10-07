from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol

from src.task_completion import complete_owned

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


class WebRunner(Protocol):
    """Serves the page until told to exit."""

    @abstractmethod
    async def serve(self) -> None:
        """Run the server on this loop until it is asked to exit."""

    @abstractmethod
    def request_exit(self) -> None:
        """Ask the server to finish; :meth:`serve` returns shortly after."""


@dataclass(frozen=True, slots=True)
class PanelTasks:
    stop: asyncio.Event
    web: WebRunner
    close: Callable[[], Awaitable[str]]

    async def run(
        self, control: asyncio.Task[None], observers: tuple[asyncio.Task[None], ...]
    ) -> bool:
        web = asyncio.create_task(self.web.serve())
        stopped = asyncio.create_task(self.stop.wait())
        tasks = (control, *observers, web)
        try:
            await asyncio.wait((*tasks, stopped), return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stop.set()
            self.web.request_exit()
            stopped.cancel()
            cleanup = asyncio.create_task(self._close(control, (*observers, web)))
            try:
                await complete_owned(cleanup)
            finally:
                await asyncio.gather(stopped, return_exceptions=True)
        failed = False
        for task in tasks:
            error = None if task.cancelled() else task.exception()
            if error is not None:
                failed = True
                _LOG.error("console task failed: %r", error)
        return failed

    async def _close(
        self, control: asyncio.Task[None], observers: tuple[asyncio.Task[None], ...]
    ) -> None:
        control.cancel()
        for task in observers[:-1]:
            task.cancel()
        try:
            await asyncio.gather(control, return_exceptions=True)
            detail = await self.close()
            _LOG.warning("console stopped: %s", detail)
        finally:
            for task in observers:
                task.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
