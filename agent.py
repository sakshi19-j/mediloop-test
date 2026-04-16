"""
agent.py — MediLoop Voice Agent
Sarvam AI (STT + TTS) + OpenAI (LLM) + LiveKit
Handles outbound medicine refill reminder calls to patients.
"""

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.plugins import openai, sarvam
from supabase import create_client

load_dotenv()

logger = logging.getLogger("mediloop-voice-agent")
logger.setLevel(logging.INFO)

# ── Supabase client ────────────────────────────────────────────────────────────
supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY"),
)


# ── Helper: fetch call metadata from room name ─────────────────────────────────
def get_call_metadata(room_name: str) -> dict:
    """
    Room name format: mediloop_{patient_id}_{medicine_id}_{reminder_log_id}
    Returns patient and medicine info from Supabase.
    """
    try:
        parts = room_name.split("_")
        if len(parts) < 4:
            return {}

        patient_id = parts[1]
        medicine_id = parts[2]
        reminder_log_id = parts[3]

        patient = supabase.table("patients").select(
            "id, name, phone, pharmacy_id"
        ).eq("id", patient_id).single().execute().data

        medicine = supabase.table("medicines").select(
            "id, name, refill_days, next_due_date"
        ).eq("id", medicine_id).single().execute().data

        pharmacy = supabase.table("pharmacies").select(
            "id, name"
        ).eq("id", patient["pharmacy_id"]).single().execute().data

        return {
            "patient_id": patient_id,
            "patient_name": patient.get("name", ""),
            "phone": patient.get("phone", ""),
            "pharmacy_id": patient.get("pharmacy_id", ""),
            "pharmacy_name": pharmacy.get("name", "MediLoop Pharmacy"),
            "medicine_id": medicine_id,
            "medicine_name": medicine.get("name", ""),
            "next_due_date": medicine.get("next_due_date", ""),
            "reminder_log_id": reminder_log_id,
        }
    except Exception as e:
        logger.error(f"[Agent] Metadata fetch error: {e}")
        return {}


# ── Helper: process patient reply in Supabase ──────────────────────────────────
def process_reply(meta: dict, reply: str):
    """
    Mirrors the YES/NO logic from webhook_whatsapp.py.
    reply must be 'YES' or 'NO'.
    """
    try:
        reminder_log_id = meta.get("reminder_log_id")
        medicine_id = meta.get("medicine_id")
        patient_id = meta.get("patient_id")
        pharmacy_id = meta.get("pharmacy_id")
        journey_id = None

        # Get journey_id from reminder log
        log = supabase.table("reminder_logs").select("journey_id").eq(
            "id", reminder_log_id
        ).single().execute().data
        if log:
            journey_id = log.get("journey_id")

        # Update reminder log
        supabase.table("reminder_logs").update({
            "patient_replied": True,
            "reply": reply,
        }).eq("id", reminder_log_id).execute()

        if reply == "YES":
            # Mark medicine as reorder_requested
            supabase.table("medicines").update({
                "status": "reorder_requested"
            }).eq("id", medicine_id).execute()

            # Create reorder request
            supabase.table("reorder_requests").insert({
                "medicine_id": medicine_id,
                "patient_id": patient_id,
                "pharmacy_id": pharmacy_id,
                "reminder_log_id": reminder_log_id,
                "status": "pending",
            }).execute()

            # Complete the journey with conversion
            if journey_id:
                supabase.table("journeys").update({
                    "status": "completed",
                    "conversion_flag": 1,
                }).eq("id", journey_id).execute()

            logger.info(f"[Agent] YES processed — reorder created for medicine {medicine_id}")

        elif reply == "NO":
            # Journey stays active, no reorder
            logger.info(f"[Agent] NO processed — skipped for medicine {medicine_id}")

    except Exception as e:
        logger.error(f"[Agent] Reply processing error: {e}")


# ── Voice Agent ────────────────────────────────────────────────────────────────
class MediLoopVoiceAgent(Agent):
    def __init__(self, meta: dict) -> None:
        self.meta = meta
        patient_name = meta.get("patient_name", "")
        medicine_name = meta.get("medicine_name", "")
        pharmacy_name = meta.get("pharmacy_name", "")
        next_due_date = meta.get("next_due_date", "")

        # Format due date nicely if available
        try:
            due = datetime.strptime(next_due_date, "%Y-%m-%d").strftime("%d %B")
        except Exception:
            due = next_due_date or "jaldi"

        instructions = f"""
Aap MediLoop ke taraf se bol rahe hain, jo {pharmacy_name} ki automated medicine refill service hai.

Aap {patient_name} ko call kar rahe hain unki dawai {medicine_name} ke refill ke baare mein,
jo {due} ko due hai.

Apna parichay do, dawai ka naam batao, aur puchho kya woh refill karwana chahte hain.

Agar patient HAAN kahe (ya "yes", "ha", "han", "chahiye", "order karo"):
- Unka shukriya karo
- Batao ki unka order pharmacy dashboard mein register ho gaya hai
- Conversation band karo

Agar patient NA kahe (ya "no", "nahi", "abhi nahi"):
- Shukriya karo unke samay ka
- Batao ki aap dobara remind karenge
- Conversation band karo

Agar patient STOP kahe:
- Unhe inform karo ki unhe future reminders nahi aayenge
- Conversation band karo

Rules:
- Hindi mein baat karo (Hinglish bhi theek hai)
- Conversation chhota aur friendly rakho (max 4-5 lines)
- Kisi bhi dawa ki dose, medical advice ya doctor se related baat mat karo
- Sirf refill confirmation ke liye call hai
- Patient ke jawab ke baad conversation naturally close karo
        """.strip()

        super().__init__(
            instructions=instructions,

            # Sarvam STT — Hindi speech to text
            stt=sarvam.STT(
                language="hi-IN",
                model="saaras:v3",
                mode="transcribe",
            ),

            # OpenAI LLM — brain
            llm=openai.LLM(model="gpt-4o"),

            # Sarvam TTS — Hindi text to speech
            tts=sarvam.TTS(
                target_language_code="hi-IN",
                model="bulbul:v3",
                speaker="anand",  # Male voice — change to "priya" for female
            ),
        )

    async def on_enter(self):
        """Agent starts the conversation as soon as patient connects."""
        self.session.generate_reply()

    async def on_user_turn_completed(self, turn_ctx, new_message):
        """Intercept patient's reply to process YES/NO in Supabase."""
        text = new_message.content.strip().upper() if hasattr(new_message, "content") else ""

        yes_words = ["HAAN", "HA", "HAN", "HAA", "YES", "Y", "CHAHIYE", "ORDER", "KARO", "BILKUL"]
        no_words = ["NAHI", "NA", "NO", "N", "ABHI NAHI", "MAT KARO"]
        stop_words = ["STOP", "BAND", "UNSUBSCRIBE"]

        if any(w in text for w in yes_words):
            process_reply(self.meta, "YES")
        elif any(w in text for w in no_words):
            process_reply(self.meta, "NO")
        elif any(w in text for w in stop_words):
            try:
                phone = self.meta.get("phone", "")
                if phone:
                    supabase.table("patients").update({
                        "opted_out": True,
                        "opted_out_at": datetime.utcnow().isoformat()
                    }).eq("id", self.meta["patient_id"]).execute()
                    supabase.table("opt_outs").upsert({"phone": phone}).execute()
                logger.info(f"[Agent] STOP — opted out patient {self.meta.get('patient_id')}")
            except Exception as e:
                logger.error(f"[Agent] Opt-out error: {e}")

        await super().on_user_turn_completed(turn_ctx, new_message)


# ── Entrypoint ─────────────────────────────────────────────────────────────────
async def entrypoint(ctx: JobContext):
    logger.info(f"[Agent] Call started — room: {ctx.room.name}")

    meta = get_call_metadata(ctx.room.name)
    if not meta:
        logger.error("[Agent] Could not fetch call metadata. Exiting.")
        return

    logger.info(
        f"[Agent] Calling {meta.get('patient_name')} for {meta.get('medicine_name')} "
        f"— {meta.get('pharmacy_name')}"
    )

    session = AgentSession()
    await session.start(
        agent=MediLoopVoiceAgent(meta=meta),
        room=ctx.room,
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))