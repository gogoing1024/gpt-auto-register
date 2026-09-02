"""AtomicMail 邮箱 provider（atomicmail.io，号池型）。

收码链路移植自 gr0k-register-atomicmail/atomicmail.py（login / _resolve_inbox /
_list_messages）和 register_grok.py 的 _poll_otp_grok，API 是 atomicmail.io 网页版
自己在用的那套：

    POST /auth/sign-in                       {username, password: sha256(密码), platform, deviceId}
                                             → {accessToken}
    GET  /mailboxes                          → {results: [{id, path, name}]}，取 INBOX
    GET  /mailboxes/{inbox}/messages         → {results: [{id, subject, intro, from}]}
    GET  /mailboxes/{inbox}/messages/{mid}   → {subject, text, html}
    401 → 重新登录一次再拉

⚠️ 密码**不是**明文发过去的：网页端先做 sha256 再提交，这里必须一样，
   否则永远 401，主人会以为导入的密码是错的。

导入格式 2 段：email----AtomicMail密码，和 gr0k-register-atomicmail/emails.txt 一致。
号是 register_atomicmail.py 批量注册出来的，只有 @atomicmail.io、不支持 +alias
（那边 emails.txt 的文件头就是这么写的），导入时顺手把这两点校验掉。

能力：pooled=True（号池 claim / 用完 mark_done）
      ephemeral=False（地址固定，OpenAI 可能当老号处理）

「本轮的信」怎么认（issued_after）：
    列表项如果带时间戳（date / receivedAt / createdAt ...）就按时间过滤；
    带不带、叫什么名字参考实现里没用到、也没有公开文档，所以再加一道保险：
    create_mailbox() 时把收件箱里已有的邮件 id 拍个快照（参考实现的 known_ids
    就是这个思路），没时间戳的信只认快照之外的。快照拍在 get_auth_url 之前，
    OpenAI 靠 login_hint 抢跑发的那封不会被误当成旧信（cf_temp 踩过这个坑）。
"""
from __future__ import annotations

import email.utils as _eu
import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from .base import MailProvider, MailProviderError, extract_otp, register, validate_email

logger = logging.getLogger(__name__)

API_BASE = "https://api.atomicmail.io/v1"
APP_ORIGIN = "https://atomicmail.io"
# register_atomicmail.py 只会造这个域名的号；login 接口也只认 username，
# 别的域名连表达都表达不了，导入时直接拒掉，免得选错来源的号混进池子。
_ALLOWED_DOMAINS = ("atomicmail.io",)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 只认 OpenAI / ChatGPT 的来信（发件人或标题里带这两个词之一）。
# AtomicMail 自己的欢迎邮件之类里也可能有 6 位数字，不过滤会把它当验证码交上去。
_OPENAI_HINTS = ("openai", "chatgpt")

# 列表项里可能装时间戳的字段名，按顺序试。参考实现没用到时间戳，这里是防御性猜测：
# 猜中了按时间过滤，全没猜中就退回 id 快照（见模块头）。
_TS_KEYS = ("date", "receivedAt", "received_at", "createdAt", "created_at", "timestamp", "time")

_POLL_INTERVAL = 3


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _username_of(email_or_user: str) -> str:
    """登录用的是用户名（@ 前那段），+alias 也要去掉 —— 和参考实现一致。"""
    s = (email_or_user or "").strip()
    if "@" in s:
        return s.split("@", 1)[0].split("+", 1)[0]
    return s


def _brief(data: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(data, ensure_ascii=False)
    except Exception:
        s = str(data)
    return s[:limit]


def _loads(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:500]}


def _request(
    method: str,
    path: str,
    token: Optional[str] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """返回 (status, json)。传输层异常（连不上 / 超时 / SSL）status=0，json={"error": ...}。

    Origin / Referer 照网页端带上：参考实现就是这么过的，少了不确定服务端会不会拒。
    """
    url = path if path.startswith("http") else API_BASE + path
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": APP_ORIGIN,
        "Referer": f"{APP_ORIGIN}/app/",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return e.code, _loads(raw)
    except Exception as e:
        return 0, {"error": str(e)}


def _unwrap(data: Any) -> dict:
    """有的接口把正文包在 results 里、有的是扁平的（参考实现两种都兼容了）。"""
    if not isinstance(data, dict):
        return {}
    inner = data.get("results")
    return inner if isinstance(inner, dict) else data


def _as_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("results", "messages", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _sender_of(msg: dict) -> str:
    """from 字段见过 {address, name}、纯字符串两种；列表形态顺手兼容。"""
    f = msg.get("from")
    if isinstance(f, list):
        f = f[0] if f else ""
    if isinstance(f, dict):
        return str(f.get("address") or f.get("email") or f.get("name") or "")
    return str(f or "")


def _is_openai_mail(subject: str, sender: str) -> bool:
    blob = f"{subject} {sender}".lower()
    return any(h in blob for h in _OPENAI_HINTS)


def _msg_epoch(msg: dict) -> Optional[float]:
    """从列表项里尽力读出到达时间（epoch 秒）；读不出返回 None。

    兼容 ISO 8601（含 Z）、RFC 2822、数字 epoch（秒 / 毫秒都可能）。
    """
    for key in _TS_KEYS:
        v = msg.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            n = float(v)
            return n / 1000.0 if n > 1e11 else n
        s = str(v).strip()
        if s.isdigit():
            n = float(s)
            return n / 1000.0 if n > 1e11 else n
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        try:
            return _eu.parsedate_to_datetime(s).timestamp()
        except Exception:
            continue
    return None


@register
class AtomicMailProvider(MailProvider):
    """AtomicMail（atomicmail.io）号池邮箱：用导入的密码登录 API 收 OTP。"""

    kind = "atomicmail"
    display_name = "AtomicMail 邮箱"
    pooled = True
    ephemeral = False

    line_segments = 2
    import_hint = "每行一个：email----AtomicMail密码（gr0k-register-atomicmail 的 emails.txt 原样粘贴即可）"
    import_placeholder = "james.smith4821@atomicmail.io----A0zLnX28%uzo8j"

    config_fields = []   # 凭证全在号池里，无全局配置

    def __init__(self, email: str, password: str, timeout: int = 15):
        email = (email or "").strip().lower()
        password = password or ""
        if not email:
            raise ValueError("AtomicMail 邮箱地址不能为空")
        validate_email(email)
        if not password:
            raise ValueError("AtomicMail 密码不能为空")
        self.email = email
        self.password = password
        self.username = _username_of(email)
        self.http_timeout = timeout
        self.last_persona = None
        self._dead = False
        self._token: str = ""
        self._inbox_id: str = ""
        self._seen: set[str] = set()              # 已经交上去用过的邮件 id
        self._baseline: Optional[set[str]] = None  # 开跑前收件箱里就有的 id（见模块头）

    # ── 构造 / 导入 ─────────────────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        if not account:
            raise MailProviderError(
                "AtomicMail 是号池型：请先去「导入邮箱」页导入号，格式 email----密码",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        password = account.get("password") or ""
        if not password:
            raise MailProviderError(
                f"号池里的 {email} 没有密码，请按 email----密码 重新导入",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, password=password)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    @classmethod
    def parse_line(cls, line: str) -> dict:
        parts = [p.strip() for p in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（email----AtomicMail密码），实际 {len(parts)} 段"
            )
        email, password = parts
        validate_email(email)
        email = email.lower()
        local, domain = email.rsplit("@", 1)
        if domain not in _ALLOWED_DOMAINS:
            raise ValueError(
                f"不是 AtomicMail 邮箱（域名 {domain}，只认 {' / '.join(_ALLOWED_DOMAINS)}）"
                "—— 是不是选错了邮箱来源？"
            )
        if "+" in local:
            raise ValueError("AtomicMail 不支持 +alias 变体，请填原邮箱")
        if not password:
            raise ValueError("第 2 段（密码）为空")
        if password.lower().startswith(("http://", "https://")):
            raise ValueError(
                "第 2 段是个链接，AtomicMail 需要的是登录密码 —— 这行像是 iCloud 取件链接格式"
            )
        return {"email": email, "kind": cls.kind, "password": password}

    # ── 号池语义 ─────────────────────────────────────────

    @property
    def exhausted(self) -> bool:
        return self._dead

    def mark_dead(self, reason: str = "") -> None:
        logger.warning("[atomicmail] %s mark dead: %s", self.email, reason)
        self._dead = True

    # ── AtomicMail API ──────────────────────────────────

    def _login(self, force: bool = False) -> tuple[str, str]:
        """登录拿 (token, inbox_id)，缓存在实例上；401 时调用方传 force=True 重登。"""
        if self._token and self._inbox_id and not force:
            return self._token, self._inbox_id

        status, data = _request(
            "POST", "/auth/sign-in",
            body={
                "password": _sha256(self.password),
                "username": self.username,
                "platform": "web",
                "deviceId": secrets.token_hex(16),
            },
            timeout=30,
        )
        if status == 0:
            raise MailProviderError(
                f"AtomicMail 登录网络失败: {(data or {}).get('error')}",
                fatal=False, kind=self.kind,
            )
        payload = _unwrap(data)
        token = str(payload.get("accessToken") or "")
        if status not in (200, 201) or not token:
            # 4xx = 密码错 / 号没了，这号废了；5xx / 429 是服务端的问题，回头再试
            fatal = 400 <= status < 500 and status != 429
            raise MailProviderError(
                f"AtomicMail 登录失败 [{status}] {self.email}: {_brief(data)}"
                + ("（密码错误或账号不可用）" if fatal else ""),
                fatal=fatal, kind=self.kind,
            )

        inbox_id = self._resolve_inbox(token)
        self._token, self._inbox_id = token, inbox_id
        return token, inbox_id

    def _resolve_inbox(self, token: str) -> str:
        status, data = _request("GET", "/mailboxes", token=token, timeout=self.http_timeout)
        if status == 0:
            raise MailProviderError(
                f"AtomicMail 获取邮箱列表网络失败: {(data or {}).get('error')}",
                fatal=False, kind=self.kind,
            )
        if status != 200:
            raise MailProviderError(
                f"AtomicMail 获取邮箱列表失败 [{status}]: {_brief(data)}",
                fatal=status in (401, 403), kind=self.kind,
            )
        boxes = _as_list(data)
        for box in boxes:
            if not isinstance(box, dict):
                continue
            if (
                str(box.get("path", "")).upper() == "INBOX"
                or str(box.get("name", "")).upper() == "INBOX"
            ):
                return str(box.get("id"))
        if boxes and isinstance(boxes[0], dict) and boxes[0].get("id") is not None:
            return str(boxes[0]["id"])
        raise MailProviderError("AtomicMail 账号下没有任何邮箱", fatal=True, kind=self.kind)

    def _list_messages(self) -> list[dict]:
        token, inbox_id = self._login()
        status, data = _request(
            "GET", f"/mailboxes/{inbox_id}/messages", token=token, timeout=self.http_timeout,
        )
        if status == 401:
            token, inbox_id = self._login(force=True)
            status, data = _request(
                "GET", f"/mailboxes/{inbox_id}/messages", token=token, timeout=self.http_timeout,
            )
        if status == 0:
            raise MailProviderError(
                f"AtomicMail 拉取邮件列表网络失败: {(data or {}).get('error')}",
                fatal=False, kind=self.kind,
            )
        if status != 200:
            raise MailProviderError(
                f"AtomicMail 拉取邮件列表失败 [{status}]: {_brief(data)}",
                fatal=False, kind=self.kind,
            )
        return [m for m in _as_list(data) if isinstance(m, dict)]

    def _message_text(self, mid: str) -> str:
        """列表里的 subject+intro 没找到码时，再拉一次详情（text + html）。"""
        token, inbox_id = self._login()
        status, data = _request(
            "GET", f"/mailboxes/{inbox_id}/messages/{mid}", token=token, timeout=self.http_timeout,
        )
        if status != 200:
            logger.debug("[atomicmail] 详情 %s 拉取失败 [%s]", mid, status)
            return ""
        d = _unwrap(data)
        return "\n".join(str(d.get(k) or "") for k in ("subject", "text", "html"))

    # ── 找码 ─────────────────────────────────────────────

    def _find_otp(self, issued_after: Optional[float]) -> Optional[tuple[str, str, str]]:
        """扫一遍收件箱，返回 (邮件 id, otp, subject)；没有返回 None。不改 _seen。"""
        msgs = self._list_messages()
        cutoff = (issued_after - 5) if issued_after else None

        # 有时间戳就按新→旧排，同一轮 OpenAI 会连发几封同码的信，先拿最新的
        def _key(m: dict) -> float:
            return _msg_epoch(m) or 0.0
        if any(_msg_epoch(m) is not None for m in msgs):
            msgs = sorted(msgs, key=_key, reverse=True)

        for msg in msgs:
            mid = str(msg.get("id") or "")
            if not mid or mid in self._seen:
                continue
            if self._baseline is not None and mid in self._baseline:
                continue
            ts = _msg_epoch(msg)
            if cutoff is not None and ts is not None and ts < cutoff:
                logger.debug("[atomicmail] 跳过旧信 id=%s ts=%s", mid, ts)
                continue
            subject = str(msg.get("subject") or "")
            sender = _sender_of(msg)
            if not _is_openai_mail(subject, sender):
                logger.debug("[atomicmail] 跳过非 OpenAI 来信: %s / %s", sender[:60], subject[:60])
                continue
            otp = extract_otp(f"{subject}\n{msg.get('intro') or ''}")
            if not otp:
                otp = extract_otp(self._message_text(mid))
            if otp:
                return mid, otp, subject
            logger.debug("[atomicmail] OpenAI 来信但没抽到码 id=%s subject=%s", mid, subject[:60])
        return None

    # ── MailProvider 接口 ────────────────────────────────

    def create_mailbox(self) -> str:
        logger.info("[atomicmail] 使用 AtomicMail 账号: %s", self.email)
        # 这里就登录一次：① 密码错的号在花 OpenAI 那边的流程之前就拦下来；
        # ② 拍收件箱快照，后面没时间戳的信靠它区分新旧（见模块头）。
        try:
            msgs = self._list_messages()
            self._baseline = {str(m.get("id")) for m in msgs if m.get("id") is not None}
            logger.info(
                "[atomicmail] 登录成功，收件箱已有 %d 封（本轮只认之后新到的）",
                len(self._baseline),
            )
        except MailProviderError as e:
            if e.fatal:
                self.mark_dead(str(e))
                raise
            # 网络问题不在这儿掐死整轮：收码时还会重试
            logger.warning("[atomicmail] 开跑前登录/拉取失败（收码时再试）: %s", e)
        except Exception as e:
            logger.warning("[atomicmail] 开跑前登录/拉取异常（收码时再试）: %s", e)
        return self.email

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """非破坏性预读：命中也**不记 _seen**，留给紧接着的 wait_for_otp 正式消费。"""
        deadline = time.time() + max(0.0, float(wait or 0))
        while True:
            try:
                hit = self._find_otp(issued_after)
                if hit:
                    logger.info("[atomicmail] 👀 预读命中 OTP=%s (id=%s)，省掉一次发码", hit[1], hit[0])
                    return hit[1]
            except Exception as e:
                logger.debug("[atomicmail] peek 异常（当作没探到）: %s", e)
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
        logger.info("[atomicmail] 等待 OTP -> %s (timeout=%ss)", email_addr, timeout)
        while time.time() < deadline:
            try:
                hit = self._find_otp(issued_after)
                if hit:
                    mid, otp, subject = hit
                    self._seen.add(mid)
                    logger.info("[atomicmail] ✅ OTP=%s (id=%s %s)", otp, mid, subject[:40])
                    return otp
            except MailProviderError as e:
                if e.fatal:
                    # 密码错 / 号没了：再等也等不来，直接废号让外层换下一个
                    self.mark_dead(str(e))
                    raise
                logger.warning("[atomicmail] 拉取失败（重试）: %s", e)
            except Exception as e:
                logger.warning("[atomicmail] 拉取异常（重试）: %s", e)
            time.sleep(_POLL_INTERVAL)
        raise TimeoutError(
            f"AtomicMail OTP 超时 {timeout}s（{email_addr}）—— 确认这个号能登录 atomicmail.io 且收到了 OpenAI 的信"
        )

    def self_test(self) -> dict:
        try:
            msgs = self._list_messages()
        except MailProviderError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}"}
        return {"ok": True, "message": f"登录成功，{self.email} 收件箱共 {len(msgs)} 封"}
