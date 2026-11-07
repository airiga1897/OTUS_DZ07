"""Clone-local ordering and retry; provider details stay in the backend."""

from collections.abc import Iterable
from itertools import chain
from typing import Protocol

from progress_logging import get_logger

LOGGER = get_logger(__name__)


class PlacementExhausted(RuntimeError):
    """All candidates failed with their failed allocations confirmed absent."""


class PlacementBackend[Candidate, Identities](Protocol):
    def completed(self, name: str) -> bool: ...
    def reapply(self, name: str) -> None: ...
    def identities(self, exclude_pending: bool = False) -> Identities: ...
    def recover_attempt(self, name: str) -> None: ...
    def assert_preserved(self, before: Identities) -> None: ...
    def pending(self, name: str) -> Candidate | None: ...
    def begin(self, name: str, candidate: Candidate) -> None: ...
    def apply(self, name: str) -> object: ...
    def capture_fault(self, name: str) -> None: ...
    def is_capacity_error(self, error: Exception, name: str) -> bool: ...
    def cleanup_attempt(self, name: str) -> None: ...
    def finish(self, name: str) -> None: ...


def place[Candidate, Identities](
    backend: PlacementBackend[Candidate, Identities], name: str, candidates: Iterable[Candidate]
) -> None:
    if backend.completed(name):
        LOGGER.info("Этап %s: начало проверки плана и повторного применения существующих ресурсов", name)
        backend.reapply(name)
        LOGGER.info("Этап %s: повторное применение завершено", name)
        return
    preserved = backend.identities(exclude_pending=True)
    backend.recover_attempt(name)
    backend.assert_preserved(preserved)
    pending = backend.pending(name)
    choices = chain([pending] if pending else [], (c for c in candidates if c != pending))
    for candidate in choices:
        LOGGER.info("Этап %s: начало попытки размещения", name)
        if pending != candidate:
            backend.begin(name, candidate)
        try:
            backend.apply(name)
        except Exception as error:
            LOGGER.warning("Этап %s: ошибка %s; сохраняется диагностика", name, type(error).__name__)
            backend.capture_fault(name)
            if not backend.is_capacity_error(error, name):
                raise
            # The backend must prove empty stage state AND API absence before return.
            backend.cleanup_attempt(name)
            backend.assert_preserved(preserved)
            pending = None
            continue
        backend.assert_preserved(preserved)
        backend.finish(name)
        LOGGER.info("Этап %s: ресурсы созданы и проверены", name)
        return
    raise PlacementExhausted("No compatible placement succeeded for " + name)
