from datetime import datetime, timedelta, timezone

from django.conf import settings
from jose import JWTError, jwt
from ninja.security import HttpBearer

from desk.models import User


def hash_password(password: str) -> str:
    from passlib.context import CryptContext

    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    from passlib.context import CryptContext

    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    return ctx.verify(password, hashed)


def create_access_token(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(user.pk),
        "username": user.username,
        "role": user.role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


class BearerAuth(HttpBearer):
    def authenticate(self, request, token):
        payload = decode_token(token)
        if not payload:
            return None
        try:
            user = User.objects.get(pk=int(payload["sub"]))
        except (User.DoesNotExist, ValueError, KeyError):
            return None
        return user


bearer_auth = BearerAuth()
