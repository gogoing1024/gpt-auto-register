"""iCloud 取件：只认 /openapi/mail/{email}/{token}/latest。

GET 返回两种形态：

    有信  {"email":"...","message":{id,subject,sender,code,preview,bodyText,bodyHtml,receivedAt}}
    空箱  {"email":"...","message":null}

只认 sender 属于 ChatGPT / OpenAI 的信，避免把别家验证码交上去。
码只用接口给出的 message.code，不扫正文、不认其它中转站。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .base import MailProvider, MailProviderError, register, validate_email

logger = logging.getLogger(__name__)

# iCloud「隐藏我的邮件」会把 noreply@tm.openai.com 改写成
# noreply_at_tm_openai_com_xxxx@icloud.com，所以两种都要认。
_SENDER_HINTS = (
    "chatgpt",
    "openai",
    "tm.openai",
    "tm_openai",
    "noreply_at_tm_openai",
    "otp_at_tm",
)

_RE_OTP = re.compile(r"^\d{6}$")


def _is_openai_sender(sender: str) -> bool:
    blob = (sender or "").lower()
    return any(h in blob for h in _SENDER_HINTS)


def _parse_received_at(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def parse_latest(raw: str) -> Optional[dict]:
    """解析 /latest 响应。空箱或非目标发件人返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    msg = data.get("message")
    if msg is None:
        return None
    if not isinstance(msg, dict):
        return None

    sender = str(msg.get("sender") or "")
    if not _is_openai_sender(sender):
        logger.info(
            "[icloud] 跳过非目标发件人: %s",
            sender[:80] or "(空)",
        )
        return None

    code = str(msg.get("code") or "").strip()
    if not _RE_OTP.match(code):
        logger.info("[icloud] 目标邮件没有 6 位 code，已忽略")
        return None

    ts = _parse_received_at(str(msg.get("receivedAt") or ""))
    uid = str(msg.get("id") or "").strip()
    return {
        "uid": uid,
        "sender": sender,
        "subject": str(msg.get("subject") or ""),
        "otp": code,
        "ts": ts,
        "date_str": str(msg.get("receivedAt") or ""),
    }


@register
class ICloudProvider(MailProvider):
    """iCloud 邮箱（/openapi/mail/{email}/{token}/latest）。"""

    kind = "icloud"
    display_name = "iCloud 邮箱"
    pooled = True
    ephemeral = False
    line_segments = 2
    import_hint = "email----取件链接"
    import_placeholder = (
        "name@icloud.com----https://host/openapi/mail/"
        "name%40icloud.com/TOKEN/latest"
    )
    config_fields = []
    accepts_existing_account = True

    def __init__(self, email: str, relay_url: str, timeout: int = 20):
        email = (email or "").strip().lower()
        relay_url = (relay_url or "").strip()
        if not email:
            raise ValueError("iCloud 邮箱地址不能为空")
        validate_email(email)
        if not relay_url.lower().startswith(("http://", "https://")):
            raise ValueError("取件链接必须是 http(s):// 开头的完整地址")

        self.email = email
        self.relay_url = relay_url
        self.http_timeout = timeout
        self._dead = False
        self.last_persona = None
        self._host = urllib.parse.urlsplit(relay_url).netloc
        self._seen: set[str] = set()

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        if not account:
            raise MailProviderError(
                "iCloud 是号池型：请先去「导入邮箱」页导入号，"
                "格式 email----取件链接",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        relay = (account.get("relay_url") or "").strip()
        if not relay:
            raise MailProviderError(
                f"号池里的 {email} 没有取件链接，请按 email----取件链接 重新导入",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, relay_url=relay)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    def create_mailbox(self) -> str:
        return self.email

    def _fetch(self) -> str:
        req = urllib.request.Request(self.relay_url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, */*;q=0.8",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return json.dumps({"email": self.email, "message": None})
            if e.code in (401, 403, 410):
                raise MailProviderError(
                    f"取件链接无效（HTTP {e.code}）—— token 可能过期或链接填错了",
                    fatal=True, kind=self.kind,
                ) from e
            raise

    def _latest(self) -> Optional[dict]:
        try:
            return parse_latest(self._fetch())
        except MailProviderError:
            raise
        except Exception as e:
            logger.warning("[icloud] 拉取异常（吞掉重试）: %s", e)
            return None

    @staticmethod
    def _fp(m: dict) -> str:
        uid = m.get("uid") or ""
        if uid:
            return f"uid:{uid}"
        return f"{m.get('date_str','')}|{m.get('otp','')}"

    def _usable(self, m: Optional[dict], issued_after: Optional[float]) -> Optional[dict]:
        if not m:
            return None
        cutoff = (issued_after - 5) if issued_after else None
        if cutoff and m.get("ts") and m["ts"] < cutoff:
            logger.debug(
                "[icloud] 跳过旧邮件 %s (%s)",
                m.get("date_str"), (m.get("subject") or "")[:40],
            )
            return None
        return m

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        deadline = time.time() + max(0.0, float(wait or 0))
        while True:
            m = self._usable(self._latest(), issued_after)
            if m:
                return m["otp"]
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        logger.info(
            "[icloud] 等待 OTP -> %s (timeout=%ss)",
            email_addr, timeout,
        )
        while time.time() < deadline:
            m = self._usable(self._latest(), issued_after)
            if m:
                fp = self._fp(m)
                if fp in self._seen:
                    time.sleep(3)
                    continue
                self._seen.add(fp)
                logger.info(
                    "[icloud] ✅ OTP=%s (%s %s)",
                    m["otp"], m.get("date_str") or "", (m.get("subject") or "")[:40],
                )
                return m["otp"]
            time.sleep(3)
        raise TimeoutError(
            f"iCloud OTP 超时 {timeout}s（{email_addr}）—— 确认取件链接能收到这个邮箱的信"
        )

    @classmethod
    def parse_line(cls, line: str) -> dict:
        parts = [p.strip() for p in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（email----取件链接），实际 {len(parts)} 段"
            )
        email, relay = parts
        validate_email(email)
        if not relay.lower().startswith(("http://", "https://")):
            raise ValueError("第 2 段必须是 http(s):// 开头的取件链接")
        return {
            "email": email.lower(),
            "kind": cls.kind,
            "relay_url": relay,
        }

    def self_test(self) -> dict:
        try:
            raw = self._fetch()
            data = json.loads(raw) if (raw or "").strip() else {}
        except MailProviderError as e:
            return {"ok": False, "message": f"[{self._host}] {e}"}
        except Exception as e:
            return {"ok": False, "message": f"[{self._host}] 拉取失败: {e}"}

        if not isinstance(data, dict):
            return {"ok": False, "message": f"[{self._host}] 返回不是 JSON 对象"}

        msg = data.get("message")
        if msg is None:
            return {
                "ok": True,
                "message": (
                    f"[{self._host}] 链接可访问，当前没有邮件（{self.email}）。"
                ),
            }
        if not isinstance(msg, dict):
            return {"ok": False, "message": f"[{self._host}] message 字段格式不对"}

        parsed = parse_latest(raw)
        if not parsed:
            sender = str(msg.get("sender") or "") or "(空)"
            return {
                "ok": True,
                "message": (
                    f"[{self._host}] 最新一封不是 ChatGPT/OpenAI 来信"
                    f"（sender={sender[:80]}），已过滤。"
                ),
            }
        return {
            "ok": True,
            "message": (
                f"[{self._host}] 连接成功，{self.email} 最新验证码 "
                f"{parsed['otp']}（{parsed.get('date_str') or '时间未知'}）"
            ),
        }
