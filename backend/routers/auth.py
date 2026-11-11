"""路由：用户注册、登录、当前用户信息。"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import User, get_db
from services.auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    now_iso,
    verify_password,
)


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PASSWORD_HAS_LETTER_RE = re.compile(r"[A-Za-z]")
PASSWORD_HAS_DIGIT_RE = re.compile(r"\d")


def validate_username(username: str) -> str:
    normalized = username.strip()
    if not USERNAME_RE.fullmatch(normalized):
        raise ValueError("用户名需为3-20位，只能包含英文字母、数字和下划线")
    return normalized


def validate_password(password: str) -> str:
    if len(password) < 8 or len(password) > 32:
        raise ValueError("密码需为8-32位")
    if any(ch.isspace() for ch in password):
        raise ValueError("密码不能包含空格")
    if not PASSWORD_HAS_LETTER_RE.search(password) or not PASSWORD_HAS_DIGIT_RE.search(password):
        raise ValueError("密码需同时包含字母和数字")
    return password


class AuthRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_must_match_rules(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("password")
    @classmethod
    def password_must_match_rules(cls, value: str) -> str:
        return validate_password(value)


def _user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "created_at": user.created_at,
    }


@router.post("/register")
def register(body: AuthRequest, db: Session = Depends(get_db)):
    username = body.username
    exists = db.query(User).filter(User.username == username).first()
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")

    now = now_iso()
    user = User(
        username=username,
        password_hash=hash_password(body.password),
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.query(User.id).filter(User.username == username).first():
            raise HTTPException(status_code=409, detail="用户名已存在") from None
        raise
    db.refresh(user)
    token = create_access_token(user)
    return {"code": 200, "message": "success", "data": {"token": token, "user": _user_payload(user)}}


@router.post("/login")
def login(body: AuthRequest, db: Session = Depends(get_db)):
    username = body.username.strip()
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user)
    return {"code": 200, "message": "success", "data": {"token": token, "user": _user_payload(user)}}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {"code": 200, "message": "success", "data": {"user": _user_payload(current_user)}}
