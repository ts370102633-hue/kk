from fastapi import Request
from .config import get_settings

settings = get_settings()


def request_identity(request: Request) -> dict:
    return {
        "email": request.headers.get("x-user-email", settings.default_user_email),
        "name": request.headers.get("x-user-name", settings.default_user_name),
    }


def get_or_create_user(db, email, name):
    from .models import User
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, name=name)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
