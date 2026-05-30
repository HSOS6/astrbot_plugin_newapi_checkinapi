import asyncio
import json
import random
import re
import smtplib
import ssl
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

BEIJING_TZ = timezone(timedelta(hours=8))


class NewAPIError(RuntimeError):
    """NewAPI 调用错误。"""


class NewAPIClient:
    """基于 NewAPI 管理接口的异步客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        admin_user_id: str,
        timeout: int = 30,
        user_agent: str = "AstrBot-NewAPI-Checkin/1.0",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.admin_user_id = str(admin_user_id).strip()
        self.timeout = timeout
        self.user_agent = user_agent

    def _headers(self, include_json: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "New-Api-User": self.admin_user_id,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if include_json:
            headers["Content-Type"] = "application/json"
        return headers

    async def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.base_url or not self.api_key or not self.admin_user_id:
            raise NewAPIError("NewAPI 地址、密钥或管理员用户 ID 未配置")

        url = f"{self.base_url}{path}"
        logger.debug(f"[NewAPIClient] {method} {url}")
        try:
            timeout = aiohttp.ClientTimeout(
                total=self.timeout,
                connect=self.timeout,
                sock_read=self.timeout,
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    url,
                    headers=self._headers(include_json=payload is not None),
                    json=payload,
                ) as response:
                    raw_text = await response.text()
                    if response.status >= 400:
                        raise NewAPIError(f"HTTP {response.status}: {raw_text[:200]}")
        except asyncio.TimeoutError as exc:
            raise NewAPIError(
                f"请求超时({self.timeout}秒)，请检查 API 地址和网络连接: {self.base_url}"
            ) from exc
        except Exception as exc:
            raise NewAPIError(f"接口请求失败: {exc}") from exc

        try:
            result = json.loads(raw_text) if raw_text else {}
        except json.JSONDecodeError as exc:
            raise NewAPIError("接口返回了无法解析的 JSON 数据") from exc

        if not isinstance(result, dict):
            return {"success": True, "data": result}

        if result.get("success") is False:
            raise NewAPIError(str(result.get("message") or "接口返回失败"))

        return result

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        result = await self.request("GET", f"/api/user/{user_id}")
        data = result.get("data")
        if not isinstance(data, dict):
            raise NewAPIError("获取用户信息失败：返回数据格式异常")
        return data

    async def search_user(self, keyword: str) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"keyword": keyword, "p": 1, "page_size": 10})
        result = await self.request("GET", f"/api/user/search?{query}")
        data = result.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise NewAPIError("搜索用户失败：返回数据格式异常")

        normalized = keyword.strip().lower()
        exact_matches = [
            item
            for item in items
            if str(item.get("email", "")).lower() == normalized
            or str(item.get("username", "")).lower() == normalized
            or str(item.get("display_name", "")).lower() == normalized
        ]
        if exact_matches:
            return exact_matches[0]
        if len(items) == 1:
            return items[0]
        if not items:
            raise NewAPIError("未找到匹配的 NewAPI 用户")
        raise NewAPIError("搜索到多个用户，请改用数字 ID 绑定")

    async def increase_user_quota(self, user_id: int, value: int) -> None:
        if value <= 0:
            raise NewAPIError("增加额度必须大于 0")
        payload: Dict[str, Any] = {
            "id": user_id,
            "action": "add_quota",
            "value": value,
            "mode": "add",
        }
        await self.request("POST", "/api/user/manage", payload=payload)

    async def decrease_user_quota(self, user_id: int, value: int) -> None:
        if value <= 0:
            raise NewAPIError("扣除额度必须大于 0")
        payload: Dict[str, Any] = {
            "id": user_id,
            "action": "add_quota",
            "value": value,
            "mode": "subtract",
        }
        await self.request("POST", "/api/user/manage", payload=payload)


@register(
    "astrbot_plugin_newapi_checkinapi",
    "星见雅",
    "NewAPI 邮箱验证码绑定与每日签到额度发放插件",
    "v11.4.5",
)
class NewAPICheckinProPlugin(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config

        self.api_base_url = str(config.get("api_base_url", "")).rstrip("/")
        self.api_key = str(config.get("api_key", ""))
        self.api_display_name = str(config.get("api_display_name", "NewAPI"))
        self.admin_user_id = str(config.get("admin_user_id", "1"))
        self.api_timeout_seconds = int(config.get("api_timeout_seconds", 30))

        self.checkin_quota = int(config.get("checkin_quota", 500000))
        self.target_quota = int(config.get("target_quota", 0) or 0)
        self.penalty_quota = int(config.get("penalty_quota", 0) or 0)
        self.checkin_quota_min = int(config.get("checkin_quota_min", 0) or 0)
        self.checkin_quota_max = int(config.get("checkin_quota_max", 0) or 0)
        self._use_random_quota = (
            self.checkin_quota_max > 0
            and self.checkin_quota_max >= self.checkin_quota_min
        )
        self.enable_daily_limit = bool(config.get("enable_daily_limit", True))
        self.reset_hour = int(config.get("reset_hour", 0))
        self.require_email_verification = bool(config.get("require_email_verification", True))
        self.verification_expire_seconds = int(config.get("verification_expire_seconds", 300))
        self.auto_confirm_qq_email = bool(config.get("auto_confirm_qq_email", False))

        self.quota_to_money_rate = int(config.get("quota_to_money_rate", 500000))
        self.quota_symbol = str(config.get("quota_symbol", "$"))
        self.quota_symbol_position = str(config.get("quota_symbol_position", "before"))

        self.unbind_cooldown_hours = int(config.get("unbind_cooldown_hours", 72))

        self.smtp_host = str(config.get("smtp_host", "smtp.qq.com"))
        self.smtp_port = int(config.get("smtp_port", 465))
        self.smtp_username = str(config.get("smtp_username", ""))
        self.smtp_password = str(config.get("smtp_password", ""))
        self.smtp_use_ssl = bool(config.get("smtp_use_ssl", True))
        self.from_address = str(config.get("from_address", ""))
        self.from_display_name = str(config.get("from_display_name", f"AstrBot {self.api_display_name} 签到助手"))
        self.verify_email_subject = str(config.get("verify_email_subject", f"{self.api_display_name} 账号绑定验证码"))
        self.verify_email_template = str(
            config.get(
                "verify_email_template",
                (
                    f"<p>您好，您正在通过 AstrBot 绑定 {self.api_display_name} 账号。</p>"
                    f"<p>{self.api_display_name} 用户：<b>{{username}}</b>（ID: {{user_id}}）</p>"
                    "<p>验证码：<b style=\"font-size: 24px;\">{code}</b></p>"
                    "<p>验证码 {timeout_minutes} 分钟内有效。如非本人操作，请忽略此邮件。</p>"
                ),
            )
        )

        self.client = NewAPIClient(
            base_url=self.api_base_url,
            api_key=self.api_key,
            admin_user_id=self.admin_user_id,
            timeout=self.api_timeout_seconds,
        )

        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_newapipro"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.state = self._load_state()
        self.pending_bindings: Dict[str, Dict[str, Any]] = {}
        self.processing_users = set()

        logger.info("[NewAPIPro] 插件已加载")

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("bindings", {})
                    data.setdefault("checkins", {})
                    data.setdefault("unbinds", {})
                    return data
            except Exception as exc:
                logger.error(f"[NewAPIPro] 读取状态文件失败: {exc}")
        return {"bindings": {}, "checkins": {}, "unbinds": {}}

    def _save_state(self) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"[NewAPIPro] 保存状态文件失败: {exc}")

    def _is_mail_configured(self) -> bool:
        return all(
            [
                self.smtp_host,
                self.smtp_port,
                self.smtp_username,
                self.smtp_password,
                self.from_address,
            ]
        )

    def _generate_code(self) -> str:
        return str(random.randint(100000, 999999))

    def _mask_email(self, email: str) -> str:
        if "@" not in email:
            return email
        name, domain = email.split("@", 1)
        if len(name) <= 2:
            masked_name = name[:1] + "*"
        else:
            masked_name = name[:2] + "*" * max(2, len(name) - 3) + name[-1:]
        return f"{masked_name}@{domain}"

    def _format_quota(self, quota: int) -> str:
        amount = quota / self.quota_to_money_rate
        formatted = f"{amount:.2f}"
        if self.quota_symbol_position == "after":
            return f"{formatted}{self.quota_symbol}"
        return f"{self.quota_symbol}{formatted}"

    def _get_cycle_start(self) -> datetime:
        now = datetime.now(BEIJING_TZ)
        reset_hour = max(0, min(23, self.reset_hour))
        cycle = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        return cycle if now >= cycle else cycle - timedelta(days=1)

    def _can_checkin(self, qq_id: str) -> bool:
        if not self.enable_daily_limit:
            return True
        last_checkin = int(self.state.get("checkins", {}).get(qq_id, 0) or 0)
        if not last_checkin:
            return True
        last_time = datetime.fromtimestamp(last_checkin, BEIJING_TZ)
        return last_time < self._get_cycle_start()

    def _next_reset_text(self) -> str:
        return (self._get_cycle_start() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    def _get_binding(self, qq_id: str) -> Optional[Dict[str, Any]]:
        binding = self.state.get("bindings", {}).get(qq_id)
        return binding if isinstance(binding, dict) else None

    def _find_binding_by_user_id(self, newapi_user_id: int) -> Optional[str]:
        for qq_id, binding in self.state.get("bindings", {}).items():
            if int(binding.get("newapi_user_id", 0) or 0) == int(newapi_user_id):
                return str(qq_id)
        return None

    def _extract_bind_account(self, message_str: str) -> str:
        match = re.match(
            r"(?:绑定|newapi绑定|NewAPI绑定)\s+(.+)",
            message_str.strip(),
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    def _save_binding(self, qq_id: str, user: Dict[str, Any]) -> None:
        self.state.setdefault("bindings", {})[qq_id] = {
            "newapi_user_id": int(user["id"]),
            "username": str(user.get("username", "")),
            "display_name": str(user.get("display_name", "")),
            "email": str(user.get("email", "")),
            "bind_time": int(time.time()),
            "last_checkin": 0,
        }
        self._save_state()

    def _remove_expired_pending(self) -> None:
        now = time.time()
        expired = [
            qq_id
            for qq_id, pending in self.pending_bindings.items()
            if float(pending.get("expires_at", 0)) <= now
        ]
        for qq_id in expired:
            self.pending_bindings.pop(qq_id, None)

    async def _send_email_async(self, to_email: str, subject: str, html_body: str) -> None:
        await asyncio.to_thread(self._send_email_sync, to_email, subject, html_body)

    def _send_email_sync(self, to_email: str, subject: str, html_body: str) -> None:
        if not self._is_mail_configured():
            raise RuntimeError("SMTP 邮件配置不完整")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((self.from_display_name, self.from_address))
        msg["To"] = to_email
        msg.set_content("请使用支持 HTML 的邮件客户端查看验证码。")
        msg.add_alternative(html_body, subtype="html")

        try:
            context = ssl.create_default_context()
            if self.smtp_use_ssl:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context) as server:
                    server.login(self.smtp_username, self.smtp_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.smtp_username, self.smtp_password)
                    server.send_message(msg)
        except Exception as exc:
            logger.error(f"[NewAPIPro] 发送邮件到 {to_email} 失败: {exc}")
            raise

    async def _send_bind_code(self, qq_id: str, user: Dict[str, Any]) -> None:
        email = str(user.get("email", "")).strip()
        if not email:
            raise RuntimeError(f"该 {self.api_display_name} 用户未绑定邮箱，无法发送验证码")

        code = self._generate_code()
        expires_at = time.time() + self.verification_expire_seconds
        self.pending_bindings[qq_id] = {
            "stage": "verify",
            "user": user,
            "code": code,
            "email": email,
            "expires_at": expires_at,
            "attempts": 0,
        }

        timeout_minutes = max(1, self.verification_expire_seconds // 60)
        html_body = self.verify_email_template.format(
            code=code,
            user_id=user.get("id", ""),
            username=user.get("username", "") or user.get("display_name", ""),
            display_name=user.get("display_name", "") or user.get("username", ""),
            email=email,
            timeout_minutes=timeout_minutes,
        )
        await self._send_email_async(email, self.verify_email_subject, html_body)

    async def _query_user_for_binding(self, account: str) -> Dict[str, Any]:
        account = account.strip()
        if not account:
            raise NewAPIError("绑定参数为空")
        if account.isdigit():
            return await self.client.get_user(int(account))
        return await self.client.search_user(account)

    @filter.command("绑定", alias={"newapi绑定", "NewAPI绑定"})
    async def bind_account(self, event: AstrMessageEvent, account: str = ""):
        """绑定 NewAPI 账号。用法：/绑定 数字ID或邮箱"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        self._remove_expired_pending()

        unbind_time = self.state.get("unbinds", {}).get(qq_id, 0)
        if unbind_time:
            elapsed_hours = (time.time() - unbind_time) / 3600
            if elapsed_hours < self.unbind_cooldown_hours:
                remaining_hours = int(self.unbind_cooldown_hours - elapsed_hours) + 1
                yield event.plain_result(
                    f"解绑后 {self.unbind_cooldown_hours} 小时内不可重新绑定。\n"
                    f"还需等待约 {remaining_hours} 小时。"
                )
                return

        if not account:
            account = self._extract_bind_account(event.message_str)

        if not account:
            yield event.plain_result(f"用法：/绑定 <{self.api_display_name}数字ID或邮箱>\n示例：/绑定 123 或 /绑定 user@example.com")
            return

        existing = self._get_binding(qq_id)
        if existing:
            yield event.plain_result(
                f"你已经绑定了 {self.api_display_name} 账号：\n"
                f"ID：{existing.get('newapi_user_id')}\n"
                f"账号：{existing.get('username') or existing.get('display_name')}\n"
                "如需更换，请先使用 /解绑"
            )
            return

        try:
            user = await asyncio.wait_for(
                self._query_user_for_binding(account),
                timeout=self.api_timeout_seconds,
            )
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"查询 {self.api_display_name} 用户超时({self.api_timeout_seconds}秒)。"
                f"请检查 API 地址和网络连接。"
            )
            return
        except Exception as exc:
            yield event.plain_result(f"查询失败：{exc}")
            return

        user_id = int(user.get("id", 0) or 0)
        if not user_id:
            yield event.plain_result("查询失败：接口未返回有效用户 ID")
            return

        bound_qq = self._find_binding_by_user_id(user_id)
        if bound_qq and bound_qq != qq_id:
            yield event.plain_result(f"该 {self.api_display_name} 账号已被其他 QQ 绑定，无法重复绑定。")
            return

        email = str(user.get("email", "")).strip()
        qq_email = f"{qq_id}@qq.com"
        qq_email_match = email.lower() == qq_email.lower()

        if self.auto_confirm_qq_email and qq_email_match:
            self._save_binding(qq_id, user)
            yield event.plain_result(
                "绑定成功！\n"
                f"API ID：{user_id}\n"
                f"账号：{user.get('username') or user.get('display_name')}"
            )
            return

        if not email:
            yield event.plain_result(
                f"该 {self.api_display_name} 账号未绑定邮箱，无法完成绑定。\n"
                f"请先在 {self.api_display_name} 后台为账号绑定邮箱后再尝试绑定。"
            )
            return

        if not self.require_email_verification:
            self._save_binding(qq_id, user)
            yield event.plain_result(
                "绑定成功！\n"
                f"API ID：{user_id}\n"
                f"账号：{user.get('username') or user.get('display_name')}"
            )
            return

        self.pending_bindings[qq_id] = {
            "stage": "confirm",
            "user": user,
            "email": email,
            "expires_at": time.time() + self.verification_expire_seconds,
        }

        info = (
            f"请确认要绑定以下 {self.api_display_name} 账号：\n"
            f"ID：{user_id}\n"
            f"用户名：{user.get('username', '')}\n"
            f"显示名：{user.get('display_name', '')}\n"
        )
        if self.require_email_verification:
            info += f"邮箱：{self._mask_email(email)}\n"
        info += (
            f"当前额度：{self._format_quota(int(user.get('quota', 0) or 0))}\n"
            f"QQ邮箱匹配：{'是' if qq_email_match else '否'}\n\n"
            "确认无误请发送：/确认绑定\n"
            "取消请发送：/取消绑定"
        )
        yield event.plain_result(info)

    @filter.command("确认绑定")
    async def confirm_bind(self, event: AstrMessageEvent):
        """确认绑定并向 NewAPI 账号邮箱发送验证码。"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        self._remove_expired_pending()

        unbind_time = self.state.get("unbinds", {}).get(qq_id, 0)
        if unbind_time:
            elapsed_hours = (time.time() - unbind_time) / 3600
            if elapsed_hours < self.unbind_cooldown_hours:
                remaining_hours = int(self.unbind_cooldown_hours - elapsed_hours) + 1
                yield event.plain_result(
                    f"解绑后 {self.unbind_cooldown_hours} 小时内不可重新绑定。\n"
                    f"还需等待约 {remaining_hours} 小时。"
                )
                return

        pending = self.pending_bindings.get(qq_id)

        if not pending or pending.get("stage") != "confirm":
            yield event.plain_result("当前没有待确认的绑定请求，请先使用 /绑定 <数字ID或邮箱>。")
            return

        try:
            user = pending["user"]
            user_id = int(user.get("id", 0) or 0)

            existing = self._get_binding(qq_id)
            if existing:
                del self.pending_bindings[qq_id]
                yield event.plain_result(
                    f"你已经绑定了一个 {self.api_display_name} 账号，无法重复绑定。\n"
                    f"已绑 API ID：{existing.get('newapi_user_id')}\n"
                    f"账号：{existing.get('username') or existing.get('display_name')}\n"
                    "如需更换请先发送 /解绑。"
                )
                return

            bound_qq = self._find_binding_by_user_id(user_id)
            if bound_qq and bound_qq != qq_id:
                del self.pending_bindings[qq_id]
                yield event.plain_result(f"该 {self.api_display_name} 账号已被其他 QQ 绑定，无法重复绑定。")
                return

            email = str(user.get("email", "")).strip()
            if not email:
                del self.pending_bindings[qq_id]
                yield event.plain_result(
                    f"该 {self.api_display_name} 账号未绑定邮箱，无法完成绑定。\n"
                    f"请先在 {self.api_display_name} 后台为账号绑定邮箱后再尝试绑定。"
                )
                return

            if not self.require_email_verification:
                self._save_binding(qq_id, user)
                self.pending_bindings.pop(qq_id, None)
                yield event.plain_result(
                    "绑定成功！\n"
                    f"API ID：{user.get('id')}\n"
                    f"账号：{user.get('username') or user.get('display_name')}"
                )
                return
            await self._send_bind_code(qq_id, user)
            yield event.plain_result(f"验证码已发送至 {self._mask_email(email)}，请在群中直接发送 6 位验证码完成绑定。")
        except Exception as exc:
            self.pending_bindings.pop(qq_id, None)
            logger.error(f"[NewAPIPro] 发送绑定验证码失败: {exc}")
            yield event.plain_result(f"发送验证码失败：{exc}")

    @filter.command("取消绑定")
    async def cancel_bind(self, event: AstrMessageEvent):
        """取消当前绑定流程。"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        if self.pending_bindings.pop(qq_id, None):
            yield event.plain_result("已取消本次绑定流程。")
        else:
            yield event.plain_result("当前没有待取消的绑定流程。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def verify_code_listener(self, event: AstrMessageEvent):
        """监听用户在群/私聊中发送的验证码。"""
        qq_id = str(event.get_sender_id())
        self._remove_expired_pending()
        pending = self.pending_bindings.get(qq_id)
        if not pending or pending.get("stage") != "verify":
            return

        text = (event.message_str or "").strip()
        if not text.isdigit() or len(text) != 6:
            return

        event.stop_event()
        pending["attempts"] = int(pending.get("attempts", 0) or 0) + 1

        if pending["attempts"] > 5:
            self.pending_bindings.pop(qq_id, None)
            yield event.plain_result("验证码错误次数过多，绑定流程已取消。请重新使用 /绑定 发起绑定。")
            return

        if text != str(pending.get("code")):
            yield event.plain_result("验证码错误，请检查邮件后重试。")
            return

        user = pending["user"]
        self._save_binding(qq_id, user)
        self.pending_bindings.pop(qq_id, None)

        yield event.plain_result(
            "绑定成功！\n"
            f"API ID：{user.get('id')}\n"
            f"账号：{user.get('username') or user.get('display_name')}"
        )

    @filter.command("签到", alias={"newapi签到", "NewAPI签到"})
    async def checkin(self, event: AstrMessageEvent):
        """NewAPI 每日签到，直接给已绑定账号增加额度。"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        if qq_id in self.processing_users:
            return
        self.processing_users.add(qq_id)

        try:
            binding = self._get_binding(qq_id)
            if not binding:
                yield event.plain_result(f"你还没有绑定 {self.api_display_name} 账号。\n请先使用 /绑定 <数字ID或邮箱> 完成邮箱验证码绑定。")
                return

            user_id = int(binding.get("newapi_user_id", 0) or 0)

            user = await self.client.get_user(user_id)
            old_quota = int(user.get("quota", 0) or 0)

            can_checkin = self._can_checkin(qq_id)

            if can_checkin:
                if self.target_quota > 0 and old_quota >= self.target_quota:
                    await self.client.decrease_user_quota(user_id, old_quota)
                    updated_user = await self.client.get_user(user_id)
                    new_quota = int(updated_user.get("quota", 0) or 0)
                    self.state.setdefault("checkins", {})[qq_id] = int(time.time())
                    self._save_state()
                    yield event.plain_result(
                        "签到成功！已达到目标额度，已将全部额度扣除。\n"
                        f"账号：{user.get('username') or user.get('display_name') or user_id}\n"
                        f"扣除额度：{self._format_quota(old_quota)}\n"
                        f"现额度：{self._format_quota(new_quota)}"
                    )
                    return

                if self._use_random_quota:
                    actual_quota = random.randint(self.checkin_quota_min, self.checkin_quota_max)
                else:
                    actual_quota = self.checkin_quota

                await self.client.increase_user_quota(user_id, actual_quota)
                updated_user = await self.client.get_user(user_id)
                new_quota = int(updated_user.get("quota", 0) or 0)
                self.state.setdefault("checkins", {})[qq_id] = int(time.time())
                self._save_state()
                yield event.plain_result(
                    "签到成功！\n"
                    f"账号：{user.get('username') or user.get('display_name') or user_id}\n"
                    f"增加额度：{self._format_quota(actual_quota)}\n"
                    f"原额度：{self._format_quota(old_quota)}\n"
                    f"现额度：{self._format_quota(new_quota)}"
                )
            else:
                if self.target_quota > 0 and old_quota >= self.target_quota:
                    await self.client.decrease_user_quota(user_id, old_quota)
                    updated_user = await self.client.get_user(user_id)
                    new_quota = int(updated_user.get("quota", 0) or 0)
                    self.state.setdefault("checkins", {})[qq_id] = int(time.time())
                    self._save_state()
                    yield event.plain_result(
                        "重复签到！已达到目标额度，已将全部额度扣除。\n"
                        f"账号：{user.get('username') or user.get('display_name') or user_id}\n"
                        f"扣除额度：{self._format_quota(old_quota)}\n"
                        f"现额度：{self._format_quota(new_quota)}"
                    )
                    return

                if self.penalty_quota > 0:
                    actual_penalty = min(self.penalty_quota, old_quota)
                    await self.client.decrease_user_quota(user_id, actual_penalty)
                    updated_user = await self.client.get_user(user_id)
                    new_quota = int(updated_user.get("quota", 0) or 0)
                    self.state.setdefault("checkins", {})[qq_id] = int(time.time())
                    self._save_state()
                    yield event.plain_result(
                        "重复签到！已扣除惩罚额度。\n"
                        f"账号：{user.get('username') or user.get('display_name') or user_id}\n"
                        f"扣除额度：{self._format_quota(actual_penalty)}\n"
                        f"现额度：{self._format_quota(new_quota)}"
                    )
                else:
                    yield event.plain_result(f"今天已经签到过了。\n下次可签到时间：{self._next_reset_text()}")
        except Exception as exc:
            yield event.plain_result(f"签到失败：{exc}")
            logger.error(f"[NewAPIPro] 签到失败 (QQ={qq_id}): {exc}", exc_info=True)
        finally:
            self.processing_users.discard(qq_id)

    @filter.command("我的账号", alias={"账号信息"})
    async def my_account(self, event: AstrMessageEvent):
        """查看当前 QQ 绑定的 NewAPI 账号信息、额度和调用次数。"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        binding = self._get_binding(qq_id)
        if not binding:
            yield event.plain_result(f"你还没有绑定 {self.api_display_name} 账号。\n使用 /绑定 <数字ID或邮箱> 开始绑定。")
            return

        user_id = int(binding.get("newapi_user_id", 0) or 0)
        bind_time = datetime.fromtimestamp(int(binding.get("bind_time", 0) or 0), BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        last_ts = int(binding.get("last_checkin", 0) or 0)
        last_text = datetime.fromtimestamp(last_ts, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "从未签到"
        status = "今日可签到" if self._can_checkin(qq_id) else f"今日已签到，下次：{self._next_reset_text()}"

        lines = [
            f"{self.api_display_name} 账号信息",
            f"API ID：{user_id}",
            f"账号：{binding.get('username') or binding.get('display_name')}",
            f"邮箱：{self._mask_email(str(binding.get('email', '')))}",
            f"绑定时间：{bind_time}",
            f"上次签到：{last_text}",
            f"签到状态：{status}",
        ]

        try:
            user = await self.client.get_user(user_id)
            quota = int(user.get("quota", 0) or 0)
            used_quota = int(user.get("used_quota", 0) or 0)
            req_count = int(user.get("request_count", 0) or 0)
            lines.append(f"剩余额度：{self._format_quota(quota)}")
            lines.append(f"已用额度：{self._format_quota(used_quota)}")
            lines.append(f"调用次数：{req_count}")
        except Exception:
            lines.append("（实时数据查询失败）")

        yield event.plain_result("\n".join(lines))

    @filter.command("解绑")
    async def unbind(self, event: AstrMessageEvent):
        """解绑当前 QQ 的 NewAPI 账号。"""
        event.stop_event()
        qq_id = str(event.get_sender_id())
        if self.state.get("bindings", {}).pop(qq_id, None):
            self.state.setdefault("unbinds", {})[qq_id] = int(time.time())
            self._save_state()
            self.pending_bindings.pop(qq_id, None)
            yield event.plain_result(f"已解绑 {self.api_display_name} 账号。")
        else:
            yield event.plain_result(f"当前 QQ 尚未绑定 {self.api_display_name} 账号。")

    @filter.command("签到帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示插件帮助。"""
        event.stop_event()
        lines = [
            f"{self.api_display_name} 签到插件指令：",
            "/绑定 <数字ID或邮箱> - 查询账号并发起绑定",
            "/确认绑定 - 确认并完成绑定",
            "/取消绑定 - 取消当前绑定流程",
        ]
        if self.require_email_verification:
            lines.append("发送 6 位验证码 - 完成绑定")
        lines.extend([
            "/签到 - 每日签到，额度直接加到绑定账号",
            "/我的账号 - 查看账号信息、额度和调用次数",
            "/解绑 - 解除当前绑定",
        ])
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        self._save_state()
        logger.info("[NewAPIPro] 插件已卸载")
