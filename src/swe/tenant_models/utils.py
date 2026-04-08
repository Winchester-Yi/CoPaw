# -*- coding: utf-8 -*-
"""Utility functions for tenant model configuration."""

import os
import re
from typing import Optional


def resolve_env_vars(value: Optional[str]) -> Optional[str]:
    """
    Resolve environment variable references in a string.

    Replaces ${ENV:VAR_NAME} patterns with the value of the corresponding
    environment variable. If the environment variable doesn't exist, it's
    replaced with an empty string.

    Args:
        value: The string to process, or None.

    Returns:
        The string with environment variables resolved, or None if the input
        was None.

    Examples:
        >>> import os
        >>> os.environ['API_KEY'] = 'secret'
        >>> resolve_env_vars("${ENV:API_KEY}")
        'secret'
        >>> resolve_env_vars("prefix_${ENV:API_KEY}_suffix")
        'prefix_secret_suffix'
        >>> resolve_env_vars("${ENV:NONEXISTENT}")
        ''
        >>> resolve_env_vars(None)
        None
    """
    if value is None:
        return None

    # Pattern to match ${ENV:VAR_NAME}
    pattern = r"\$\{ENV:([^}]*)\}"

    def replace_env_var(match):
        var_name = match.group(1)
        # If var_name is empty or the env var doesn't exist, return empty string
        return os.environ.get(var_name, "")

    return re.sub(pattern, replace_env_var, value)
