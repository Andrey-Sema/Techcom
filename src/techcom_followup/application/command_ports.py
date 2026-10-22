"""Injected CLI services; composition lives in bootstrap."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

from techcom_followup.application.dispatch import DispatchActions
from techcom_followup.application.process_tasks import ProcessTasks
from techcom_followup.application.reconcile import ReconcileUnknown
from techcom_followup.application.repository import Repository


@dataclass(frozen=True, slots=True)
class CommandOptions:
    """Non-secret operator inputs for a single command lifecycle."""

    as_of: date
    fixture: Path | None = None
    demo: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class CommandServices:
    """One owned runtime; commands never construct clients."""

    repository: Repository
    processor: Callable[[], ProcessTasks]
    dispatcher: Callable[[], DispatchActions]
    reconciler: Callable[[], ReconcileUnknown]
    stop_claiming: Callable[[], None]
    persistent: bool
    advance_demo_clock: Callable[[timedelta], None]


class RuntimeFactory(Protocol):
    """Open and close a runtime for each independent CLI invocation."""

    def __call__(
        self, options: CommandOptions
    ) -> AbstractAsyncContextManager[CommandServices]:
        """Return an asynchronous resource ownership context."""
        ...
