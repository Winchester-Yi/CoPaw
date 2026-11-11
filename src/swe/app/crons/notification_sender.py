# -*- coding: utf-8 -*-
"""完成通知发送能力；调用方提供定义，不读取任务文件或操作调度平台。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx

from .models import CronJobSpec

logger = logging.getLogger(__name__)


class CronNotificationSender:
    def __init__(self, channel_manager, *, agent_id=None, tenant_id=None):
        self._channel_manager = channel_manager
        self._agent_id = agent_id
        self._tenant_id = tenant_id

    @classmethod
    def from_workspace(cls, workspace):
        return cls(
            workspace.channel_manager,
            agent_id=workspace.agent_id,
            tenant_id=workspace.tenant_id,
        )

    async def send_task_success_notification(
        self,
        job_id: str,
        *,
        job: CronJobSpec,
    ) -> None:
        if job.id != job_id:
            raise ValueError("cron_notification_snapshot_conflict")
        await self._push_task_success_notification(job, raise_on_error=True)

    def _build_wplus_link(self, session_id: str) -> str:
        """Build W+ deep link for cron task completion notification.

        生成格式：CMBMobileOA:///?pcSysId=xxx&pcWebConfig=xxx&pcParams=xxx
        用于在 PC 端招乎上跳转 W+ 并自动登录。
        """
        from ...config.utils import load_config

        config = load_config()
        zhaohu_config = config.channels.zhaohu

        # 获取配置
        menu_id = zhaohu_config.cron_task_menu_id or ""
        error_page = zhaohu_config.cron_task_error_page or ""
        sys_id = zhaohu_config.cron_task_sys_id or ""

        # 构建参数
        param = {
            "errorPage": error_page,
            "to": menu_id,
            "type": "toMenu",
            "queryParam": {
                "sessionId": session_id,
                "origin": "Y",
            },
        }

        # 参数格式化: encodeURIComponent(btoa(JSON.stringify(param)))
        pc_params = base64.b64encode(
            json.dumps(param, ensure_ascii=False).encode("utf-8"),
        ).decode("utf-8")
        pc_params = self._url_encode(pc_params)

        # 再封装一层: encodeURIComponent(btoa('pcParams='+pc_params))
        pc_params_wrapper = base64.b64encode(
            f"pcParams={pc_params}".encode("utf-8"),
        ).decode("utf-8")
        pc_params_wrapper = self._url_encode(pc_params_wrapper)

        pc_web_config = "eyJuYW1lIjoi6LSi5a%2BMVysiLCJ5c3RBdXRoIjoidHJ1ZSJ9"

        # 拼接地址
        wplus_link = (
            f"CMBMobileOA:///?pcSysId={sys_id}"
            f"&pcWebConfig={pc_web_config}"
            f"&pcParams={pc_params_wrapper}"
        )
        return wplus_link

    def _url_encode(self, text: str) -> str:
        """URL encode text."""
        import urllib.parse

        return urllib.parse.quote(text, safe="")

    def _get_zhaohu_push_config(self) -> Any:
        """Read the zhaohu channel config for session-end push links.

        Prefer the tenant agent config (where the prefix is distributed via
        console / inherited from template); fall back to global config.json
        so the call never fails.
        """
        from ...config.utils import load_config

        try:
            from ...config.config import load_agent_config

            agent_cfg = load_agent_config(
                self._agent_id or "default",
                tenant_id=self._tenant_id,
            )
            channels = getattr(agent_cfg, "channels", None)
            if channels is not None:
                zhaohu = getattr(channels, "zhaohu", None)
                if zhaohu is not None:
                    return zhaohu
        except Exception:
            logger.info(
                "zhaohu push config from agent failed, fallback to global",
            )
        return load_config().channels.zhaohu

    def _build_session_end_push_link(self, job: CronJobSpec) -> str:
        """非 RMASSIST 来源的定时任务通知跳转链接。

        链接前缀与 ID 类型来自 zhaohu 渠道配置
        （session_end_push_link_prefix / session_end_push_link_id_type）：
        - id_type=chat_id    -> {prefix}?chatId={job.meta.task_chat_id}
        - id_type=session_id -> {prefix}?sessionId={job.meta.task_session_id}
        前缀为空或对应 ID 值为空时返回空串（不附加链接）。
        """
        zhaohu_cfg = self._get_zhaohu_push_config()
        prefix = getattr(zhaohu_cfg, "session_end_push_link_prefix", "") or ""
        if not prefix:
            return ""
        sep = "&" if "?" in prefix else "?"
        id_type = (
            getattr(zhaohu_cfg, "session_end_push_link_id_type", "")
            or "session_id"
        )
        if id_type == "chat_id":
            chat_id = str(
                (getattr(job, "meta", None) or {}).get("task_chat_id", ""),
            )
            return f"{prefix}{sep}chatId={chat_id}" if chat_id else ""
        session_id = (getattr(job, "meta", None) or {}).get(
            "task_session_id",
            "",
        ) or ""
        return f"{prefix}{sep}sessionId={session_id}" if session_id else ""

    def _build_wplus_pc_link(self, session_id: str) -> str:
        """Build W+ PC menu HTTPS link for notification jump.

        格式: {domain}/gate/wpluspcmenuui/pcmenu?isSafeUrl=Y&sysid=retailbanking&pcParams=xxx
        用于在 W+ 消息推送中跳转到小助 claw 版指定会话。
        """
        from ...constant import CRON_WPLUS_PC_MENU_DOMAIN

        domain = CRON_WPLUS_PC_MENU_DOMAIN.rstrip("/")
        if not domain:
            return ""

        param = {
            "type": "toMenu",
            "to": "cmbclaw",
            "queryParam": {
                "sessionId": session_id,
                "origin": "Y",
            },
        }
        pc_params = base64.b64encode(
            json.dumps(param, ensure_ascii=False).encode("utf-8"),
        ).decode("utf-8")
        pc_params = self._url_encode(pc_params)

        return (
            f"{domain}/gate/wpluspcmenuui/pcmenu"
            f"?isSafeUrl=Y&sysid=retailbanking&pcParams={pc_params}"
        )

    async def _push_wplus_notification(
        self,
        job: CronJobSpec,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Push W+ message notification when a cron task completes."""

        from ...constant import (
            CRON_WPLUS_MSG_APP_ID,
            CRON_WPLUS_MSG_HEADER_KEY,
            CRON_WPLUS_MSG_HEADER_VALUE,
            CRON_WPLUS_MSG_NOTICE_ID,
            CRON_WPLUS_MSG_URL,
        )

        msg_url = CRON_WPLUS_MSG_URL
        notice_id = CRON_WPLUS_MSG_NOTICE_ID
        app_id = CRON_WPLUS_MSG_APP_ID
        header_key = CRON_WPLUS_MSG_HEADER_KEY
        header_value = CRON_WPLUS_MSG_HEADER_VALUE
        if (
            not msg_url
            or not notice_id
            or not app_id
            or not header_key
            or not header_value
        ):
            logger.debug(
                "Skip W+ push: config incomplete "
                "(msg_url=%s, notice_id=%s, app_id=%s, header_key=%s, header_value=%s)",
                bool(msg_url),
                bool(notice_id),
                bool(app_id),
                bool(header_key),
                bool(header_value),
            )
            return

        session_id = job.meta.get("task_session_id")
        creator_id = job.meta.get("creator_user_id")
        if not creator_id:
            logger.info("Skip W+ push: job %s has no creator_user_id", job.id)
            return

        now = datetime.now()
        wplus_pc_link = (
            self._build_wplus_pc_link(session_id) if session_id else ""
        )

        payload = {
            "appId": app_id,
            "msgUrl": wplus_pc_link,
            "msgText": (
                f"你发起的定时任务【{job.name}】已完成，"
                f"请进入小助claw版查看，完成时间:"
                f"{now.strftime('%Y-%m-%d %H:%M:%S')}"
            ),
            "msgTime": now.strftime("%Y-%m-%d %H:%M:%S.")
            + f"{now.microsecond // 1000:03d}",
            "msgStyle": "TEXT",
            "msgTitle": f"{job.name}完成啦~",
            "noticeId": notice_id,
            "msgExpand": "",
            "msgSubTitle": "",
            "pushChannel": "HELP_CAT",
            "targetSapId": str(creator_id),
            "externalMsgId": str(uuid4()),
            "msgPictureUrl": "",
            "targetSapIdList": [],
        }

        headers = {
            header_key: header_value,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    msg_url,
                    json=payload,
                    headers=headers,
                )
                result = resp.json()
                if result.get("code") != "success":
                    logger.warning(
                        "W+ push returned non-success: job_id=%s result=%s",
                        job.id,
                        result,
                    )
                else:
                    logger.info(
                        "W+ push sent: job_id=%s job_name=%s target=%s",
                        job.id,
                        job.name,
                        creator_id,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "W+ push cancelled: job_id=%s job_name=%s",
                job.id,
                job.name,
            )
            raise
        except Exception as exc:
            logger.warning(
                "W+ push failed: job_id=%s job_name=%s error=%s",
                job.id,
                job.name,
                repr(exc),
            )
            if raise_on_error:
                raise

    async def _push_task_success_notification(
        self,
        job: CronJobSpec,
        *,
        raise_on_error: bool = False,
    ) -> None:
        """Push success notification when an agent task completes."""
        # 只对 agent 类型的任务发送通知
        if job.task_type not in {"agent", "workflow"}:
            logger.debug(
                "Skip notification: job %s has no completion push",
                job.id,
            )
            return

        task_session_id = job.meta.get("task_session_id")
        if not task_session_id:
            logger.info("Skip notification: job %s has no session_id", job.id)
            return
        creator_id = job.meta.get("creator_user_id")
        logger.info(
            "Sending cron task completion notification: "
            "job_id=%s job_name=%s session_id=%s",
            job.id,
            job.name,
            task_session_id,
        )

        # 构建 meta，包含 link 和 summary
        meta = dict(job.dispatch.meta or {})

        # RMASSIST 使用 W+ 深链；其他来源从 zhaohu 配置生成跳转链接
        is_rmassist = job.source_id == "RMASSIST"
        if is_rmassist:
            wplus_link = self._build_wplus_link(task_session_id)
            logger.debug("Generated W+ link: %s", wplus_link)
            meta["link_url"] = wplus_link
            meta["link_text"] = "点击跳转小助claw版查看"
        else:
            link_url = self._build_session_end_push_link(job)
            if link_url:
                meta["link_url"] = link_url
                meta["link_text"] = "点击查看详情"
        meta["notification_summary"] = "小助claw定时任务完成提醒"

        await self.push_message(
            creator_id,
            job,
            task_session_id,
            meta,
            raise_on_error=raise_on_error,
        )

        # W+ 消息推送: 仅 RMASSIST 来源且 zhaohu 渠道启用时触发
        if is_rmassist and self._channel_manager is not None:
            zhaohu_ch = await self._channel_manager.get_channel("zhaohu")
            if zhaohu_ch is not None:
                await self._push_wplus_notification(
                    job,
                    raise_on_error=raise_on_error,
                )

    async def push_message(
        self,
        creator_id: Any | None,
        job: CronJobSpec,
        session_id: Any | None,
        meta: Optional[Dict[str, Any]] | None,
        *,
        raise_on_error: bool = False,
    ):
        # 固定使用 zhaohu 通道发送通知
        # 用 try-except 包裹，避免任务被取消时通知发送失败影响主流程
        try:
            await self._channel_manager.send_text(
                channel="zhaohu",
                user_id=creator_id,
                session_id=session_id,
                text=f"叮咚，你发起的定时任务【{job.name}】已完成，快来查收结果~",
                meta=meta,
            )
            logger.info(
                "Cron task completion notification sent: "
                "job_id=%s job_name=%s",
                job.id,
                job.name,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Cron task notification cancelled: job_id=%s job_name=%s",
                job.id,
                job.name,
            )
            raise
        except Exception as exc:
            logger.warning(
                "Failed to send cron task notification: "
                "job_id=%s job_name=%s error=%s",
                job.id,
                job.name,
                repr(exc),
            )
            if raise_on_error:
                raise
