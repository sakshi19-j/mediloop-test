from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from database import supabase

router = APIRouter()

class PatientCreate(BaseModel):
    name: str
    phone: str
    disease_type: str

@router.post("/")
def add_patient(patient: PatientCreate, pharmacy_id: str = Header(...)):
    result = supabase.table("patients").insert({
        "pharmacy_id": pharmacy_id,
        "name": patient.name,
        "phone": patient.phone,
        "disease_type": patient.disease_type
    }).execute()
    return result.data[0]

@router.get("/")
def get_patients(pharmacy_id: str = Header(...)):
    result = supabase.table("patients")\
        .select("*")\
        .eq("pharmacy_id", pharmacy_id)\
        .eq("is_active", True)\
        .execute()
    return result.data

@router.delete("/{patient_id}")
def delete_patient(patient_id: str, pharmacy_id: str = Header(...)):
    supabase.table("patients")\
        .update({"is_active": False})\
        .eq("id", patient_id)\
        .eq("pharmacy_id", pharmacy_id)\
        .execute()
    return {"message": "Patient deactivated"}