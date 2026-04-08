# -*- coding: utf-8 -*-
"""A Manager class to handle all providers, including built-in and custom ones.
It provides a unified interface to manage providers, such as listing available
providers, adding/removing custom providers, and fetching provider details."""

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, List

try:
    import fcntl
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover (Unix)
    msvcrt = None

from pydantic import BaseModel

from agentscope.model import ChatModelBase

from swe.providers.provider import (
    ModelInfo,
    Provider,
    ProviderInfo,
)
from swe.providers.models import ModelSlotConfig
from swe.providers.openai_provider import OpenAIProvider
from swe.providers.anthropic_provider import AnthropicProvider

# from swe.providers.gemini_provider import GeminiProvider
from swe.providers.ollama_provider import OllamaProvider
from swe.constant import SECRET_DIR


logger = logging.getLogger(__name__)

if fcntl is None and msvcrt is None:  # pragma: no cover
    raise ImportError(
        "No file locking module available (need fcntl or msvcrt)",
    )


def _try_lock_file(file_obj) -> None:
    """Acquire a non-blocking exclusive lock for the lock file."""
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return

    if msvcrt is None:  # pragma: no cover
        raise RuntimeError("No supported file locking backend available")

    file_obj.seek(0)
    file_obj.write("0")
    file_obj.flush()
    file_obj.seek(0)
    msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_file(file_obj) -> None:
    """Release the lock acquired by _try_lock_file."""
    if fcntl is not None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        return

    if msvcrt is None:  # pragma: no cover
        raise RuntimeError("No supported file locking backend available")

    file_obj.seek(0)
    msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)


# -------------------------------------------------------
# Built-in provider definitions and their default models.
# -------------------------------------------------------

MODELSCOPE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="Qwen/Qwen3.5-122B-A10B",
        name="Qwen3.5-122B-A10B",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="ZhipuAI/GLM-5",
        name="GLM-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

DASHSCOPE_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3-max",
        name="Qwen3 Max",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-235b-a22b-thinking-2507",
        name="Qwen3 235B A22B Thinking",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-v3.2",
        name="DeepSeek-V3.2",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

ALIYUN_CODINGPLAN_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3.5-plus",
        name="Qwen3.5 Plus",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-5",
        name="GLM-5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="glm-4.7",
        name="GLM-4.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5",
        name="MiniMax M2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2.5",
        name="Kimi K2.5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-max-2026-01-23",
        name="Qwen3 Max 2026-01-23",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-coder-next",
        name="Qwen3 Coder Next",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="qwen3-coder-plus",
        name="Qwen3 Coder Plus",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

OPENAI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5.2",
        name="GPT-5.2",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5",
        name="GPT-5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-nano",
        name="GPT-5 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1",
        name="GPT-4.1",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-mini",
        name="GPT-4.1 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-nano",
        name="GPT-4.1 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="o3",
        name="o3",
        supports_image=True,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="o4-mini",
        name="o4-mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o",
        name="GPT-4o",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

AZURE_OPENAI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5-chat",
        name="GPT-5 Chat",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-5-nano",
        name="GPT-5 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1",
        name="GPT-4.1",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-mini",
        name="GPT-4.1 Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4.1-nano",
        name="GPT-4.1 Nano",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o",
        name="GPT-4o",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gpt-4o-mini",
        name="GPT-4o Mini",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

MINIMAX_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="MiniMax-M2.5",
        name="MiniMax M2.5",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.5-highspeed",
        name="MiniMax M2.5 Highspeed",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7",
        name="MiniMax M2.7",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="MiniMax-M2.7-highspeed",
        name="MiniMax M2.7 Highspeed",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

KIMI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="kimi-k2.5",
        name="Kimi K2.5",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-0905-preview",
        name="Kimi K2 0905 Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-0711-preview",
        name="Kimi K2 0711 Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-turbo-preview",
        name="Kimi K2 Turbo Preview",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-thinking",
        name="Kimi K2 Thinking",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="kimi-k2-thinking-turbo",
        name="Kimi K2 Thinking Turbo",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

DEEPSEEK_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="deepseek-chat",
        name="DeepSeek Chat",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
    ModelInfo(
        id="deepseek-reasoner",
        name="DeepSeek Reasoner",
        supports_image=False,
        supports_video=False,
        probe_source="documentation",
    ),
]

ANTHROPIC_MODELS: List[ModelInfo] = []

GEMINI_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gemini-3.1-pro-preview",
        name="Gemini 3.1 Pro Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-3-flash-preview",
        name="Gemini 3 Flash Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-3.1-flash-lite-preview",
        name="Gemini 3.1 Flash Lite Preview",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-flash",
        name="Gemini 2.5 Flash",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.5-flash-lite",
        name="Gemini 2.5 Flash Lite",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
    ModelInfo(
        id="gemini-2.0-flash",
        name="Gemini 2.0 Flash",
        supports_image=True,
        supports_video=True,
        probe_source="documentation",
    ),
]

PROVIDER_MODELSCOPE = OpenAIProvider(
    id="modelscope",
    name="ModelScope",
    base_url="https://api-inference.modelscope.cn/v1",
    api_key_prefix="ms",
    models=MODELSCOPE_MODELS,
    freeze_url=True,
)

PROVIDER_DASHSCOPE = OpenAIProvider(
    id="dashscope",
    name="DashScope",
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    api_key_prefix="sk",
    models=DASHSCOPE_MODELS,
    freeze_url=True,
)

PROVIDER_ALIYUN_CODINGPLAN = OpenAIProvider(
    id="aliyun-codingplan",
    name="Aliyun Coding Plan",
    base_url="https://coding.dashscope.aliyuncs.com/v1",
    api_key_prefix="sk-sp",
    models=ALIYUN_CODINGPLAN_MODELS,
    # This provider doesn't support connection check without model config
    support_connection_check=False,
    freeze_url=True,
)

PROVIDER_SWE_LOCAL = OpenAIProvider(
    id="swe-local",
    name="SWE Local",
    is_local=True,
    require_api_key=False,
)

PROVIDER_OPENAI = OpenAIProvider(
    id="openai",
    name="OpenAI",
    base_url="https://api.openai.com/v1",
    api_key_prefix="sk-",
    models=OPENAI_MODELS,
    freeze_url=True,
)

PROVIDER_AZURE_OPENAI = OpenAIProvider(
    id="azure-openai",
    name="Azure OpenAI",
    api_key_prefix="",
    models=AZURE_OPENAI_MODELS,
)

PROVIDER_MINIMAX = AnthropicProvider(
    id="minimax",
    name="MiniMax (International)",
    base_url="https://api.minimax.io/anthropic",
    models=MINIMAX_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
)

PROVIDER_MINIMAX_CN = AnthropicProvider(
    id="minimax-cn",
    name="MiniMax (China)",
    base_url="https://api.minimaxi.com/anthropic",
    models=MINIMAX_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
    # This provider doesn't support connection check without model config
    support_connection_check=False,
)

PROVIDER_KIMI_CN = OpenAIProvider(
    id="kimi-cn",
    name="Kimi (China)",
    base_url="https://api.moonshot.cn/v1",
    api_key_prefix="",
    models=KIMI_MODELS,
    chat_model="KimiChatModel",
    freeze_url=True,
)

PROVIDER_KIMI_INTL = OpenAIProvider(
    id="kimi-intl",
    name="Kimi (International)",
    base_url="https://api.moonshot.ai/v1",
    api_key_prefix="",
    models=KIMI_MODELS,
    chat_model="KimiChatModel",
    freeze_url=True,
)

PROVIDER_DEEPSEEK = OpenAIProvider(
    id="deepseek",
    name="DeepSeek",
    base_url="https://api.deepseek.com",
    api_key_prefix="sk-",
    models=DEEPSEEK_MODELS,
    freeze_url=True,
)

PROVIDER_ANTHROPIC = AnthropicProvider(
    id="anthropic",
    name="Anthropic",
    base_url="https://api.anthropic.com",
    api_key_prefix="sk-ant-",
    models=ANTHROPIC_MODELS,
    chat_model="AnthropicChatModel",
    freeze_url=True,
)

# PROVIDER_GEMINI = GeminiProvider(
#     id="gemini",
#     name="Google Gemini",
#     base_url="https://generativelanguage.googleapis.com",
#     api_key_prefix="",
#     models=GEMINI_MODELS,
#     chat_model="GeminiChatModel",
#     freeze_url=True,
#     support_model_discovery=True,
# )

PROVIDER_OLLAMA = OllamaProvider(
    id="ollama",
    name="Ollama",
    is_local=True,
    require_api_key=False,
    support_model_discovery=True,
    generate_kwargs={"max_tokens": None},
)

PROVIDER_LMSTUDIO = OpenAIProvider(
    id="lmstudio",
    name="LM Studio",
    is_local=True,
    base_url="http://localhost:1234/v1",
    require_api_key=False,
    api_key_prefix="",
    support_model_discovery=True,
    generate_kwargs={"max_tokens": None},
)


class ActiveModelsInfo(BaseModel):
    active_llm: ModelSlotConfig | None


class ProviderManager:
    """A manager class to handle all providers,
    including built-in and custom ones."""

    _instance = None
    _instances: dict[str, "ProviderManager"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, tenant_id: str = "default") -> None:
        """Initialize provider manager for a specific tenant.

        Args:
            tenant_id: The tenant ID for isolated storage. Defaults to "default".
        """
        # Initialize provider manager, load providers from registry and store
        # any necessary state (e.g., cached models).
        self.tenant_id = tenant_id
        self.builtin_providers: Dict[str, Provider] = {}
        self.custom_providers: Dict[str, Provider] = {}
        self.active_model: ModelSlotConfig | None = None
        self.root_path = self._get_tenant_root_path(tenant_id)
        self.builtin_path = self.root_path / "builtin"
        self.custom_path = self.root_path / "custom"
        self._prepare_disk_storage()
        self._init_builtins()
        try:
            self._migrate_legacy_providers()
        except Exception as e:
            logger.warning("Failed to migrate legacy providers: %s", e)
        self._init_from_storage()
        self._apply_default_annotations()

    @staticmethod
    def _get_tenant_root_path(tenant_id: str) -> Path:
        """Get the root path for a tenant's provider configuration.

        Args:
            tenant_id: The tenant ID.

        Returns:
            Path to the tenant's provider configuration directory.
        """
        return SECRET_DIR / tenant_id / "providers"

    @staticmethod
    def _do_initialize_provider_storage(
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Initialize provider storage for a tenant.

        Args:
            tenant_id: The tenant ID.
            tenant_providers_dir: Target directory for provider storage.
        """
        default_dir = SECRET_DIR / "default" / "providers"
        if default_dir.exists() and any(default_dir.iterdir()):
            logger.info(
                "Initializing provider config for tenant %s from default tenant",
                tenant_id,
            )
            shutil.copytree(default_dir, tenant_providers_dir)
            logger.info("Provider config initialized for tenant %s", tenant_id)
        else:
            logger.info(
                "Creating empty provider config structure for tenant %s",
                tenant_id,
            )
            tenant_providers_dir.mkdir(parents=True, exist_ok=True)
            (tenant_providers_dir / "builtin").mkdir(exist_ok=True)
            (tenant_providers_dir / "custom").mkdir(exist_ok=True)

    @staticmethod
    def ensure_tenant_provider_storage(tenant_id: str | None) -> None:
        """Ensure tenant provider storage exists, initializing if needed.

        This method is idempotent and concurrency-safe. It initializes tenant
        provider storage by copying from the default tenant's configuration
        when it doesn't exist. If the default tenant has no configuration,
        an empty directory structure is created.

        Args:
            tenant_id: The tenant ID to ensure storage for. If None, uses "default".

        Raises:
            TimeoutError: If unable to acquire initialization lock within timeout.
            OSError: If initialization fails due to filesystem issues.

        Note:
            This method is called automatically at provider feature boundaries
            (provider APIs, local model APIs, runtime model creation). It is safe
            to call multiple times - subsequent calls are no-ops if storage exists.
        """
        tenant_id = tenant_id or "default"
        tenant_providers_dir = SECRET_DIR / tenant_id / "providers"

        # Fast path: already exists
        if tenant_providers_dir.exists():
            return

        lock_file = tenant_providers_dir.parent / ".provider_init.lock"
        try:
            tenant_providers_dir.parent.mkdir(parents=True, exist_ok=True)

            # Simple file-based locking with timeout
            max_wait_seconds = 30.0
            deadline = time.monotonic() + max_wait_seconds

            with open(lock_file, "w", encoding="utf-8") as f:
                # Wait until we acquire the lock or another process initializes
                # Only lock holder may proceed with initialization
                while True:
                    try:
                        _try_lock_file(f)
                        break  # Acquired lock, proceed with initialization
                    except (IOError, OSError) as exc:
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                f"Timeout waiting for provider "
                                f"initialization lock for tenant {tenant_id}",
                            ) from exc
                        # Another process is initializing, wait and recheck
                        logger.debug(
                            "Waiting for concurrent provider initialization "
                            "for tenant %s",
                            tenant_id,
                        )
                        time.sleep(0.05)
                        # Recheck after wait - if directory exists, another
                        # process completed initialization
                        if tenant_providers_dir.exists():
                            return

                # Double-check after acquiring lock (another process may have
                # completed while we were waiting)
                if tenant_providers_dir.exists():
                    return

                # Try to copy from default tenant
                default_dir = SECRET_DIR / "default" / "providers"
                if default_dir.exists() and any(default_dir.iterdir()):
                    logger.info(
                        "Initializing provider config for tenant %s "
                        "from default tenant",
                        tenant_id,
                    )
                    try:
                        shutil.copytree(default_dir, tenant_providers_dir)
                        logger.info(
                            "Provider config initialized for tenant %s",
                            tenant_id,
                        )
                    except Exception as copy_error:
                        # Copy failed - log error and re-raise to prevent
                        # silent degradation to empty config
                        logger.error(
                            "Failed to copy provider config from default "
                            "for tenant %s: %s",
                            tenant_id,
                            copy_error,
                        )
                        raise
                else:
                    # Create empty directory structure (only when default
                    # doesn't exist or is empty - this is expected behavior)
                    logger.info(
                        "Creating empty provider config structure "
                        "for tenant %s (default has no config)",
                        tenant_id,
                    )
                    tenant_providers_dir.mkdir(parents=True, exist_ok=True)
                    (tenant_providers_dir / "builtin").mkdir(exist_ok=True)
                    (tenant_providers_dir / "custom").mkdir(exist_ok=True)

                # Release lock
                _unlock_file(f)

        except Exception as e:
            logger.error(
                "Failed to initialize provider config for tenant %s: %s",
                tenant_id,
                e,
            )
            raise

    @staticmethod
    def _initialize_with_lock(
        lock_file: Path,
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Initialize provider storage with file locking.

        Args:
            lock_file: Path to lock file.
            tenant_id: The tenant ID.
            tenant_providers_dir: Target directory for provider storage.
        """
        max_wait_seconds = 30.0
        deadline = time.monotonic() + max_wait_seconds

        with open(lock_file, "w", encoding="utf-8") as f:
            # Acquire lock
            ProviderManager._wait_for_lock(
                f,
                deadline,
                tenant_id,
                tenant_providers_dir,
            )

            # Double-check after acquiring lock
            if tenant_providers_dir.exists():
                return

            # Initialize storage
            ProviderManager._do_initialize_provider_storage(
                tenant_id,
                tenant_providers_dir,
            )

            # Release lock
            ProviderManager._release_lock(f)

    @staticmethod
    def _wait_for_lock(
        f,
        deadline: float,
        tenant_id: str,
        tenant_providers_dir: Path,
    ) -> None:
        """Wait for file lock with timeout.

        Args:
            f: File handle.
            deadline: Timeout deadline (monotonic time).
            tenant_id: Tenant ID for logging.
            tenant_providers_dir: Provider directory to check during wait.
        """
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover (Windows)
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (IOError, OSError) as exc:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timeout waiting for provider initialization lock for tenant {tenant_id}",
                    ) from exc
                logger.debug(
                    "Waiting for concurrent provider initialization for tenant %s",
                    tenant_id,
                )
                time.sleep(0.05)
                if tenant_providers_dir.exists():
                    return

    @staticmethod
    def _release_lock(f) -> None:
        """Release file lock.

        Args:
            f: File handle.
        """
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover (Windows)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def get_instance(tenant_id: str | None = None) -> "ProviderManager":
        """Get a ProviderManager instance for a specific tenant.

        This method implements a multi-instance singleton pattern where
        each tenant has its own isolated ProviderManager instance.

        Args:
            tenant_id: The tenant ID. If None, uses "default" tenant.

        Returns:
            ProviderManager instance for the specified tenant.
        """
        tenant_id = tenant_id or "default"

        # Fast path: check if instance exists without lock
        if tenant_id in ProviderManager._instances:
            return ProviderManager._instances[tenant_id]

        # Slow path: create instance with lock
        with ProviderManager._instances_lock:
            # Double-check after acquiring lock
            if tenant_id not in ProviderManager._instances:
                ProviderManager._instances[tenant_id] = ProviderManager(
                    tenant_id,
                )
            return ProviderManager._instances[tenant_id]

    @staticmethod
    def get_active_chat_model() -> ChatModelBase:
        """Get the currently active provider/model configuration.

        .. deprecated::
            This method is deprecated in multi-tenant environments.
            Use TenantModelContext.get_config() for tenant-isolated model selection.
        """
        import warnings

        warnings.warn(
            "get_active_chat_model() accesses global active model which is not "
            "isolated per tenant. In multi-tenant environments, use "
            "TenantModelContext.get_config() for proper tenant isolation.",
            DeprecationWarning,
            stacklevel=2,
        )
        manager = ProviderManager.get_instance()
        model = manager.get_active_model()
        if model is None or model.provider_id == "" or model.model == "":
            raise ValueError("No active model configured.")
        provider = manager.get_provider(model.provider_id)
        if provider is None:
            raise ValueError(
                f"Active provider '{model.provider_id}' not found.",
            )
        return provider.get_chat_model_instance(model.model)

    def _prepare_disk_storage(self):
        """Prepare directory structure"""
        for path in [self.root_path, self.builtin_path, self.custom_path]:
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)  # Restrict permissions for security
            except Exception:
                pass

    def _init_builtins(self):
        # Deep copy builtin providers to ensure per-tenant isolation
        self._add_builtin(deepcopy(PROVIDER_SWE_LOCAL))
        self._add_builtin(deepcopy(PROVIDER_MODELSCOPE))
        self._add_builtin(deepcopy(PROVIDER_DASHSCOPE))
        self._add_builtin(deepcopy(PROVIDER_ALIYUN_CODINGPLAN))
        self._add_builtin(deepcopy(PROVIDER_OPENAI))
        self._add_builtin(deepcopy(PROVIDER_AZURE_OPENAI))
        self._add_builtin(deepcopy(PROVIDER_KIMI_CN))
        self._add_builtin(deepcopy(PROVIDER_KIMI_INTL))
        self._add_builtin(deepcopy(PROVIDER_DEEPSEEK))
        self._add_builtin(deepcopy(PROVIDER_ANTHROPIC))
        # self._add_builtin(deepcopy(PROVIDER_GEMINI))
        self._add_builtin(deepcopy(PROVIDER_MINIMAX_CN))
        self._add_builtin(deepcopy(PROVIDER_MINIMAX))
        self._add_builtin(deepcopy(PROVIDER_OLLAMA))
        self._add_builtin(deepcopy(PROVIDER_LMSTUDIO))

    def _add_builtin(self, provider: Provider):
        self.builtin_providers[provider.id] = provider

    async def list_provider_info(self) -> List[ProviderInfo]:
        tasks = [
            provider.get_info() for provider in self.builtin_providers.values()
        ]
        tasks += [
            provider.get_info() for provider in self.custom_providers.values()
        ]
        provider_infos = await asyncio.gather(*tasks)
        return list(provider_infos)

    def get_provider(self, provider_id: str) -> Provider | None:
        # Return a provider instance by its ID. This will be used to create
        # chat model instances for the agent.
        if provider_id in self.builtin_providers:
            return self.builtin_providers[provider_id]
        if provider_id in self.custom_providers:
            return self.custom_providers[provider_id]
        return None

    async def get_provider_info(self, provider_id: str) -> ProviderInfo | None:
        provider = self.get_provider(provider_id)
        return await provider.get_info() if provider else None

    def get_active_model(self) -> ModelSlotConfig | None:
        # Return the currently active provider/model configuration.
        return self.active_model

    def update_provider(self, provider_id: str, config: Dict) -> bool:
        # Update the configuration of a provider (e.g., base URL, API key).
        # This will be called when the user edits a provider's settings in the
        # UI. It should update the in-memory provider instance and persist the
        # changes to providers.json.
        provider = self.get_provider(provider_id)
        if not provider:
            return False
        provider.update_config(config)
        self._save_provider(
            provider,
            is_builtin=provider_id in self.builtin_providers,
        )
        return True

    def start_local_model_resume(self, local_manager) -> None:
        """Schedule background restore of the active local model server."""
        task = asyncio.create_task(
            self._resume_local_model(local_manager),
            name="swe-local-model-resume",
        )
        task.add_done_callback(self._on_local_model_resume_done)

    @staticmethod
    def _on_local_model_resume_done(task: asyncio.Task[None]) -> None:
        """Log unexpected failures from background local model restore."""
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Background local model restore failed: %s",
                exc,
                exc_info=exc,
            )
        logger.info("Background local model restore completed")

    async def fetch_provider_models(
        self,
        provider_id: str,
    ) -> List[ModelInfo]:
        """Fetch the list of available models from a provider and update."""
        provider = self.get_provider(provider_id)
        if not provider:
            return []
        try:
            models = await provider.fetch_models()
            provider.extra_models = models
            self._save_provider(
                provider,
                is_builtin=provider_id in self.builtin_providers,
            )
            return models
        except Exception as e:
            logger.warning(
                "Failed to fetch models for provider '%s': %s",
                provider_id,
                e,
            )
            return []

    def _resolve_custom_provider_id(self, provider_id: str) -> str:
        """Resolve provider ID conflicts for a custom provider."""
        base_id = provider_id
        if base_id in self.builtin_providers:
            base_id = f"{base_id}-custom"

        resolved_id = base_id
        while (
            resolved_id in self.builtin_providers
            or resolved_id in self.custom_providers
        ):
            resolved_id = f"{resolved_id}-new"

        return resolved_id

    async def add_custom_provider(self, provider_data: ProviderInfo):
        # Add a new custom provider with the given data. This will update the
        # providers.json file and make the new provider available in the UI.
        provider_payload = provider_data.model_dump()
        provider_payload["id"] = self._resolve_custom_provider_id(
            provider_data.id,
        )
        provider_payload["is_custom"] = True
        provider = self._provider_from_data(
            provider_payload,
        )  # Validate provider data
        # For custom providers, we assume they don't support connection check
        # without model config, to avoid false negatives in the UI.
        provider.support_connection_check = False
        self.custom_providers[provider.id] = provider
        self._save_provider(provider, is_builtin=False)
        return await provider.get_info()

    def remove_custom_provider(self, provider_id: str) -> bool:
        # Remove a custom provider by its ID. This will update the
        # providers.json file and remove the provider from the UI.
        if provider_id in self.custom_providers:
            del self.custom_providers[provider_id]
            provider_path = self.custom_path / f"{provider_id}.json"
            if provider_path.exists():
                os.remove(provider_path)
            return True
        return False

    async def activate_model(self, provider_id: str, model_id: str):
        # Set the active provider and model for the agent. This will update
        # providers.json and determine which provider/model is used when the
        # agent creates chat model instances.
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        if not provider.has_model(model_id):
            raise ValueError(
                f"Model '{model_id}' not found in provider '{provider_id}'.",
            )
        self.active_model = ModelSlotConfig(
            provider_id=provider_id,
            model=model_id,
        )
        self.save_active_model(self.active_model)

        self.maybe_probe_multimodal(provider_id, model_id)

    def maybe_probe_multimodal(self, provider_id: str, model_id: str) -> None:
        """Schedule multimodal probing for a model if capability is unknown."""
        provider = self.get_provider(provider_id)
        # Auto-probe multimodal if not yet probed
        for model in provider.models + provider.extra_models:
            if model.id == model_id and model.supports_multimodal is None:
                asyncio.create_task(
                    self._auto_probe_multimodal(provider_id, model_id),
                )
                break

    async def _auto_probe_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> None:
        """Background probe that doesn't block model activation."""
        try:
            result = await self.probe_model_multimodal(provider_id, model_id)
            logger.info(
                "Auto-probe for %s/%s: image=%s, video=%s",
                provider_id,
                model_id,
                result.get("supports_image"),
                result.get("supports_video"),
            )
        except Exception as e:
            logger.warning("Auto-probe multimodal failed: %s", e)

    async def add_model_to_provider(
        self,
        provider_id: str,
        model_info: ModelInfo,
    ) -> ProviderInfo:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        await provider.add_model(model_info)
        self._save_provider(
            provider,
            is_builtin=provider_id in self.builtin_providers,
        )
        return await provider.get_info()

    async def delete_model_from_provider(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderInfo:
        provider = self.get_provider(provider_id)
        if not provider:
            raise ValueError(f"Provider '{provider_id}' not found.")
        await provider.delete_model(model_id=model_id)
        self._save_provider(
            provider,
            is_builtin=provider_id in self.builtin_providers,
        )
        return await provider.get_info()

    async def probe_model_multimodal(
        self,
        provider_id: str,
        model_id: str,
    ) -> dict:
        """Probe a model's multimodal capabilities and persist the result."""
        provider = self.get_provider(provider_id)
        if not provider:
            return {"error": f"Provider '{provider_id}' not found"}

        result = await provider.probe_model_multimodal(model_id)

        # Update the model's capability flags
        for model in provider.models + provider.extra_models:
            if model.id == model_id:
                model.supports_image = result.supports_image
                model.supports_video = result.supports_video
                model.supports_multimodal = result.supports_multimodal
                model.probe_source = "probed"
                break

        # Compare probe result against expected baseline
        from .capability_baseline import (
            ExpectedCapabilityRegistry,
            compare_probe_result,
        )

        registry = ExpectedCapabilityRegistry()
        expected = registry.get_expected(provider_id, model_id)
        if expected:
            discrepancies = compare_probe_result(
                expected,
                result.supports_image,
                result.supports_video,
            )
            for d in discrepancies:
                logger.warning(
                    "Probe discrepancy: %s/%s %s expected=%s actual=%s (%s)",
                    d.provider_id,
                    d.model_id,
                    d.field,
                    d.expected,
                    d.actual,
                    d.discrepancy_type,
                )

        # Persist to disk
        self._save_provider(
            provider,
            is_builtin=provider_id in self.builtin_providers,
        )
        return {
            "supports_image": result.supports_image,
            "supports_video": result.supports_video,
            "supports_multimodal": result.supports_multimodal,
            "image_message": result.image_message,
            "video_message": result.video_message,
        }

    def _save_provider(
        self,
        provider: Provider,
        is_builtin: bool = False,
        skip_if_exists: bool = False,
    ):
        """Save a provider configuration to disk."""
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider.id}.json"
        if skip_if_exists and provider_path.exists():
            return
        with open(provider_path, "w", encoding="utf-8") as f:
            json.dump(provider.model_dump(), f, ensure_ascii=False, indent=2)
        try:
            os.chmod(provider_path, 0o600)
        except OSError:
            pass

    def load_provider(
        self,
        provider_id: str,
        is_builtin: bool = False,
    ) -> Provider | None:
        """Load a provider configuration from disk."""
        provider_dir = self.builtin_path if is_builtin else self.custom_path
        provider_path = provider_dir / f"{provider_id}.json"
        if not provider_path.exists():
            return None
        try:
            with open(provider_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return self._provider_from_data(data)
        except Exception as e:
            logger.warning(
                "Failed to load provider '%s' from %s: %s",
                provider_id,
                provider_path,
                e,
            )
            return None

    def _provider_from_data(self, data: Dict) -> Provider:
        """Deserialize provider data to a concrete provider type."""
        provider_id = str(data.get("id", ""))
        chat_model = str(data.get("chat_model", ""))

        if provider_id == "anthropic" or chat_model == "AnthropicChatModel":
            return AnthropicProvider.model_validate(data)
        # if provider_id == "gemini" or chat_model == "GeminiChatModel":
        #     return GeminiProvider.model_validate(data)
        if provider_id == "ollama":
            return OllamaProvider.model_validate(data)
        return OpenAIProvider.model_validate(data)

    def save_active_model(self, active_model: ModelSlotConfig):
        """Save the active provider/model configuration to disk."""
        active_path = self.root_path / "active_model.json"
        with open(active_path, "w", encoding="utf-8") as f:
            json.dump(
                active_model.model_dump(),
                f,
                ensure_ascii=False,
                indent=2,
            )
        try:
            os.chmod(active_path, 0o600)
        except OSError:
            pass

    def load_active_model(self) -> ModelSlotConfig | None:
        """Load the active provider/model configuration from disk.

        If active_model.json doesn't exist but legacy tenant_models.json does,
        recovers the active slot from the legacy config and migrates it.
        """
        active_path = self.root_path / "active_model.json"

        # Try to load from new location first
        if active_path.exists():
            try:
                with open(active_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return ModelSlotConfig.model_validate(data)
            except Exception:
                return None

        # Recovery: migrate from legacy tenant_models.json if it exists
        legacy_config = self._recover_from_legacy_tenant_models()
        if legacy_config:
            logger.info(
                "Recovered active model from legacy tenant_models.json "
                "for tenant %s: %s/%s",
                self.tenant_id,
                legacy_config.provider_id,
                legacy_config.model,
            )
            # Save to new location for future reads
            self.save_active_model(legacy_config)
            return legacy_config

        return None

    def _recover_from_legacy_tenant_models(self) -> ModelSlotConfig | None:
        """Recover active model from legacy tenant_models.json.

        This provides one-time migration for tenants that have tenant_models.json
        but don't yet have providers/active_model.json.

        Returns:
            ModelSlotConfig if recovery succeeded, None otherwise.
        """
        try:
            from swe.tenant_models.manager import TenantModelManager

            # Check if legacy config exists for this tenant
            legacy_path = TenantModelManager.get_config_path(self.tenant_id)
            if not legacy_path.exists():
                # Try default tenant as fallback
                if self.tenant_id != "default":
                    legacy_path = TenantModelManager.get_config_path("default")
                    if not legacy_path.exists():
                        return None
                else:
                    return None

            # Load and extract active slot from legacy config
            legacy_config = TenantModelManager.load(self.tenant_id)
            if not legacy_config:
                return None

            active_slot = legacy_config.get_active_slot()
            if active_slot and active_slot.provider_id and active_slot.model:
                return ModelSlotConfig(
                    provider_id=active_slot.provider_id,
                    model=active_slot.model,
                )
        except Exception as e:
            logger.debug(
                "Failed to recover from legacy tenant_models.json "
                "for tenant %s: %s",
                self.tenant_id,
                e,
            )
        return None

    def _migrate_legacy_providers(self):
        """Migrate from legacy providers.json format to the new structure."""
        legacy_path = SECRET_DIR / "providers.json"
        if legacy_path.exists() and legacy_path.is_file():
            with open(legacy_path, "r", encoding="utf-8") as f:
                legacy_data = json.load(f)
            builtin_providers = legacy_data.get("providers", {})
            custom_providers = legacy_data.get("custom_providers", {})
            active_model = legacy_data.get("active_llm", {})
            # Migrate built-in providers
            for provider_id, config in builtin_providers.items():
                provider = self.get_provider(provider_id)
                if not provider:
                    logger.warning(
                        "Legacy provider '%s' not found in"
                        " registry, skipping migration for this provider.",
                        provider_id,
                    )
                    continue
                if "api_key" in config:
                    provider.api_key = config["api_key"]
                if "extra_models" in config:
                    provider.extra_models = [
                        ModelInfo.model_validate(model)
                        for model in config["extra_models"]
                    ]
                if not provider.freeze_url and "base_url" in config:
                    provider.base_url = config["base_url"]
                self._save_provider(provider, is_builtin=True)
            # Migrate custom providers
            for provider_id, data in custom_providers.items():
                custom_provider = OpenAIProvider(
                    id=provider_id,
                    name=data.get("name", provider_id),
                    base_url=data.get("base_url", ""),
                    api_key=data.get("api_key", ""),
                    is_custom=True,
                )
                if "models" in data:
                    # migrate models to extra_models field
                    custom_provider.extra_models = [
                        ModelInfo.model_validate(model)
                        for model in data["models"]
                    ]
                if "chat_model" in data:
                    custom_provider.chat_model = data["chat_model"]
                self._save_provider(custom_provider, is_builtin=False)
            # Migrate active model
            if active_model:
                try:
                    self.active_model = ModelSlotConfig.model_validate(
                        active_model,
                    )
                    self.save_active_model(self.active_model)
                except Exception:
                    logger.warning(
                        "Failed to migrate active model, using default.",
                    )
            # Remove legacy file after migration
            try:
                os.remove(legacy_path)
            except Exception:
                logger.warning(
                    "Failed to remove legacy providers.json after migration.",
                )

    def _init_from_storage(self):
        """Initialize all providers and active model from disk storage."""
        # Load built-in providers
        for builtin in self.builtin_providers.values():
            provider = self.load_provider(builtin.id, is_builtin=True)
            if provider:
                # inherit user-configured base_url only when freeze_url=False
                if not builtin.freeze_url:
                    builtin.base_url = provider.base_url
                builtin.api_key = provider.api_key
                builtin.extra_models = provider.extra_models
                builtin.generate_kwargs.update(provider.generate_kwargs)
        # Load custom providers
        for provider_file in self.custom_path.glob("*.json"):
            provider = self.load_provider(provider_file.stem, is_builtin=False)
            if provider:
                self.custom_providers[provider.id] = provider
        # Load active model config
        active_model = self.load_active_model()
        if active_model:
            self.active_model = active_model

    def _apply_default_annotations(self):
        """Apply doc-based default annotations for unprobed models.

        Models that already carry static annotations (supports_image /
        supports_video set at definition time) only need the derived
        supports_multimodal flag computed.  Models with no annotations
        at all fall back to the ExpectedCapabilityRegistry.
        """
        from .capability_baseline import ExpectedCapabilityRegistry

        registry = ExpectedCapabilityRegistry()
        for provider in self.builtin_providers.values():
            for model in provider.models:
                # Already fully annotated (e.g. by a prior probe) → skip
                if model.supports_multimodal is not None:
                    continue

                # Static annotations present → compute derived flag only
                if (
                    model.supports_image is not None
                    or model.supports_video is not None
                ):
                    model.supports_multimodal = bool(
                        model.supports_image or model.supports_video,
                    )
                    continue

                # No annotations at all → fall back to registry
                expected = registry.get_expected(provider.id, model.id)
                if expected:
                    model.supports_image = expected.expected_image
                    model.supports_video = expected.expected_video
                    model.supports_multimodal = bool(
                        expected.expected_image or expected.expected_video,
                    )
                    model.probe_source = "documentation"

    async def _resume_local_model(self, local_manager) -> None:
        """Resume the active local model server from the previous run."""
        local_models = self.get_provider("swe-local").extra_models
        model_id = local_models[0].id if local_models else None
        if model_id is None:
            return

        installed, _ = local_manager.check_llamacpp_installation()
        if not installed:
            logger.info(
                "Skipping local model restore because llama.cpp is not "
                "installed.",
            )
            return

        if not local_manager.is_model_downloaded(model_id):
            logger.warning(
                "Skipping local model restore because model is not "
                "downloaded: %s",
                model_id,
            )
            return

        try:
            port = await local_manager.setup_server(model_id)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Failed to restore local model server for %s: %s",
                model_id,
                exc,
            )
            return

        self.update_provider(
            "swe-local",
            {
                "base_url": f"http://127.0.0.1:{port}/v1",
                "extra_models": [ModelInfo(id=model_id, name=model_id)],
            },
        )
