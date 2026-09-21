from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import smtplib
import ssl
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException, Request, Response

from .config import PublicConfig
from .db import Database


HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
COOKIE = "p2h_session"
CSRF_COOKIE = "p2h_csrf"
LOGGER = logging.getLogger(__name__)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def email_address(value: str) -> str:
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise HTTPException(422, "邮箱格式不正确") from exc


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise HTTPException(422, "密码长度需要为 12–128 个字符")
    return HASHER.hash(password)


def password_matches(hashed: str, password: str) -> bool:
    try:
        return HASHER.verify(hashed, password)
    except (VerificationError, ValueError):
        return False


class Auth:
    def __init__(self, db: Database, config: PublicConfig):
        self.db, self.config = db, config

    def captcha(self, token: str, ip: str):
        if self.config.development:
            return
        if not token or len(token) > 2048:
            raise HTTPException(400, "请完成人机验证")
        data = urllib.parse.urlencode(
            {"secret": self.config.turnstile_secret, "response": token, "remoteip": ip}
        ).encode()
        request = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            # Only the fixed HTTPS verification endpoint above is reachable.
            with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
                result = json.loads(response.read(16384))
        except Exception as exc:
            raise HTTPException(503, "验证服务暂不可用，请稍后重试") from exc
        if (
            not result.get("success")
            or result.get("hostname")
            != urllib.parse.urlsplit(self.config.origin).hostname
        ):
            raise HTTPException(400, "人机验证失败，请重试")

    def send_token(self, user_id: str, email: str, kind: str):
        token = secrets.token_urlsafe(32)
        ttl = 86400 if kind == "verify" else 1800
        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM auth_tokens WHERE user_id=%s AND kind=%s", (user_id, kind)
            )
            conn.execute(
                "INSERT INTO auth_tokens VALUES (%s,%s,%s,now()+%s*interval '1 second')",
                (digest(token), user_id, kind, ttl),
            )
        # Fragment keeps the one-use token out of HTTP access logs and Referer.
        link = f"{self.config.origin}/#{kind}={token}"
        if self.config.development:
            # Development mail is retrievable only by the operator through the CLI.
            folder = self.config.data_dir / "dev-mail"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / f"{user_id}-{kind}.txt"
            path.touch(mode=0o600, exist_ok=True)
            path.write_text(link, encoding="utf-8")
            return
        mail = EmailMessage()
        mail["Subject"] = (
            "验证邮箱 · 题包转换公益站"
            if kind == "verify"
            else "重置密码 · 题包转换公益站"
        )
        mail["From"], mail["To"] = self.config.smtp_from, email
        mail.set_content(
            f"请打开以下链接继续操作：\n{link}\n\n链接有效期：{ttl // 60} 分钟。若不是您操作，请忽略。"
        )
        try:
            context = ssl.create_default_context()
            if self.config.smtp_port == 465:
                server = smtplib.SMTP_SSL(
                    self.config.smtp_host,
                    self.config.smtp_port,
                    timeout=15,
                    context=context,
                )
            else:
                server = smtplib.SMTP(
                    self.config.smtp_host, self.config.smtp_port, timeout=15
                )
                server.starttls(context=context)
            with server:
                if self.config.smtp_user:
                    server.login(self.config.smtp_user, self.config.smtp_password)
                server.send_message(mail)
        except Exception:
            # Avoid leaking account existence or SMTP credentials into responses/logs.
            LOGGER.error("authentication_email_delivery_failed")

    def register(self, email: str, password: str, invitation: str):
        email = email_address(email)
        hashed = password_hash(password)
        uid = uuid.uuid4().hex
        with self.db.connect() as conn:
            if not conn.execute(
                "SELECT registration_open FROM site_settings WHERE id=1"
            ).fetchone()["registration_open"]:
                raise HTTPException(503, "目前暂停注册")
            conn.execute("SELECT pg_advisory_xact_lock(728603)")
            invite = conn.execute(
                "SELECT id FROM invitations WHERE token_hash=%s AND NOT revoked AND used_at IS NULL AND expires_at>now() FOR UPDATE",
                (digest(invitation),),
            ).fetchone()
            if (
                not invite
                or conn.execute(
                    "SELECT id FROM users WHERE email=%s", (email,)
                ).fetchone()
            ):
                return
            conn.execute(
                "INSERT INTO users(id,email,password_hash) VALUES (%s,%s,%s)",
                (uid, email, hashed),
            )
            conn.execute(
                "UPDATE invitations SET used_by=%s,used_at=now() WHERE id=%s",
                (uid, invite["id"]),
            )
        self.send_token(uid, email, "verify")

    def request_email(self, email: str, kind: str):
        email = email_address(email)
        row = self.db.one(
            "SELECT id,email,verified FROM users WHERE email=%s AND NOT banned",
            (email,),
        )
        if row and (kind == "reset" or not row["verified"]):
            self.send_token(row["id"], row["email"], kind)

    def consume_token(self, token: str, kind: str, password: str | None = None):
        hashed = password_hash(password) if password is not None else None
        with self.db.connect() as conn:
            row = conn.execute(
                "DELETE FROM auth_tokens WHERE token_hash=%s AND kind=%s AND expires_at>now() RETURNING user_id",
                (digest(token), kind),
            ).fetchone()
            if not row:
                raise HTTPException(400, "链接已失效或已使用")
            if kind == "verify":
                conn.execute(
                    "UPDATE users SET verified=true WHERE id=%s", (row["user_id"],)
                )
            else:
                conn.execute(
                    "UPDATE users SET password_hash=%s WHERE id=%s",
                    (hashed, row["user_id"]),
                )
                conn.execute("DELETE FROM sessions WHERE user_id=%s", (row["user_id"],))

    def login(self, email: str, password: str, response: Response):
        row = self.db.one("SELECT * FROM users WHERE email=%s", (email_address(email),))
        valid = password_matches(row["password_hash"] if row else DUMMY_HASH, password)
        if not row or not valid or row["banned"]:
            raise HTTPException(401, "邮箱或密码错误")
        if not row["verified"]:
            raise HTTPException(403, "请先验证邮箱，可在登录页面重新发送邮件")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO sessions VALUES (%s,%s,%s,now()+%s*interval '1 second')",
            (digest(token), row["id"], digest(csrf), self.config.session_seconds),
        )
        secure = not self.config.development
        response.set_cookie(
            COOKIE,
            token,
            max_age=self.config.session_seconds,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=self.config.session_seconds,
            secure=secure,
            samesite="lax",
        )
        return row

    def current(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        row = self.db.one(
            "SELECT u.*,s.csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires_at>now() AND u.verified AND NOT u.banned",
            (digest(token),),
        )
        if not row:
            raise HTTPException(401, "请登录")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(
                row["csrf_hash"], digest(request.headers.get("x-csrf-token", ""))
            ):
                raise HTTPException(403, "页面验证已失效，请刷新后重试")
        return row

    def logout(self, request: Request, response: Response):
        self.db.execute(
            "DELETE FROM sessions WHERE token_hash=%s",
            (digest(request.cookies.get(COOKIE, "")),),
        )
        response.delete_cookie(COOKIE)
        response.delete_cookie(CSRF_COOKIE)

    def change_password(self, user, current: str, new: str):
        if not password_matches(user["password_hash"], current):
            raise HTTPException(400, "当前密码错误")
        hashed = password_hash(new)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash=%s WHERE id=%s", (hashed, user["id"])
            )
            conn.execute("DELETE FROM sessions WHERE user_id=%s", (user["id"],))
            conn.execute("DELETE FROM auth_tokens WHERE user_id=%s", (user["id"],))
        self.db.audit(user["id"], "password_changed")

    def invitations(self, count: int):
        codes = [secrets.token_urlsafe(18) for _ in range(count)]
        with self.db.connect() as conn:
            for code in codes:
                conn.execute(
                    "INSERT INTO invitations(id,token_hash) VALUES (%s,%s)",
                    (uuid.uuid4().hex, digest(code)),
                )
        return codes
