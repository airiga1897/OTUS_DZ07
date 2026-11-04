"""Guest SQL verification distinguishes missing results from a Python indexing bug."""

import subprocess
from unittest.mock import Mock, patch

import psycopg
import pytest
from remote import verify


def test_missing_recovery_status_is_a_verification_failure() -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    with patch.object(verify, "sql_connect", return_value=context), pytest.raises(AssertionError, match="query"):
        verify.check_database()


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"id": True, "slug": "s", "body": "b"},
        {"id": 1, "slug": None, "body": "b"},
        {"id": 1, "slug": "s", "body": None},
    ],
)
def test_bad_portal_record_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="portal verification"):
        verify.checked_record(value)


def test_valid_portal_record_preserves_unicode() -> None:
    value = {"id": 1, "slug": "synthetic", "body": "Проверка сохранности"}
    assert verify.checked_record(value) is value


@pytest.mark.parametrize("maximum", [None, True, -1, "12"])
def test_invalid_maximum_stops_before_application_write(maximum: object) -> None:
    connection = Mock()
    connection.execute.return_value.fetchone.side_effect = [(True,), (1,), (maximum,)]
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    with (
        patch.object(verify, "sql_connect", return_value=context),
        patch.object(subprocess, "check_output") as write,
        pytest.raises(AssertionError, match="primary key is unconfirmed"),
    ):
        verify.insert_record("baseline")
    write.assert_not_called()


@pytest.mark.parametrize("slug,body", [(None, "committed-" + "b" * 32), ("baseline-" + "a" * 32, None)])
def test_non_text_legacy_fields_are_not_normalized(slug: object, body: object) -> None:
    row = (1, slug, slug, 6, "proof", body, "", True, "timestamp")
    assert verify.manifest_row("knowledge_article", row) is row


def test_missing_initial_database_status_closes_connection() -> None:
    from remote import initial_data

    connection = Mock()
    connection.execute.return_value.fetchone.return_value = None
    with (
        patch.object(psycopg, "connect", return_value=connection),
        pytest.raises(RuntimeError, match="writable"),
    ):
        initial_data.wait_for_primary("synthetic-only")
    connection.close.assert_called_once()
