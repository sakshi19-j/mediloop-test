from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from database import supabase
from typing import Optional

router = APIRouter()

class PatientCreate(BaseModel):
    name: str
    phone: str
    disease_type: str
    notes: Optional[str] = None

class PatientUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    disease_type: Optional[str] = None
    notes: Optional[str] = None

@router.post("/")
def add_patient(patient: PatientCreate, pharmacy_id: str = Header(...)):
    try:
        # Check plan limit
        sub = supabase.table("subscriptions")\
            .select("patient_limit")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if sub.data:
            limit = sub.data[0]["patient_limit"]
            count = supabase.table("patients")\
                .select("id", count="exact")\
                .eq("pharmacy_id", pharmacy_id)\
                .eq("is_active", True)\
                .eq("is_deleted", False)\
                .execute()
            if count.count >= limit:
                raise HTTPException(
                    status_code=403,
                    detail=f"Patient limit reached ({limit}). Please upgrade your plan."
                )

        result = supabase.table("patients").insert({
            "pharmacy_id": pharmacy_id,
            "name": patient.name,
            "phone": patient.phone,
            "disease_type": patient.disease_type,
            "notes": patient.notes
        }).execute()

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/")
def get_patients(
    pharmacy_id: str = Header(...),
    search: Optional[str] = None,
    disease_type: Optional[str] = None,
    page: int = 1,
    limit: int = 20
):
    try:
        query = supabase.table("patients")\
            .select("*")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("is_active", True)\
            .eq("is_deleted", False)

        if disease_type:
            query = query.eq("disease_type", disease_type)

        offset = (page - 1) * limit
        result = query.order("name").range(offset, offset + limit - 1).execute()

        if search:
            search_lower = search.lower()
            filtered = [p for p in result.data if
                search_lower in p["name"].lower() or
                search_lower in p["phone"]
            ]
            return {"data": filtered, "page": page, "limit": limit}

        return {"data": result.data, "page": page, "limit": limit}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{patient_id}")
def get_patient_profile(patient_id: str, pharmacy_id: str = Header(...)):
    try:
        patient = supabase.table("patients")\
            .select("*")\
            .eq("id", patient_id)\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if not patient.data:
            raise HTTPException(status_code=404, detail="Patient not found")

        medicines = supabase.table("medicines")\
            .select("*")\
            .eq("patient_id", patient_id)\
            .eq("is_deleted", False)\
            .order("next_due_date")\
            .execute()

        reminders = supabase.table("reminder_logs")\
            .select("*")\
            .eq("patient_id", patient_id)\
            .order("sent_at", desc=True)\
            .limit(20)\
            .execute()

        return {
            "patient": patient.data[0],
            "medicines": medicines.data,
            "reminder_history": reminders.data
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/{patient_id}")
def update_patient(patient_id: str, body: PatientUpdate, pharmacy_id: str = Header(...)):
    try:
        updates = {k: v for k, v in body.dict().items() if v is not None}
        updates["updated_at"] = "now()"

        result = supabase.table("patients")\
            .update(updates)\
            .eq("id", patient_id)\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Patient not found")

        return result.data[0]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{patient_id}")
def delete_patient(patient_id: str, pharmacy_id: str = Header(...)):
    try:
        supabase.table("patients")\
            .update({"is_deleted": True, "is_active": False})\
            .eq("id", patient_id)\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        supabase.table("medicines")\
            .update({"is_deleted": True, "status": "inactive"})\
            .eq("patient_id", patient_id)\
            .execute()

        return {"message": "Patient deleted successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/opt-out")
def handle_opt_out(phone: str):
    """Called when patient replies STOP to WhatsApp"""
    try:
        # Mark all patients with this phone as opted out
        supabase.table("patients")\
            .update({
                "opted_out": True,
                "opted_out_at": datetime.utcnow().isoformat()
            })\
            .eq("phone", phone)\
            .execute()

        # Log in opt_outs table
        supabase.table("opt_outs").upsert({
            "phone": phone
        }).execute()

        return {"message": "Opted out successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))