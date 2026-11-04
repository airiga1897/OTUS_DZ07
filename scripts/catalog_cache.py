"""Command-local reuse of successfully read catalog data, never live resource state."""

from collections.abc import Callable, Hashable
from copy import deepcopy
from typing import TypeVar, cast

Value = TypeVar("Value")


class CatalogCache:
    def __init__(self) -> None:
        self.values: dict[Hashable, object] = {}

    def read(self, key: Hashable, loader: Callable[[], Value]) -> Value:
        if key not in self.values:
            self.values[key] = deepcopy(loader())
        # A catalog key identifies one response schema for this command.
        return cast(Value, deepcopy(self.values[key]))
