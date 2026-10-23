# -*- coding: utf-8 -*-
"""Request-owned TLS preparation for SDK clients constructed on the loop."""

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from ssl import SSLContext


@dataclass(frozen=True)
class PreparedTLSFailure:
    """A traceback-free TLS failure captured before OpenAI installation."""

    error: Exception


_prepared_tls: ContextVar[SSLContext | PreparedTLSFailure | None] = ContextVar(
    "prepared_client_tls",
    default=None,
)


class _ClientCleanup:
    """Preserve the SDK default client's best-effort asyncio cleanup."""

    def __del__(self):
        if self.is_closed:
            return
        try:
            asyncio.get_running_loop().create_task(self.aclose())
        except RuntimeError:
            pass


@contextmanager
def bind_prepared_client_tls(
    context: SSLContext | PreparedTLSFailure | None,
):
    token = _prepared_tls.set(context)
    try:
        yield
    finally:
        _prepared_tls.reset(token)


def prepared_http_client_kwargs(sdk: str) -> dict:
    """Retain SDK timeout/pool defaults and HTTPX trust-env behavior."""
    context = _prepared_tls.get()
    if context is None:
        return {}
    if isinstance(context, PreparedTLSFailure):
        if sdk == "openai":
            # Each construction gets its own traceback; never retain Agent
            # frames in the request's frozen preparation result.
            raise copy.copy(context.error)
        return {}
    if sdk == "anthropic":
        from anthropic import DefaultAsyncHttpxClient as AnthropicHttpxClient

        class AnthropicClient(_ClientCleanup, AnthropicHttpxClient):
            pass

        return {"http_client": AnthropicClient(verify=context)}
    else:
        from openai import DefaultAsyncHttpxClient as OpenAIHttpxClient

        class OpenAIClient(_ClientCleanup, OpenAIHttpxClient):
            pass

        return {"http_client": OpenAIClient(verify=context)}
