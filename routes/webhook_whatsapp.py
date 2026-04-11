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

    # ── Fetch ALL patients matching phone ──────────────────────────────────
    all_patients = []
    for p in phone_variants:
        try:
            result = supabase.table("patients") \
                .select("id, name, pharmacy_id, consent_given") \
                .eq("phone", p) \
                .eq("opted_out", False) \
                .eq("is_deleted", False) \
                .execute()
            data = result.data
            if isinstance(data, list):
                all_patients.extend(data)
        except Exception as e:
            logger.error(f"[Webhook] Patient lookup error for {p}: {e}")
            continue

    # Deduplicate by patient id
    seen_ids = set()
    unique_patients = []
    for pat in all_patients:
        if pat["id"] not in seen_ids:
            seen_ids.add(pat["id"])
            unique_patients.append(pat)

    if not unique_patients:
        logger.warning(f"[Webhook] No patient found for {phone}")
        return {"status": "ok", "action": "patient_not_found"}

    # ── CONSENT FLOW — handle YES/NO to consent request ───────────────────
    # Check if any patient is awaiting consent (consent_given is False)
    awaiting_consent = [p for p in unique_patients if not p.get("consent_given")]

    if awaiting_consent:
        patient = awaiting_consent[0]

        if raw_message in ["YES", "Y", "1", "HA", "HAN", "HAA"]:
            try:
                supabase.table("patients").update({
                    "consent_given": True,
                    "consent_given_at": "now()"
                }).eq("id", patient["id"]).execute()

                logger.info(f"[Webhook] Consent granted by {patient['name']} ({phone})")
            except Exception as e:
                logger.error(f"[Webhook] Consent YES error: {e}")

            return {"status": "ok", "action": "consent_granted"}

        if raw_message in ["NO", "N", "2", "NAI", "NAHI"]:
            try:
                supabase.table("patients").update({
                    "consent_given": False,
                    "opted_out": True,
                    "opted_out_at": "now()"
                }).eq("id", patient["id"]).execute()

                logger.info(f"[Webhook] Consent declined by {patient['name']} ({phone})")
            except Exception as e:
                logger.error(f"[Webhook] Consent NO error: {e}")

            return {"status": "ok", "action": "consent_declined"}

    # ── Find the patient with the most recent pending reminder log ─────────
    best_log = None
    best_patient = None

    for pat in unique_patients:
        if not pat.get("consent_given"):
            continue
        try:
            log_result = supabase.table("reminder_logs") \
                .select("id, medicine_id, sent_at, journey_id") \
                .eq("patient_id", pat["id"]) \
                .eq("patient_replied", False) \
                .order("sent_at", desc=True) \
                .limit(1) \
                .execute()
            data = log_result.data
            log = None
            if isinstance(data, list) and len(data) > 0:
                log = data[0]
            elif isinstance(data, dict) and data.get("id"):
                log = data

            if log:
                if best_log is None or log["sent_at"] > best_log["sent_at"]:
                    best_log = log
                    best_patient = pat
        except Exception as e:
            logger.error(f"[Webhook] Log fetch error for patient {pat['id']}: {e}")
            continue

    # ── YES — reorder request ──────────────────────────────────────────────
    if raw_message in ["YES", "Y", "1", "HA", "HAN", "HAA"]:
        if not best_log:
            logger.warning(f"[Webhook] No pending log for {phone}")
            return {"status": "ok", "action": "no_pending_reminder"}

        try:
            # Mark ALL unreplied reminder logs for this patient as replied YES
            # (because one stacked message covers multiple medicines)
            unreplied_logs = supabase.table("reminder_logs") \
                .select("id, medicine_id, journey_id") \
                .eq("patient_id", best_patient["id"]) \
                .eq("patient_replied", False) \
                .execute()

            for log in (unreplied_logs.data or []):
                supabase.table("reminder_logs").update({
                    "patient_replied": True,
                    "reply": "YES"
                }).eq("id", log["id"]).execute()

                supabase.table("medicines").update({
                    "status": "reorder_requested"
                }).eq("id", log["medicine_id"]).execute()

                supabase.table("reorder_requests").insert({
                    "medicine_id": log["medicine_id"],
                    "patient_id": best_patient["id"],
                    "pharmacy_id": best_patient["pharmacy_id"],
                    "reminder_log_id": log["id"],
                    "status": "pending"
                }).execute()

                if log.get("journey_id"):
                    supabase.table("journeys").update({
                        "status": "completed",
                        "conversion_flag": 1
                    }).eq("id", log["journey_id"]).execute()

            logger.info(f"[Webhook] Reorder created for all medicines — patient: {best_patient['name']}")

        except Exception as e:
            logger.error(f"[Webhook] YES handler error: {e}")

        return {"status": "ok", "action": "reorder_requested"}

    # ── NO ─────────────────────────────────────────────────────────────────
    if raw_message in ["NO", "N", "2", "NAI", "NAHI"]:
        if not best_log:
            return {"status": "ok", "action": "no_pending_reminder"}

        try:
            # Mark ALL unreplied logs as NO
            unreplied_logs = supabase.table("reminder_logs") \
                .select("id") \
                .eq("patient_id", best_patient["id"]) \
                .eq("patient_replied", False) \
                .execute()

            for log in (unreplied_logs.data or []):
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