from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from database import supabase
from typing import Optional
from datetime import datetime
from audit import log_audit
from logger_config import get_logger
import csv
import io

router = APIRouter()
logger = get_logger(__name__)

INVALID_PLACEHOLDERS = {
    "string", "test", "none", "null", "na", "n/a", "undefined",
    "placeholder", "example", "sample", "demo", "unknown"
}


class PatientCreate(BaseModel):
    name: str
    phone: str
    disease_type: str
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty")
        if len(v) < 2:
            raise ValueError("Name must be at least 2 characters")
        if v.lower() in INVALID_PLACEHOLDERS:
            raise ValueError(f"'{v}' is not a valid patient name")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v):
        v = v.strip().lstrip("+").replace(" ", "").replace("-", "")
        digits = v.lstrip("91") if v.startswith("91") else v
        if not digits.isdigit():
            raise ValueError("Phone number must contain only digits")
        if len(digits) != 10:
            raise ValueError("Phone number must be 10 digits")
        return v

    @field_validator("disease_type")
    @classmethod
    def validate_disease_type(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Disease type cannot be empty")
        if v.lower() in INVALID_PLACEHOLDERS:
            raise ValueError(f"'{v}' is not a valid disease type")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v):
        if v is None:
            return v
        v = v.strip()
        if v.lower() in INVALID_PLACEHOLDERS:
            return None
        return v or None


class PatientUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    disease_type: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if v is None:
            return v
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Name must be at least 2 characters")
        if v.lower() in INVALID_PLACEHOLDERS:
            raise ValueError(f"'{v}' is not a valid patient name")
        return v

    @field_validator("disease_type")
    @classmethod
    def validate_disease_type(cls, v):
        if v is None:
            return v
        v = v.strip()
        if v.lower() in INVALID_PLACEHOLDERS:
            raise ValueError(f"'{v}' is not a valid disease type")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, v):
        if v is None:
            return v
        v = v.strip()
        if v.lower() in INVALID_PLACEHOLDERS:
            return None
        return v or None


def normalize_phone(phone: str) -> str:
    phone = phone.strip().lstrip("+").replace(" ", "").replace("-", "")
    if not phone.startswith("91") and len(phone) == 10:
        phone = f"91{phone}"
    return phone


@router.post("/")
async def add_patient(patient: PatientCreate, request: Request, pharmacy_id: str = Header(...)):
    try:
        sub = supabase.table("subscriptions")\
            .select("status, plan")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if sub.data and sub.data[0]["status"] == "expired":
            raise HTTPException(status_code=402, detail="Subscription expired. Please renew to continue.")

        normalized_phone = normalize_phone(patient.phone)

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
                raise HTTPException(status_code=403, detail=f"Patient limit reached ({limit}). Please upgrade your plan.")

        result = supabase.table("patients").insert({
            "pharmacy_id": pharmacy_id,
            "name": patient.name,
            "phone": normalized_phone,
            "disease_type": patient.disease_type,
            "notes": patient.notes,
            "consent_given": False,
            "consent_requested_at": datetime.utcnow().isoformat()
        }).execute()

        new_patient = result.data[0]

        # ── Audit log ──────────────────────────────────────────────────────
        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="created",
            entity_type="patient",
            entity_id=new_patient["id"],
            new_data={"name": patient.name, "disease_type": patient.disease_type},
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Patient created", extra={
            "pharmacy_id": pharmacy_id,
            "patient_id": new_patient["id"],
            "patient_name": patient.name,
        })

        pharmacy = supabase.table("pharmacies")\
            .select("name")\
            .eq("id", pharmacy_id)\
            .execute()
        pharmacy_name = pharmacy.data[0]["name"] if pharmacy.data else "Your Pharmacy"

        from whatsapp import send_consent_request
        await send_consent_request(
            phone=normalized_phone,
            patient_name=patient.name,
            pharmacy_name=pharmacy_name
        )

        return {**new_patient, "already_exists": False, "consent_requested": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Patient create failed", extra={"pharmacy_id": pharmacy_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export")
def export_patients_csv(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("patients")\
            .select("name, phone, disease_type, notes, created_at")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("is_deleted", False)\
            .execute()

        log_audit(pharmacy_id=pharmacy_id, actor_id=pharmacy_id,
                  action="exported", entity_type="patient", entity_id="all")

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name", "phone", "disease_type", "notes", "created_at"])
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
async def import_patients_csv(file: UploadFile = File(...), pharmacy_id: str = Header(...)):
    try:
        content = await file.read()
        decoded = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(decoded))

        patients_added = []
        skipped_duplicates = []
        errors = []

        pharmacy = supabase.table("pharmacies").select("name").eq("id", pharmacy_id).execute()
        pharmacy_name = pharmacy.data[0]["name"] if pharmacy.data else "Your Pharmacy"

        for i, row in enumerate(reader):
            try:
                phone = normalize_phone(row.get("phone", ""))
                name = row.get("name", "").strip()
                disease_type = row.get("disease_type", "Other").strip()
                notes_raw = row.get("notes", "").strip()

                if not name or name.lower() in INVALID_PLACEHOLDERS:
                    errors.append({"row": i + 2, "name": name, "error": "Invalid or placeholder name"})
                    continue

                notes = None if notes_raw.lower() in INVALID_PLACEHOLDERS else (notes_raw or None)

                existing = supabase.table("patients")\
                    .select("id, name")\
                    .eq("pharmacy_id", pharmacy_id)\
                    .eq("phone", phone)\
                    .eq("is_deleted", False)\
                    .execute()

                if existing.data:
                    skipped_duplicates.append({
                        "row": i + 2, "name": name, "phone": phone,
                        "existing_patient_id": existing.data[0]["id"],
                        "existing_name": existing.data[0]["name"]
                    })
                    continue

                result = supabase.table("patients").insert({
                    "pharmacy_id": pharmacy_id,
                    "name": name,
                    "phone": phone,
                    "disease_type": disease_type,
                    "notes": notes,
                    "consent_given": False,
                    "consent_requested_at": datetime.utcnow().isoformat()
                }).execute()
                new_patient = result.data[0]
                patients_added.append(new_patient)

                log_audit(pharmacy_id=pharmacy_id, actor_id=pharmacy_id,
                          action="created", entity_type="patient",
                          entity_id=new_patient["id"],
                          new_data={"name": name, "source": "csv_import"})

                from whatsapp import send_consent_request
                await send_consent_request(phone=phone, patient_name=name, pharmacy_name=pharmacy_name)

            except Exception as e:
                errors.append({"row": i + 2, "name": row.get("name"), "error": str(e)})

        logger.info("CSV import completed", extra={
            "pharmacy_id": pharmacy_id,
            "imported": len(patients_added),
            "skipped": len(skipped_duplicates),
            "errors": len(errors),
        })

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
        normalized = normalize_phone(phone)
        supabase.table("patients")\
            .update({"opted_out": True, "opted_out_at": datetime.utcnow().isoformat()})\
            .eq("phone", normalized)\
            .execute()
        supabase.table("opt_outs").upsert({"phone": normalized}).execute()
        return {"message": "Opted out successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lookup")
def lookup_by_phone(phone: str, pharmacy_id: str = Header(...)):
    try:
        normalized = normalize_phone(phone)
        result = supabase.table("patients")\
            .select("*")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("phone", normalized)\
            .eq("is_deleted", False)\
            .execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="No patient found with this phone number")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/family/{phone}")
def get_family_by_phone(phone: str, pharmacy_id: str = Header(...)):
    try:
        normalized = normalize_phone(phone)
        phone_variants = [normalized]
        short = normalized.lstrip("91") if normalized.startswith("91") else normalized
        if short != normalized:
            phone_variants.append(short)

        all_members = []
        seen_ids = set()

        for p in phone_variants:
            result = supabase.table("patients")\
                .select("id, name, disease_type, consent_given, opted_out, created_at")\
                .eq("pharmacy_id", pharmacy_id)\
                .eq("phone", p)\
                .eq("is_deleted", False)\
                .eq("is_active", True)\
                .execute()
            for pat in (result.data or []):
                if pat["id"] not in seen_ids:
                    seen_ids.add(pat["id"])
                    all_members.append(pat)

        if not all_members:
            raise HTTPException(status_code=404, detail="No patients found for this phone number")

        return {
            "phone": normalized,
            "is_family": len(all_members) > 1,
            "member_count": len(all_members),
            "members": all_members
        }
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

        patient_data = patient.data[0]
        family_info = None
        try:
            phone = patient_data.get("phone")
            if phone:
                siblings = supabase.table("patients")\
                    .select("id, name, disease_type")\
                    .eq("pharmacy_id", pharmacy_id)\
                    .eq("phone", phone)\
                    .eq("is_deleted", False)\
                    .neq("id", patient_id)\
                    .execute()
                if siblings.data:
                    family_info = {"is_family": True, "other_members": siblings.data}
        except Exception:
            pass

        return {
            "patient": patient_data,
            "medicines": medicines.data,
            "reminder_history": reminders.data,
            "family": family_info
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{patient_id}")
def update_patient(patient_id: str, body: PatientUpdate, request: Request, pharmacy_id: str = Header(...)):
    try:
        # Snapshot old data before update for audit diff
        old = supabase.table("patients").select("*").eq("id", patient_id).eq("pharmacy_id", pharmacy_id).execute()
        old_data = old.data[0] if old.data else {}

        updates = {k: v for k, v in body.dict().items() if v is not None}

        if "phone" in updates:
            updates["phone"] = normalize_phone(updates["phone"])
            conflict = supabase.table("patients")\
                .select("id")\
                .eq("pharmacy_id", pharmacy_id)\
                .eq("phone", updates["phone"])\
                .eq("is_deleted", False)\
                .neq("id", patient_id)\
                .execute()
            if conflict.data:
                raise HTTPException(status_code=409, detail=f"Phone {updates['phone']} is already registered to another patient.")

        updates["updated_at"] = "now()"

        result = supabase.table("patients")\
            .update(updates)\
            .eq("id", patient_id)\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Patient not found")

        # ── Audit log ──────────────────────────────────────────────────────
        changed_fields = {k: v for k, v in body.dict().items() if v is not None}
        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="updated",
            entity_type="patient",
            entity_id=patient_id,
            old_data={k: old_data.get(k) for k in changed_fields},
            new_data=changed_fields,
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Patient updated", extra={"pharmacy_id": pharmacy_id, "patient_id": patient_id, "fields": list(changed_fields.keys())})

        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{patient_id}")
def delete_patient(patient_id: str, request: Request, pharmacy_id: str = Header(...)):
    try:
        # Snapshot before delete
        old = supabase.table("patients").select("name, phone, disease_type").eq("id", patient_id).eq("pharmacy_id", pharmacy_id).execute()
        old_data = old.data[0] if old.data else {}

        supabase.table("patients")\
            .update({"is_deleted": True, "is_active": False})\
            .eq("id", patient_id)\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        supabase.table("medicines")\
            .update({"is_deleted": True, "status": "inactive"})\
            .eq("patient_id", patient_id)\
            .execute()

        # ── Audit log ──────────────────────────────────────────────────────
        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="deleted",
            entity_type="patient",
            entity_id=patient_id,
            old_data=old_data,
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Patient deleted", extra={"pharmacy_id": pharmacy_id, "patient_id": patient_id})

        return {"message": "Patient deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))