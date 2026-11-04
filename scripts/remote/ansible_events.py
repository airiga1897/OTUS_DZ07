"""Bounded private progress journal for one DZ07 Ansible operation."""

import json
import os
import re
from pathlib import Path
from typing import Literal, TypedDict, cast


class ProgressEvent(TypedDict):
    seq: int
    task: str
    host: str
    kind: Literal["start", "result"]
    status: str


class ProgressCursor(TypedDict):
    offset: int
    seq: int


class ProgressPage(TypedDict):
    events: list[ProgressEvent]
    more: bool
    cursor: ProgressCursor


MAX_BYTES = 65536
MAX_EVENTS = 64
MAX_LINE = 2048
FIELDS = {"seq", "task", "host", "kind", "status"}
STATUSES = {"start", "ok", "changed", "failed", "skipped", "hidden"}
CALLBACK_PHASES = {
    "initialize": "инициализация журнала",
    "metadata": "чтение метаданных события",
    "write": "запись события",
    "unknown": "неустановленный этап",
}
CALLBACK_ERRORS = {
    "permission": "отказ в доступе",
    "existing": "журнал уже существует",
    "io": "ошибка ввода-вывода",
    "import": "ошибка импорта модуля",
    "attribute": "отсутствует ожидаемый атрибут",
    "type": "неподходящий тип данных",
    "value": "недопустимое значение",
    "unexpected": "иная ошибка callback",
}
CALLBACK_REASONS = {
    "callback_" + phase + "_" + error: "callback: " + label + "; " + message
    for phase, label in CALLBACK_PHASES.items()
    for error, message in CALLBACK_ERRORS.items()
}


def write_failure(path: Path, phase: str, error: Exception | None, sequence: object) -> None:
    """Persist only allowlisted metadata; never exception text or task fields."""
    from remote.ansible_job import atomic

    categories = (
        (PermissionError, "permission"),
        (FileExistsError, "existing"),
        (OSError, "io"),
        (ImportError, "import"),
        (AttributeError, "attribute"),
        (TypeError, "type"),
        (ValueError, "value"),
    )
    category = next((code for cls, code in categories if isinstance(error, cls)), "unexpected")
    value = {
        "version": 1,
        "phase": phase if phase in CALLBACK_PHASES else "unknown",
        "error": category,
        "seq": sequence if type(sequence) is int and 0 <= sequence <= 2**53 else 0,
    }
    atomic(Path(path), value)


def read_failure(path: Path) -> str:
    """Legacy empty markers remain readable; unknown metadata is never echoed."""
    path = Path(path)
    if path.is_symlink():
        return "callback_diagnostic_invalid"
    with path.open("rb") as stream:
        raw = stream.read(1025)
    if not raw:
        return "callback_disabled"
    try:
        value = json.loads(raw)
        if (
            len(raw) > 1024
            or not isinstance(value, dict)
            or set(value) != {"version", "phase", "error", "seq"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["phase"] not in CALLBACK_PHASES
            or value["error"] not in CALLBACK_ERRORS
            or type(value["seq"]) is not int
            or not 0 <= value["seq"] <= 2**53
        ):
            return "callback_diagnostic_invalid"
    except (ValueError, TypeError):
        return "callback_diagnostic_invalid"
    return "callback_" + cast(str, value["phase"]) + "_" + cast(str, value["error"])


def valid_event(value: object, sequence: int) -> ProgressEvent:
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or type(value["seq"]) is not int
        or value["seq"] != sequence
        or value["kind"] not in ("start", "result")
        or value["status"] not in STATUSES
        or (value["kind"] == "start" and value["status"] not in ("start", "hidden"))
        or (value["kind"] == "result" and value["status"] == "start")
        or not isinstance(value["host"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["host"])
        or not isinstance(value["task"], str)
        or not 1 <= len(value["task"]) <= 200
        or any(c in value["task"] for c in ("{", "}", "\n", "\r", "\x1b", "://"))
        or any(ord(c) < 32 for c in value["task"])
        or (value["status"] == "hidden" and value["task"] != "[no_log]")
    ):
        raise ValueError("Invalid progress event")
    return cast(ProgressEvent, value)


def cursor(value: object) -> ProgressCursor:
    if (
        not isinstance(value, dict)
        or set(value) != {"offset", "seq"}
        or any(type(v) is not int or not 0 <= v <= 2**53 for v in value.values())
        or (value["offset"] == 0) != (value["seq"] == 0)
    ):
        raise ValueError("Invalid progress cursor")
    return cast(ProgressCursor, dict(value))


def read_page(path: str | Path, position: object) -> ProgressPage:
    saved = cursor(position)
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Progress journal link forbidden")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size < saved["offset"]:
            raise ValueError("Progress journal was truncated")
        stream.seek(saved["offset"])
        data = stream.read(MAX_BYTES)
    events: list[ProgressEvent] = []
    consumed = 0
    for line in data.splitlines(keepends=True):
        if len(events) == MAX_EVENTS:
            break
        if len(line) > MAX_LINE:
            raise ValueError("Oversized progress event")
        if not line.endswith(b"\n"):
            break  # Writer has not committed the last line yet.
        events.append(valid_event(json.loads(line), saved["seq"] + len(events) + 1))
        consumed += len(line)
    return {
        "events": events,
        "more": bool(events) and size > saved["offset"] + consumed,
        "cursor": {"offset": saved["offset"] + consumed, "seq": saved["seq"] + len(events)},
    }


class Writer:
    def __init__(self, path: str | Path) -> None:
        self.path, self.sequence = Path(path), 0
        if self.path.is_symlink():
            raise ValueError("Progress journal link forbidden")
        # Exactly one configure playbook per operation. Never append a second producer.
        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)

    def emit(self, task: object, host: object, kind: object, status: object) -> None:
        value = valid_event(
            {"seq": self.sequence + 1, "task": task, "host": host, "kind": kind, "status": status}, self.sequence + 1
        )
        line = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(line) > MAX_LINE:
            raise ValueError("Oversized progress event")
        with self.path.open("ab") as stream:
            if stream.write(line) != len(line):
                raise OSError("Short progress write")
            stream.flush()
        self.sequence += 1
