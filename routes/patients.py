from fastapi import APIRouter, HTTPException, Header, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from database import supabase
from typing import Optional
from datetime import datetime
import csv
import io

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
        sub = supabase.table("subscriptions")\
            .select("status, plan")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if sub.data and sub.data[0]["status"] == "expired":
            raise HTTPException(
                status_code=402,
                detail="Subscription expired. Please renew to continue."
            )

        # Normalize phone
        normalized_phone = patient.phone.strip()

        # ── Duplicate phone check ────────────────────────────────────────────
        # If this phone already belongs to a patient in this pharmacy,
        # return their record instead of creating a duplicate.
        # The frontend should detect `already_exists: true` and redirect
        # to the existing patient's profile page.
        existing = supabase.table("patients")\
            .select("*")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("phone", normalized_phone)\
            .eq("is_deleted", False)\
            .execute()

        if existing.data:
            return {
                **existing.data[0],
                "already_exists": True,
                "message": (
                    f"A patient with phone {normalized_phone} already exists. "
                    "Any new medicines will be added to their existing profile."
                )
            }

        sub_limit = supabase.table("subscriptions")\
            .select("patient_limit")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if sub_limit.data:
            limit = sub_limit.data[0]["patient_limit"]
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
            "phone": normalized_phone,
            "disease_type": patient.disease_type,
            "notes": patient.notes
        }).execute()

        return {**result.data[0], "already_exists": False}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# IMPORTANT: /export, /import, /lookup must come BEFORE /{patient_id}
# FastAPI matches routes top-down — a literal path must beat a wildcard

@router.get("/export")
def export_patients_csv(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("patients")\
            .select("name, phone, disease_type, notes, created_at")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("is_deleted", False)\
            .execute()

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["name", "phone", "disease_type", "notes", "created_at"]
        )
        writer.writeheader()
        writer.writerows(result.data)
        output.seek(0)

        return StreamingResponse(
            io.BytesIO(output.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=patients_export.csv"}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/import")
async def import_patients_csv(
    file: UploadFile = File(...),
    pharmacy_id: str = Header(...)
):
    try:
        content = await file.read()
        decoded = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(decoded))

        patients_added = []
        skipped_duplicates = []
        errors = []

        for i, row in enumerate(reader):
            try:
                phone = row.get("phone", "").strip()

                # Skip rows whose phone already exists in this pharmacy
                existing = supabase.table("patients")\
                    .select("id, name")\
                    .eq("pharmacy_id", pharmacy_id)\
                    .eq("phone", phone)\
                    .eq("is_deleted", False)\
                    .execute()

                if existing.data:
                    skipped_duplicates.append({
                        "row": i + 2,
                        "name": row.get("name"),
                        "phone": phone,
                        "existing_patient_id": existing.data[0]["id"],
                        "existing_name": existing.data[0]["name"]
                    })
                    continue

                result = supabase.table("patients").insert({
                    "pharmacy_id": pharmacy_id,
                    "name": row.get("name", "").strip(),
                    "phone": phone,
                    "disease_type": row.get("disease_type", "Other").strip(),
                    "notes": row.get("notes", "").strip()
                }).execute()
                patients_added.append(result.data[0])

            except Exception as e:
                errors.append({"row": i + 2, "name": row.get("name"), "error": str(e)})

        return {
            "imported": len(patients_added),
            "skipped_duplicates": len(skipped_duplicates),
            "failed": len(errors),
            "duplicates": skipped_duplicates,
            "errors": errors
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/opt-out")
def handle_opt_out(phone: str):
    try:
        supabase.table("patients")\
            .update({
                "opted_out": True,
                "opted_out_at": datetime.utcnow().isoformat()
            })\
            .eq("phone", phone)\
            .execute()

        supabase.table("opt_outs").upsert({"phone": phone}).execute()
        return {"message": "Opted out successfully"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lookup")
def lookup_by_phone(phone: str, pharmacy_id: str = Header(...)):
    """
    Look up a patient by phone number for this pharmacy.
    Used by the frontend to navigate to the existing patient profile
    when add_patient returns already_exists: true.
    """
    try:
        result = supabase.table("patients")\
            .select("*")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("phone", phone.strip())\
            .eq("is_deleted", False)\
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="No patient found with this phone number")

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

        # search pushed into Supabase — works across all patients, not just current page
        if search:
            query = query.or_(f"name.ilike.%{search}%,phone.ilike.%{search}%")

        offset = (page - 1) * limit
        result = query.order("name").range(offset, offset + limit - 1).execute()

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

        # If phone is being changed, ensure it's not taken by another patient
        if "phone" in updates:
            updates["phone"] = updates["phone"].strip()
            conflict = supabase.table("patients")\
                .select("id")\
                .eq("pharmacy_id", pharmacy_id)\
                .eq("phone", updates["phone"])\
                .eq("is_deleted", False)\
                .neq("id", patient_id)\
                .execute()

            if conflict.data:
                raise HTTPException(
                    status_code=409,
                    detail=f"Phone {updates['phone']} is already registered to another patient."
                )

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