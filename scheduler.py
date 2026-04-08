from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, timedelta
from database import supabase
from whatsapp import send_reminder
import asyncio
import pytz

scheduler = AsyncIOScheduler()

IST = pytz.timezone("Asia/Kolkata")


async def send_due_reminders():
    target_date = (date.today() + timedelta(days=2)).isoformat()
    print(f"[Scheduler] Running for due date: {target_date}")

    result = supabase.table("medicines")\
        .select("*, patients(name, phone, opted_out), pharmacies(name)")\
        .eq("next_due_date", target_date)\
        .eq("status", "active")\
        .eq("is_deleted", False)\
        .eq("is_paused", False)\
        .execute()

    medicines = result.data
    print(f"[Scheduler] Found {len(medicines)} medicines due")

    sent = 0
    skipped = 0
    failed = 0

    for med in medicines:
        patient = med["patients"]
        pharmacy = med["pharmacies"]

        # Skip opted-out patients
        if patient.get("opted_out"):
            print(f"[Scheduler] Skipping opted-out patient {patient['name']}")
            skipped += 1
            continue

        # Skip ONLY if a SUCCESSFUL reminder was already sent today
        # (don't block on failed ones — those should be retried)
        existing = supabase.table("reminder_logs")\
            .select("id")\
            .eq("medicine_id", med["id"])\
            .eq("whatsapp_status", "whatsapp")\
            .gte("sent_at", date.today().isoformat())\
            .execute()

        if existing.data:
            print(f"[Scheduler] Already sent successfully today for medicine {med['id']}, skipping")
            skipped += 1
            continue

        # Send reminder
        result = await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=pharmacy["name"]
        )

        # Log result
        log_status = result["channel"]  # "whatsapp" = success, "failed" = failure

        supabase.table("reminder_logs").insert({
            "medicine_id": med["id"],
            "patient_id": med["patient_id"],
            "pharmacy_id": med["pharmacy_id"],
            "whatsapp_status": log_status,
        }).execute()

        if log_status != "failed":
            print(f"[Scheduler] Sent to {patient['name']} for {med['name']}")
            sent += 1
        else:
            print(f"[Scheduler] FAILED for {patient['name']}: {result.get('error')}")
            failed += 1

    print(f"[Scheduler] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


async def retry_failed_reminders():
    """Runs 2 hours after main job to retry any failures from today"""
    today = date.today().isoformat()

    failed = supabase.table("reminder_logs")\
        .select("*, medicines(name, patient_id, pharmacy_id, is_paused, is_deleted, patients(name, phone, opted_out), pharmacies(name))")\
        .eq("whatsapp_status", "failed")\
        .gte("sent_at", today)\
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
        supabase.table("reminder_logs")\
            .update({"whatsapp_status": new_status})\
            .eq("id", log["id"])\
            .execute()

        print(f"[Retry] {patient['name']} → {new_status}")


async def check_subscription_expiry():
    from datetime import date
    today = date.today().isoformat()

    expired = supabase.table("subscriptions")\
        .select("*, pharmacies(name, email)")\
        .eq("status", "active")\
        .lt("ends_at", today)\
        .neq("plan", "trial")\
        .execute()

    for sub in expired.data:
        supabase.table("subscriptions")\
            .update({"status": "expired"})\
            .eq("id", sub["id"])\
            .execute()

        print(f"[Billing] Subscription expired: {sub['pharmacies']['name']}")


def start_scheduler():
    # Main job at 9am IST daily
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger=CronTrigger(hour=9, minute=0, timezone=IST),
        id="daily_reminders"
    )

    # Retry job at 11am IST
    scheduler.add_job(
        lambda: asyncio.create_task(retry_failed_reminders()),
        trigger=CronTrigger(hour=11, minute=0, timezone=IST),
        id="retry_failed"
    )

    # Expiry check at midnight IST
    scheduler.add_job(
        lambda: asyncio.create_task(check_subscription_expiry()),
        trigger=CronTrigger(hour=0, minute=0, timezone=IST),
        id="check_expiry"
    )

    scheduler.start()
    print("[Scheduler] Started — daily at 9am IST, retry at 11am IST")