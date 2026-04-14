from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from database import supabase
from datetime import date, timedelta
from typing import Optional
from audit import log_audit
from logger_config import get_logger

router = APIRouter()
logger = get_logger(__name__)


class MedicineCreate(BaseModel):
    patient_id: str
    name: str
    dosage: str
    refill_days: int
    last_purchase_date: date
    notes: Optional[str] = None


class MedicineUpdate(BaseModel):
    name: Optional[str] = None
    dosage: Optional[str] = None
    refill_days: Optional[int] = None
    notes: Optional[str] = None


class PurchaseUpdate(BaseModel):
    last_purchase_date: date


class MedicineBulkCreate(BaseModel):
    patient_id: str
    medicines: list[MedicineCreate]


@router.post("/")
def add_medicine(medicine: MedicineCreate, request: Request, pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("medicines").insert({
            "pharmacy_id": pharmacy_id,
            "patient_id": medicine.patient_id,
            "name": medicine.name,
            "dosage": medicine.dosage,
            "refill_days": medicine.refill_days,
            "last_purchase_date": medicine.last_purchase_date.isoformat(),
            "notes": medicine.notes
        }).execute()

        new_med = result.data[0]

        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="created",
            entity_type="medicine",
            entity_id=new_med["id"],
            new_data={"name": medicine.name, "patient_id": medicine.patient_id, "refill_days": medicine.refill_days},
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Medicine added", extra={
            "pharmacy_id": pharmacy_id,
            "medicine_id": new_med["id"],
            "patient_id": medicine.patient_id,
            "medicine_name": medicine.name,  # fixed: was 'name' (reserved LogRecord field)
        })

        return new_med
    except Exception as e:
        logger.error("Medicine add failed", extra={"pharmacy_id": pharmacy_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
def get_all_medicines(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("medicines") \
            .select("*, patients(name, phone)") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("is_deleted", False) \
            .eq("status", "active") \
            .order("next_due_date") \
            .execute()
        return result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/upcoming")
def get_upcoming(pharmacy_id: str = Header(...)):
    try:
        in_30_days = (date.today() + timedelta(days=30)).isoformat()
        today = date.today().isoformat()

        result = supabase.table("medicines") \
            .select("*, patients(name, phone)") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("status", "active") \
            .eq("is_deleted", False) \
            .eq("is_paused", False) \
            .gte("next_due_date", today) \
            .lte("next_due_date", in_30_days) \
            .order("next_due_date") \
            .execute()
        return result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/reorder-requests")
def get_reorder_requests(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("reorder_requests") \
            .select("*, medicines(name, dosage), patients(name, phone)") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("status", "pending") \
            .order("created_at", desc=True) \
            .execute()
        return result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/journeys")
def get_journeys(pharmacy_id: str = Header(...), status: Optional[str] = None):
    try:
        query = supabase.table("journeys") \
            .select("*, patients(name), medicines(name)") \
            .eq("pharmacy_id", pharmacy_id) \
            .order("start_date", desc=True)

        if status:
            query = query.eq("status", status)

        result = query.execute()
        data = result.data
        total     = len(data)
        confirmed = sum(1 for j in data if j["conversion_flag"] == 1)
        no_reply  = sum(1 for j in data if j["status"] == "expired" and j["conversion_flag"] == 0)

        skipped_result = supabase.table("reminder_logs") \
            .select("journey_id") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("reply", "NO") \
            .execute()
        skipped = len(skipped_result.data)

        return {
            "stats": {"total_journeys": total, "confirmed": confirmed, "no_reply": no_reply, "skipped": skipped},
            "journeys": data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk")
def add_medicines_bulk(body: MedicineBulkCreate, pharmacy_id: str = Header(...)):
    try:
        records = []
        for med in body.medicines:
            records.append({
                "pharmacy_id": pharmacy_id,
                "patient_id": body.patient_id,
                "name": med.name,
                "dosage": med.dosage,
                "refill_days": med.refill_days,
                "last_purchase_date": med.last_purchase_date.isoformat(),
                "notes": med.notes
            })

        result = supabase.table("medicines").insert(records).execute()

        for med in result.data:
            log_audit(
                pharmacy_id=pharmacy_id,
                actor_id=pharmacy_id,
                action="created",
                entity_type="medicine",
                entity_id=med["id"],
                new_data={"name": med["name"], "patient_id": body.patient_id, "source": "bulk"},
            )

        logger.info("Bulk medicines added", extra={
            "pharmacy_id": pharmacy_id,
            "patient_id": body.patient_id,
            "count": len(result.data),
        })

        return {"added": len(result.data), "medicines": result.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remind-all")
async def remind_all_for_patient(patient_id: str, pharmacy_id: str = Header(...)):
    try:
        from whatsapp import send_bulk_reminder

        today = date.today().isoformat()
        in_30_days = (date.today() + timedelta(days=30)).isoformat()

        meds = supabase.table("medicines") \
            .select("*, patients(name, phone, opted_out, consent_given), pharmacies(name)") \
            .eq("patient_id", patient_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("status", "active") \
            .eq("is_deleted", False) \
            .eq("is_paused", False) \
            .gte("next_due_date", today) \
            .lte("next_due_date", in_30_days) \
            .execute()

        if not meds.data:
            raise HTTPException(status_code=404, detail="No medicines due in next 30 days")

        patient  = meds.data[0]["patients"]
        pharmacy = meds.data[0]["pharmacies"]

        if patient.get("opted_out"):
            raise HTTPException(status_code=400, detail="Patient has opted out")
        if not patient.get("consent_given"):
            raise HTTPException(status_code=400, detail="Patient has not given consent")

        medicine_names = [m["name"] for m in meds.data]

        result = await send_bulk_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_names=medicine_names,
            pharmacy_name=pharmacy["name"]
        )

        logger.info("Manual bulk reminder sent", extra={
            "pharmacy_id": pharmacy_id,
            "patient_id": patient_id,
            "medicine_count": len(medicine_names),
            "status": result["channel"],
        })

        return {
            "message": f"Bulk reminder sent for {len(medicine_names)} medicines",
            "medicines": medicine_names,
            "status": result["channel"]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/reorder-requests/{request_id}/fulfil")
def fulfil_reorder(request_id: str, request: Request, pharmacy_id: str = Header(...)):
    try:
        req = supabase.table("reorder_requests") \
            .select("medicine_id, patient_id") \
            .eq("id", request_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .single() \
            .execute()

        if not req.data:
            raise HTTPException(status_code=404, detail="Reorder request not found")

        medicine_id = req.data["medicine_id"]
        today = date.today().isoformat()

        supabase.table("reorder_requests").update({"status": "fulfilled", "fulfilled_at": today}).eq("id", request_id).execute()
        supabase.table("medicines").update({"status": "active", "last_purchase_date": today}).eq("id", medicine_id).execute()
        supabase.table("reminder_logs").update({"converted": True, "purchased_at": today}).eq("medicine_id", medicine_id).eq("converted", False).execute()

        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="updated",
            entity_type="reorder",
            entity_id=request_id,
            new_data={"status": "fulfilled", "fulfilled_at": today},
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Reorder fulfilled", extra={"pharmacy_id": pharmacy_id, "medicine_id": medicine_id})

        return {"message": "Reorder fulfilled", "medicine_id": medicine_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{medicine_id}/remind")
async def manual_remind(medicine_id: str, pharmacy_id: str = Header(...)):
    try:
        from whatsapp import send_reminder

        med = supabase.table("medicines") \
            .select("*, patients(name, phone), pharmacies(name)") \
            .eq("id", medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        if not med.data:
            raise HTTPException(status_code=404, detail="Medicine not found")

        m = med.data[0]

        if m.get("is_paused") or m.get("is_deleted"):
            raise HTTPException(status_code=400, detail="Medicine is paused or deleted")
        if m["patients"].get("opted_out"):
            raise HTTPException(status_code=400, detail="Patient has opted out of reminders")

        result = await send_reminder(
            phone=m["patients"]["phone"],
            patient_name=m["patients"]["name"],
            medicine_name=m["name"],
            pharmacy_name=m["pharmacies"]["name"]
        )

        log_status = result.get("channel", "failed")

        log = supabase.table("reminder_logs").insert({
            "medicine_id": medicine_id,
            "patient_id": m["patient_id"],
            "pharmacy_id": pharmacy_id,
            "whatsapp_status": log_status,
            "patient_replied": False,
        }).execute()

        logger.info("Manual reminder sent", extra={
            "pharmacy_id": pharmacy_id,
            "medicine_id": medicine_id,
            "status": log_status,
        })

        return {
            "message": "Reminder sent" if log_status != "failed" else "Reminder failed",
            "status": log_status,
            "log_id": log.data[0]["id"] if log.data else None,
            "result": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{medicine_id}")
def update_medicine(medicine_id: str, body: MedicineUpdate, request: Request, pharmacy_id: str = Header(...)):
    try:
        old = supabase.table("medicines").select("*").eq("id", medicine_id).eq("pharmacy_id", pharmacy_id).execute()
        old_data = old.data[0] if old.data else {}

        updates = {k: v for k, v in body.dict().items() if v is not None}
        updates["updated_at"] = "now()"

        result = supabase.table("medicines") \
            .update(updates) \
            .eq("id", medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="Medicine not found")

        changed_fields = {k: v for k, v in body.dict().items() if v is not None}
        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="updated",
            entity_type="medicine",
            entity_id=medicine_id,
            old_data={k: old_data.get(k) for k in changed_fields},
            new_data=changed_fields,
            ip_address=request.client.host if request.client else None,
        )

        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{medicine_id}/purchased")
def mark_purchased(medicine_id: str, body: PurchaseUpdate, pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("medicines") \
            .update({"last_purchase_date": body.last_purchase_date.isoformat(), "status": "active"}) \
            .eq("id", medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        supabase.table("reminder_logs") \
            .update({"converted": True, "purchased_at": body.last_purchase_date.isoformat()}) \
            .eq("medicine_id", medicine_id) \
            .eq("converted", False) \
            .execute()

        supabase.table("reorder_requests") \
            .update({"status": "fulfilled", "fulfilled_at": body.last_purchase_date.isoformat()}) \
            .eq("medicine_id", medicine_id) \
            .eq("status", "pending") \
            .execute()

        log_audit(pharmacy_id=pharmacy_id, actor_id=pharmacy_id,
                  action="updated", entity_type="medicine", entity_id=medicine_id,
                  new_data={"last_purchase_date": body.last_purchase_date.isoformat(), "status": "active"})

        return result.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{medicine_id}/pause")
def toggle_pause(medicine_id: str, request: Request, pharmacy_id: str = Header(...)):
    try:
        current = supabase.table("medicines") \
            .select("is_paused") \
            .eq("id", medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        if not current.data:
            raise HTTPException(status_code=404, detail="Medicine not found")

        new_state = not current.data[0]["is_paused"]
        supabase.table("medicines").update({"is_paused": new_state}).eq("id", medicine_id).execute()

        log_audit(pharmacy_id=pharmacy_id, actor_id=pharmacy_id,
                  action="updated", entity_type="medicine", entity_id=medicine_id,
                  old_data={"is_paused": not new_state},
                  new_data={"is_paused": new_state},
                  ip_address=request.client.host if request.client else None)

        logger.info("Medicine pause toggled", extra={
            "pharmacy_id": pharmacy_id, "medicine_id": medicine_id, "is_paused": new_state
        })

        return {"is_paused": new_state}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{medicine_id}")
def delete_medicine(medicine_id: str, request: Request, pharmacy_id: str = Header(...)):
    try:
        old = supabase.table("medicines").select("name, patient_id").eq("id", medicine_id).eq("pharmacy_id", pharmacy_id).execute()
        old_data = old.data[0] if old.data else {}

        supabase.table("medicines") \
            .update({"is_deleted": True, "status": "inactive"}) \
            .eq("id", medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        log_audit(
            pharmacy_id=pharmacy_id,
            actor_id=pharmacy_id,
            action="deleted",
            entity_type="medicine",
            entity_id=medicine_id,
            old_data=old_data,
            ip_address=request.client.host if request.client else None,
        )

        logger.info("Medicine deleted", extra={"pharmacy_id": pharmacy_id, "medicine_id": medicine_id})

        return {"message": "Medicine deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))