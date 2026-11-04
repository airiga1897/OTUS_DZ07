"""Terraform state shapes remain read-only, partial, and provider-metadata agnostic."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from stage_store import StageStore, resources
from terraform_state_types import checked_state


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"lineage": 2},
        {"resources": None},
        {"resources": [None]},
        {"resources": [{"mode": False}]},
        {"resources": [{"instances": None}]},
        {"resources": [{"instances": [{"attributes": None}]}]},
        {"resources": [{"instances": [{"index_key": True}]}]},
    ],
)
def test_bad_state_keeps_original_file(tmp_path: Path, value: object) -> None:
    api = Mock(spec_set=["ROOT", "TF", "RUN", "redact"])
    api.ROOT = tmp_path
    store = StageStore(api, {})
    path = store.folder("es1") / "terraform.tfstate"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Terraform"):
        store.state("es1")
    assert path.read_bytes() == original


def test_partial_state_retains_unknown_provider_fields() -> None:
    value = {
        "lineage": "сохранённая-линия",
        "provider_metadata": None,
        "resources": [
            {
                "mode": "managed",
                "type": "vm",
                "name": "es1",
                "module": "module.stage",
                "instances": [
                    {"attributes": {"id": "local-id", "metadata": {"custom": None}}, "index_key": "es1"},
                    {"index_key": 0},
                    {"index_key": None},
                    {},
                ],
            },
            {},
        ],
    }
    assert checked_state(value) is value
    assert checked_state({}) == {}
    assert checked_state({"lineage": None}) == {"lineage": None}


def test_non_string_keys_rejected_at_object_boundary() -> None:
    with pytest.raises(ValueError, match="Terraform state object"):
        checked_state({1: "bad"})


def test_recursive_output_module_filters_only_managed_resources() -> None:
    managed = {"mode": "managed", "type": "vm", "values": {"id": "local-id"}}
    assert resources({"resources": [{"mode": "data"}], "child_modules": [{"resources": [managed]}]}) == [managed]
    assert resources({}) == []


@pytest.mark.parametrize("value", [None, {"resources": [None]}, {"child_modules": [False]}])
def test_invalid_output_module(value: object) -> None:
    with pytest.raises(ValueError, match="Terraform"):
        resources(value)
