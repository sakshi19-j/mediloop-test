from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from database import supabase
from datetime import datetime, timedelta
from jose import jwt
import os

router = APIRouter()

SECRET_KEY = os.getenv("SECRET_KEY", "mediloop-secret-change-in-prod")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 72

class PharmacyRegister(BaseModel):
    name: str
    phone: str
    email: str
    password: str

class PharmacyLogin(BaseModel):
    email: str
    password: str

def create_token(pharmacy_id: str, pharmacy_name: str) -> str:
    payload = {
        "pharmacy_id": pharmacy_id,
        "pharmacy_name": pharmacy_name,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

@router.post("/register")
def register(body: PharmacyRegister):
    try:
        # Check if email already exists in pharmacies table
        existing = supabase.table("pharmacies")\
            .select("id")\
            .eq("email", body.email)\
            .execute()

        if existing.data:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Insert pharmacy record directly — no Supabase Auth dependency
        result = supabase.table("pharmacies").insert({
            "name": body.name,
            "phone": body.phone,
            "email": body.email,
            "password_hash": body.password,  # plain for MVP, hash later
            "subscription_plan": "basic"
        }).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create pharmacy record")

        pharmacy = result.data[0]
        token = create_token(pharmacy["id"], pharmacy["name"])

        return {
            "token": token,
            "pharmacy_id": pharmacy["id"],
            "pharmacy_name": pharmacy["name"],
            "message": "Registered successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/login")
def login(body: PharmacyLogin):
    try:
        result = supabase.table("pharmacies")\
            .select("*")\
            .eq("email", body.email)\
            .eq("password_hash", body.password)\
            .execute()

        if not result.data:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        pharmacy = result.data[0]
        token = create_token(pharmacy["id"], pharmacy["name"])

        return {
            "token": token,
            "pharmacy_id": pharmacy["id"],
            "pharmacy_name": pharmacy["name"],
            "subscription_plan": pharmacy["subscription_plan"]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/verify")
def verify_token(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    payload = decode_token(token)
    return {
        "pharmacy_id": payload["pharmacy_id"],
        "pharmacy_name": payload["pharmacy_name"]
    }