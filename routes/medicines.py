from fastapi import APIRouter, Header
from pydantic import BaseModel
from database import supabase
from datetime import date, timedelta

router = APIRouter()

class MedicineCreate(BaseModel):
    patient_id: str
    name: str
    dosage: str
    refill_days: int
    last_purchase_date: date

class PurchaseUpdate(BaseModel):
    last_purchase_date: date

@router.post("/")
def add_medicine(medicine: MedicineCreate, pharmacy_id: str = Header(...)):
    result = supabase.table("medicines").insert({
        "pharmacy_id": pharmacy_id,
        "patient_id": medicine.patient_id,
        "name": medicine.name,
        "dosage": medicine.dosage,
        "refill_days": medicine.refill_days,
        "last_purchase_date": medicine.last_purchase_date.isoformat()
    }).execute()
    return result.data[0]

@router.get("/")
def get_all_medicines(pharmacy_id: str = Header(...)):
    result = supabase.table("medicines")\
        .select("*, patients(name, phone)")\
        .eq("pharmacy_id", pharmacy_id)\
        .eq("status", "active")\
        .order("next_due_date")\
        .execute()
    return result.data

@router.get("/upcoming")
def get_upcoming(pharmacy_id: str = Header(...)):
    in_30_days = (date.today() + timedelta(days=30)).isoformat()
    today = date.today().isoformat()

    result = supabase.table("medicines")\
        .select("*, patients(name, phone)")\
        .eq("pharmacy_id", pharmacy_id)\
        .eq("status", "active")\
        .gte("next_due_date", today)\
        .lte("next_due_date", in_30_days)\
        .order("next_due_date")\
        .execute()
    return result.data

@router.patch("/{medicine_id}/purchased")
def mark_purchased(medicine_id: str, body: PurchaseUpdate, pharmacy_id: str = Header(...)):
    result = supabase.table("medicines")\
        .update({"last_purchase_date": body.last_purchase_date.isoformat()})\
        .eq("id", medicine_id)\
        .eq("pharmacy_id", pharmacy_id)\
        .execute()

    supabase.table("reminder_logs")\
        .update({"converted": True, "purchased_at": body.last_purchase_date.isoformat()})\
        .eq("medicine_id", medicine_id)\
        .eq("converted", False)\
        .execute()

    return result.data[0]