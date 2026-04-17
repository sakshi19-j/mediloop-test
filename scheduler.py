from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, timedelta, datetime, timezone
from database import supabase
from whatsapp import send_bulk_reminder, send_reminder
from queue_worker import enqueue_reminder, get_redis_connection
import asyncio
import pytz
import os
from collections import defaultdict

scheduler = AsyncIOScheduler()
IST = pytz.timezone("Asia/Kolkata")

LIVEKIT_URL        = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY    = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


def get_or_create_journey(medicine_id: str, patient_id: str, pharmacy_id: str) -> str:
    """Return existing active journey_id or create a new one."""
    existing = supabase.table("journeys") \
        .select("id") \
        .eq("medicine_id", medicine_id) \
        .eq("status", "active") \
        .execute()

    if existing.data:
        return existing.data[0]["id"]

    new_journey = supabase.table("journeys").insert({
        "medicine_id": medicine_id,
        "patient_id": patient_id,
        "pharmacy_id": pharmacy_id,
        "start_date": date.today().isoformat(),
        "status": "active",
        "total_reminders_sent": 0,
        "conversion_flag": 0
    }).execute()

    return new_journey.data[0]["id"]


async def send_reminders_for_offset(days_offset: int, reminder_type: str):
    target_date = (date.today() + timedelta(days=days_offset)).isoformat()
    print(f"[Scheduler] [{reminder_type}] Running for due date: {target_date}")

    result = supabase.table("medicines") \
        .select("*, patients(name, phone, opted_out, consent_given), pharmacies(name)") \
        .eq("next_due_date", target_date) \
        .eq("status", "active") \
        .eq("is_deleted", False) \
        .eq("is_paused", False) \
        .execute()

    medicines = result.data
    print(f"[Scheduler] [{reminder_type}] Found {len(medicines)} medicines due")

    # ── Group medicines by (patient_id, pharmacy_id) for stacking ────────────
    grouped = defaultdict(list)
    for med in medicines:
        patient = med["patients"]

        if patient.get("opted_out"):
            continue
        if not patient.get("consent_given"):
            print(f"[Scheduler] [{reminder_type}] No consent — skipping {patient['name']}")
            continue

        key = (med["patient_id"], med["pharmacy_id"])
        grouped[key].append(med)

    use_queue = get_redis_connection() is not None

    enqueued = 0
    sent     = 0
    skipped  = 0
    failed   = 0

    for (patient_id, pharmacy_id), patient_medicines in grouped.items():
        patient  = patient_medicines[0]["patients"]
        pharmacy = patient_medicines[0]["pharmacies"]

        # Dedup — don't resend if this reminder_type already went today
        already_sent = False
        for med in patient_medicines:
            existing = supabase.table("reminder_logs") \
                .select("id") \
                .eq("medicine_id", med["id"]) \
                .eq("reminder_type", reminder_type) \
                .gte("sent_at", date.today().isoformat()) \
                .execute()
            if existing.data:
                already_sent = True
                break

        if already_sent:
            print(f"[Scheduler] [{reminder_type}] Already sent today for {patient['name']}, skipping")
            skipped += 1
            continue

        medicine_names = [m["name"] for m in patient_medicines]
        medicine_ids   = [m["id"] for m in patient_medicines]

        for med in patient_medicines:
            journey_id = get_or_create_journey(med["id"], patient_id, pharmacy_id)

            if not use_queue:
                supabase.table("reminder_logs").insert({
                    "medicine_id":     med["id"],
                    "patient_id":      patient_id,
                    "pharmacy_id":     pharmacy_id,
                    "whatsapp_status": "pending",
                    "reminder_type":   reminder_type,
                    "journey_id":      journey_id,
                }).execute()

            journey = supabase.table("journeys") \
                .select("total_reminders_sent") \
                .eq("id", journey_id) \
                .execute()
            current_count = journey.data[0]["total_reminders_sent"] if journey.data else 0
            supabase.table("journeys").update({
                "total_reminders_sent": current_count + 1
            }).eq("id", journey_id).execute()

        if use_queue:
            job_id = enqueue_reminder(
                phone=patient["phone"],
                patient_name=patient["name"],
                medicine_names=medicine_names,
                pharmacy_name=pharmacy["name"],
                pharmacy_id=pharmacy_id,
                patient_id=patient_id,
                reminder_type=reminder_type,
                medicine_ids=medicine_ids,
            )
            if job_id:
                print(f"[Scheduler] [{reminder_type}] Enqueued for {patient['name']} — {len(medicine_names)} medicines")
                enqueued += 1
            else:
                print(f"[Scheduler] [{reminder_type}] Queue failed for {patient['name']}, skipping")
                skipped += 1
            continue

        send_result = await send_bulk_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_names=medicine_names,
            pharmacy_name=pharmacy["name"]
        )

        log_status = send_result["channel"]

        for med in patient_medicines:
            supabase.table("reminder_logs") \
                .update({"whatsapp_status": log_status}) \
                .eq("medicine_id", med["id"]) \
                .eq("reminder_type", reminder_type) \
                .eq("pharmacy_id", pharmacy_id) \
                .gte("sent_at", date.today().isoformat()) \
                .execute()

        if log_status != "failed":
            print(f"[Scheduler] [{reminder_type}] Sent to {patient['name']} — {', '.join(medicine_names)}")
            sent += 1
        else:
            print(f"[Scheduler] [{reminder_type}] FAILED for {patient['name']}: {send_result.get('error')}")
            failed += 1

    if use_queue:
        print(f"[Scheduler] [{reminder_type}] Done — enqueued: {enqueued}, skipped: {skipped}")
    else:
        print(f"[Scheduler] [{reminder_type}] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


# ── Voice Call Fallback ────────────────────────────────────────────────────────
async def trigger_voice_fallback_calls():
    """
    Runs at 9:30 AM IST daily.
    Finds reminder_logs from yesterday where:
      - WhatsApp was sent (whatsapp_status = 'whatsapp')
      - Patient has NOT replied (patient_replied = False)
    Then triggers a LiveKit voice call as fallback for each medicine.
    Skips if voice call already attempted today for that reminder_log.
    """
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        print("[Voice Fallback] LiveKit env vars not set — skipping")
        return

    yesterday_start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    yesterday_end   = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()

    print(f"[Voice Fallback] Checking no-reply reminders between {yesterday_start} and {yesterday_end}")

    try:
        logs = supabase.table("reminder_logs") \
            .select("id, medicine_id, patient_id, pharmacy_id, journey_id") \
            .eq("whatsapp_status", "whatsapp") \
            .eq("patient_replied", False) \
            .gte("sent_at", yesterday_start) \
            .lte("sent_at", yesterday_end) \
            .execute()

        pending_logs = logs.data or []
        print(f"[Voice Fallback] Found {len(pending_logs)} no-reply reminders")

        for log in pending_logs:
            reminder_log_id = log["id"]
            medicine_id     = log["medicine_id"]
            patient_id      = log["patient_id"]
            pharmacy_id     = log["pharmacy_id"]

            # Check patient is still active and not opted out
            try:
                patient = supabase.table("patients") \
                    .select("id, name, phone, opted_out, is_deleted") \
                    .eq("id", patient_id) \
                    .single() \
                    .execute().data

                if not patient or patient.get("opted_out") or patient.get("is_deleted"):
                    print(f"[Voice Fallback] Skipping — patient inactive or opted out: {patient_id}")
                    continue
            except Exception as e:
                print(f"[Voice Fallback] Patient fetch error: {e}")
                continue

            # Check medicine is still active
            try:
                medicine = supabase.table("medicines") \
                    .select("id, name, status, is_deleted, is_paused") \
                    .eq("id", medicine_id) \
                    .single() \
                    .execute().data

                if not medicine or medicine.get("is_deleted") or medicine.get("is_paused"):
                    print(f"[Voice Fallback] Skipping — medicine inactive: {medicine_id}")
                    continue
            except Exception as e:
                print(f"[Voice Fallback] Medicine fetch error: {e}")
                continue

            # Skip if voice call already dispatched for this log
            already_called = supabase.table("reminder_logs") \
                .select("id") \
                .eq("id", reminder_log_id) \
                .eq("whatsapp_status", "voice_dispatched") \
                .execute()

            if already_called.data:
                print(f"[Voice Fallback] Already called for log {reminder_log_id} — skipping")
                continue

            # ── Dispatch LiveKit voice call ────────────────────────────────
            try:
                from livekit import api as livekit_api

                room_name = f"mediloop_{patient_id}_{medicine_id}_{reminder_log_id}"

                lk = livekit_api.LiveKitAPI(
                    url=LIVEKIT_URL,
                    api_key=LIVEKIT_API_KEY,
                    api_secret=LIVEKIT_API_SECRET,
                )

                await lk.room.create_room(
                    livekit_api.CreateRoomRequest(name=room_name)
                )

                await lk.agent_dispatch.create_dispatch(
                    livekit_api.CreateAgentDispatchRequest(
                        agent_name="mediloop-voice-agent",
                        room=room_name,
                    )
                )

                await lk.aclose()

                # Mark reminder_log as voice_dispatched
                supabase.table("reminder_logs").update({
                    "whatsapp_status": "voice_dispatched"
                }).eq("id", reminder_log_id).execute()

                print(
                    f"[Voice Fallback] Call dispatched — "
                    f"patient: {patient['name']} | medicine: {medicine['name']} | room: {room_name}"
                )

            except Exception as e:
                print(f"[Voice Fallback] LiveKit dispatch error for {patient_id}: {e}")
                continue

    except Exception as e:
        print(f"[Voice Fallback] Job error: {e}")


async def expire_old_journeys():
    """Mark journeys with no YES reply as expired — only after 1 full day past due date."""
    expiry_cutoff = (date.today() - timedelta(days=1)).isoformat()

    active_journeys = supabase.table("journeys") \
        .select("id, medicine_id") \
        .eq("status", "active") \
        .execute()

    for journey in active_journeys.data:
        med = supabase.table("medicines") \
            .select("next_due_date") \
            .eq("id", journey["medicine_id"]) \
            .execute()

        if not med.data:
            continue

        due_date = med.data[0]["next_due_date"]
        if due_date and due_date < expiry_cutoff:
            supabase.table("journeys").update({
                "status": "expired",
                "conversion_flag": 0
            }).eq("id", journey["id"]).execute()
            print(f"[Scheduler] Journey {journey['id']} expired (due: {due_date})")


async def retry_pending_consent():
    """
    Finds patients with no consent reply 48+ hours after request.
    Sends one follow-up via queue if available, direct otherwise.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    print(f"[Consent Retry] Looking for patients with pending consent before {cutoff}")

    try:
        pending = supabase.table("patients") \
            .select("id, name, phone, pharmacy_id, consent_followup_sent") \
            .eq("consent_given", False) \
            .eq("opted_out", False) \
            .eq("is_deleted", False) \
            .eq("is_active", True) \
            .lte("consent_requested_at", cutoff) \
            .execute()

        patients = [
            p for p in (pending.data or [])
            if not p.get("consent_followup_sent")
        ]

        print(f"[Consent Retry] Found {len(patients)} patients needing follow-up")

        use_queue = get_redis_connection() is not None

        for patient in patients:
            try:
                pharmacy = supabase.table("pharmacies") \
                    .select("name") \
                    .eq("id", patient["pharmacy_id"]) \
                    .execute()
                pharmacy_name = pharmacy.data[0]["name"] if pharmacy.data else "Your Pharmacy"

                if use_queue:
                    from queue_worker import enqueue_consent
                    enqueue_consent(
                        phone=patient["phone"],
                        patient_name=patient["name"],
                        pharmacy_name=pharmacy_name,
                        pharmacy_id=patient["pharmacy_id"],
                        patient_id=patient["id"],
                    )
                else:
                    from whatsapp import send_consent_request
                    await send_consent_request(
                        phone=patient["phone"],
                        patient_name=patient["name"],
                        pharmacy_name=pharmacy_name
                    )

                supabase.table("patients") \
                    .update({"consent_followup_sent": True}) \
                    .eq("id", patient["id"]) \
                    .execute()

                print(f"[Consent Retry] Follow-up {'queued' if use_queue else 'sent'} for {patient['name']}")

            except Exception as e:
                print(f"[Consent Retry] Failed for {patient['name']}: {e}")
                continue

    except Exception as e:
        print(f"[Consent Retry] Job error: {e}")


async def send_3day_reminders():
    await send_reminders_for_offset(3, "3day")


async def send_1day_reminders():
    await send_reminders_for_offset(1, "1day")


async def send_due_reminders():
    await send_reminders_for_offset(2, "2day")


async def retry_failed_reminders():
    """
    Retries failed reminder_logs from today. Always direct send —
    these are already logged, worker just needs to resend.
    """
    today = date.today().isoformat()

    failed = supabase.table("reminder_logs") \
        .select("*, medicines(name, patient_id, pharmacy_id, is_paused, is_deleted, patients(name, phone, opted_out, consent_given), pharmacies(name))") \
        .eq("whatsapp_status", "failed") \
        .gte("sent_at", today) \
        .execute()

    print(f"[Retry] Found {len(failed.data)} failed reminders to retry")

    for log in failed.data:
        med = log["medicines"]
        if not med or med.get("is_paused") or med.get("is_deleted"):
            continue

        patient = med["patients"]
        if patient.get("opted_out") or not patient.get("consent_given"):
            continue

        result = await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=med["pharmacies"]["name"]
        )

        new_status = f"retry_{result['channel']}"
        supabase.table("reminder_logs") \
            .update({"whatsapp_status": new_status}) \
            .eq("id", log["id"]) \
            .execute()

        print(f"[Retry] {patient['name']} → {new_status}")


async def check_subscription_expiry():
    from datetime import date
    today = date.today().isoformat()

    expired = supabase.table("subscriptions") \
        .select("*, pharmacies(name, email)") \
        .eq("status", "active") \
        .lt("ends_at", today) \
        .neq("plan", "trial") \
        .execute()

    for sub in expired.data:
        supabase.table("subscriptions") \
            .update({"status": "expired"}) \
            .eq("id", sub["id"]) \
            .execute()

        print(f"[Billing] Subscription expired: {sub['pharmacies']['name']}")


def start_scheduler():
    scheduler.add_job(
        lambda: asyncio.create_task(send_3day_reminders()),
        trigger=CronTrigger(hour=9, minute=0, timezone=IST),
        id="reminders_3day"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger=CronTrigger(hour=9, minute=5, timezone=IST),
        id="reminders_2day"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(send_1day_reminders()),
        trigger=CronTrigger(hour=9, minute=10, timezone=IST),
        id="reminders_1day"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(retry_failed_reminders()),
        trigger=CronTrigger(hour=11, minute=0, timezone=IST),
        id="retry_failed"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(expire_old_journeys()),
        trigger=CronTrigger(hour=0, minute=30, timezone=IST),
        id="expire_journeys"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(check_subscription_expiry()),
        trigger=CronTrigger(hour=0, minute=0, timezone=IST),
        id="check_expiry"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(retry_pending_consent()),
        trigger=CronTrigger(hour=10, minute=0, timezone=IST),
        id="consent_retry"
    )
    # ── Voice call fallback — fires 24hrs after WhatsApp, no reply ────────
    scheduler.add_job(
        lambda: asyncio.create_task(trigger_voice_fallback_calls()),
        trigger=CronTrigger(hour=9, minute=30, timezone=IST),
        id="voice_fallback"
    )

    scheduler.start()
    print("[Scheduler] Started — including voice fallback at 09:30 IST")