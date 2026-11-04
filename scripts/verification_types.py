"""Checked, non-secret canary metadata retained across deployment retries."""

import re
from typing import TypedDict, cast


class BaselineResult(TypedDict, total=False):
    new_record: dict[str, object]


class ExpansionSnapshot(TypedDict, total=False):
    run_id: str
    before: BaselineResult
    server_ids: dict[str, str]
    elastic_checkpoint: bool


class VerificationReport(TypedDict):
    baseline: dict[str, object]
    logs: dict[str, object]


def document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("Invalid verification document")
    return cast(dict[str, object], value)


def baseline(value: object) -> BaselineResult:
    values = document(value)
    if "new_record" in values:
        document(values["new_record"])
    return cast(BaselineResult, values)


def checked(value: object) -> ExpansionSnapshot:
    values = document(value)
    if "run_id" in values:
        run_id = values["run_id"]
        if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
            raise ValueError("Invalid verification operation identity")
    if "before" in values:
        baseline(values["before"])
    if "server_ids" in values:
        identifiers = document(values["server_ids"])
        if any(not isinstance(item, str) for item in identifiers.values()):
            raise ValueError("Invalid verification server identities")
    if "elastic_checkpoint" in values and type(values["elastic_checkpoint"]) is not bool:
        raise ValueError("Invalid verification checkpoint flag")
    return cast(ExpansionSnapshot, values)
