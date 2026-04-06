from passlib.context import CryptContext
from jose import jwt
from datetime import datetime, timedelta
from fastapi import HTTPException, Header
import os

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = os.getenv("SECRET_KEY", "mediloop-secret-change-in-prod")
ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_token(pharmacy_id: str, pharmacy_name: str, expires_hours: int = 72) -> str:
    payload = {
        "pharmacy_id": pharmacy_id,
        "pharmacy_name": pharmacy_name,
        "exp": datetime.utcnow() + timedelta(hours=expires_hours)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def get_pharmacy_id(pharmacy_id: str = Header(...)) -> str:
    return pharmacy_id