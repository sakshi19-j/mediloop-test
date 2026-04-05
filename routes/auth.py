from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import supabase

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
def register(data: PharmacyRegister):
    # Check if email already exists
    existing = supabase.table("pharmacies")\
        .select("id")\
        .eq("email", data.email)\
        .execute()
    
    if existing.data:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    result = supabase.table("pharmacies").insert({
        "name": data.name,
        "phone": data.phone,
        "email": data.email,
        "password_hash": data.password  # hash this in production
    }).execute()
    
    pharmacy = result.data[0]
    return {
        "pharmacy_id": pharmacy["id"],
        "name": pharmacy["name"],
        "message": "Registered successfully"
    }

@router.post("/login")
def login(data: PharmacyLogin):
    result = supabase.table("pharmacies")\
        .select("*")\
        .eq("email", data.email)\
        .eq("password_hash", data.password)\
        .execute()
    
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    pharmacy = result.data[0]
    return {
        "pharmacy_id": pharmacy["id"],
        "name": pharmacy["name"],
        "message": "Login successful"
    }