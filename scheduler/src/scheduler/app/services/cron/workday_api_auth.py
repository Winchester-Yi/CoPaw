# -*- coding: utf-8 -*-
"""行内日历网关签名；只封装给定的 CMBSM 协议，不实现密码算法。"""

import os
import re
import time
from dataclasses import dataclass

PREFIX = "SCHEDULER_WORKDAY_API_"


class WorkdayAuthError(RuntimeError):
    """仅携带固定安全错误码，不能包含 SDK 原始异常或凭据。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _required(name):
    value = os.environ.get(PREFIX + name, "")
    if not value.strip():
        raise WorkdayAuthError("workday_auth_config_missing")
    return value


def _valid_header(value):
    return value == value.strip() and all(
        32 <= ord(char) <= 126 for char in value
    )


@dataclass(frozen=True, repr=False)
class WorkdayAuthConfig:
    app_id: str
    app_secret: str
    private_key: str
    sign: str

    @classmethod
    def load(cls):
        config = cls(
            *(
                _required(name)
                for name in (
                    "APP_ID",
                    "APP_SECRET",
                    "PRIVATE_KEY",
                    "SIGN",
                )
            ),
        )
        if (
            not _valid_header(config.app_id)
            or not _valid_header(config.sign)
            or re.fullmatch(r"[0-9a-fA-F]{64}", config.private_key) is None
            or any(char in config.app_secret for char in "\r\n")
        ):
            raise WorkdayAuthError("workday_auth_config_invalid")
        return config


def _sign(config, message):
    try:
        from CMBSM.CMBSMFunction import CMBSM2SignWithSM3
        from CMBSM.utils import com
    except Exception:
        # SDK 仅在行内刷新日历时需要，缺少它不能阻止其他接口或缓存读取。
        raise WorkdayAuthError("workday_auth_sdk_unavailable") from None
    try:
        signature = CMBSM2SignWithSM3(
            com.aschex_to_bcdhex(config.private_key),
            message.encode("utf-8"),
        )
        if not isinstance(signature, (bytes, bytearray)) or not signature:
            raise ValueError("invalid_signature")
        return signature.hex()
    except Exception:
        # 不输出 SDK 的错误消息，它可能包含私钥、secret 或完整签名原文。
        raise WorkdayAuthError("workday_auth_signing_failed") from None


def build_workday_headers():
    """每次外部请求重新取 Unix 秒时间戳，保持 demo 字段顺序和 hex 编码。"""
    config = WorkdayAuthConfig.load()
    timestamp = str(int(time.time()))
    message = (
        f"appid={config.app_id}&secret={config.app_secret}"
        f"&sign={config.sign}&timestamp={timestamp}"
    )
    return {
        "Content-Type": "application/json;charset=UTF-8",
        "Connection": "close",
        "appid": config.app_id,
        "apisign": _sign(config, message),
        "verify": "SM3withSM2",
        "timestamp": timestamp,
        "sign": config.sign,
    }
