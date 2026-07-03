from __future__ import annotations

import json
import time
from typing import Any

import httpx

from core.sdk import AutomationProvider, BasePlugin, OperationResult

DEFAULT_EVENTS = ["task.completed", "task.failed"]
DEFAULT_API_URL = "http://www.pushplus.plus/send"


class WechatPushPlusAutomationPlugin(BasePlugin, AutomationProvider):
    plugin_id = "automation.wechat_pushplus"
    plugin_name = "PushPlus 微信消息通知"
    plugin_version = "0.1.0"

    def __init__(self) -> None:
        self._runtime_config: dict[str, Any] = {}

    # ==================== 生命周期 ====================

    def set_runtime_config(self, config: dict[str, Any]) -> None:
        self._runtime_config = self._normalize_runtime_config(config)

    def validate_runtime_config(self, config: dict[str, Any]) -> OperationResult:
        normalized = self._normalize_runtime_config(config)
        errors: list[str] = []
        if not str(normalized.get("token") or "").strip():
            errors.append("缺少必填配置：token")
        if errors:
            return OperationResult(
                success=False, message="插件配置校验失败。", errors=errors
            )
        return OperationResult(success=True, message="插件配置校验通过。", data=normalized)

    def health(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "message": "PushPlus 微信消息通知插件运行正常。",
            "details": {
                "configured": self._is_configured(),
                "subscribed_events": self.subscribed_events(),
            },
        }

    # ==================== AutomationProvider 协议 ====================

    def subscribed_events(self) -> list[str]:
        raw = str(self._runtime_config.get("enabled_events") or ",".join(DEFAULT_EVENTS))
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or list(DEFAULT_EVENTS)

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("event_type") or "unknown")
        title, content = self._build_message(event)

        result = self._send_pushplus(title, content)

        return OperationResult(
            success=result.success,
            message=f"{self.plugin_name} 已处理事件：{event_type}",
            data={
                "event_type": event_type,
                "title": title,
                "content": content,
                "sent": result.success,
                "api_response": result.data if result.success else None,
                "error": result.message if not result.success else None,
            },
        ).model_dump(mode="json")

    # ==================== 内部方法 ====================

    def _is_configured(self) -> bool:
        return bool(str(self._runtime_config.get("token") or "").strip())

    @staticmethod
    def _normalize_runtime_config(config: dict[str, Any] | None) -> dict[str, Any]:
        return dict(config or {})

    @staticmethod
    def _build_message(event: dict[str, Any]) -> tuple[str, str]:
        """构建推送标题和内容。"""
        event_type = str(event.get("event_type") or "unknown")
        payload = dict(event.get("payload") or {})
        title_prefix = str(
            next(
                (
                    k
                    for k in (
                        "title_prefix",
                        "settings",
                        {},
                    )
                    for k in (dict(k).get("title_prefix") if isinstance(k, dict) else k)
                ),
                "",
            )
            or "[T3FAP]"
        ).strip() or "[T3FAP]"

        task_name = str(
            payload.get("task_name")
            or payload.get("title")
            or event.get("task_id")
            or "未命名任务"
        )
        summary = str(payload.get("summary") or "").strip()
        error_message = str(
            payload.get("error_message")
            or payload.get("error")
            or summary
            or "未知错误"
        ).strip()

        if event_type == "task.completed":
            content = f"{task_name} 已执行完成。"
            if summary:
                content += f"\n\n{summary}"
            return f"{title_prefix} [任务完成]", content

        if event_type == "task.failed":
            return (
                f"{title_prefix} [任务失败]",
                f"{task_name} 执行失败：{error_message}",
            )

        if summary:
            return f"{title_prefix} [通知]", f"{task_name}：{summary}"
        return f"{title_prefix} [通知]", f"{task_name} 触发事件：{event_type}"

    def _send_pushplus(self, title: str, content: str) -> OperationResult:
        """向 PushPlus 发送微信消息。"""
        token = str(self._runtime_config.get("token") or "").strip()
        if not token:
            return OperationResult(success=False, message="缺少 token 配置。")

        template = str(
            self._runtime_config.get("template") or "html"
        )  # html / markdown / txt
        timeout = int(self._runtime_config.get("timeout_seconds") or 10)

        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": template,
        }

        try:
            resp = httpx.post(
                DEFAULT_API_URL,
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            return OperationResult(
                success=(body.get("code") == 200),
                message=body.get("msg", "请求成功"),
                data={"push_id": body.get("data")},
            )
        except httpx.HTTPStatusError as exc:
            return OperationResult(
                success=False,
                message=f"HTTP {exc.response.status_code}: {exc.response.text}",
            )
        except Exception as exc:
            return OperationResult(success=False, message=str(exc))


plugin = WechatPushPlusAutomationPlugin()
