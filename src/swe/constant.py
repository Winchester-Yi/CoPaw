# -*- coding: utf-8 -*-
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from dotenv import load_dotenv

# Project root directory
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Load base .env file (contains SWE_ENV and local overrides)
_base_env_path = _PROJECT_ROOT / ".env"
if _base_env_path.exists():
    load_dotenv(_base_env_path, override=False)


_ENV_VAR_OVERRIDES: ContextVar[dict[str, str] | None] = ContextVar(
    "swe_env_var_overrides",
    default=None,
)


def _get_overrides() -> dict[str, str]:
    """Get current env var overrides, returning empty dict if not set."""
    val = _ENV_VAR_OVERRIDES.get()
    return val if val is not None else {}


@contextmanager
def env_var_overrides(overrides: dict[str, str]):
    current = _get_overrides().copy()
    current.update(overrides)
    token = _ENV_VAR_OVERRIDES.set(current)
    try:
        yield
    finally:
        _ENV_VAR_OVERRIDES.reset(token)


class EnvVarLoader:
    """Utility to load and parse environment variables with type safety
    and defaults.
    """

    @staticmethod
    def get_bool(env_var: str, default: bool = False) -> bool:
        """Get a boolean environment variable,
        interpreting common truthy values."""
        overrides = _get_overrides()
        val = overrides.get(
            env_var,
            os.environ.get(env_var, str(default)),
        ).lower()
        return val in ("true", "1", "yes")

    @staticmethod
    def get_float(
        env_var: str,
        default: float = 0.0,
        min_value: float | None = None,
        max_value: float | None = None,
        allow_inf: bool = False,
    ) -> float:
        """Get a float environment variable with optional bounds
        and infinity handling."""
        try:
            overrides = _get_overrides()
            value = float(
                overrides.get(env_var, os.environ.get(env_var, str(default))),
            )
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            if not allow_inf and (
                value == float("inf") or value == float("-inf")
            ):
                return default
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_int(
        env_var: str,
        default: int = 0,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        """Get an integer environment variable with optional bounds."""
        try:
            overrides = _get_overrides()
            value = int(
                overrides.get(env_var, os.environ.get(env_var, str(default))),
            )
            if min_value is not None and value < min_value:
                return min_value
            if max_value is not None and value > max_value:
                return max_value
            return value
        except (TypeError, ValueError):
            return default

    @staticmethod
    def get_str(env_var: str, default: str = "") -> str:
        """Get a string environment variable with a default fallback."""
        overrides = _get_overrides()
        return overrides.get(env_var, os.environ.get(env_var, default))


WORKING_DIR = (
    Path(EnvVarLoader.get_str("SWE_WORKING_DIR", "~/.swe"))
    .expanduser()
    .resolve()
)
SECRET_DIR = (
    Path(
        EnvVarLoader.get_str(
            "SWE_SECRET_DIR",
            f"{WORKING_DIR}.secret",
        ),
    )
    .expanduser()
    .resolve()
)

# Default media directory for channels (cross-platform)
DEFAULT_MEDIA_DIR = WORKING_DIR / "media"

# Default local provider directory
DEFAULT_LOCAL_PROVIDER_DIR = WORKING_DIR / "local_models"

JOBS_FILE = EnvVarLoader.get_str("SWE_JOBS_FILE", "jobs.json")

CHATS_FILE = EnvVarLoader.get_str("SWE_CHATS_FILE", "chats.json")

# Builtin multi-agent profile: SWE Q&A helper.
BUILTIN_QA_AGENT_ID = "SWE_QA_Agent_0.1beta1"
BUILTIN_QA_AGENT_NAME = "QA Agent"
# Default skills when the builtin QA workspace is first created only.
BUILTIN_QA_AGENT_SKILL_NAMES: tuple[str, ...] = (
    "guidance",
    "swe_source_index",
)

TOKEN_USAGE_FILE = EnvVarLoader.get_str(
    "SWE_TOKEN_USAGE_FILE",
    "token_usage.json",
)

# Tracing configuration
TRACING_ENABLED = EnvVarLoader.get_bool("SWE_TRACING_ENABLED", False)
TRACING_BATCH_SIZE = EnvVarLoader.get_int(
    "SWE_TRACING_BATCH_SIZE",
    100,
    min_value=1,
)
TRACING_FLUSH_INTERVAL = EnvVarLoader.get_int(
    "SWE_TRACING_FLUSH_INTERVAL",
    5,
    min_value=1,
)
TRACING_RETENTION_DAYS = EnvVarLoader.get_int(
    "SWE_TRACING_RETENTION_DAYS",
    30,
    min_value=0,
)
TRACING_SANITIZE_OUTPUT = EnvVarLoader.get_bool(
    "SWE_TRACING_SANITIZE_OUTPUT",
    True,
)
TRACING_MAX_OUTPUT_LENGTH = EnvVarLoader.get_int(
    "SWE_TRACING_MAX_OUTPUT_LENGTH",
    500,
    min_value=100,
)
TRACING_STORAGE_PATH = EnvVarLoader.get_str("SWE_TRACING_STORAGE_PATH", "")

# Database configuration (shared by tracing, instance, etc.)
# New environment variable names (recommended)
DB_HOST = EnvVarLoader.get_str("SWE_DB_HOST", "")
DB_PORT = EnvVarLoader.get_int(
    "SWE_DB_PORT",
    3306,
    min_value=1,
    max_value=65535,
)
DB_USER = EnvVarLoader.get_str("SWE_DB_USER", "root")
DB_PASSWORD = EnvVarLoader.get_str("SWE_DB_PASSWORD", "")
DB_NAME = EnvVarLoader.get_str(
    "SWE_DB_NAME",
    "swe",
)
DB_MIN_CONN = EnvVarLoader.get_int(
    "SWE_DB_MIN_CONN",
    2,
    min_value=1,
)
DB_MAX_CONN = EnvVarLoader.get_int(
    "SWE_DB_MAX_CONN",
    10,
    min_value=1,
)

# Backward compatibility: TRACING_DB_* aliases
# These will be removed in a future version
TRACING_DB_HOST = DB_HOST or EnvVarLoader.get_str("SWE_TRACING_DB_HOST", "")
TRACING_DB_PORT = (
    EnvVarLoader.get_int(
        "SWE_TRACING_DB_PORT",
        DB_PORT,
        min_value=1,
        max_value=65535,
    )
    if not DB_HOST
    else DB_PORT
)
TRACING_DB_USER = (
    DB_USER
    if DB_HOST
    else EnvVarLoader.get_str(
        "SWE_TRACING_DB_USER",
        "root",
    )
)
TRACING_DB_PASSWORD = (
    DB_PASSWORD
    if DB_HOST
    else EnvVarLoader.get_str(
        "SWE_TRACING_DB_PASSWORD",
        "",
    )
)
TRACING_DB_NAME = (
    DB_NAME
    if DB_HOST
    else EnvVarLoader.get_str(
        "SWE_TRACING_DB_NAME",
        "swe_tracing",
    )
)
TRACING_DB_MIN_CONN = DB_MIN_CONN
TRACING_DB_MAX_CONN = DB_MAX_CONN

CONFIG_FILE = EnvVarLoader.get_str("SWE_CONFIG_FILE", "config.json")

HEARTBEAT_FILE = EnvVarLoader.get_str("SWE_HEARTBEAT_FILE", "HEARTBEAT.md")
HEARTBEAT_DEFAULT_EVERY = "6h"
HEARTBEAT_DEFAULT_TARGET = "main"
HEARTBEAT_TARGET_LAST = "last"

# Debug history file for /dump_history and /load_history commands
DEBUG_HISTORY_FILE = EnvVarLoader.get_str(
    "SWE_DEBUG_HISTORY_FILE",
    "debug_history.jsonl",
)
MAX_LOAD_HISTORY_COUNT = 10000

# Env key for app log level (used by CLI and app load for reload child).
LOG_LEVEL_ENV = "SWE_LOG_LEVEL"

# Env to indicate running inside a container (e.g. Docker). Set to 1/true/yes.
RUNNING_IN_CONTAINER = EnvVarLoader.get_bool(
    "SWE_RUNNING_IN_CONTAINER",
    False,
)

# Timeout in seconds for checking if a provider is reachable.
MODEL_PROVIDER_CHECK_TIMEOUT = EnvVarLoader.get_float(
    "SWE_MODEL_PROVIDER_CHECK_TIMEOUT",
    5.0,
    min_value=0,
    allow_inf=False,
)

# Playwright: use system Chromium when set (e.g. in Docker).
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH_ENV = "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"

# When True, expose /docs, /redoc, /openapi.json
# (dev only; keep False in prod).
DOCS_ENABLED = EnvVarLoader.get_bool("SWE_OPENAPI_DOCS", False)

# Memory directory
MEMORY_DIR = WORKING_DIR / "memory"

# Custom channel modules (installed via `swe channels install`); manager
# loads BaseChannel subclasses from here.
CUSTOM_CHANNELS_DIR = WORKING_DIR / "custom_channels"

# Local models directory
MODELS_DIR = WORKING_DIR / "models"

MEMORY_COMPACT_KEEP_RECENT = EnvVarLoader.get_int(
    "SWE_MEMORY_COMPACT_KEEP_RECENT",
    3,
    min_value=0,
)

# Memory compaction configuration
MEMORY_COMPACT_RATIO = EnvVarLoader.get_float(
    "SWE_MEMORY_COMPACT_RATIO",
    0.7,
    min_value=0,
    allow_inf=False,
)

DASHSCOPE_BASE_URL = EnvVarLoader.get_str(
    "DASHSCOPE_BASE_URL",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# CORS configuration — comma-separated list of allowed origins for dev mode.
# Example: SWE_CORS_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
# When unset, CORS middleware is not applied.
CORS_ORIGINS = EnvVarLoader.get_str("SWE_CORS_ORIGINS", "*").strip()

# LLM API retry configuration
LLM_MAX_RETRIES = EnvVarLoader.get_int(
    "SWE_LLM_MAX_RETRIES",
    3,
    min_value=0,
)

LLM_BACKOFF_BASE = EnvVarLoader.get_float(
    "SWE_LLM_BACKOFF_BASE",
    1.0,
    min_value=0.1,
)

LLM_BACKOFF_CAP = EnvVarLoader.get_float(
    "SWE_LLM_BACKOFF_CAP",
    10.0,
    min_value=0.5,
)

# LLM concurrency control
# Maximum number of concurrent in-flight LLM calls; excess requests wait on
# the semaphore.  Tune to your API quota: start conservatively at 3-5 and
# increase (e.g. OpenAI Tier 1 ~500 QPM allows ~25 at 3 s/call average).
LLM_MAX_CONCURRENT = EnvVarLoader.get_int(
    "SWE_LLM_MAX_CONCURRENT",
    10,
    min_value=1,
)

# Maximum queries per minute (QPM), enforced via a 60-second sliding window.
# New requests that would exceed this limit will wait before being dispatched
# to the API — proactively preventing 429s rather than reacting to them.
# 0 = unlimited (disabled).
# Examples: Anthropic Tier-1 ≈ 50 QPM; OpenAI Tier-1 ≈ 500 QPM.
LLM_MAX_QPM = EnvVarLoader.get_int(
    "SWE_LLM_MAX_QPM",
    600,
    min_value=0,
)

# Default global pause duration (seconds) applied to all waiters when a 429
# is received.  Overridden by the API's Retry-After header when present.
LLM_RATE_LIMIT_PAUSE = EnvVarLoader.get_float(
    "SWE_LLM_RATE_LIMIT_PAUSE",
    5.0,
    min_value=1.0,
)

# Random jitter range (seconds) added on top of the pause remaining time so
# concurrent waiters stagger their wake-up and avoid a new burst.
LLM_RATE_LIMIT_JITTER = EnvVarLoader.get_float(
    "SWE_LLM_RATE_LIMIT_JITTER",
    1.0,
    min_value=0.0,
)

# Maximum time (seconds) a caller will wait for a semaphore slot before
# giving up with a RuntimeError rather than blocking indefinitely.
LLM_ACQUIRE_TIMEOUT = EnvVarLoader.get_float(
    "SWE_LLM_ACQUIRE_TIMEOUT",
    300.0,
    min_value=10.0,
)

# Tool guard approval timeout (seconds).
try:
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = max(
        float(
            os.environ.get("SWE_TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS", "600"),
        ),
        1.0,
    )
except (TypeError, ValueError):
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS = 600.0

# Marker prepended to every truncation notice.
# Format:
#   <<<TRUNCATED>>>
#   The output above was truncated.
#   The full content is saved to the file and contains Z lines in total.
#   This excerpt starts at line X and covers the next N bytes.
#   If the current content is not enough, call `read_file` with
#   file_path=<path> start_line=Y to read more.
#
# Split output on this marker to recover the original (untruncated) portion:
#   original = output.split(TRUNCATION_NOTICE_MARKER)[0]
TRUNCATION_NOTICE_MARKER = "<<<TRUNCATED>>>"
