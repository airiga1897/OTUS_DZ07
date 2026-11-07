"""The guest bootstrap accepts only a confirmed address and route, never unknown JSON."""

import pytest
from remote.check_ipv4 import validate


def addresses() -> list[dict[str, object]]:
    return [
        {
            "ifname": "ens3",
            "flags": ["UP"],
            "addr_info": [{"family": "inet", "local": "10.77.0.10", "prefixlen": 24}],
        }
    ]


def routes() -> list[dict[str, object]]:
    return [{"dst": "default", "dev": "ens3", "gateway": "10.77.0.1"}]


@pytest.mark.parametrize("value", [None, {}, "PRIVATE", [None], [[]], [{0: "PRIVATE"}]])
@pytest.mark.parametrize("source", ["addresses", "routes"])
def test_unknown_rows_cannot_confirm_bootstrap(value: object, source: str) -> None:
    with pytest.raises(ValueError, match="response shape") as error:
        validate(
            "10.77.0.10/24",
            "10.77.0.1",
            value if source == "addresses" else addresses(),
            value if source == "routes" else routes(),
        )
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("flags", [None, {}, "UP", [None], [1]])
def test_unknown_flags_cannot_confirm_up_interface(flags: object) -> None:
    values = addresses()
    values[0]["flags"] = flags
    with pytest.raises(ValueError, match="interface flags"):
        validate("10.77.0.10/24", "10.77.0.1", values, routes())


@pytest.mark.parametrize("name", [None, [], True, ""])
def test_matching_address_needs_a_real_interface_name(name: object) -> None:
    values = addresses()
    values[0]["ifname"] = name
    with pytest.raises(ValueError, match="interface identity"):
        validate("10.77.0.10/24", "10.77.0.1", values, routes())


@pytest.mark.parametrize("items", [None, {}, [None]])
def test_unknown_address_details_are_rejected(items: object) -> None:
    values = addresses()
    values[0]["addr_info"] = items
    with pytest.raises(ValueError, match="response shape"):
        validate("10.77.0.10/24", "10.77.0.1", values, routes())


def test_boolean_prefix_cannot_equal_an_integer_prefix() -> None:
    values = addresses()
    values[0]["addr_info"] = [{"family": "inet", "local": "10.77.0.10", "prefixlen": True}]
    with pytest.raises(ValueError, match="missing or ambiguous"):
        validate("10.77.0.10/1", "10.77.0.1", values, routes())


def test_down_interface_does_not_override_the_confirmed_interface() -> None:
    validate("10.77.0.10/24", "10.77.0.1", [{"flags": ["DOWN"]}, *addresses()], routes())
