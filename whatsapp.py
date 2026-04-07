"""
whatsapp.py — AiSensy integration for MediLoop
Replaces Twilio. Drop-in replacement — same send_reminder() signature.

Add to your .env:
    AISENSY_API_KEY=your_key_here
    AISENSY_USERNAME=MediLoop
    AISENSY_CAMPAIGN_NAME=medicine_refill_reminder
"""

import os
import logging
import httpx

logger = logging.getLogger(__name__)

AISENSY_ENDPOINT = "https://backend.aisensy.com/campaign/t1/api/v2"


async def send_reminder(
    phone: str,
    patient_name: str,
    medicine_name: str,
    pharmacy_name: str,
) -> dict:
    """
    Send WhatsApp refill reminder via AiSensy.
    Exact same signature as previous Twilio version — nothing else needs changing.

    Args:
        phone:         Patient phone with country code, digits only.
                       e.g. "919876543210"  your scheduler already sends this format
        patient_name:  e.g. "Sudesh Dahale"
        medicine_name: e.g. "Metformin 500mg"
        pharmacy_name: e.g. "Sahil Medical"

    Returns:
        dict — always has "channel" key:
            {"channel": "whatsapp", "status": "sent", ...}   success
            {"channel": "failed",   "error": "...", ...}      failure

    AiSensy template (Utility category, name: medicine_refill_reminder):
        Hi {{1}}, your medicine *{{2}}* is due for refill in 2 days.
        Reply *YES* to reorder from {{3}}.
        Reply *NO* to skip this reminder.
    """

    # Normalise phone — strip spaces, dashes, leading +
    phone = phone.strip().lstrip("+").replace(" ", "").replace("-", "")

    # Pull config from env
    api_key = os.getenv("AISENSY_API_KEY")
    username = os.getenv("AISENSY_USERNAME", "MediLoop")
    campaign_name = os.getenv("AISENSY_CAMPAIGN_NAME", "medicine_refill_reminder")

    if not api_key:
        logger.error("AISENSY_API_KEY not set in .env")
        return {"channel": "failed", "error": "AISENSY_API_KEY missing"}

    payload = {
        "apiKey": api_key,
        "campaignName": campaign_name,
        "destination": phone,
        "userName": username,
        "source": "mediloop_scheduler",
        "templateParams": [
            patient_name,   # {{1}} Hi Sudesh,
            medicine_name,  # {{2}} Metformin 500mg
            pharmacy_name,  # {{3}} Sahil Medical
        ],
        "tags": ["refill_reminder"],
        "attributes": {
            "pharmacy": pharmacy_name,
            "medicine": medicine_name,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                AISENSY_ENDPOINT,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code == 200:
            data = response.json()
            logger.info(f"[WhatsApp] Sent to {phone} — {data}")
            return {
                "channel": "whatsapp",
                "status": "sent",
                "message_id": data.get("messageId") or data.get("id", ""),
            }

        else:
            logger.warning(
                f"[WhatsApp] AiSensy error {response.status_code} "
                f"for {phone}: {response.text}"
            )
            return {
                "channel": "failed",
                "error": response.text,
                "status_code": response.status_code,
            }

    except httpx.TimeoutException:
        logger.error(f"[WhatsApp] Timeout for {phone}")
        return {"channel": "failed", "error": "Request timed out"}

    except httpx.RequestError as e:
        logger.error(f"[WhatsApp] Request error for {phone}: {e}")
        return {"channel": "failed", "error": str(e)}