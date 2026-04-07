"""
routes/webhook_whatsapp.py
Handles incoming patient replies from AiSensy webhook.

In AiSensy dashboard:
    Manage → API Key → Webhook URL
    Set to: https://yourdomain.com/webhook/whatsapp
"""

import logging
from fastapi import APIRouter, Request
from database import supabase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


@router.post("/webhook/whatsapp")
async def whatsapp_reply_webhook(request: Request):
    """
    AiSensy posts here when a patient replies to a WhatsApp message.
    Handles YES (reorder), NO (skip), STOP (opt-out).
    Always returns 200 fast — AiSensy expects quick acknowledgement.
    """
    try:
        payload = await request.json()
    except Exception:
        # AiSensy sometimes sends form-encoded pings — just acknowledge
        return {"status": "ok"}

    logger.info(f"[Webhook] AiSensy payload: {payload}")

    # ── Parse phone and message from AiSensy payload ──────────────────────
    phone = (
        payload.get("waId")
        or payload.get("destination")
        or payload.get("mobile")
        or ""
    ).strip().lstrip("+")

    raw_message = (
        payload.get("text", {}).get("body", "")
        or payload.get("message", "")
        or payload.get("body", "")
        or ""
    ).strip().upper()

    if not phone or not raw_message:
        return {"status": "ok", "note": "empty payload ignored"}

    logger.info(f"[Webhook] From: {phone} | Message: {raw_message}")

    # ── Handle opt-out (mirrors your existing Twilio logic) ───────────────
    if raw_message in ["STOP", "UNSUBSCRIBE", "CANCEL", "QUIT", "NO MORE"]:
        try:
            supabase.table("patients") \
                .update({"opted_out": True}) \
                .eq("phone", phone) \
                .execute()

            supabase.table("opt_outs").upsert({"phone": phone}).execute()
            logger.info(f"[Webhook] Opted out: {phone}")
        except Exception as e:
            logger.error(f"[Webhook] Opt-out DB error for {phone}: {e}")

        return {"status": "ok", "action": "opted_out"}

    # ── Handle YES — patient wants to reorder ─────────────────────────────
    if raw_message in ["YES", "Y", "1", "HA", "HAN", "HAA"]:
        try:
            # Find patient
            patient_result = supabase.table("patients") \
                .select("id, name, pharmacy_id") \
                .eq("phone", phone) \
                .eq("opted_out", False) \
                .single() \
                .execute()

            if not patient_result.data:
                logger.warning(f"[Webhook] No patient found for {phone}")
                return {"status": "ok", "action": "patient_not_found"}

            patient = patient_result.data

            # Find their most recent pending reminder log
            log_result = supabase.table("reminder_logs") \
                .select("id, medicine_id, pharmacy_id") \
                .eq("patient_id", patient["id"]) \
                .eq("patient_replied", False) \
                .order("sent_at", desc=True) \
                .limit(1) \
                .execute()

            if log_result.data:
                log = log_result.data[0]

                # Mark reminder as replied
                supabase.table("reminder_logs") \
                    .update({
                        "patient_replied": True,
                        "reply": "YES"
                    }) \
                    .eq("id", log["id"]) \
                    .execute()

                # Mark medicine as purchased (uses your existing route logic)
                supabase.table("medicines") \
                    .update({"status": "reorder_requested"}) \
                    .eq("id", log["medicine_id"]) \
                    .execute()

                logger.info(
                    f"[Webhook] Reorder requested — "
                    f"patient: {patient['name']}, medicine: {log['medicine_id']}"
                )

        except Exception as e:
            logger.error(f"[Webhook] YES handler error for {phone}: {e}")

        return {"status": "ok", "action": "reorder_requested"}

    # ── Handle NO — patient skipping this cycle ───────────────────────────
    if raw_message in ["NO", "N", "2", "NAI", "NAHI"]:
        try:
            patient_result = supabase.table("patients") \
                .select("id") \
                .eq("phone", phone) \
                .single() \
                .execute()

            if patient_result.data:
                # Just mark the reminder log as replied with NO
                supabase.table("reminder_logs") \
                    .update({
                        "patient_replied": True,
                        "reply": "NO"
                    }) \
                    .eq("patient_id", patient_result.data["id"]) \
                    .eq("patient_replied", False) \
                    .execute()

                logger.info(f"[Webhook] Skipped by patient: {phone}")

        except Exception as e:
            logger.error(f"[Webhook] NO handler error for {phone}: {e}")

        return {"status": "ok", "action": "skipped"}

    # ── Any other message — log it, ignore for now ────────────────────────
    logger.info(f"[Webhook] Unrecognised reply from {phone}: '{raw_message}'")
    return {"status": "ok", "action": "unrecognised"}