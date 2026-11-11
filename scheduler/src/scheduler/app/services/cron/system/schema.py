# -*- coding: utf-8 -*-
"""系统任务只读 schema 契约，不创建或修改业务表。"""

REQUIRED_COLUMNS = {
    "swe_cron_system_jobs": (
        "job_id scope_kind source_id tenant_id agent_id task_type "
        "route_tenant_id route_agent_id enabled desired_config_json "
        "effective_config_json previous_config_json generation sync_generation synced_generation "
        "operation_id effective_operation_id previous_operation_id "
        "external_job_id sync_status sync_stage sync_error_code sync_owner "
        "sync_lease_until last_request_id active_execution_id execution_fence "
        "last_status last_run_at version created_at updated_at"
    ),
    "swe_cron_system_executions": (
        "execution_id job_id scope_kind tenant_id source_id agent_id task_type "
        "operation_id config_generation config_json fence status received_at "
        "dispatch_owner dispatch_lease_until dispatch_attempt accepted_at "
        "run_owner run_lease_until started_at finished_at result_json error_code "
        "resolution_request_id resolved_by resolved_at resolution_reason "
        "version created_at updated_at"
    ),
}


class SystemSchemaUnavailable(RuntimeError):
    """目标环境尚未人工准备系统任务 schema。"""

    def __init__(self, table):
        self.table = table
        super().__init__("cron_system_schema_unavailable")


async def assert_system_schema_ready(db):
    """只校验必要字段可读，不扫描配置、不接管绑定、不隐式修复。"""
    for table, fields in REQUIRED_COLUMNS.items():
        columns = ",".join(f"`{field}`" for field in fields.split())
        try:
            await db.fetch_all(f"SELECT {columns} FROM `{table}` LIMIT 0")
        except Exception as exc:
            raise SystemSchemaUnavailable(table) from exc
