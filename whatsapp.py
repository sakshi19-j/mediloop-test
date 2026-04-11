"""
whatsapp.py — Meta Cloud API integration for MediLoop
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
META_API_URL = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"


def normalize_phone(phone: str) -> str:
    phone = phone.strip().lstrip("+").replace(" ", "").replace("-", "")
    if not phone.startswith("91") and len(phone) == 10:
        phone = f"91{phone}"
    return phone


async def send_reminder(
    phone: str,
    patient_name: str,
    medicine_name: str,
    pharmacy_name: str,
) -> dict:
    """Send a single-medicine reminder (used for manual remind and retry)."""
    phone = normalize_phone(phone)

    if not WHATSAPP_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("META credentials not set in .env")
        return {"channel": "failed", "error": "META credentials missing"}

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": "medicine_reminder_v1",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": patient_name},
                        {"type": "text", "text": medicine_name},
                        {"type": "text", "text": pharmacy_name},
                    ]
                }
            ]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                META_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

        data = response.json()

        if response.status_code == 200:
            logger.info(f"[WhatsApp] Sent to {phone} — {data}")
            return {
                "channel": "whatsapp",
                "status": "sent",
                "message_id": data.get("messages", [{}])[0].get("id", ""),
            }
        else:
            logger.warning(f"[WhatsApp] Meta error {response.status_code} for {phone}: {data}")
            return {
                "channel": "failed",
                "error": str(data),
                "status_code": response.status_code,
            }

    except httpx.TimeoutException:
        logger.error(f"[WhatsApp] Timeout for {phone}")
        return {"channel": "failed", "error": "Request timed out"}

    except httpx.RequestError as e:
        logger.error(f"[WhatsApp] Request error for {phone}: {e}")
        return {"channel": "failed", "error": str(e)}


async def send_bulk_reminder(
    phone: str,
    patient_name: str,
    medicine_names: list,
    pharmacy_name: str,
) -> dict:
    """
    Send a stacked reminder listing ALL due medicines in one message.
    Uses template medicine_reminder_bulk_v1 with parameters:
      {{1}} patient_name
      {{2}} medicine list (comma separated)
      {{3}} pharmacy_name
    """
    phone = normalize_phone(phone)

    if not WHATSAPP_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("META credentials not set in .env")
        return {"channel": "failed", "error": "META credentials missing"}

    medicine_list = ", ".join(medicine_names)

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": "medicine_reminder_bulk_v1",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": patient_name},
                        {"type": "text", "text": medicine_list},
                        {"type": "text", "text": pharmacy_name},
                    ]
                }
            ]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                META_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

        data = response.json()

        if response.status_code == 200:
            logger.info(f"[WhatsApp] Bulk reminder sent to {phone} — {len(medicine_names)} medicines")
            return {
                "channel": "whatsapp",
                "status": "sent",
                "message_id": data.get("messages", [{}])[0].get("id", ""),
            }
        else:
            logger.warning(f"[WhatsApp] Bulk Meta error {response.status_code} for {phone}: {data}")
            return {
                "channel": "failed",
                "error": str(data),
                "status_code": response.status_code,
            }

    except httpx.TimeoutException:
        logger.error(f"[WhatsApp] Timeout for {phone}")
        return {"channel": "failed", "error": "Request timed out"}

    except httpx.RequestError as e:
        logger.error(f"[WhatsApp] Request error for {phone}: {e}")
        return {"channel": "failed", "error": str(e)}


async def send_consent_request(
    phone: str,
    patient_name: str,
    pharmacy_name: str,
) -> dict:
    """
    Send a consent request to new patient when they are registered.
    Uses template medicine_consent_v1 with parameters:
      {{1}} patient_name
      {{2}} pharmacy_name
    Patient replies YES to give consent, NO to decline.
    """
    phone = normalize_phone(phone)

    if not WHATSAPP_ACCESS_TOKEN or not META_PHONE_NUMBER_ID:
        logger.error("META credentials not set in .env")
        return {"channel": "failed", "error": "META credentials missing"}

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": "medicine_consent_v1",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": patient_name},
                        {"type": "text", "text": pharmacy_name},
                    ]
                }
            ]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                META_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

        data = response.json()

        if response.status_code == 200:
            logger.info(f"[WhatsApp] Consent request sent to {phone}")
            return {"channel": "whatsapp", "status": "sent"}
        else:
            logger.warning(f"[WhatsApp] Consent error {response.status_code} for {phone}: {data}")
            return {"channel": "failed", "error": str(data)}

    except httpx.TimeoutException:
        return {"channel": "failed", "error": "Request timed out"}

    except httpx.RequestError as e:
        return {"channel": "failed", "error": str(e)}