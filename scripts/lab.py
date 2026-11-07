"""DZ07 command line: parse, build dependencies explicitly, dispatch."""

import argparse
import time
from pathlib import Path
from typing import TYPE_CHECKING

from progress_logging import LEVELS, get_logger
from progress_logging import configure as configure_logging

if TYPE_CHECKING:
    from runtime import Runtime

LOGGER = get_logger(__name__)


def main(api: "Runtime | None" = None) -> None:
    from operator_network import OperatorIPv4MismatchError

    try:
        run_command(api)
    except OperatorIPv4MismatchError as error:
        LOGGER.error("Проверка внешнего IPv4: %s", error)
        raise SystemExit(1) from None


def run_command(api: "Runtime | None") -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "preflight",
            "deploy",
            "repair-access",
            "configure",
            "verify",
            "add-es4",
            "destroy",
            "cleanup",
            "test",
            "test-cycle",
            "open",
        ],
    )
    parser.add_argument("--log-level", choices=LEVELS)
    parser.add_argument("--purge-artifacts", action="store_true")
    parser.add_argument("--es-failover", action="store_true")
    parser.add_argument("--repair-access", action="store_true", help="Restore missing rules before a repeat deploy")
    args = parser.parse_args()
    configure_logging(args.log_level)
    started = time.perf_counter()
    LOGGER.info("Подготовка контроллера: загрузка зависимостей и локальной конфигурации; начало")
    if api is None:
        from runtime import create_runtime

        api = create_runtime(Path(__file__).resolve().parents[1])
    LOGGER.info(
        "Подготовка контроллера завершена; длительность %.1f с",
        time.perf_counter() - started,
    )
    LOGGER.info("%s: начало команды", args.action)
    if args.purge_artifacts and args.action != "destroy":
        parser.error("--purge-artifacts requires destroy")
    if args.es_failover and args.action != "verify":
        parser.error("--es-failover requires verify")
    if args.repair_access and args.action != "deploy":
        parser.error("--repair-access requires deploy")
    from test_cycle import require_access

    require_access(api, cleanup=args.action in ["destroy", "cleanup"])
    if args.action in ["deploy", "configure", "verify", "add-es4"]:
        LOGGER.info("Проверка административного доступа: начало")
        from operator_network import check_saved_access

        check_saved_access(
            api.LOCAL / "staged-deployment.json"
            if (api.LOCAL / "staged-deployment.json").exists()
            else api.ROOT / api.CLOUD["terraform_directory"] / "terraform.tfvars.json",
            api.placement()["operator_ip_services"],
            api.placement().get("admin_cidrs", []),
        )
    if args.action == "prepare":
        from cloud_settings import sequential_enabled as enabled

        if enabled(api.ROOT):
            from staged_runtime import prepare as staged_prepare

            staged_prepare(api)
        else:
            api.prepare_selected()
    elif args.action == "preflight":
        from cloud_settings import sequential_enabled as enabled

        if enabled(api.ROOT):
            from staged_runtime import preflight as staged_preflight

            staged_preflight(api)
        else:
            api.preflight()
    elif args.action == "open":
        from local_runtime import open_portal
        from stage_store import deployment_exists as exists

        if exists(api.ROOT):
            from staged_runtime import open_portal as stage_open

            stage_open(api)
        else:
            open_portal(api.ROOT, 6)
    elif args.action in ("repair-access", "deploy"):
        deploy_or_repair(api, args.action == "deploy", args.repair_access)
    elif args.action in ["test", "test-cycle"]:
        api.regression()
    elif args.action == "configure":
        api.configure()
    elif args.action == "verify":
        if args.es_failover:
            from elastic_options import verify_failover

            verify_failover(api)
        else:
            api.verify(False)
    elif args.action == "add-es4":
        from elastic_options import add_es4

        add_es4(api)
    else:
        api.destroy(args.purge_artifacts)
    LOGGER.info("%s: команда завершена успешно", args.action)


def deploy_or_repair(api: "Runtime", deploy: bool, repair_first: bool) -> None:
    if deploy and not repair_first:
        api.deploy_selected()
        return
    from stage_store import operation
    from staged_runtime import repair_access

    with operation(api.LOCAL):
        repair_access(api)
        if deploy:
            api.deploy_selected()


if __name__ == "__main__":
    main()
