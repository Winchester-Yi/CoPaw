# -*- coding: utf-8 -*-
"""Data sanitization utilities for tracing.

Provides functions to sanitize sensitive data before storage.
"""
from typing import Any, Optional

# Sensitive keys to redact from tool input/output
SENSITIVE_KEYS = frozenset(
    [
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "credential",
        "private_key",
        "access_token",
        "refresh_token",
        "session_id",
        "auth",
        "private-key",
        "privatekey",
        "secret_key",
        "secretkey",
        "api_secret",
        "apisecret",
    ],
)


def sanitize_dict(
    data: Optional[dict[str, Any]],
    max_length: int = 500,
) -> Optional[dict]:
    """Sanitize dictionary by redacting sensitive keys.

    Args:
        data: Dictionary to sanitize
        max_length: Maximum string length for truncation

    Returns:
        Sanitized dictionary with sensitive values redacted
    """
    if data is None:
        return None

    result = {}
    for key, value in data.items():
        key_lower = key.lower()
        # Check if key contains any sensitive keyword
        if any(sensitive in key_lower for sensitive in SENSITIVE_KEYS):
            result[key] = "[REDACTED]"
        elif isinstance(value, str) and len(value) > max_length:
            result[key] = value[:max_length] + "..."
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, max_length)
        elif isinstance(value, list):
            result[key] = [
                sanitize_dict(item, max_length)
                if isinstance(item, dict)
                else item[:max_length] + "..."
                if isinstance(item, str) and len(item) > max_length
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def sanitize_string(
    text: Optional[str],
    max_length: int = 500,
) -> Optional[str]:
    """Sanitize string by truncating and removing media references.

    Args:
        text: String to sanitize
        max_length: Maximum length

    Returns:
        Sanitized string
    """
    if text is None:
        return None
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def sanitize_user_message(
    message: Optional[str],
    max_length: int = 500,
) -> Optional[str]:
    """Sanitize user message for storage.

    Args:
        message: User message to sanitize
        max_length: Maximum length

    Returns:
        Sanitized message
    """
    return sanitize_string(message, max_length)
