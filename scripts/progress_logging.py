"""Console progress for this application, without enabling SDK debug output."""

import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TypeVar

import privacy

ROOT = Path(__file__).resolve().parents[1]
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
HANDLER_NAME = "otus_dz07.progress"
_status_interval = 60
Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")


class Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = privacy.redact(record.getMessage())
        record.args = ()
        return True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger("otus_dz07." + name)


def configure(level: object = None, root: Path = ROOT) -> None:
    global _status_interval
    settings = json.loads((root / "config/logging.json").read_text(encoding="utf-8-sig"))
    selected = level if level is not None else settings["level"]
    if not isinstance(selected, str) or selected not in LEVELS:
        raise ValueError("Invalid progress logging level")
    interval = settings.get("status_interval_seconds", 60)
    if type(interval) is not int or not 1 <= interval <= 3600:
        raise ValueError("Invalid progress status interval")
    _status_interval = interval
    logger = logging.getLogger("otus_dz07")
    for handler in list(logger.handlers):
        if handler.name == HANDLER_NAME:
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler()
    handler.name = HANDLER_NAME
    handler.addFilter(Redact())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(selected)
    logger.propagate = False


def _emit(logger: logging.Logger, level: int, message: str, *args: object) -> None:
    # A broken console/file handler must not replace the operation's error.
    with suppress(Exception):
        logger.log(level, message, *args)


class WaitProgress:
    def __init__(self, logger: logging.Logger, label: str) -> None:
        self.logger, self.label = logger, label
        self.started = self.updated = time.perf_counter()
        self.previous: str | None = None

    def status(self, state: str) -> None:
        now = time.perf_counter()
        if state != self.previous or now - self.updated >= _status_interval:
            _emit(self.logger, logging.INFO, "%s: состояние %s; прошло %.1f с", self.label, state, now - self.started)
            self.previous, self.updated = state, now


@contextmanager
def phase(
    logger: logging.Logger, label: str, *, pool: str | None = None, role: str | None = None
) -> Iterator[WaitProgress]:
    context = label + ((" [пул " + pool + "]") if pool else "") + ((" [роль " + role + "]") if role else "")
    watch = WaitProgress(logger, context)
    _emit(logger, logging.INFO, "%s: начало", context)
    try:
        yield watch
    except BaseException as error:
        _emit(
            logger,
            logging.ERROR,
            "%s: ошибка %s; длительность %.1f с",
            context,
            type(error).__name__,
            time.perf_counter() - watch.started,
        )
        raise
    else:
        _emit(logger, logging.INFO, "%s: успешно; длительность %.1f с", context, time.perf_counter() - watch.started)


def timed(label: str) -> Callable[[Callable[Parameters, Result]], Callable[Parameters, Result]]:
    def decorate(function: Callable[Parameters, Result]) -> Callable[Parameters, Result]:
        @wraps(function)
        def run(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            with phase(get_logger(function.__module__), label):
                return function(*args, **kwargs)

        return run

    return decorate
