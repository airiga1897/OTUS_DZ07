"""Mutable, non-secret progress and outcomes of one explicitly owned test cycle."""

from typing import Literal, TypedDict

type DiagnosticField = Literal["terraform_stage", "terraform_operation", "failure_evidence", "diagnostics_error_type"]


class CycleReport(TypedDict, total=False):
    run_id: str
    started_utc: str
    finished_utc: str
    success: bool
    stages_completed: bool
    state_and_api_cleanup_verified: bool
    steps: list[str]
    completed_steps: list[str]
    stage: str
    require_distinct_hosts: bool
    diagnostic_colocation_allowed: bool
    existing_ids_preserved: bool
    repeat_deploy_verified: bool
    database_nodes: int
    elasticsearch_nodes: int
    elasticsearch_documents_preserved: bool
    application_and_logs_verified: bool
    original_data_and_media_preserved: bool
    error_type: str
    failed_stage: str
    api_error_context: object
    terraform_stage: str
    terraform_operation: str
    failure_evidence: str
    diagnostics_error_type: str
    cleanup_error_type: str
    cleanup_failure_evidence: str
