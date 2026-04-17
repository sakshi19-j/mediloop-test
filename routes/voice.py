"""
routes/voice.py — MediLoop Voice Call Route
Triggers an outbound voice call via LiveKit for medicine refill reminders.
Called by scheduler (fallback) or manually from pharmacy dashboard.
"""

import os
import logging
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from database import supabase
from livekit import api as livekit_api

logger = logging.getLogger(__name__)
router = APIRouter()

LIVEKIT_URL    = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


# ── Request schema ─────────────────────────────────────────────────────────────
class VoiceCallRequest(BaseModel):
    patient_id: str
    medicine_id: str
    reminder_log_id: str


# ── POST /api/v1/voice/call ────────────────────────────────────────────────────
@router.post("/call")
async def trigger_voice_call(
    body: VoiceCallRequest,
    pharmacy_id: str = Header(...),
):
    """
    Triggers an outbound LiveKit voice call for a medicine refill reminder.
    Room name format: mediloop_{patient_id}_{medicine_id}_{reminder_log_id}
    The agent.py worker picks up the room and calls the patient.
    """

    # ── Validate patient belongs to this pharmacy ──────────────────────────
    try:
        patient_result = supabase.table("patients") \
            .select("id, name, phone, opted_out, is_deleted") \
            .eq("id", body.patient_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .single() \
            .execute()
        patient = patient_result.data
    except Exception as e:
        logger.error(f"[Voice] Patient fetch error: {e}")
        raise HTTPException(status_code=404, detail="Patient not found")

    if not patient:
        raise HTTPException(status_code=404, detail="Patient not found")

    if patient.get("is_deleted"):
        raise HTTPException(status_code=400, detail="Patient is deleted")

    if patient.get("opted_out"):
        raise HTTPException(status_code=400, detail="Patient has opted out of reminders")

    # ── Validate medicine belongs to this pharmacy ─────────────────────────
    try:
        medicine_result = supabase.table("medicines") \
            .select("id, name, status, is_deleted, is_paused") \
            .eq("id", body.medicine_id) \
            .eq("pharmacy_id", pharmacy_id) \
            .single() \
            .execute()
        medicine = medicine_result.data
    except Exception as e:
        logger.error(f"[Voice] Medicine fetch error: {e}")
        raise HTTPException(status_code=404, detail="Medicine not found")

    if not medicine:
        raise HTTPException(status_code=404, detail="Medicine not found")

    if medicine.get("is_deleted"):
        raise HTTPException(status_code=400, detail="Medicine is deleted")

    if medicine.get("is_paused"):
        raise HTTPException(status_code=400, detail="Medicine is paused — no reminders")

    # ── Build room name ────────────────────────────────────────────────────
    room_name = f"mediloop_{body.patient_id}_{body.medicine_id}_{body.reminder_log_id}"

    # ── Create LiveKit room and dispatch agent ─────────────────────────────
    try:
        lk = livekit_api.LiveKitAPI(
            url=LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )

        # Create the room
        await lk.room.create_room(
            livekit_api.CreateRoomRequest(name=room_name)
        )

        # Dispatch agent worker to handle this room
        await lk.agent_dispatch.create_dispatch(
            livekit_api.CreateAgentDispatchRequest(
                agent_name="mediloop-voice-agent",
                room=room_name,
            )
        )

        await lk.aclose()

        logger.info(
            f"[Voice] Call dispatched — room: {room_name} | "
            f"patient: {patient['name']} | medicine: {medicine['name']}"
        )

    except Exception as e:
        logger.error(f"[Voice] LiveKit dispatch error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trigger voice call: {str(e)}")

    # ── Update reminder_log with voice channel ─────────────────────────────
    try:
        supabase.table("reminder_logs").update({
            "whatsapp_status": "voice_dispatched",
        }).eq("id", body.reminder_log_id).execute()
    except Exception as e:
        logger.warning(f"[Voice] Could not update reminder_log: {e}")

    return {
        "status": "dispatched",
        "room": room_name,
        "patient": patient["name"],
        "medicine": medicine["name"],
        "phone": patient["phone"],
    }


# ── GET /api/v1/voice/status/{room_name} ──────────────────────────────────────
@router.get("/status/{room_name}")
async def get_call_status(
    room_name: str,
    pharmacy_id: str = Header(...),
):
    """
    Check if a LiveKit room (call) is still active.
    """
    try:
        lk = livekit_api.LiveKitAPI(
            url=LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )

        rooms = await lk.room.list_rooms(
            livekit_api.ListRoomsRequest(names=[room_name])
        )
        await lk.aclose()

        if rooms.rooms:
            room = rooms.rooms[0]
            return {
                "status": "active",
                "room": room_name,
                "num_participants": room.num_participants,
            }
        else:
            return {
                "status": "ended",
                "room": room_name,
            }

    except Exception as e:
        logger.error(f"[Voice] Status check error: {e}")
        raise HTTPException(status_code=500, detail=str(e))