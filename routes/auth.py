from fastapi import APIRouter, HTTPException
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
    # Check if email already exists
    existing = supabase.table("pharmacies")\
        .select("id")\
        .eq("email", body.email)\
        .execute()

    if existing.data:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Hash the password using Supabase Auth
    auth_response = supabase.auth.sign_up({
        "email": body.email,
        "password": body.password
    })

    if not auth_response.user:
        raise HTTPException(status_code=400, detail="Registration failed")

    # Insert pharmacy record
    result = supabase.table("pharmacies").insert({
        "name": body.name,
        "phone": body.phone,
        "email": body.email,
        "subscription_plan": "basic"
    }).execute()

    pharmacy = result.data[0]
    token = create_token(pharmacy["id"], pharmacy["name"])

    return {
        "token": token,
        "pharmacy_id": pharmacy["id"],
        "pharmacy_name": pharmacy["name"],
        "message": "Registered successfully"
    }

@router.post("/login")
def login(body: PharmacyLogin):
    # Authenticate via Supabase Auth
    auth_response = supabase.auth.sign_in_with_password({
        "email": body.email,
        "password": body.password
    })

    if not auth_response.user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Fetch pharmacy record
    result = supabase.table("pharmacies")\
        .select("*")\
        .eq("email", body.email)\
        .execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Pharmacy not found")

    pharmacy = result.data[0]
    token = create_token(pharmacy["id"], pharmacy["name"])

    return {
        "token": token,
        "pharmacy_id": pharmacy["id"],
        "pharmacy_name": pharmacy["name"],
        "subscription_plan": pharmacy["subscription_plan"]
    }

@router.get("/me")
def get_me(pharmacy_id: str = None, authorization: str = None):
    # Accept either header style for flexibility
    from fastapi import Header
    raise HTTPException(status_code=400, detail="Use /auth/verify instead")

@router.post("/verify")
def verify_token(authorization: str):
    token = authorization.replace("Bearer ", "")
    payload = decode_token(token)
    return {
        "pharmacy_id": payload["pharmacy_id"],
        "pharmacy_name": payload["pharmacy_name"]
    }