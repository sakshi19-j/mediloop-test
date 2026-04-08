from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, timedelta
from database import supabase
from whatsapp import send_reminder
import asyncio
import pytz

scheduler = AsyncIOScheduler()

IST = pytz.timezone("Asia/Kolkata")


async def send_reminders_for_offset(days_offset: int, reminder_type: str):
    """
    Generic reminder sender for any day offset.
    reminder_type: '3day', '1day', '2day' etc — used to avoid duplicate sends.
    """
    target_date = (date.today() + timedelta(days=days_offset)).isoformat()
    print(f"[Scheduler] [{reminder_type}] Running for due date: {target_date}")

    result = supabase.table("medicines") \
        .select("*, patients(name, phone, opted_out), pharmacies(name)") \
        .eq("next_due_date", target_date) \
        .eq("status", "active") \
        .eq("is_deleted", False) \
        .eq("is_paused", False) \
        .execute()

    medicines = result.data
    print(f"[Scheduler] [{reminder_type}] Found {len(medicines)} medicines due")

    sent = 0
    skipped = 0
    failed = 0

    for med in medicines:
        patient = med["patients"]
        pharmacy = med["pharmacies"]

        if patient.get("opted_out"):
            skipped += 1
            continue

        # Skip if this reminder_type was already successfully sent today for this medicine
        existing = supabase.table("reminder_logs") \
            .select("id") \
            .eq("medicine_id", med["id"]) \
            .eq("whatsapp_status", "whatsapp") \
            .eq("reminder_type", reminder_type) \
            .gte("sent_at", date.today().isoformat()) \
            .execute()

        if existing.data:
            print(f"[Scheduler] [{reminder_type}] Already sent today for medicine {med['id']}, skipping")
            skipped += 1
            continue

        result = await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=pharmacy["name"]
        )

        log_status = result["channel"]

        supabase.table("reminder_logs").insert({
            "medicine_id": med["id"],
            "patient_id": med["patient_id"],
            "pharmacy_id": med["pharmacy_id"],
            "whatsapp_status": log_status,
            "reminder_type": reminder_type,
        }).execute()

        if log_status != "failed":
            print(f"[Scheduler] [{reminder_type}] Sent to {patient['name']} for {med['name']}")
            sent += 1
        else:
            print(f"[Scheduler] [{reminder_type}] FAILED for {patient['name']}: {result.get('error')}")
            failed += 1

    print(f"[Scheduler] [{reminder_type}] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


async def send_3day_reminders():
    """Fires at 9am — medicines due in 3 days"""
    await send_reminders_for_offset(3, "3day")


async def send_1day_reminders():
    """Fires at 9am — medicines due tomorrow (24hrs before)"""
    await send_reminders_for_offset(1, "1day")


async def send_due_reminders():
    """Fires at 9am — medicines due in 2 days (existing behaviour kept)"""
    await send_reminders_for_offset(2, "2day")


async def retry_failed_reminders():
    """Runs 2 hours after main job to retry any failures from today"""
    today = date.today().isoformat()

    failed = supabase.table("reminder_logs") \
        .select("*, medicines(name, patient_id, pharmacy_id, is_paused, is_deleted, patients(name, phone, opted_out), pharmacies(name))") \
        .eq("whatsapp_status", "failed") \
        .gte("sent_at", today) \
        .execute()

    print(f"[Retry] Found {len(failed.data)} failed reminders to retry")

    for log in failed.data:
        med = log["medicines"]
        if not med or med.get("is_paused") or med.get("is_deleted"):
            continue

        patient = med["patients"]
        if patient.get("opted_out"):
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
    # 3 days before due — 9am IST
    scheduler.add_job(
        lambda: asyncio.create_task(send_3day_reminders()),
        trigger=CronTrigger(hour=9, minute=0, timezone=IST),
        id="reminders_3day"
    )

    # 2 days before due — 9am IST (existing)
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger=CronTrigger(hour=9, minute=5, timezone=IST),
        id="reminders_2day"
    )

    # 1 day before due (24hrs) — 9am IST
    scheduler.add_job(
        lambda: asyncio.create_task(send_1day_reminders()),
        trigger=CronTrigger(hour=9, minute=10, timezone=IST),
        id="reminders_1day"
    )

    # Retry failures at 11am IST
    scheduler.add_job(
        lambda: asyncio.create_task(retry_failed_reminders()),
        trigger=CronTrigger(hour=11, minute=0, timezone=IST),
        id="retry_failed"
    )

    # Subscription expiry check at midnight IST
    scheduler.add_job(
        lambda: asyncio.create_task(check_subscription_expiry()),
        trigger=CronTrigger(hour=0, minute=0, timezone=IST),
        id="check_expiry"
    )

    scheduler.start()
    print("[Scheduler] Started — 3day/2day/1day reminders at 9am IST, retry at 11am IST")