# -*- coding: utf-8 -*-
"""Retry wrapper for ChatModelBase instances.

Transparently retries LLM API calls on transient errors (rate-limit,
timeout, connection) with configurable exponential back-off.

Concurrency and rate-limit control (LLMRateLimiter):
- A tenant-local agent scoped semaphore caps the number of concurrent
  in-flight LLM calls for that agent scope.
- When a 429 is received, callers in the same agent scope are paused for the
  same duration before retrying. Unrelated agents keep their own cooldown.

Semaphore ownership rules:
- Non-streaming: __call__'s finally block always releases the slot
  (owns_semaphore stays True throughout).
- Streaming: ownership transfers to _consume_stream_with_slot the moment
  __call__ returns the generator.  owns_semaphore is set to False before
  the return so __call__'s finally skips the release.
  _consume_stream_with_slot releases after the first chunk arrives.
- Cancellation safety: the boolean flag `acquired` tracks whether the
  semaphore slot has actually been taken; the final block only releases
  when acquired is True, preventing a spurious release on CancelledError.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Awaitable

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse

from ..constant import (
    DEFAULT_LLM_CHAT_MAX_CONCURRENT,
    DEFAULT_LLM_CRON_MAX_CONCURRENT,
    LLM_ACQUIRE_TIMEOUT,
    LLM_BACKOFF_BASE,
    LLM_BACKOFF_CAP,
    LLM_CALL_TIMEOUT,
    LLM_MAX_CONCURRENT,
    LLM_MAX_RETRIES,
    LLM_MAX_QPM,
    LLM_RATE_LIMIT_JITTER,
    LLM_RATE_LIMIT_PAUSE,
    LLM_STREAM_STALL_TIMEOUT,
)
from ..config.llm_workload import (
    LLM_WORKLOAD_CHAT,
    LLM_WORKLOAD_CRON,
    LLMWorkload,
    get_current_llm_workload,
    normalize_llm_workload,
)
from .rate_limiter import (
    LLMRateLimiter,
    RateLimiterScopeKey,
    get_rate_limiter,
    resolve_rate_limiter_scope,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 432, 433, 500, 502, 503, 504, 529}
EMPTY_MODEL_OUTPUT_MAX_RETRIES = 1

_openai_retryable: tuple[type[Exception], ...] | None = None


class EmptyModelOutputError(RuntimeError):
    """Raised when a model call returns no usable content after retry."""


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Retry policy for transient LLM API failures."""

    enabled: bool = LLM_MAX_RETRIES > 0
    max_retries: int = max(LLM_MAX_RETRIES, 1)
    backoff_base: float = LLM_BACKOFF_BASE
    backoff_cap: float = LLM_BACKOFF_CAP


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Rate-limiting policy for LLM calls.

    Controls the scoped LLMRateLimiter that caps concurrency and coordinates
    pauses when a 429 is received inside the tenant-local agent scope.

    Attributes:
        max_concurrent: Fallback maximum concurrent in-flight LLM calls.
        chat_max_concurrent: Chat workload concurrency cap.
        cron_max_concurrent: Cron workload concurrency cap.
        max_qpm: Shared maximum queries per minute. 0 = disabled.
        pause_seconds: Scope-local pause duration (s) on a 429 response.
        jitter_range: Random jitter (s) added on top of the pause.
        acquire_timeout: Default max seconds to wait for a slot.
        chat_acquire_timeout: Optional chat workload acquire-timeout override.
        cron_acquire_timeout: Optional cron workload acquire-timeout override.
    """

    max_concurrent: int = LLM_MAX_CONCURRENT
    chat_max_concurrent: int | None = DEFAULT_LLM_CHAT_MAX_CONCURRENT
    cron_max_concurrent: int | None = DEFAULT_LLM_CRON_MAX_CONCURRENT
    max_qpm: int = LLM_MAX_QPM
    pause_seconds: float = LLM_RATE_LIMIT_PAUSE
    jitter_range: float = LLM_RATE_LIMIT_JITTER
    acquire_timeout: float = LLM_ACQUIRE_TIMEOUT
    chat_acquire_timeout: float | None = None
    cron_acquire_timeout: float | None = None

    def max_concurrent_for(self, workload: str | None) -> int:
        """Return the concurrency cap for *workload* with default fallback."""
        normalized = normalize_llm_workload(workload)
        if (
            normalized == LLM_WORKLOAD_CHAT
            and self.chat_max_concurrent is not None
        ):
            return self.chat_max_concurrent
        if normalized == LLM_WORKLOAD_CHAT:
            return DEFAULT_LLM_CHAT_MAX_CONCURRENT
        if (
            normalized == LLM_WORKLOAD_CRON
            and self.cron_max_concurrent is not None
        ):
            return self.cron_max_concurrent
        if normalized == LLM_WORKLOAD_CRON:
            return DEFAULT_LLM_CRON_MAX_CONCURRENT
        return self.max_concurrent

    def acquire_timeout_for(self, workload: str | None) -> float:
        """Return the acquire timeout for *workload* with default fallback."""
        normalized = normalize_llm_workload(workload)
        if (
            normalized == LLM_WORKLOAD_CHAT
            and self.chat_acquire_timeout is not None
        ):
            return self.chat_acquire_timeout
        if (
            normalized == LLM_WORKLOAD_CRON
            and self.cron_acquire_timeout is not None
        ):
            return self.cron_acquire_timeout
        return self.acquire_timeout


def _get_openai_retryable() -> tuple[type[Exception], ...]:
    global _openai_retryable
    if _openai_retryable is None:
        try:
            import openai

            _openai_retryable = (
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.APIConnectionError,
            )
        except ImportError:
            _openai_retryable = ()
    return _openai_retryable


def _is_retryable(exc: Exception) -> bool:
    """Return *True* if *exc* should trigger a retry."""
    # CancelledError 永远不重试（用户主动取消）
    if isinstance(exc, asyncio.CancelledError):
        return False
    # TimeoutError from our own wait_for / stall detection is retryable
    if isinstance(exc, TimeoutError):
        return True

    retryable = _get_openai_retryable()
    if retryable and isinstance(exc, retryable):
        return True

    status = getattr(exc, "status_code", None)
    if status is not None and status in RETRYABLE_STATUS_CODES:
        return True

    return False


def _is_rate_limit(exc: Exception) -> bool:
    """Return *True* if *exc* is specifically a 429 rate-limit error."""
    return getattr(exc, "status_code", None) == 429


def _extract_retry_after(exc: Exception) -> float | None:
    """Parse the Retry-After header value (in seconds) from an exception.

    SDK exceptions can expose headers directly or on an attached response.
    """
    headers = getattr(exc, "headers", None) or getattr(
        getattr(exc, "response", None),
        "headers",
        None,
    )
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return None


def _normalize_retry_config(retry_config: RetryConfig | None) -> RetryConfig:
    """Normalize externally supplied retry config into safe bounds."""
    if retry_config is None:
        return RetryConfig()
    normalized_backoff_base = max(0.1, retry_config.backoff_base)
    normalized_backoff_cap = max(
        0.5,
        retry_config.backoff_cap,
        normalized_backoff_base,
    )
    return RetryConfig(
        enabled=retry_config.enabled,
        max_retries=max(1, retry_config.max_retries),
        backoff_base=normalized_backoff_base,
        backoff_cap=normalized_backoff_cap,
    )


def _normalize_rate_limit_config(
    cfg: RateLimitConfig | None,
) -> RateLimitConfig:
    """Normalize externally supplied rate-limit config into safe bounds."""
    if cfg is None:
        return RateLimitConfig()
    return RateLimitConfig(
        max_concurrent=max(1, cfg.max_concurrent),
        chat_max_concurrent=(
            max(1, cfg.chat_max_concurrent)
            if cfg.chat_max_concurrent is not None
            else DEFAULT_LLM_CHAT_MAX_CONCURRENT
        ),
        cron_max_concurrent=(
            max(1, cfg.cron_max_concurrent)
            if cfg.cron_max_concurrent is not None
            else DEFAULT_LLM_CRON_MAX_CONCURRENT
        ),
        max_qpm=max(0, cfg.max_qpm),
        pause_seconds=max(1.0, cfg.pause_seconds),
        jitter_range=max(0.0, cfg.jitter_range),
        acquire_timeout=max(10.0, cfg.acquire_timeout),
        chat_acquire_timeout=(
            max(10.0, cfg.chat_acquire_timeout)
            if cfg.chat_acquire_timeout is not None
            else None
        ),
        cron_acquire_timeout=(
            max(10.0, cfg.cron_acquire_timeout)
            if cfg.cron_acquire_timeout is not None
            else None
        ),
    )


def _compute_backoff(attempt: int, retry_config: RetryConfig) -> float:
    """Exponential back-off: base * 2^(attempt-1), capped."""
    return min(
        retry_config.backoff_cap,
        retry_config.backoff_base * (2 ** max(0, attempt - 1)),
    )


def _content_block_has_usable_model_output(block: Any) -> bool:
    if block is None:
        return False
    if isinstance(block, str):
        return bool(block.strip())
    if isinstance(block, dict):
        block_type = block.get("type")
        if block_type in {"tool_use", "thinking", "reasoning"}:
            return True
        if "text" in block:
            return bool(str(block.get("text") or "").strip())
        return bool(block)
    text = getattr(block, "text", None)
    if text is not None:
        return bool(str(text).strip())
    block_type = getattr(block, "type", None)
    if block_type in {"tool_use", "thinking", "reasoning"}:
        return True
    return True


def _has_usable_model_output(response: ChatResponse) -> bool:
    content = getattr(response, "content", None)
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            _content_block_has_usable_model_output(block) for block in content
        )
    return True


class RetryChatModel(ChatModelBase):
    """Transparent retry wrapper around any :class:`ChatModelBase`.

    The wrapper delegates every call to the underlying *inner* model and
    retries on transient errors with exponential back-off.  Streaming
    responses are also covered: if the stream fails mid-consumption the
    entire request is retried from scratch.

    A scoped LLMRateLimiter is consulted on every call to cap concurrency and
    coordinate 429 pauses within the tenant-local agent scope.
    """

    def __init__(
        self,
        inner: ChatModelBase,
        retry_config: RetryConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        call_timeout: float = LLM_CALL_TIMEOUT,
        stream_stall_timeout: float = LLM_STREAM_STALL_TIMEOUT,
        on_retry: (
            Callable[[int, int, Exception, float], Awaitable[None]] | None
        ) = None,
        on_request_attempt: (
            Callable[[int, bool], Awaitable[None]] | None
        ) = None,
    ) -> None:
        super().__init__(model_name=inner.model_name, stream=inner.stream)
        self._inner = inner
        self._on_retry = on_retry
        self._on_request_attempt = on_request_attempt
        self._retry_config = _normalize_retry_config(retry_config)
        self._rate_limit_config = _normalize_rate_limit_config(
            rate_limit_config,
        )
        self._limiter_tenant_id = tenant_id
        self._limiter_agent_id = agent_id
        self._explicit_limiter_scope = (
            RateLimiterScopeKey(tenant_id, agent_id)
            if tenant_id and agent_id
            else None
        )
        self._call_timeout = max(30.0, call_timeout)
        self._stream_stall_timeout = max(10.0, stream_stall_timeout)

    # Expose the real model's class so that formatter mapping keeps working
    # when code inspects ``model.__class__`` after wrapping.
    @property
    def inner_class(self) -> type:
        return self._inner.__class__

    async def _consume_stream_with_slot(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        limiter: LLMRateLimiter,
    ) -> AsyncGenerator[ChatResponse, None]:
        """Yield all chunks from *stream*, managing the semaphore slot
        lifecycle and detecting stalls.

        Releases the semaphore slot after the first chunk arrives — once the
        API starts streaming the request has been accepted and will not be
        rate-limited mid-flight, so holding the slot for the full streaming
        duration would unnecessarily starve other callers.

        If no chunk arrives within ``self._stream_stall_timeout`` seconds,
        the stream is considered stalled and a ``TimeoutError`` is raised
        so the caller can retry.

        Always closes *stream* on completion or error.  Any exception raised
        during iteration propagates to the caller's ``async for`` loop
        (i.e. _wrap_stream), which handles retry decisions.  The exception
        does not propagate to the final consumer unless all retries are
        exhausted.
        """
        import time as _time

        first_chunk = True
        last_chunk_time = _time.monotonic()
        try:
            async for chunk in stream:
                now = _time.monotonic()
                gap = now - last_chunk_time
                if not first_chunk and gap > self._stream_stall_timeout:
                    logger.warning(
                        "LLM stream stalled: %.1fs since last chunk "
                        "(threshold=%.0fs), aborting",
                        gap,
                        self._stream_stall_timeout,
                    )
                    raise TimeoutError(
                        f"LLM stream stalled: no chunk for {gap:.1f}s "
                        f"(threshold={self._stream_stall_timeout:.0f}s)",
                    )
                last_chunk_time = now
                if first_chunk:
                    first_chunk = False
                    # return the slot once the API starts delivering
                    limiter.release()
                yield chunk
        finally:
            await stream.aclose()
            if first_chunk:
                # Stream failed before producing any chunk;
                # slot not yet released.
                limiter.release()

    async def __call__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        workload = get_current_llm_workload()
        acquire_timeout = self._rate_limit_config.acquire_timeout_for(
            workload,
        )
        limiter_scope = resolve_rate_limiter_scope(
            self._explicit_limiter_scope,
            tenant_id=self._limiter_tenant_id,
            agent_id=self._limiter_agent_id,
        )
        limiter = await get_rate_limiter(
            scope_key=limiter_scope,
            workload=workload,
            max_concurrent=self._rate_limit_config.max_concurrent_for(
                workload,
            ),
            max_qpm=self._rate_limit_config.max_qpm,
            default_pause_seconds=self._rate_limit_config.pause_seconds,
            jitter_range=self._rate_limit_config.jitter_range,
        )

        retries = (
            self._retry_config.max_retries if self._retry_config.enabled else 0
        )
        attempts = retries + 1
        empty_output_retries_remaining = EMPTY_MODEL_OUTPUT_MAX_RETRIES
        transient_attempt = 1
        call_attempt = 0

        while True:
            call_attempt += 1
            # Acquire a semaphore slot, with a timeout to prevent
            # indefinite blocking. `acquired` tracks whether the slot was
            # taken so the final block can skip the release on
            # CancelledError (slot was never acquired).
            acquired = False
            owns_semaphore = True
            try:
                try:
                    await asyncio.wait_for(
                        limiter.acquire(),
                        timeout=acquire_timeout,
                    )
                    acquired = True
                except asyncio.TimeoutError as exc:
                    logger.warning(
                        "LLM rate limiter timed out: scope=%s/%s, "
                        "workload=%s, "
                        "timeout=%.0fs",
                        limiter_scope.tenant_id,
                        limiter_scope.agent_id,
                        workload,
                        acquire_timeout,
                    )
                    raise RuntimeError(
                        f"LLM rate limiter: timed out waiting"
                        f" {acquire_timeout:.0f}s "
                        "for an execution slot "
                        f"(scope={limiter_scope.tenant_id}/"
                        f"{limiter_scope.agent_id}, workload={workload})",
                    ) from exc

                await self._notify_request_attempt(call_attempt)
                result = await asyncio.wait_for(
                    self._inner(*args, **kwargs),
                    timeout=self._call_timeout,
                )

                if isinstance(result, AsyncGenerator):
                    # Transfer semaphore ownership to _wrap_stream, which uses
                    # _consume_stream_with_slot internally and handles
                    # retries on stream failure.
                    owns_semaphore = False
                    return self._wrap_stream(
                        result,
                        args,
                        kwargs,
                        transient_attempt,
                        attempts,
                        call_attempt,
                        limiter,
                        limiter_scope,
                        workload,
                        acquire_timeout,
                    )

                if not _has_usable_model_output(result):
                    if empty_output_retries_remaining <= 0:
                        raise EmptyModelOutputError(
                            "LLM call returned empty model output after "
                            f"retry: model={self.model_name}, "
                            "stream=false, "
                            f"attempt={call_attempt}",
                        )
                    empty_output_retries_remaining -= 1
                    logger.warning(
                        "LLM call returned empty model output "
                        "(attempt %d, model=%s). Retrying once ...",
                        call_attempt,
                        self.model_name,
                    )
                    await self._notify_retry(
                        call_attempt,
                        call_attempt + 1,
                        EmptyModelOutputError(
                            "LLM call returned empty model output",
                        ),
                        0,
                    )
                    continue

                return result

            except Exception as exc:
                if _is_retryable(exc) and _is_rate_limit(exc):
                    await limiter.report_rate_limit(_extract_retry_after(exc))

                if not _is_retryable(exc) or transient_attempt >= attempts:
                    raise

                delay = _compute_backoff(transient_attempt, self._retry_config)
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s. "
                    "Retrying in %.1fs ...",
                    transient_attempt,
                    attempts,
                    exc,
                    delay,
                )
                await self._notify_retry(
                    transient_attempt,
                    attempts,
                    exc,
                    delay,
                )
                transient_attempt += 1
                await asyncio.sleep(delay)

            finally:
                if owns_semaphore and acquired:
                    limiter.release()

    async def _handle_stream_failure(
        self,
        exc: Exception,
        attempt: int,
        max_attempts: int,
        limiter: LLMRateLimiter,
        message: str,
    ) -> None:
        """Report retryable stream failures and sleep before the next try."""
        if _is_retryable(exc) and _is_rate_limit(exc):
            await limiter.report_rate_limit(_extract_retry_after(exc))

        if not _is_retryable(exc) or attempt >= max_attempts:
            raise exc

        delay = _compute_backoff(attempt, self._retry_config)
        logger.warning(
            "%s (attempt %d/%d): %s. Retrying in %.1fs ...",
            message,
            attempt,
            max_attempts,
            exc,
            delay,
        )
        await self._notify_retry(attempt, max_attempts, exc, delay)
        await asyncio.sleep(delay)

    async def _notify_request_attempt(self, attempt: int) -> None:
        if self._on_request_attempt is None:
            return
        try:
            await self._on_request_attempt(attempt, bool(self.stream))
        except Exception:
            pass

    async def _notify_retry(
        self,
        attempt: int,
        max_attempts: int,
        error: Exception,
        delay: float,
    ) -> None:
        if self._on_retry is None:
            return
        try:
            await self._on_retry(attempt, max_attempts, error, delay)
        except Exception:
            pass  # 回调失败不影响重试逻辑

    async def _acquire_stream_retry_slot(
        self,
        limiter: LLMRateLimiter,
        limiter_scope: RateLimiterScopeKey,
        workload: LLMWorkload,
        acquire_timeout: float,
    ) -> None:
        """Acquire a limiter slot for a stream retry with scoped diagnostics."""
        try:
            await asyncio.wait_for(
                limiter.acquire(),
                timeout=acquire_timeout,
            )
        except asyncio.TimeoutError as exc:
            logger.warning(
                "LLM rate limiter timed out: scope=%s/%s, workload=%s, "
                "timeout=%.0fs, stream_retry=true",
                limiter_scope.tenant_id,
                limiter_scope.agent_id,
                workload,
                acquire_timeout,
            )
            raise RuntimeError(
                f"LLM rate limiter: timed out waiting"
                f" {acquire_timeout:.0f}s"
                f" for an execution slot (stream retry, "
                f"scope={limiter_scope.tenant_id}/{limiter_scope.agent_id}, "
                f"workload={workload})",
            ) from exc

    async def _consume_stream_with_usage(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        limiter: LLMRateLimiter,
    ) -> AsyncGenerator[tuple[ChatResponse, bool], None]:
        async for chunk in self._consume_stream_with_slot(stream, limiter):
            yield chunk, _has_usable_model_output(chunk)

    async def _consume_empty_output_retry(
        self,
        remaining: int,
        *,
        attempt: int,
        stream: bool,
        message: str,
    ) -> int:
        if remaining <= 0:
            raise EmptyModelOutputError(
                f"{message} after retry: model={self.model_name}, "
                f"stream={str(stream).lower()}, attempt={attempt}",
            )
        logger.warning(
            "%s (attempt %d, model=%s). Retrying once ...",
            message,
            attempt,
            self.model_name,
        )
        # 空输出重试不等待，delay 固定为 0；与非流式路径保持一致
        await self._notify_retry(
            attempt,
            attempt + 1,
            EmptyModelOutputError(message),
            0,
        )
        return remaining - 1

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        call_args: tuple,
        call_kwargs: dict,
        current_transient_attempt: int,
        max_attempts: int,
        current_call_attempt: int,
        limiter: LLMRateLimiter,
        limiter_scope: RateLimiterScopeKey,
        workload: LLMWorkload,
        acquire_timeout: float,
    ) -> AsyncGenerator[ChatResponse, None]:
        """Yield chunks from *stream*; on transient failure, retry the full
        request and yield from the new stream instead."""
        empty_output_retries_remaining = EMPTY_MODEL_OUTPUT_MAX_RETRIES
        transient_attempt = current_transient_attempt
        call_attempt = current_call_attempt
        limiter.begin_stream()
        try:
            try:
                has_usable_output = False
                async for (
                    chunk,
                    chunk_usable,
                ) in self._consume_stream_with_usage(
                    stream,
                    limiter,
                ):
                    has_usable_output = has_usable_output or chunk_usable
                    yield chunk
                if has_usable_output:
                    return  # stream completed without error
                empty_output_retries_remaining = (
                    await self._consume_empty_output_retry(
                        empty_output_retries_remaining,
                        attempt=call_attempt,
                        stream=True,
                        message="LLM stream returned empty model output",
                    )
                )
            except Exception as failed_exc:
                await self._handle_stream_failure(
                    failed_exc,
                    transient_attempt,
                    max_attempts,
                    limiter,
                    "LLM stream failed",
                )
                transient_attempt += 1

            # Retry loop for stream failures
            while True:
                call_attempt += 1
                acquired = False
                owns_semaphore = True
                try:
                    await self._acquire_stream_retry_slot(
                        limiter,
                        limiter_scope,
                        workload,
                        acquire_timeout,
                    )
                    acquired = True

                    await self._notify_request_attempt(call_attempt)
                    result = await asyncio.wait_for(
                        self._inner(*call_args, **call_kwargs),
                        timeout=self._call_timeout,
                    )

                    if isinstance(result, AsyncGenerator):
                        owns_semaphore = False
                        has_usable_output = False
                        async for (
                            chunk,
                            chunk_usable,
                        ) in self._consume_stream_with_usage(
                            result,
                            limiter,
                        ):
                            has_usable_output = (
                                has_usable_output or chunk_usable
                            )
                            yield chunk
                        if has_usable_output:
                            return  # stream completed without error
                        empty_output_retries_remaining = await self._consume_empty_output_retry(
                            empty_output_retries_remaining,
                            attempt=call_attempt,
                            stream=True,
                            message="LLM stream returned empty model output",
                        )
                        continue

                    if _has_usable_model_output(result):
                        yield result
                        return

                    empty_output_retries_remaining = await self._consume_empty_output_retry(
                        empty_output_retries_remaining,
                        attempt=call_attempt,
                        stream=True,
                        message="LLM stream retry returned empty model output",
                    )
                    continue

                except Exception as retry_exc:
                    await self._handle_stream_failure(
                        retry_exc,
                        transient_attempt,
                        max_attempts,
                        limiter,
                        "LLM stream retry failed",
                    )
                    transient_attempt += 1

                finally:
                    if owns_semaphore and acquired:
                        limiter.release()
        finally:
            limiter.end_stream()
