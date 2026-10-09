# -*- coding: utf-8 -*-
"""Workspace: Encapsulates a complete independent agent runtime.

Each Workspace represents a standalone agent workspace with its own:
- Runner (request processing)
- ChannelManager (communication channels)
- BaseMemoryManager (conversation memory)
- CronManager (scheduled tasks)

All existing single-agent components are reused without modification.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from .service_manager import ServiceDescriptor, ServiceManager
from .service_factories import (
    create_chat_service,
    create_channel_service,
    create_agent_config_watcher,
)
from ..runner import AgentRunner
from ..runner.task_tracker import TaskTracker
from ..answer_turn import AnswerTurnCoordinator, TurnIdentity
from ..crons.manager import CronManager
from ..crons.scheduler_adapter import (
    NoopSchedulerAdapter,
    RealSchedulerAdapter,
)
from ..crons.repo.json_repo import JsonJobRepository
from ...config.config import load_agent_config
from ...agents.memory.reme_light_memory_manager import (
    ReMeLightMemoryManager,
)

logger = logging.getLogger(__name__)


class _WorkspaceGoalPort:
    def __init__(self, workspace: "Workspace") -> None:
        self._workspace = workspace

    async def interrupt_if_matches(
        self,
        identity: TurnIdentity,
        reason: str,
    ) -> None:
        from ..goals.registry import get_goal_service

        service = get_goal_service()
        if service is None:
            return
        goal = await service.recent_for_chat(identity.chat_id)
        if goal is not None:
            await service.interrupt_turn_if_matches(
                goal.goal_id,
                identity.msgid,
                reason,
            )


class _WorkspaceApprovalPort:
    async def supersede_for_turn(self, identity: TurnIdentity) -> None:
        from ..approvals import get_approval_service

        await get_approval_service().supersede_pending_for_turn(
            identity.chat_id,
            identity.msgid,
        )


class _WorkspaceSubAgentPort:
    def __init__(self, workspace: "Workspace") -> None:
        self._workspace = workspace

    async def cancel_for_turn(self, identity: TurnIdentity) -> None:
        from ...agents.tools.subagent_background import (
            build_background_subagent_scope,
            get_default_background_subagent_supervisor,
        )

        workspace = self._workspace
        supervisor = getattr(workspace, "subagent_supervisor", None)
        if supervisor is None:
            supervisor = get_default_background_subagent_supervisor()
        scope = build_background_subagent_scope(
            parent_agent_config=workspace.config,
            request_context={
                "tenant_id": workspace.tenant_id,
                "agent_id": workspace.agent_id,
                "chat_id": identity.chat_id,
                "msgid": identity.msgid,
            },
        )
        await supervisor.cancel_turn_runs(
            scope,
            chat_id=identity.chat_id,
            msgid=identity.msgid,
        )


def _build_scheduler_adapter():
    """根据环境变量构造调度平台适配器。

    有 SWE_CRON_SCHEDULER_BASE_URL 时返回 RealSchedulerAdapter，
    否则返回 NoopSchedulerAdapter（本地/测试环境）。
    """
    base_url = os.environ.get("SWE_CRON_SCHEDULER_BASE_URL", "").strip()
    if not base_url:
        return NoopSchedulerAdapter()

    def _env(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    job_group_str = _env("SWE_CRON_SCHEDULER_JOB_GROUP", "0")
    return RealSchedulerAdapter(
        base_url=base_url,
        job_group=int(job_group_str) if job_group_str else 0,
        author=_env("SWE_CRON_SCHEDULER_AUTHOR"),
        alarm_email=_env("SWE_CRON_SCHEDULER_ALARM_EMAIL"),
        client_no=_env("SWE_CRON_SCHEDULER_CLIENT_NO"),
        client_key=_env("SWE_CRON_SCHEDULER_CLIENT_KEY"),
        client_remark=_env("SWE_CRON_SCHEDULER_CLIENT_REMARK"),
    )


def _resolve_memory_class(backend: str) -> type:
    """Return the memory manager class for the given backend name."""
    if backend == "remelight":
        return ReMeLightMemoryManager
    raise ValueError(f"Unsupported memory manager backend: '{backend}'")


class Workspace:
    """Single agent workspace with complete runtime components.

    Each Workspace is an independent agent instance with its own:
    - Runner: Processes agent requests
    - ChannelManager: Manages communication channels
    - BaseMemoryManager: Manages conversation memory
    - CronManager: Manages scheduled tasks

    All components use existing single-agent code without modification.
    """

    def __init__(
        self,
        agent_id: str,
        workspace_dir: str,
        tenant_id: Optional[str] = None,
        source_system_config_service: object | None = None,
        workflow_config_store: object | None = None,
        continuous_governance_service: object | None = None,
    ):
        """Initialize agent instance.

        Args:
            agent_id: Unique agent identifier
            workspace_dir: Path to agent's workspace directory
            tenant_id: Optional tenant identifier owning this workspace
        """
        self.agent_id = agent_id
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.tenant_id = tenant_id
        self._source_system_config_service = source_system_config_service
        self._workflow_config_store = workflow_config_store
        self._continuous_governance_service = continuous_governance_service
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # Service manager (unified component management)
        self._service_manager = ServiceManager(self)

        # Non-service state
        self._config = None  # Loaded before start()
        self._started = False
        self._starting = False
        self._manager = None  # Reference to MultiAgentManager
        self._task_tracker = TaskTracker()
        self._answer_turn_coordinator: AnswerTurnCoordinator | None = None

        # Register all services
        self._register_services()

        logger.debug(
            f"Created Workspace: {agent_id} at {self.workspace_dir}",
        )

    # Service access via properties (delegates to ServiceManager)
    @property
    def runner(self) -> Optional[AgentRunner]:
        """Get runner instance from ServiceManager."""
        return self._service_manager.services.get("runner")

    @property
    def memory_manager(self):
        """Get memory manager instance from ServiceManager."""
        return self._service_manager.services.get("memory_manager")

    @property
    def chat_manager(self):
        """Get chat manager instance from ServiceManager."""
        return self._service_manager.services.get("chat_manager")

    @property
    def channel_manager(self):
        """Get channel manager instance from ServiceManager."""
        return self._service_manager.services.get("channel_manager")

    @property
    def cron_manager(self):
        """Get cron manager instance from ServiceManager."""
        return self._service_manager.services.get("cron_manager")

    # Non-service state
    @property
    def task_tracker(self) -> TaskTracker:
        """Get task tracker for background chat and reconnect."""
        return self._task_tracker

    @property
    def answer_turn_coordinator(self) -> AnswerTurnCoordinator | None:
        """Get the workspace-owned answer-turn state machine."""
        return self._answer_turn_coordinator

    @property
    def config(self):
        """Get agent configuration."""
        if self._config is None:
            self._config = load_agent_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            )
        return self._config

    def set_manager(self, manager) -> None:
        """Set reference to MultiAgentManager for /daemon restart.

        Args:
            manager: MultiAgentManager instance
        """
        self._manager = manager
        # Pass to runner for /daemon restart command
        if self.runner is not None:
            self.runner._manager = manager  # pylint: disable=protected-access

    def _get_user_timezone(self) -> str:
        """Get user timezone from config.

        Returns:
            User timezone string (e.g., "Asia/Shanghai") or "UTC" as fallback.
        """
        from ...config.utils import load_config

        try:
            config = load_config()
            return config.user_timezone or "UTC"
        except Exception:
            return "UTC"

    def _create_answer_turn_coordinator(self) -> AnswerTurnCoordinator:
        """Wire the per-workspace answer-turn ports around its Runner."""
        runner = self._service_manager.services["runner"]
        coordinator = AnswerTurnCoordinator(
            stream=self._task_tracker,
            execution=runner,
            session=runner,
            goal=_WorkspaceGoalPort(self),
            subagent=_WorkspaceSubAgentPort(self),
            approval=_WorkspaceApprovalPort(),
        )
        runner.set_answer_turn_coordinator(coordinator)
        self._answer_turn_coordinator = coordinator
        self._service_manager.services["answer_turn_coordinator"] = coordinator
        return coordinator

    def _register_services(  # pylint: disable=too-many-statements
        self,
    ) -> None:
        """Register all workspace services with ServiceManager.

        Uses declarative ServiceDescriptor configuration to replace
        hardcoded initialization logic.
        """
        # pylint: disable=protected-access
        sm = self._service_manager

        # Priority 10: Runner
        sm.register(
            ServiceDescriptor(
                name="runner",
                service_class=AgentRunner,
                init_args=lambda ws: {
                    "agent_id": ws.agent_id,
                    "workspace_dir": ws.workspace_dir,
                    "task_tracker": ws._task_tracker,
                    "tenant_id": ws.tenant_id,
                },
                stop_method="stop",
                priority=10,
                concurrent_init=False,
            ),
        )

        # Priority 15: Answer-turn ownership after its Runner exists.
        sm.register(
            ServiceDescriptor(
                name="answer_turn_coordinator",
                service_class=None,
                post_init=lambda ws, _: ws._create_answer_turn_coordinator(),
                priority=15,
                concurrent_init=False,
            ),
        )

        # Priority 20: Core services (concurrent)
        sm.register(
            ServiceDescriptor(
                name="memory_manager",
                service_class=lambda ws: _resolve_memory_class(
                    ws._config.running.memory_manager_backend,
                ),
                init_args=lambda ws: {
                    "working_dir": str(ws.workspace_dir),
                    "agent_id": ws.agent_id,
                    "tenant_id": ws.tenant_id,
                },
                post_init=lambda ws, mm: setattr(
                    ws._service_manager.services["runner"],
                    "memory_manager",
                    mm,
                ),
                start_method="start",
                stop_method="close",
                reusable=True,
                priority=20,
                concurrent_init=True,
            ),
        )

        sm.register(
            ServiceDescriptor(
                name="chat_manager",
                service_class=None,
                post_init=create_chat_service,
                reusable=True,
                priority=20,
                concurrent_init=True,
            ),
        )

        # Priority 25: Runner start
        sm.register(
            ServiceDescriptor(
                name="runner_start",
                service_class=None,
                post_init=lambda ws, _: ws._service_manager.services[
                    "runner"
                ].start(),
                priority=25,
                concurrent_init=False,
            ),
        )

        # Priority 30: Channel manager
        sm.register(
            ServiceDescriptor(
                name="channel_manager",
                service_class=None,
                post_init=create_channel_service,
                start_method="start_all",
                stop_method="stop_all",
                priority=30,
                concurrent_init=False,
            ),
        )

        # Priority 40: Cron manager
        sm.register(
            ServiceDescriptor(
                name="cron_manager",
                service_class=CronManager,
                init_args=lambda ws: {  # pylint: disable=protected-access
                    "repo": JsonJobRepository(
                        str(ws.workspace_dir / "jobs.json"),
                    ),
                    "runner": ws._service_manager.services["runner"],
                    "chat_manager": ws._service_manager.services.get(
                        "chat_manager",
                    ),
                    "channel_manager": ws._service_manager.services.get(
                        "channel_manager",
                    ),
                    "timezone": ws._get_user_timezone(),
                    "agent_id": ws.agent_id,
                    "tenant_id": ws.tenant_id,
                    "scheduler_adapter": _build_scheduler_adapter(),
                    "source_system_config_service": (
                        ws._source_system_config_service
                    ),
                    "workflow_config_store": ws._workflow_config_store,
                    "continuous_governance_service": (
                        ws._continuous_governance_service
                    ),
                },
                start_method="start",
                stop_method="stop",
                priority=40,
                concurrent_init=False,
            ),
        )

        # Priority 50: Agent Config Watcher (conditional)
        sm.register(
            ServiceDescriptor(
                name="agent_config_watcher",
                service_class=None,
                post_init=create_agent_config_watcher,
                start_method="start",
                stop_method="stop",
                priority=50,
                concurrent_init=False,
            ),
        )

    async def set_reusable_components(self, components: dict) -> None:
        """Set components to reuse from previous instance.

        Must be called BEFORE start(). Allows reusing components that support
        hot-reload without recreating them. If a service has a reload_func,
        it will be called during this process.

        Args:
            components: Dict mapping component name to instance.
                Supported keys:
                - 'memory_manager': BaseMemoryManager instance
                - 'chat_manager': ChatManager instance

        Example:
            new_ws = Workspace("default", workspace_dir)
            await new_ws.set_reusable_components({
                'memory_manager': old_ws.memory_manager,
                'chat_manager': old_ws.chat_manager,
            })
            await new_ws.start()
        """
        if self._started:
            logger.warning(
                f"Cannot set reusable components for already started "
                f"workspace: {self.agent_id}",
            )
            return

        # Delegate to ServiceManager
        for name, component in components.items():
            await self._service_manager.set_reusable(name, component)

    async def start(self):
        """Start workspace and initialize all components."""
        if self._started:
            logger.debug(f"Workspace already started: {self.agent_id}")
            return

        logger.info(f"Starting workspace: {self.agent_id}")

        try:
            self._starting = True
            # 1. Load agent configuration
            self._config = load_agent_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            )
            logger.debug(f"Loaded config for agent: {self.agent_id}")

            # 2. Start all services via ServiceManager
            await self._service_manager.start_all()

            self._started = True
            self._starting = False
            logger.info(f"Workspace started successfully: {self.agent_id}")

        except asyncio.CancelledError:
            logger.info(f"Workspace start cancelled: {self.agent_id}")
            await self.stop(final=True, stop_reused=False)
            raise
        except Exception as e:
            logger.error(
                f"Failed to start agent instance {self.agent_id}: {e}",
            )
            # Clean up partially started components
            await self.stop(final=True, stop_reused=False)
            raise

    async def stop(self, final: bool = True, *, stop_reused: bool = True):
        """Stop agent instance and clean up all resources.

        Args:
            final: If True (default), stop ALL services including reusable.
                   If False, skip reusable services (for reload scenario).
            stop_reused: If False, skip services borrowed from another
                workspace even during final cleanup.
        """
        if not self._started and not self._starting:
            logger.debug(f"Workspace not started: {self.agent_id}")
            return

        logger.info(
            f"Stopping agent instance: {self.agent_id} "
            f"(final={final}, stop_reused={stop_reused})",
        )

        service_refs = (
            dict(getattr(self._service_manager, "services", {}))
            if final
            else {}
        )

        # Stop all services via ServiceManager (handles reuse automatically)
        await self._service_manager.stop_all(
            final=final,
            stop_reused=stop_reused,
        )

        if final:
            self._release_final_service_references(service_refs)

        self._started = False
        self._starting = False
        logger.info(f"Workspace stopped: {self.agent_id}")

    def _release_final_service_references(self, services: dict) -> None:
        """Drop reverse references held by services after final shutdown."""
        runner = services.get("runner")
        if runner is not None:
            if hasattr(runner, "set_workspace"):
                runner.set_workspace(None)
            if hasattr(runner, "set_chat_manager"):
                runner.set_chat_manager(None)
            if hasattr(runner, "memory_manager"):
                runner.memory_manager = None
            if hasattr(runner, "_manager"):
                runner._manager = None  # pylint: disable=protected-access
            if hasattr(runner, "set_answer_turn_coordinator"):
                runner.set_answer_turn_coordinator(None)

        channel_manager = services.get("channel_manager")
        if channel_manager is not None and hasattr(
            channel_manager,
            "set_workspace",
        ):
            channel_manager.set_workspace(None)

        self._manager = None
        self._config = None
        self._answer_turn_coordinator = None

    def __repr__(self) -> str:
        """String representation of workspace."""
        status = "started" if self._started else "stopped"
        return (
            f"Workspace(id={self.agent_id}, "
            f"workspace={self.workspace_dir}, "
            f"status={status})"
        )
