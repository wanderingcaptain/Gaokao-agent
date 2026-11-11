"""认证服务：密码哈希、JWT 签发与当前用户解析。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database.models import User, get_db


CST = timezone(timedelta(hours=8))
security = HTTPBearer(auto_error=False)
_LOCAL_SECRET = secrets.token_bytes(48)


def now_iso() -> str:
    return datetime.now(CST).isoformat()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256$120000${_b64url_encode(salt)}${_b64url_encode(digest)}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, rounds_text, salt_text, digest_text = password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = _b64url_decode(salt_text)
        expected = _b64url_decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _jwt_secret() -> bytes:
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if secret in {"", "dev-only-change-me", "please_change_this_secret"}:
        return _LOCAL_SECRET
    return secret.encode("utf-8")


def create_access_token(user: User) -> str:
    expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
    exp = int((datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)).timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": str(user.id), "username": user.username, "exp": exp}
    signing_input = ".".join([
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
    ])
    signature = hmac.new(_jwt_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_access_token(token: str) -> dict:
    try:
        header_text, payload_text, signature_text = token.split(".")
        signing_input = f"{header_text}.{payload_text}"
        expected = hmac.new(_jwt_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
        actual = _b64url_decode(signature_text)
        if not hmac.compare_digest(actual, expected):
            raise ValueError("invalid signature")
        payload = json.loads(_b64url_decode(payload_text).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("expired token")
        return payload
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已失效，请重新登录",
        )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
        )
    payload = decode_access_token(credentials.credentials)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在，请重新登录",
        )
    return user
