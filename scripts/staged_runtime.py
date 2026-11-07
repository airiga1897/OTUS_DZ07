"""User lifecycle integration for retained, sequential infrastructure stages."""

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from cloud_contract import InfrastructureResult, InfrastructureUnavailableError, StandDescription
from cloud_settings import staged_backend
from local_runtime import checked_tree
from progress_logging import get_logger
from runtime_ports import ApplicationPort, FilePort, RetainedLifecyclePort, StagedControllerPort, TerraformPort
from stage_journal import StageJournal, checked
from stage_store import JOURNAL, StageStore, atomic_json, operation

if TYPE_CHECKING:
    from selectel_staged import Backend

LOGGER = get_logger(__name__)
backend_factory = staged_backend


def read_journal(root: Path) -> StageJournal:
    path = Path(root) / ".local" / JOURNAL
    checked_tree(path, Path(root).resolve())
    return checked(json.loads(path.read_text()))


def local_store(api: TerraformPort) -> StageStore:
    journal = read_journal(api.ROOT)
    return StageStore(api, journal)


def managed_ids(api: TerraformPort) -> dict[tuple[str, str], str]:
    store = local_store(api)
    return {
        (name, address): identifier
        for name in store.journal["stages"]
        for address, identifier in store.identities(name).items()
    }


def output_path(api: FilePort) -> Path:
    path = api.LOCAL / "infrastructure.json"
    checked_tree(path, Path(api.ROOT).resolve())
    if not path.is_file():
        raise InfrastructureUnavailableError(
            "Есть контекст поэтапного стенда, но ещё нет результата развёртывания. "
            "Сохраните данные восстановления и повторите deploy после устранения ошибки."
        )
    return path


def output(api: ApplicationPort) -> InfrastructureResult:
    result = InfrastructureResult.from_dict(json.loads(output_path(api).read_text()))
    journal = read_journal(api.ROOT)
    if (
        result.provider != journal["provider"]
        or result.scope != journal["scope"]
        or result.namespace != journal["namespace"]
    ):
        raise ValueError("Infrastructure result belongs to another deployment")
    store = local_store(api)
    for name, node in result.nodes.items():
        saved = journal["stages"][name]
        if saved["status"] != "active" or saved["output"] != node:
            raise ValueError("Infrastructure output is stale")
        if saved.get("lineage") and store.state(name).get("lineage") != saved["lineage"]:
            raise ValueError("Stage lineage differs from its saved context")
        if "identities" in saved and store.identities(name) != saved["identities"]:
            raise ValueError("Stage resource identities differ from saved context")
        stored_ids = set(store.identities(name).values())
        if not {node["id"], node["port_id"], node["volume_id"]} <= stored_ids:
            raise ValueError("Infrastructure identity differs from its stage state")
    from cloud_settings import adapter

    adapter().validate_staged_local(api, result, journal, store)
    return result


def open_portal(api: ApplicationPort, opener: Callable[[str], object] | None = None) -> str:
    """Open the saved site using local state only; no provider authentication."""
    import webbrowser

    result = output(api)
    url = "http://" + result.application()["app_public_ip"]
    for name, node in result.nodes.items():
        print(name + ": " + node["region"] + "/" + node["zone"], flush=True)
    from service_config import kibana_port

    print("Portal: " + url, flush=True)
    print("Kibana: " + url + ":" + str(kibana_port(api.ROOT)), flush=True)
    if not (opener or webbrowser.open)(url):
        print("Use the URL above to open the portal.", flush=True)
    return url


def backend(api: StagedControllerPort) -> "Backend":
    active: Backend | None = vars(api).get("_STAGED_BACKEND")
    return active if active is not None else backend_factory(api)


def reject_shared_namespace(api: FilePort) -> None:
    from cloud_settings import adapter

    return adapter().reject_shared_namespace(api)


def cleanup_empty(api: RetainedLifecyclePort, clean_local: bool = True) -> None:
    from cloud_settings import adapter
    from test_cycle import require_access

    require_access(api, cleanup=True)
    with operation(api.LOCAL):
        LOGGER.info("Очистка: локальный стенд отсутствует; начало проверки пространства имён через API")
        try:
            report = adapter().audit_empty_stages(api)
        except Exception as error:
            try:
                api.save(
                    api.ROOT / "docs/cleanup-result.json",
                    {
                        "success": False,
                        "audit_complete": False,
                        "error_type": type(error).__name__,
                        "recovery_files_preserved": True,
                    },
                )
            except Exception:
                LOGGER.error("Не удалось сохранить результат неполного аудита; первоначальная ошибка сохранена")
            raise
        LOGGER.info(
            "Очистка: API подтвердил отсутствие ресурсов собственного пространства имён в согласованной области аудита"
        )
        api.save(api.ROOT / "docs/cleanup-result.json", {"success": True, "staged": True, **report})
        if clean_local:
            from artifact_lifecycle import release

            release(api.ROOT)
            from local_runtime import cleanup_runtime

            cleanup_runtime(api.ROOT)


def checked_backend(api: StagedControllerPort) -> "Backend":
    from operator_network import allowed_cidrs, discover_cidrs

    LOGGER.info("Предварительная проверка: начало чтения контекста и подключения к Selectel API")
    provider = backend_factory(api)
    LOGGER.info("Проверка внешнего IPv4 оператора: начало")
    observed = discover_cidrs(provider.policy["operator_ip_services"])
    provider.journal["admin_cidrs"] = allowed_cidrs(
        provider.policy["admin_cidrs"], observed, provider.journal.get("admin_cidrs", [])
    )
    LOGGER.info("Проверка принадлежности сохранённых ресурсов: начало")
    provider.validate_existing()
    LOGGER.info("Подбор совместимого размещения и проверка квот: начало")
    provider.preflight()
    LOGGER.info("Предварительная проверка размещения: успешно")
    return provider


def prepare_files(api: RetainedLifecyclePort) -> None:
    LOGGER.info("Подготовка ключей и доставки исходников: начало")
    api.prepare_identity()
    import delivery

    delivery.prepare(api.ROOT)


def preflight(api: RetainedLifecyclePort) -> "Backend":
    from test_cycle import require_access

    require_access(api)
    with operation(api.LOCAL):
        return checked_backend(api)


def prepare(api: RetainedLifecyclePort) -> None:
    from test_cycle import require_access

    require_access(api)
    with operation(api.LOCAL):
        provider = checked_backend(api)
        provider.prepare_tools()
        provider.save()
        prepare_files(api)


def repair_access(api: RetainedLifecyclePort) -> None:
    from elastic_options import require_no_failover
    from selectel_access_repair import repair
    from stage_store import deployment_exists

    if not deployment_exists(api.ROOT):
        raise ValueError("Восстановление правил требует сохранённого контекста поэтапного стенда")
    require_no_failover(api)
    provider = backend_factory(api)
    repair(provider)


def deploy(api: RetainedLifecyclePort) -> None:
    import stack_config
    from staged_deploy import place
    from test_cycle import require_access

    require_access(api)
    with operation(api.LOCAL):
        from elastic_options import require_no_failover

        require_no_failover(api)
        from configuration import resume

        if resume(api):
            journal = read_journal(api.ROOT)
            expansion_marker = api.LOCAL / "expansion-pending.json"
            if expansion_marker.exists():
                from verification import expansion

                expansion(api, json.loads(expansion_marker.read_text()))
            journal["configured"], journal["stack_sha256"] = True, stack_config.digest(api.STACK)
            atomic_json(api.LOCAL / JOURNAL, journal, api.ROOT)
            from ansible_operation import finalize

            finalize(api)
            return
        provider = checked_backend(api)
        provider.prepare_tools()
        api._STAGED_BACKEND = provider
        try:
            journal = provider.journal
            if journal.get("configured") and journal["stack_sha256"] != stack_config.digest(api.STACK):
                raise ValueError("Changing a running stack requires a separate migration")
            provider.save()
            prepare_files(api)
            snapshot = None
            if journal.get("configured"):
                if journal.get("optional_es4") and not provider.completed("es4"):
                    previous = output(api)
                    if set(previous.nodes) != set(provider.desired["nodes"]) - {"es4"}:
                        raise ValueError("Incomplete es4 extension has an unexpected infrastructure snapshot")
                else:
                    previous = provider.result()
                atomic_json(api.LOCAL / "infrastructure.json", asdict(previous), Path(api.ROOT))
                from verification import snapshot as checkpoint

                snapshot = checkpoint(api, api.lab_output())
            before = provider.identities(exclude_pending=True)
            for name in provider.deployment_order():
                place(provider, name, provider.candidates_for(name))
            provider.assert_preserved(before)
            result = provider.result()
            atomic_json(api.LOCAL / "infrastructure.json", asdict(result), Path(api.ROOT))
            LOGGER.info("Ansible: начало настройки серверов и наблюдения за операцией на app")
            api.configure(existing=journal.get("configured", False))
            LOGGER.info("Ansible: настройка серверов завершена")
            if snapshot:
                from verification import expansion

                expansion(api, snapshot)
            journal["configured"], journal["stack_sha256"] = True, stack_config.digest(api.STACK)
            provider.save()
            from ansible_operation import finalize

            finalize(api)
        finally:
            api._STAGED_BACKEND = None


def cleanup(api: RetainedLifecyclePort, clean_local: bool = True) -> None:
    from test_cycle import require_access

    require_access(api, cleanup=True)
    with operation(api.LOCAL):
        LOGGER.info("Очистка: начало проверки сохранённого контекста и подключения к API")
        try:
            provider = backend_factory(api, cleanup=True)
        except (json.JSONDecodeError, KeyError) as error:
            from cloud_settings import adapter

            adapter().recover_stages(api, error)
            raise
        provider.journal["destroy_requested"] = True
        provider.save()
        try:
            from ansible_operation import before_destroy

            LOGGER.info("Очистка: проверка завершения или остановка Ansible перед удалением ВМ")
            before_destroy(api, provider)
        except Exception as error:
            api.save(
                api.ROOT / "docs/cleanup-result.json",
                {
                    "success": False,
                    "staged": True,
                    "ansible_stop_error_type": type(error).__name__,
                    "recovery_files_preserved": True,
                },
            )
            raise
        from cleanup_dependencies import children

        dependencies = children(provider.journal["stages"], provider.desired["nodes"])
        ordered = provider.cleanup_order()
        errors = {}
        for name in ordered:
            if provider.journal["stages"][name]["status"] != "deleted":
                # A parent is removable only after each own child passed its
                # state/API deletion audit. Independent pools can still finish.
                if any(provider.journal["stages"][child]["status"] != "deleted" for child in dependencies[name]):
                    LOGGER.warning("Очистка: этап %s ожидает подтверждённого удаления зависимых этапов", name)
                    errors[name] = "BlockedByCleanupDependency"
                    continue
                try:
                    provider.cleanup_attempt(name)
                except Exception as error:
                    LOGGER.error(
                        "Очистка этапа %s не подтверждена (%s); данные восстановления сохранены",
                        name,
                        type(error).__name__,
                    )
                    errors[name] = type(error).__name__
        if errors:
            api.save(
                api.ROOT / "docs/cleanup-result.json",
                {"success": False, "staged": True, "stage_errors": errors, "recovery_files_preserved": True},
            )
            raise RuntimeError("Some stages could not be removed; preserve recovery data: " + ", ".join(errors))
        if provider.identities():
            raise RuntimeError("Managed resources remain in stage states")
        LOGGER.info("Очистка: начало итогового аудита Terraform state и API")
        try:
            audit_report = provider.audit_deletion()
        except Exception as error:
            try:
                api.save(
                    api.ROOT / "docs/cleanup-result.json",
                    {
                        "success": False,
                        "staged": True,
                        "audit_complete": False,
                        "audit_error_type": type(error).__name__,
                        "recovery_files_preserved": True,
                    },
                )
            except Exception:
                LOGGER.error("Не удалось сохранить результат неполного аудита; первоначальная ошибка сохранена")
            raise
        LOGGER.info("Очистка: state и API подтвердили отсутствие ресурсов стенда")
        provider.journal["cleanup_verified"] = True
        provider.save()
        api.save(
            api.ROOT / "docs/cleanup-result.json",
            {"success": True, "staged": True, "managed_remaining": 0, **audit_report},
        )
        if clean_local:
            from artifact_lifecycle import release

            release(api.ROOT)
            from local_runtime import cleanup_runtime

            cleanup_runtime(api.ROOT)


def discover_route(api: ApplicationPort, lab: StandDescription) -> None:
    import secrets
    import urllib.request

    from operator_network import signed_route_cidr

    key = json.loads((api.LOCAL / "secrets.json").read_text())["api"].encode()
    nonce = secrets.token_hex(24)
    for _ in range(40):
        try:
            with urllib.request.urlopen(lab["site_url"] + "/?nonce=" + nonce, timeout=3) as response:
                cidr = signed_route_cidr(json.load(response), nonce, key)
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    else:
        raise RuntimeError("Signed direct HTTP route unavailable; preserve the deployment")
    provider = backend(api)
    provider.extend_admin_access(lab, cidr)
    api.save(
        api.LOCAL / "operator-network.json", {"tcp_http_cidr": cidr, "admin_cidrs": provider.journal["admin_cidrs"]}
    )
