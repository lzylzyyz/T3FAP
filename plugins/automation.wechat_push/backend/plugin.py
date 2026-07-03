from __future__ import annotations

import json
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.sdk import AutomationProvider, BasePlugin, OperationResult

DEFAULT_EVENTS = ["task.completed", "task.failed"]
DEFAULT_PUSH_URL = "https://wechat.powerautomate.top/api/push"
DEFAULT_TITLE_PREFIX = "[T3FAP]"
DEFAULT_TIMEOUT = 10
DEFAULT_RETRY_COUNT = 2


class WeChatPushAutomationPlugin(BasePlugin, AutomationProvider):
    """微信推送通知自动化插件。

    监听 T3FAP 平台任务和系统事件，将通知消息通过
    wechat.powerautomate.top 推送服务实时发送到微信。
    """

    plugin_id = "automation.wechat_push"
    plugin_name = "微信推送通知自动化"
    plugin_version = "1.1.0"

    # 测试通知事件始终处理，不受订阅列表限制
    TEST_EVENT_TYPE = "notification.test"

    def __init__(self) -> None:
        self._runtime_config: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 生命周期与配置
    # ------------------------------------------------------------------

    def set_runtime_config(self, config: dict[str, Any]) -> None:
        self._runtime_config = self._normalize_runtime_config(config)

    def validate_runtime_config(self, config: dict[str, Any]) -> OperationResult:
        normalized = self._normalize_runtime_config(config)
        errors: list[str] = []
        if not str(normalized.get("uid") or "").strip():
            errors.append("缺少必填配置：uid（微信推送 UID）")
        push_url = str(normalized.get("push_url") or "").strip()
        if push_url and not push_url.startswith(("http://", "https://")):
            errors.append("push_url 必须以 http:// 或 https:// 开头")
        timeout = normalized.get("timeout_seconds")
        if timeout is not None:
            try:
                timeout_val = int(timeout)
                if timeout_val <= 0:
                    errors.append("timeout_seconds 必须大于 0")
            except (ValueError, TypeError):
                errors.append("timeout_seconds 必须为整数")
        retry = normalized.get("retry_count")
        if retry is not None:
            try:
                retry_val = int(retry)
                if retry_val < 0:
                    errors.append("retry_count 不能为负数")
            except (ValueError, TypeError):
                errors.append("retry_count 必须为整数")
        if errors:
            return OperationResult(success=False, message="插件配置校验失败。", errors=errors)
        return OperationResult(success=True, message="插件配置校验通过。", data=normalized)

    def health(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "message": "微信推送通知插件运行正常。",
            "details": {
                "configured": self._is_configured(),
                "push_url": self._get_push_url(),
                "subscribed_events": self.subscribed_events(),
            },
        }

    # ------------------------------------------------------------------
    # AutomationProvider 协议
    # ------------------------------------------------------------------

    def subscribed_events(self) -> list[str]:
        raw = str(
            self._runtime_config.get("enabled_events") or ",".join(DEFAULT_EVENTS)
        )
        values = [item.strip() for item in raw.split(",") if item.strip()]
        return values or list(DEFAULT_EVENTS)

    # ------------------------------------------------------------------
    # 通知测试
    # ------------------------------------------------------------------

    def test_notification(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送一条测试通知，用于验证推送配置是否正确。

        可传入临时 config 进行测试（不影响运行时配置），
        也可不传参数直接使用当前运行时配置。
        返回 OperationResult 的 dict 形式。
        """
        if config is not None:
            prev_config = self._runtime_config
            self._runtime_config = self._normalize_runtime_config(config)
        else:
            prev_config = None

        try:
            if not self._is_configured():
                return OperationResult(
                    success=False,
                    message=f"{self.plugin_name} 测试失败：缺少必填配置 uid。",
                    errors=["缺少必填配置：uid"],
                    data={"configured": False, "test": True},
                ).model_dump(mode="json")

            title = f"{self._get_title_prefix()} 测试通知"
            content = (
                "这是一条来自 T3FAP 微信推送插件的测试消息。\n"
                "如果您在微信中收到了这条消息，说明推送配置已正确生效。\n\n"
                f"推送地址：{self._get_push_url()}\n"
                f"UID：{self._get_uid()[:8]}{'*' * (len(self._get_uid()) - 8) if len(self._get_uid()) > 8 else '***'}\n"
                f"订阅事件：{', '.join(self.subscribed_events())}\n"
                f"超时时间：{self._get_timeout()} 秒\n"
                f"重试次数：{self._get_retry_count()} 次"
            )

            push_result = self._send_push(title, content)

            return OperationResult(
                success=push_result["success"],
                message=(
                    f"{self.plugin_name} 测试通知发送成功。"
                    if push_result["success"]
                    else f"{self.plugin_name} 测试通知发送失败：{push_result.get('error', '未知错误')}"
                ),
                errors=[] if push_result["success"] else [push_result.get("error", "未知错误")],
                data={
                    "test": True,
                    "configured": True,
                    "title": title,
                    "content": content,
                    "push_result": push_result,
                },
            ).model_dump(mode="json")
        finally:
            if config is not None:
                self._runtime_config = prev_config

    # ------------------------------------------------------------------
    # AutomationProvider 协议
    # ------------------------------------------------------------------

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("event_type") or "unknown")

        if not self._is_configured():
            return OperationResult(
                success=False,
                message=f"{self.plugin_name} 未正确配置 uid，跳过事件：{event_type}",
                errors=["缺少必填配置：uid"],
                data={"event_type": event_type, "configured": False},
            ).model_dump(mode="json")

        # 测试通知事件始终处理，不受订阅列表限制
        if event_type == self.TEST_EVENT_TYPE:
            return self.test_notification()

        title, content = self._build_message(event)

        if not self._should_handle(event_type):
            return OperationResult(
                success=True,
                message=f"{self.plugin_name} 事件 {event_type} 不在订阅列表中，已跳过。",
                data={
                    "event_type": event_type,
                    "skipped": True,
                    "title": title,
                    "content": content,
                },
            ).model_dump(mode="json")

        push_result = self._send_push(title, content)

        return OperationResult(
            success=push_result["success"],
            message=(
                f"{self.plugin_name} 已处理事件：{event_type}"
                if push_result["success"]
                else f"{self.plugin_name} 推送失败：{push_result.get('error', '未知错误')}"
            ),
            errors=[] if push_result["success"] else [push_result.get("error", "未知错误")],
            data={
                "event_type": event_type,
                "title": title,
                "content": content,
                "configured": True,
                "push_result": push_result,
            },
        ).model_dump(mode="json")

    # ------------------------------------------------------------------
    # 内部方法 — 配置读取
    # ------------------------------------------------------------------

    def _is_configured(self) -> bool:
        return bool(str(self._runtime_config.get("uid") or "").strip())

    def _get_push_url(self) -> str:
        return str(
            self._runtime_config.get("push_url") or DEFAULT_PUSH_URL
        ).strip()

    def _get_uid(self) -> str:
        return str(self._runtime_config.get("uid") or "").strip()

    def _get_title_prefix(self) -> str:
        return str(
            self._runtime_config.get("title_prefix") or DEFAULT_TITLE_PREFIX
        ).strip()

    def _get_timeout(self) -> int:
        try:
            return int(self._runtime_config.get("timeout_seconds") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            return DEFAULT_TIMEOUT

    def _get_retry_count(self) -> int:
        try:
            return int(self._runtime_config.get("retry_count") or DEFAULT_RETRY_COUNT)
        except (ValueError, TypeError):
            return DEFAULT_RETRY_COUNT

    def _should_handle(self, event_type: str) -> bool:
        return event_type in self.subscribed_events()

    @staticmethod
    def _normalize_runtime_config(config: dict[str, Any] | None) -> dict[str, Any]:
        return dict(config or {})

    # ------------------------------------------------------------------
    # 内部方法 — 消息构建
    # ------------------------------------------------------------------

    def _build_message(self, event: dict[str, Any]) -> tuple[str, str]:
        """从事件数据构建微信推送的标题和正文。

        根据事件类型生成不同格式的通知内容，
        支持任务完成、任务失败、任务开始、资源发现、系统告警等。
        """
        event_type = str(event.get("event_type") or "unknown")
        payload = dict(event.get("payload") or {})
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
        include_detail = self._runtime_config.get("include_detail", True)
        prefix = self._get_title_prefix()

        if event_type == "task.completed":
            title = f"{prefix} 任务完成"
            content = f"任务「{task_name}」已执行完成。"
            if include_detail:
                content = self._append_detail(content, payload, summary)
            return title, content

        if event_type == "task.failed":
            title = f"{prefix} 任务失败"
            content = f"任务「{task_name}」执行失败：\n{error_message}"
            if include_detail:
                content = self._append_detail(content, payload, summary)
            return title, content

        if event_type == "task.started":
            title = f"{prefix} 任务开始"
            content = f"任务「{task_name}」已开始执行。"
            if include_detail:
                content = self._append_detail(content, payload, summary)
            return title, content

        if event_type == "resource.found":
            resource_title = str(payload.get("resource_title") or task_name)
            source = str(payload.get("source") or "未知来源")
            title = f"{prefix} 发现资源"
            content = f"发现新资源：「{resource_title}」\n来源：{source}"
            if include_detail and summary:
                content += f"\n{summary}"
            return title, content

        if event_type == "system.warning":
            title = f"{prefix} 系统告警"
            content = f"系统告警：{summary or error_message}"
            if include_detail:
                content = self._append_detail(content, payload, summary)
            return title, content

        # 兜底：通用事件通知
        title = f"{prefix} 系统通知"
        if summary:
            content = f"{task_name}：{summary}"
        else:
            content = f"{task_name} 触发事件：{event_type}"
        if include_detail:
            content = self._append_detail(content, payload, summary)
        return title, content

    @staticmethod
    def _append_detail(
        content: str, payload: dict[str, Any], summary: str
    ) -> str:
        """在消息正文末尾追加事件的详细信息。"""
        detail_lines: list[str] = []
        task_id = str(payload.get("task_id") or "").strip()
        if task_id:
            detail_lines.append(f"任务ID：{task_id}")
        resource_title = str(payload.get("resource_title") or "").strip()
        if resource_title:
            detail_lines.append(f"资源：{resource_title}")
        source = str(payload.get("source") or "").strip()
        if source:
            detail_lines.append(f"来源：{source}")
        if summary and summary not in content:
            detail_lines.append(f"摘要：{summary}")
        extra = str(payload.get("detail") or "").strip()
        if extra:
            detail_lines.append(f"详情：{extra}")
        if detail_lines:
            content += "\n\n" + "\n".join(detail_lines)
        return content

    # ------------------------------------------------------------------
    # 内部方法 — HTTP 推送
    # ------------------------------------------------------------------

    def _send_push(self, title: str, content: str) -> dict[str, Any]:
        """向微信推送服务发送 POST 请求。

        使用 urllib 标准库发送 JSON 请求体，
        支持超时控制和失败重试。
        """
        uid = self._get_uid()
        push_url = self._get_push_url()
        timeout = self._get_timeout()
        retry_count = self._get_retry_count()

        body = json.dumps(
            {"uid": uid, "title": title, "content": content},
            ensure_ascii=False,
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "T3FAP-WeChatPush/1.0",
        }

        last_error = ""
        for attempt in range(retry_count + 1):
            try:
                req = urllib_request.Request(
                    url=push_url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=timeout) as resp:
                    status_code = resp.getcode()
                    resp_body = resp.read().decode("utf-8", errors="replace")

                if 200 <= status_code < 300:
                    return {
                        "success": True,
                        "status_code": status_code,
                        "response": resp_body,
                        "attempts": attempt + 1,
                    }

                last_error = (
                    f"HTTP {status_code}: {resp_body[:200]}"
                )

            except urllib_error.HTTPError as e:
                resp_body = ""
                try:
                    resp_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                last_error = f"HTTP {e.code}: {resp_body[:200] or e.reason}"

            except urllib_error.URLError as e:
                last_error = f"网络错误：{e.reason}"

            except Exception as e:
                last_error = f"未知错误：{e}"

            if attempt < retry_count:
                time.sleep(1.0 * (attempt + 1))

        return {
            "success": False,
            "status_code": None,
            "response": "",
            "attempts": retry_count + 1,
            "error": last_error,
        }


plugin = WeChatPushAutomationPlugin()
