"""Typed persistence boundary; independent of any database or HTTP framework."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from review_models import ReviewIssue

if TYPE_CHECKING:
    from review_events import ReviewEvent
    from review_pipeline import ReviewBatch


class ReviewRunError(RuntimeError):
    """Fatal run-level control flow, never a tolerable single-file failure."""


class ReviewInterruptedError(ReviewRunError):
    code = 'shutdown'

    def __init__(self):
        super().__init__('Review execution interrupted')


class ReviewPersistenceError(ReviewRunError):
    """Fatal run error, never a tolerable single-file/model failure."""

    code = "persistence_failed"

    def __init__(self):
        super().__init__("Review persistence failed; execution stopped")


@dataclass(frozen=True)
class FileCheckpoint:
    """Validated file output and its completion event, committed together.

    Failed/skipped/excluded files have no findings. Diagnostics use safe reason
    codes, not raw exception bodies. Evidence is preserved on reviewed findings.
    This is a file completion boundary, not a resumable LangGraph checkpoint.
    """

    path: str
    status: Literal["reviewed", "failed", "skipped", "excluded"]
    findings: tuple[ReviewIssue, ...]
    reason_code: str | None
    event: ReviewEvent


class ReviewSink(Protocol):
    def record_event(self, event: ReviewEvent) -> None:
        """Persist a non-file-completion event."""
        ...

    def file_completed(self, checkpoint: FileCheckpoint) -> None:
        """Atomically persist the file, findings, and checkpoint.event once."""
        ...

    def finish(self, batch: ReviewBatch) -> None:
        """Atomically persist the final batch and terminal execution status."""
        ...


class PersistenceGuard:
    """A per-run failure latch. A failed sink must never dispatch more work."""

    def __init__(self, sink: ReviewSink | None = None, before_work=None):
        self._sink = sink
        self._failed = False
        self._before_work = before_work

    @property
    def failed(self) -> bool:
        return self._failed

    def check(self) -> None:
        if self._failed:
            raise ReviewPersistenceError()

    def before_work(self) -> None:
        self.check()
        if self._before_work is not None:
            self._before_work()

    def _write(self, method: str, value) -> None:
        self.check()
        if self._sink is None:
            return
        try:
            # A sink cannot mutate live counters, findings, or final output.
            getattr(self._sink, method)(deepcopy(value))
        except ReviewInterruptedError:
            # Shutdown is not a failed write. Completed in-flight work may have
            # been saved before the next dispatch boundary stopped the run.
            raise
        except Exception:
            self._failed = True
            raise ReviewPersistenceError() from None

    def record_event(self, event: ReviewEvent) -> None:
        self._write("record_event", event)

    def file_completed(self, checkpoint: FileCheckpoint) -> None:
        self._write("file_completed", checkpoint)

    def finish(self, batch: ReviewBatch) -> None:
        self._write("finish", batch)
