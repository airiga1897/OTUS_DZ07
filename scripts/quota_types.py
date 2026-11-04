"""Validated numeric quota records; unknown API data cannot authorize allocation."""

from typing import NotRequired, TypedDict, cast


class QuotaRecord(TypedDict):
    limit: int
    in_use: NotRequired[int]
    used: NotRequired[int]
    reserved: NotRequired[int]


def checked_quota(value: object) -> QuotaRecord:
    if not isinstance(value, dict) or type(value.get("limit")) is not int or value["limit"] < -1:
        raise ValueError("Invalid quota API limit")
    for field in ("in_use", "used", "reserved"):
        if field in value and (type(value[field]) is not int or value[field] < 0):
            raise ValueError("Invalid quota API usage: " + field)
    return cast(QuotaRecord, value)
