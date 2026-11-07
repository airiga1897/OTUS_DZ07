"""Read-only progress with explicit, non-secret diagnostic categories."""

import json
import shlex
from collections.abc import Callable

from ansible_operation import OperationContext
from paramiko import AuthenticationException, BadHostKeyException, SSHClient
from progress_logging import get_logger
from remote.ansible_events import (
    CALLBACK_REASONS,
    MAX_BYTES,
    MAX_EVENTS,
    ProgressCursor,
    ProgressEvent,
    cursor,
    valid_event,
)
from runtime_ports import FilePort, OperationPort, StandDescription

type ProgressCommand = Callable[[SSHClient, str, object], str]
type SaveProgress = Callable[[FilePort, OperationContext], None]

LOGGER = get_logger(__name__)
MAX_PAGES = 16
LABELS = {
    "start": "начало",
    "ok": "ok",
    "changed": "changed",
    "failed": "failed",
    "skipped": "skipped",
    "hidden": "скрыто (no_log)",
}
TEMPORARY = {
    "transport_unavailable": "не удалось получить ответ SSH/RPC",
    "progress_unavailable": "источник пока не сообщил подробный прогресс",
    "not_started": "callback ещё не начал передачу событий",
    "journal_read_unavailable": "журнал временно недоступен для чтения",
    "more_events_pending": "оставшиеся события будут прочитаны следующим опросом",
}
PROBLEMS = {
    **CALLBACK_REASONS,
    "callback_diagnostic_invalid": "диагностика callback повреждена или имеет неизвестный формат",
    "callback_disabled": "callback прекратил запись событий после ошибки",
    "journal_missing": "после завершения операции отсутствует журнал событий",
    "journal_incomplete": "после завершения операции осталась недописанная строка журнала",
    "journal_invalid": "нарушены формат или последовательность событий журнала",
    "journal_permission_denied": "отказано в доступе к журналу прогресса на app",
    "observer_module_missing": "на app отсутствует модуль чтения прогресса",
    "observer_internal_error": "на app произошла ошибка обработчика прогресса",
    "binding_mismatch": "ответ прогресса относится к другой операции, ВМ или стенду",
    "ssh_identity_unconfirmed": "не подтверждены SSH-ключ сервера или реквизиты SSH",
    "response_invalid": "получен ответ прогресса неподдерживаемого формата",
    "response_oversized": "ответ прогресса превышает допустимый размер",
    "cursor_invalid": "нарушена сохранённая позиция чтения прогресса",
    "event_invalid": "нарушены формат, порядок событий или принадлежность узла",
    "cursor_save_failed": "не удалось сохранить позицию чтения; сообщения могут повториться",
    "local_observer_error": "ошибка локального обработчика прогресса",
}
STATES = {
    "preparing": "идёт подготовка операции",
    "running": "операция ещё выполняется",
    "succeeded": "операция успешно завершена",
    "failed": "операция завершилась ошибкой",
    "stopped": "операция остановлена",
    "unknown": "состояние операции не подтверждено",
}


class ProgressUnavailable(ValueError):
    """Temporary observation failure; not an Ansible execution failure."""


class ProgressProblem(ValueError):
    """Confirmed observer/protocol problem with an allowlisted reason code."""


def notice(value: OperationContext, reason: str, state: str | None = None) -> None:
    confirmed = reason in PROBLEMS
    text = PROBLEMS[reason] if confirmed else TEMPORARY[reason]
    state = state if state in STATES else "unknown"
    key = [reason, state]
    if value.get("_progress_notice") == key:
        return
    try:
        method = LOGGER.warning if confirmed else LOGGER.info
        method("Ansible: %s; %s; наблюдение продолжается", text, STATES[state])
    except Exception:
        pass
    value["_progress_notice"] = key
    if confirmed:
        value["_progress_warning"] = True
    else:
        value.pop("_progress_warning", None)


def warning(value: OperationContext) -> None:
    notice(value, "event_invalid")


def page(
    api: OperationPort, lab: StandDescription, value: OperationContext, command: ProgressCommand
) -> tuple[list[ProgressEvent], ProgressCursor, bool]:
    try:
        position = cursor(value.get("progress_cursor", {"offset": 0, "seq": 0}))
    except ValueError:
        raise ProgressProblem("cursor_invalid") from None
    remote = api.STACK["paths"]["remote_root"]
    try:
        with api.connect(lab) as client:
            raw = command(
                client,
                "python3 " + shlex.quote(remote + "/.ansible-operation.py") + " --root " + shlex.quote(remote) + " rpc",
                {"action": "events", "request": value["request"], "cursor": position},
            )
    except (AuthenticationException, BadHostKeyException):
        raise ProgressProblem("ssh_identity_unconfirmed") from None
    except Exception:
        raise ProgressUnavailable("transport_unavailable") from None
    if not isinstance(raw, str):
        raise ProgressProblem("response_invalid")
    if len(raw) > MAX_BYTES * 2:
        raise ProgressProblem("response_oversized")
    try:
        response = json.loads(raw)
    except ValueError:
        raise ProgressProblem("response_invalid") from None
    if not isinstance(response, dict) or type(response.get("available")) is not bool:
        raise ProgressProblem("response_invalid")
    if response.get("binding") != value["request"]:
        raise ProgressProblem("binding_mismatch")
    if not response["available"]:
        if set(response) == {"binding", "available", "pending"} and response["pending"] is True:
            raise ProgressUnavailable("not_started")
        if set(response) == {"binding", "available"}:
            raise ProgressUnavailable("progress_unavailable")
        reason = response.get("reason")
        if set(response) != {"binding", "available", "reason"} or not isinstance(reason, str):
            raise ProgressProblem("response_invalid")
        if reason in TEMPORARY:
            raise ProgressUnavailable(reason)
        if reason in PROBLEMS:
            raise ProgressProblem(reason)
        raise ProgressProblem("response_invalid")
    if set(response) != {"binding", "available", "events", "cursor", "more"}:
        raise ProgressProblem("response_invalid")
    events = response["events"]
    try:
        following = cursor(response["cursor"])
    except ValueError:
        raise ProgressProblem("cursor_invalid") from None
    if not isinstance(events, list) or len(events) > MAX_EVENTS or type(response["more"]) is not bool:
        raise ProgressProblem("response_invalid")
    if (
        following["seq"] != position["seq"] + len(events)
        or not 0 <= following["offset"] - position["offset"] <= MAX_BYTES
        or (bool(events) != (following["offset"] > position["offset"]))
        or (response["more"] and not events)
    ):
        raise ProgressProblem("cursor_invalid")
    checked: list[ProgressEvent] = []
    for index, event in enumerate(events):
        try:
            parsed = valid_event(event, position["seq"] + index + 1)
        except (ValueError, TypeError):
            raise ProgressProblem("event_invalid") from None
        if parsed["host"] not in {*lab["server_ids"], "localhost"}:
            raise ProgressProblem("event_invalid")
        checked.append(parsed)
    return checked, following, response["more"]


def observe(
    api: OperationPort,
    lab: StandDescription,
    value: OperationContext,
    command: ProgressCommand,
    save: SaveProgress,
    *,
    operation_state: str | None = None,
) -> None:
    state = operation_state or value.get("state")
    try:
        for _ in range(MAX_PAGES):
            events, following, more = page(api, lab, value, command)
            for event in events:
                LOGGER.info("Ansible | %s | %s | %s", event["host"], event["task"], LABELS[event["status"]])
            if events:
                previous = value.get("progress_cursor")
                value["progress_cursor"] = following
                try:
                    save(api, value)
                except Exception:
                    if previous is None:
                        value.pop("progress_cursor", None)
                    else:
                        value["progress_cursor"] = previous
                    raise ProgressProblem("cursor_save_failed") from None
            value.pop("_progress_warning", None)
            value.pop("_progress_notice", None)
            if not more:
                return
        notice(value, "more_events_pending", state)
    except ProgressUnavailable as error:
        notice(value, error.args[0], state)
    except ProgressProblem as error:
        notice(value, error.args[0], state)
    except Exception:
        notice(value, "local_observer_error", state)
