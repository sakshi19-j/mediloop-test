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


async def send_reminder(
    phone: str,
    patient_name: str,
    medicine_name: str,
    pharmacy_name: str,
) -> dict:
    phone = phone.strip().lstrip("+").replace(" ", "").replace("-", "")

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