from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, timedelta, datetime, timezone
from database import supabase
from whatsapp import send_bulk_reminder, send_reminder
import asyncio
import pytz
from collections import defaultdict

scheduler = AsyncIOScheduler()
IST = pytz.timezone("Asia/Kolkata")


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

    sent = 0
    skipped = 0
    failed = 0

    for (patient_id, pharmacy_id), patient_medicines in grouped.items():
        patient = patient_medicines[0]["patients"]
        pharmacy = patient_medicines[0]["pharmacies"]

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
            print(f"[Scheduler] [{reminder_type}] Already sent today for patient {patient['name']}, skipping")
            skipped += 1
            continue

        medicine_names = [m["name"] for m in patient_medicines]

        result = await send_bulk_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_names=medicine_names,
            pharmacy_name=pharmacy["name"]
        )

        log_status = result["channel"]

        for med in patient_medicines:
            journey_id = get_or_create_journey(med["id"], patient_id, pharmacy_id)

            supabase.table("reminder_logs").insert({
                "medicine_id": med["id"],
                "patient_id": patient_id,
                "pharmacy_id": pharmacy_id,
                "whatsapp_status": log_status,
                "reminder_type": reminder_type,
                "journey_id": journey_id,
            }).execute()

            journey = supabase.table("journeys").select("total_reminders_sent").eq("id", journey_id).execute()
            current_count = journey.data[0]["total_reminders_sent"] if journey.data else 0
            supabase.table("journeys").update({
                "total_reminders_sent": current_count + 1
            }).eq("id", journey_id).execute()

        if log_status != "failed":
            print(f"[Scheduler] [{reminder_type}] Bulk sent to {patient['name']} — {len(medicine_names)} medicines: {', '.join(medicine_names)}")
            sent += 1
        else:
            print(f"[Scheduler] [{reminder_type}] FAILED for {patient['name']}: {result.get('error')}")
            failed += 1

    print(f"[Scheduler] [{reminder_type}] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


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
    Task 7 — Consent retry.
    Finds patients who:
      - have NOT given consent (consent_given = False)
      - have NOT opted out
      - had consent requested 48+ hours ago
      - have NOT already received a follow-up (consent_followup_sent = False or null)
    Sends one follow-up consent message and marks consent_followup_sent = True.
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

        for patient in patients:
            try:
                # Fetch pharmacy name
                pharmacy = supabase.table("pharmacies") \
                    .select("name") \
                    .eq("id", patient["pharmacy_id"]) \
                    .execute()
                pharmacy_name = pharmacy.data[0]["name"] if pharmacy.data else "Your Pharmacy"

                from whatsapp import send_consent_request
                result = await send_consent_request(
                    phone=patient["phone"],
                    patient_name=patient["name"],
                    pharmacy_name=pharmacy_name
                )

                # Mark follow-up sent regardless of WhatsApp result
                # so we don't spam them if WhatsApp fails
                supabase.table("patients") \
                    .update({"consent_followup_sent": True}) \
                    .eq("id", patient["id"]) \
                    .execute()

                status = result.get("channel", "failed")
                print(f"[Consent Retry] Follow-up sent to {patient['name']} ({patient['phone']}) — {status}")

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

    scheduler.start()
    print("[Scheduler] Started")