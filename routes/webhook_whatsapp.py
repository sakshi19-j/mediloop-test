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

WHATSAPP_VERIFY_TOKEN = "mediloop_verify"  # Must match Meta dashboard


# ── 1. Webhook verification (Meta sends GET on setup) ─────────────────────────
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


# ── 2. Incoming messages (Meta sends POST for real events) ────────────────────
@router.post("/webhook/whatsapp")
async def whatsapp_reply_webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}

    logger.info(f"[Webhook] Meta payload: {payload}")

    # ── Parse Meta Cloud API payload structure ─────────────────────────────
    # Meta wraps messages inside: entry[0].changes[0].value.messages[0]
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

    logger.info(f"[Webhook] From: {phone} | Message: {raw_message}")

    # ── Opt-out ────────────────────────────────────────────────────────────
    if raw_message in ["STOP", "UNSUBSCRIBE", "CANCEL", "QUIT", "NO MORE"]:
        try:
            supabase.table("patients").update({"opted_out": True}).eq("phone", phone).execute()
            supabase.table("opt_outs").upsert({"phone": phone}).execute()
            logger.info(f"[Webhook] Opted out: {phone}")
        except Exception as e:
            logger.error(f"[Webhook] Opt-out DB error: {e}")
        return {"status": "ok", "action": "opted_out"}

    # ── YES — reorder ──────────────────────────────────────────────────────
    if raw_message in ["YES", "Y", "1", "HA", "HAN", "HAA"]:
        try:
            patient_result = supabase.table("patients") \
                .select("id, name, pharmacy_id") \
                .eq("phone", phone).eq("opted_out", False).single().execute()

            if not patient_result.data:
                return {"status": "ok", "action": "patient_not_found"}

            patient = patient_result.data
            log_result = supabase.table("reminder_logs") \
                .select("id, medicine_id") \
                .eq("patient_id", patient["id"]).eq("patient_replied", False) \
                .order("sent_at", desc=True).limit(1).execute()

            if log_result.data:
                log = log_result.data[0]
                supabase.table("reminder_logs").update(
                    {"patient_replied": True, "reply": "YES"}
                ).eq("id", log["id"]).execute()

                supabase.table("medicines").update(
                    {"status": "reorder_requested"}
                ).eq("id", log["medicine_id"]).execute()

                logger.info(f"[Webhook] Reorder — patient: {patient['name']}")
        except Exception as e:
            logger.error(f"[Webhook] YES handler error: {e}")
        return {"status": "ok", "action": "reorder_requested"}

    # ── NO — skip ──────────────────────────────────────────────────────────
    if raw_message in ["NO", "N", "2", "NAI", "NAHI"]:
        try:
            patient_result = supabase.table("patients") \
                .select("id").eq("phone", phone).single().execute()

            if patient_result.data:
                supabase.table("reminder_logs").update(
                    {"patient_replied": True, "reply": "NO"}
                ).eq("patient_id", patient_result.data["id"]).eq("patient_replied", False).execute()

                logger.info(f"[Webhook] Skipped: {phone}")
        except Exception as e:
            logger.error(f"[Webhook] NO handler error: {e}")
        return {"status": "ok", "action": "skipped"}

    logger.info(f"[Webhook] Unrecognised reply from {phone}: '{raw_message}'")
    return {"status": "ok", "action": "unrecognised"}