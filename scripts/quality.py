"""Local static tools. Credentials are not needed or forwarded to checker plugins."""

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolResult:
    name: str
    exit_code: int
    duration_seconds: float
    log: str


def checker_environment(root: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "VIRTUAL_ENV",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["DJANGO_SECRET_KEY"] = "local-static-analysis-only"
    environment["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
    environment["PYTHONPATH"] = os.pathsep.join(str(root / path) for path in ("scripts", "app", "tests/quality"))
    return environment


def tool_commands() -> dict[str, list[str]]:
    roots = ["scripts", "app", "tests"]
    return {
        "ruff": ["-m", "ruff", "check", *roots],
        "format": ["-m", "ruff", "format", "--check", *roots],
        "mypy": ["-m", "mypy", "--config-file", "mypy.ini", *roots],
    }


def run_tool(root: Path, name: str, arguments: list[str]) -> ToolResult:
    LOGGER.info("Локальная проверка %s: начало", name)
    started = time.perf_counter()
    targets = ("win32", "linux") if name == "mypy" else (None,)
    exit_code = 0
    messages: list[str] = []
    for target in targets:
        platform = (
            ["--cache-dir", str(root / ".tools/quality" / ("mypy-cache-" + target)), "--platform", target]
            if target is not None
            else []
        )
        result = subprocess.run(
            [sys.executable, *arguments, *platform],
            cwd=root,
            env=checker_environment(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        exit_code = exit_code or result.returncode
        messages.append(("Target: " + target + "\n" if target is not None else "") + result.stdout)
    folder = root / ".tools/quality"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (name + "-latest.log")
    output.write_text("\n".join(messages), encoding="utf-8")
    measured = ToolResult(name, exit_code, round(time.perf_counter() - started, 3), output.relative_to(root).as_posix())
    LOGGER.info(
        "Локальная проверка %s: код %s; %.1f с; журнал %s",
        name,
        measured.exit_code,
        measured.duration_seconds,
        measured.log,
    )
    return measured


def run_checks(root: Path, selected: Sequence[str] = ("ruff", "format", "mypy")) -> list[ToolResult]:
    commands = tool_commands()
    if not set(selected) <= commands.keys():
        raise ValueError("Unknown local quality check")
    results = [run_tool(root, name, commands[name]) for name in selected]
    folder = root / ".tools/quality"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "latest-checks.json").write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    return results


def main(root: Path | None = None, argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("checks", nargs="*", metavar="CHECK")
    arguments = parser.parse_args(argv)
    if unknown := set(arguments.checks) - tool_commands().keys():
        parser.error("Unknown local quality check: " + ", ".join(sorted(unknown)))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    results = run_checks(
        root if root is not None else Path(__file__).resolve().parents[1], arguments.checks or tuple(tool_commands())
    )
    return int(any(result.exit_code for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
