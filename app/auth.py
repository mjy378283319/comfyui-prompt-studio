"""账号、登录会话与访问控制（对齐拓竹耗材管家的安全口径，纯标准库实现）。

- 口令 PBKDF2-HMAC-SHA256 加盐哈希，比对走 hmac.compare_digest，不存明文。
- 会话令牌 32 字节随机数，磁盘只存 SHA-256 摘要；改口令后旧会话全部作废。
- Cookie 带 HttpOnly / SameSite=Lax，反代 HTTPS 下自动加 Secure。
- 登录失败按来源 IP 计数锁定，抵御在线爆破。
- 首次创建管理员只允许回环 / 内网 TCP 直连，避免挂上公网被抢先注册。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import time
from pathlib import Path

COOKIE_NAME = "ps_session"
PBKDF2_ALGO = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 200_000
LOG = logging.getLogger("prompt-studio.auth")


class AuthError(Exception):
    """可读的认证错误，直接展示给前端。"""


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    if not password:
        raise AuthError("口令不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([PBKDF2_ALGO, str(iterations), salt.hex(), digest.hex()])


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algo, iter_text, salt_hex, digest_hex = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        expected = bytes.fromhex(digest_hex)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_text)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(candidate, expected)


def password_strength_error(password: str) -> str | None:
    """与拓竹耗材管家一致的最低约束。"""
    if len(password or "") < 8:
        return "口令至少 8 位"
    if len(password) > 256:
        return "口令过长"
    if password.isdigit() or password.isalpha():
        return "口令不要只用纯数字或纯字母"
    return None


def is_private_host(host: str) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def is_https(headers, trust_proxy: bool) -> bool:
    if trust_proxy:
        return headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
    return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Auth:
    """单用户账号体系。状态存 DATA_DIR/auth.json（Unraid 挂载卷，升级镜像不丢）。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        app_password: str = "",
        app_username: str = "admin",
        session_ttl_days: int = 30,
        trust_proxy: bool = True,
        require_https: bool = False,
        allow_public_setup: bool = False,
        max_attempts: int = 5,
        lockout_minutes: int = 5,
    ):
        self.path = Path(data_dir) / "auth.json"
        self.app_username = (app_username or "admin").strip()[:64] or "admin"
        self.session_ttl_days = max(1, min(365, int(session_ttl_days)))
        self.trust_proxy = trust_proxy
        self.require_https = require_https
        self.allow_public_setup = allow_public_setup
        self.max_attempts = max(1, int(max_attempts))
        self.lockout_minutes = max(1, int(lockout_minutes))
        self._lock = threading.Lock()
        self._failures: dict[str, list] = {}
        self.state = self._load()
        if not self.initialized and app_password:
            error = password_strength_error(app_password)
            if error:
                LOG.warning("APP_PASSWORD 不合规（%s），已忽略；请在内网首次访问时自行设置", error)
            else:
                self._initialize(self.app_username, app_password)
                LOG.info("已按 APP_PASSWORD 初始化管理员账号")

    def _load(self) -> dict:
        default = {
            "username": self.app_username, "password_hash": "",
            "secret": "", "password_changed_at": 0.0, "sessions": {},
        }
        if not self.path.exists():
            return default
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                default.update({k: data[k] for k in default if k in data})
                if not isinstance(default["sessions"], dict):
                    default["sessions"] = {}
                return default
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("auth.json 读取失败，按未初始化处理: %s", exc)
        return default

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def initialized(self) -> bool:
        return bool(self.state.get("password_hash"))

    @property
    def username(self) -> str:
        return self.state.get("username") or self.app_username

    def status(self, peer_ip: str = "") -> dict:
        return {
            "initialized": self.initialized,
            "need_setup": not self.initialized,
            "setup_allowed": self.setup_allowed(peer_ip),
            "username": self.username,
            "session_ttl_days": self.session_ttl_days,
            "require_https": self.require_https,
        }

    def setup_allowed(self, peer_ip: str) -> bool:
        return self.allow_public_setup or is_private_host(peer_ip)

    def client_ip(self, headers, peer_ip: str) -> str:
        if self.trust_proxy:
            forwarded = headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip() or peer_ip
            real = headers.get("X-Real-IP", "").strip()
            if real:
                return real
        return peer_ip

    # ---------------------------------------------------------------- 限流
    def locked_seconds(self, ip: str) -> int:
        entry = self._failures.get(ip)
        if not entry:
            return 0
        remaining = int(entry[1] - time.time())
        return remaining if remaining > 0 else 0

    def record_failure(self, ip: str) -> None:
        now = time.time()
        count, _ = self._failures.get(ip) or [0, 0.0]
        count += 1
        locked_until = 0.0
        if count >= self.max_attempts:
            over = min(count - self.max_attempts, 5)
            locked_until = now + min(self.lockout_minutes * 60 * (2 ** over), 3600)
        self._failures[ip] = [count, locked_until]
        if len(self._failures) > 5000:
            self._failures = {k: v for k, v in self._failures.items() if v[1] > now}

    def clear_failures(self, ip: str) -> None:
        self._failures.pop(ip, None)

    # ---------------------------------------------------------------- 账号
    def _initialize(self, username: str, password: str) -> None:
        self.state["username"] = (username or "admin").strip()[:64] or "admin"
        self.state["password_hash"] = hash_password(password)
        self.state["secret"] = secrets.token_hex(32)
        self.state["password_changed_at"] = time.time()
        self.state["sessions"] = {}
        self._save()

    def setup(self, username: str, password: str, peer_ip: str, ip: str, ua: str) -> str:
        with self._lock:
            if self.initialized:
                raise AuthError("管理员已创建，请直接登录")
            if not self.setup_allowed(peer_ip):
                raise AuthError("首次创建管理员只允许在内网直连完成；公网部署请先用 APP_PASSWORD 预设口令")
            error = password_strength_error(password)
            if error:
                raise AuthError(error)
            self._initialize(username, password)
            return self._issue(ip, ua)

    def login(self, username: str, password: str, ip: str, ua: str) -> str:
        if not self.initialized:
            raise AuthError("尚未创建管理员账号")
        locked = self.locked_seconds(ip)
        if locked:
            raise AuthError(f"尝试过于频繁，请 {locked} 秒后重试")
        if (username or "").strip() != self.username or not verify_password(password, self.state.get("password_hash", "")):
            self.record_failure(ip)
            raise AuthError("账号或口令错误")
        self.clear_failures(ip)
        with self._lock:
            return self._issue(ip, ua)

    def _issue(self, ip: str, ua: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        sessions = self.state.setdefault("sessions", {})
        for key in [k for k, v in sessions.items() if v.get("expires", 0) <= now]:
            sessions.pop(key, None)
        sessions[_digest(token)] = {
            "created": now, "expires": now + self.session_ttl_days * 86400,
            "last_seen": now, "ip": ip, "ua": (ua or "")[:200],
        }
        self._save()
        return token

    def resolve(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            record = self.state.get("sessions", {}).get(_digest(token))
            if not record:
                return False
            now = time.time()
            if record.get("expires", 0) <= now:
                self.state["sessions"].pop(_digest(token), None)
                self._save()
                return False
            if now - record.get("last_seen", 0) > 3600:  # 滑动续期，每小时最多落盘一次
                record["last_seen"] = now
                record["expires"] = now + self.session_ttl_days * 86400
                self._save()
            return True

    def logout(self, token: str) -> None:
        with self._lock:
            if self.state.get("sessions", {}).pop(_digest(token), None) is not None:
                self._save()

    def change_password(self, old: str, new: str) -> None:
        if not verify_password(old, self.state.get("password_hash", "")):
            raise AuthError("原口令不正确")
        error = password_strength_error(new)
        if error:
            raise AuthError(error)
        with self._lock:
            self.state["password_hash"] = hash_password(new)
            self.state["password_changed_at"] = time.time()
            self.state["sessions"] = {}  # 改口令后所有旧会话失效
            self._save()

    # ---------------------------------------------------------------- Cookie
    def cookie(self, token: str, secure: bool, remember: bool = True) -> str:
        parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if remember:
            parts.append(f"Max-Age={self.session_ttl_days * 86400}")
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def clear_cookie(secure: bool = False) -> str:
        parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def token_from(headers) -> str:
        raw = headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == COOKIE_NAME:
                return value.strip()
        return ""
