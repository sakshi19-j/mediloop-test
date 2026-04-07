from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import date, timedelta
from database import supabase
from whatsapp import send_reminder
import asyncio

scheduler = AsyncIOScheduler()

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

        # Skip if reminder already sent today
        existing = supabase.table("reminder_logs")\
            .select("id")\
            .eq("medicine_id", med["id"])\
            .gte("sent_at", date.today().isoformat())\
            .execute()

        if existing.data:
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
        status = "sent" if result["channel"] != "failed" else "failed"

        supabase.table("reminder_logs").insert({
            "medicine_id": med["id"],
            "patient_id": med["patient_id"],
            "pharmacy_id": med["pharmacy_id"],
            "whatsapp_status": result["channel"],
        }).execute()

        if status == "sent":
            sent += 1
        else:
            failed += 1

    print(f"[Scheduler] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")

async def retry_failed_reminders():
    """Runs 2 hours after main job to retry any failures"""
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    failed = supabase.table("reminder_logs")\
        .select("*, medicines(name, patient_id, pharmacy_id, is_paused, is_deleted, patients(name, phone, opted_out), pharmacies(name))")\
        .eq("whatsapp_status", "failed")\
        .gte("sent_at", yesterday)\
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

        # Update log with retry result
        supabase.table("reminder_logs")\
            .update({"whatsapp_status": f"retry_{result['channel']}"})\
            .eq("id", log["id"])\
            .execute()
        

async def check_subscription_expiry():
    from datetime import date
    today = date.today().isoformat()

    # Find subscriptions that expired
    expired = supabase.table("subscriptions")\
        .select("*, pharmacies(name, email)")\
        .eq("status", "active")\
        .lt("ends_at", today)\
        .neq("plan", "trial")\
        .execute()

    for sub in expired.data:
        # Mark as expired
        supabase.table("subscriptions")\
            .update({"status": "expired"})\
            .eq("id", sub["id"])\
            .execute()

        print(f"[Billing] Subscription expired: {sub['pharmacies']['name']}")

def start_scheduler():
    # Main job at 9am
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger="cron",
        hour=9,
        minute=0,
        id="daily_reminders"
    )

    # Retry job at 11am
    scheduler.add_job(
        lambda: asyncio.create_task(retry_failed_reminders()),
        trigger="cron",
        hour=11,
        minute=0,
        id="retry_failed"
    )

    scheduler.add_job(
        lambda: asyncio.create_task(check_subscription_expiry()),
        trigger="cron",
        hour=0,
        minute=0,
        id="check_expiry"
    )

    scheduler.start()
    print("[Scheduler] Started — daily at 9am, retry at 11am")