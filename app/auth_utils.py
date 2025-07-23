from fastapi import Request, HTTPException, Depends
from starlette.status import HTTP_302_FOUND
from passlib.context import CryptContext
from fastapi import Request
from itsdangerous import URLSafeSerializer
import os
from dotenv import load_dotenv

load_dotenv()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
secret_key = os.getenv("SECRET_KEY")
serializer = URLSafeSerializer(secret_key)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_session_cookie(username: str) -> str:
    return serializer.dumps({"username": username})

def decode_session_cookie(cookie: str) -> str | None:
    try:
        data = serializer.loads(cookie)
        return data.get("username")
    except Exception:
        return None

def get_current_user(request: Request):
    cookie = request.cookies.get("session")
    if not cookie:
        return None
    return decode_session_cookie(cookie)

def is_password_complex(password: str) -> bool:
    """Check if password meets complexity requirements:
       - At least 8 characters
       - One uppercase letter
       - One digit
       - One symbol from @$!%*?&
    """
    if len(password) < 8:
        return False

    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(c in "@$!%*?&" for c in password)

    return has_upper and has_digit and has_symbol

async def auth_required(request: Request):
    user = get_current_user(request)
    if not user:
        # Raise exception or redirect
        raise HTTPException(status_code=HTTP_302_FOUND, detail="Redirect", headers={"Location": "/welcome"})
    return user

def split_sentences(text):
    sentences = []
    current_sentence = ""
    for char in text:
        if char.isdigit() and not current_sentence.endswith("."):
            if current_sentence.strip():
                sentences.append(current_sentence.strip())
            current_sentence = ""
        else:
            current_sentence += char
    if current_sentence.strip():
        sentences.append(current_sentence.strip())
    return sentences
