"""
routes/webhook_whatsapp.py
Handles incoming patient replies via Meta Cloud API webhook.
"""

import logging
from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse
from database import supabase

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Webhooks"])

WHATSAPP_VERIFY_TOKEN = "mediloop_verify"


@router.get("/webhook/whatsapp")
async def whatsapp_verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("[Webhook] Meta verification successful")
        return PlainTextResponse(content=hub_challenge, status_code=200)
    logger.warning("[Webhook] Meta verification failed")
    return PlainTextResponse(content="Forbidden", status_code=403)


@router.post("/webhook/whatsapp")
async def whatsapp_reply_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}

    logger.info(f"[Webhook] Meta payload: {payload}")

    try:
        entry = payload.get("entry", [{}])[0]
        change = entry.get("changes", [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])

        if not messages:
            return {"status": "ok", "note": "no messages"}

        msg = messages[0]
        phone = msg.get("from", "").strip().lstrip("+")
        raw_message = (msg.get("text", {}).get("body", "") or "").strip().upper()

    except Exception as e:
        logger.error(f"[Webhook] Payload parse error: {e}")
        return {"status": "ok"}

    if not phone or not raw_message:
        return {"status": "ok", "note": "empty payload ignored"}

    # Normalize phone — try both with and without country code
    phone_variants = [phone, phone.lstrip("91")] if phone.startswith("91") else [phone, f"91{phone}"]

    logger.info(f"[Webhook] From: {phone} | Message: {raw_message}")

    # ── Opt-out ────────────────────────────────────────────────────────────
    if raw_message in ["STOP", "UNSUBSCRIBE", "CANCEL", "QUIT", "NO MORE"]:
        try:
            for p in phone_variants:
                supabase.table("patients").update({"opted_out": True}).eq("phone", p).execute()
            supabase.table("opt_outs").upsert({"phone": phone}).execute()
            logger.info(f"[Webhook] Opted out: {phone}")
        except Exception as e:
            logger.error(f"[Webhook] Opt-out DB error: {e}")
        return {"status": "ok", "action": "opted_out"}

    # ── Fetch patient (try phone variants) ────────────────────────────────
    patient = None
    for p in phone_variants:
        try:
            result = supabase.table("patients") \
                .select("id, name, pharmacy_id") \
                .eq("phone", p) \
                .eq("opted_out", False) \
                .eq("is_deleted", False) \
                .limit(1) \
                .execute()
            if result.data:
                patient = result.data
                break
        except Exception:
            continue

    if not patient:
        logger.warning(f"[Webhook] No patient found for {phone}")
        return {"status": "ok", "action": "patient_not_found"}

    # ── Fetch the single most recent pending reminder log ─────────────────
    log = None
    try:
        log_result = supabase.table("reminder_logs") \
            .select("id, medicine_id") \
            .eq("patient_id", patient["id"]) \
            .eq("patient_replied", False) \
            .order("sent_at", desc=True) \
            .limit(1) \
            .execute()
        data = log_result.data
        if isinstance(data, list) and len(data) > 0:
            log = data[0]
        elif isinstance(data, dict):
            log = data
    except Exception as e:
        logger.error(f"[Webhook] Log fetch error: {e}")

    # ── YES — reorder ──────────────────────────────────────────────────────
    if raw_message in ["YES", "Y", "1", "HA", "HAN", "HAA"]:
        if not log:
            return {"status": "ok", "action": "no_pending_reminder"}

        try:
            # Guard against duplicate reorder on same log
            already = supabase.table("reminder_logs") \
                .select("id") \
                .eq("id", log["id"]) \
                .eq("patient_replied", True) \
                .execute()

            if already.data:
                logger.warning(f"[Webhook] Duplicate YES ignored for log {log['id']}")
                return {"status": "ok", "action": "duplicate_ignored"}

            supabase.table("reminder_logs").update({
                "patient_replied": True,
                "reply": "YES"
            }).eq("id", log["id"]).execute()

            supabase.table("medicines").update({
                "status": "reorder_requested"
            }).eq("id", log["medicine_id"]).execute()

            # Notify pharmacy via a reorder_requests table so dashboard can show it
            supabase.table("reorder_requests").insert({
                "medicine_id": log["medicine_id"],
                "patient_id": patient["id"],
                "pharmacy_id": patient["pharmacy_id"],
                "reminder_log_id": log["id"],
                "status": "pending"
            }).execute()

            logger.info(f"[Webhook] Reorder created — patient: {patient['name']}, medicine: {log['medicine_id']}")

        except Exception as e:
            logger.error(f"[Webhook] YES handler error: {e}")

        return {"status": "ok", "action": "reorder_requested"}

    # ── NO — skip ──────────────────────────────────────────────────────────
    if raw_message in ["NO", "N", "2", "NAI", "NAHI"]:
        if not log:
            return {"status": "ok", "action": "no_pending_reminder"}

        try:
            supabase.table("reminder_logs").update({
                "patient_replied": True,
                "reply": "NO"
            }).eq("id", log["id"]).execute()

            logger.info(f"[Webhook] Skipped by patient: {phone}")

        except Exception as e:
            logger.error(f"[Webhook] NO handler error: {e}")

        return {"status": "ok", "action": "skipped"}

    logger.info(f"[Webhook] Unrecognised reply from {phone}: '{raw_message}'")
    return {"status": "ok", "action": "unrecognised"}