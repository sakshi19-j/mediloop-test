from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from database import supabase
from auth_utils import hash_password, verify_password, create_token, decode_token

router = APIRouter()

class PharmacyRegister(BaseModel):
    name: str
    phone: str
    email: str
    password: str

class PharmacyLogin(BaseModel):
    email: str
    password: str

@router.post("/register")
def register(body: PharmacyRegister):
    try:
        # Password length check
        if len(body.password.encode('utf-8')) > 72:
            raise HTTPException(
                status_code=400,
                detail="Password too long. Please use under 72 characters."
            )

        existing = supabase.table("pharmacies")\
            .select("id")\
            .eq("email", body.email)\
            .execute()

        if existing.data:
            raise HTTPException(status_code=400, detail="Email already registered")

        hashed = hash_password(body.password)

        result = supabase.table("pharmacies").insert({
            "name": body.name,
            "phone": body.phone,
            "email": body.email,
            "password_hash": hashed,
            "subscription_plan": "trial"
        }).execute()

        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create account")

        pharmacy = result.data[0]

        # Create trial subscription
        supabase.table("subscriptions").insert({
            "pharmacy_id": pharmacy["id"],
            "plan": "trial",
            "status": "active",
            "patient_limit": 20
        }).execute()

        token = create_token(pharmacy["id"], pharmacy["name"])

        return {
            "token": token,
            "pharmacy_id": pharmacy["id"],
            "pharmacy_name": pharmacy["name"],
            "plan": "trial",
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
            .execute()

        if not result.data:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        pharmacy = result.data[0]

        if not verify_password(body.password, pharmacy["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

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
def verify_token(authorization: str):
    token = authorization.replace("Bearer ", "")
    payload = decode_token(token)
    return {
        "pharmacy_id": payload["pharmacy_id"],
        "pharmacy_name": payload["pharmacy_name"]
    }