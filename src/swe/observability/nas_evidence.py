# -*- coding: utf-8 -*-
"""Content digests for legacy NAS evidence references."""

import hashlib
import json


def nas_reference(message):
    try:
        encoded = json.dumps(
            [message["id"], message["role"], message["content"]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (KeyError, TypeError, ValueError):
        return None
    return "nas:" + hashlib.sha256(encoded).hexdigest()
