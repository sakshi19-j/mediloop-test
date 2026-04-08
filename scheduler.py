"""
scheduler.py — APScheduler jobs for MediLoop

Jobs:
  1. send_due_reminders()     — 9:00 AM IST daily
                                Finds all medicines due in 2 days and sends WhatsApp reminders
  2. send_followup_reminders() — 10:00 AM IST daily  ← NEW
                                Auto follow-up: if a patient hasn't replied within 24h,
                                send a second reminder nudging them to reply YES/NO
  3. retry_failed_reminders()  — 11:00 AM IST daily
                                Retries any messages that failed to send today
  4. check_subscription_expiry() — midnight IST daily
                                Marks expired subscriptions
"""

import asyncio
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date, timedelta, datetime, timezone
from database import supabase
from whatsapp import send_reminder, send_text_message

scheduler = AsyncIOScheduler()
IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Main daily reminder job
# ─────────────────────────────────────────────────────────────────────────────

async def send_due_reminders():
    """
    Find all active medicines whose next_due_date is exactly 2 days from today
    and send a WhatsApp reminder to each patient (unless opted out or already sent).
    """
    target_date = (date.today() + timedelta(days=2)).isoformat()
    print(f"[Scheduler] Running for due date: {target_date}")

    result = supabase.table("medicines") \
        .select("*, patients(name, phone, opted_out), pharmacies(name)") \
        .eq("next_due_date", target_date) \
        .eq("status", "active") \
        .eq("is_deleted", False) \
        .eq("is_paused", False) \
        .execute()

    medicines = result.data
    print(f"[Scheduler] Found {len(medicines)} medicines due")

    sent = skipped = failed = 0

    for med in medicines:
        patient = med["patients"]
        pharmacy = med["pharmacies"]

        if patient.get("opted_out"):
            print(f"[Scheduler] Skipping opted-out patient: {patient['name']}")
            skipped += 1
            continue

        # Skip if a SUCCESSFUL reminder was already sent today
        existing = supabase.table("reminder_logs") \
            .select("id") \
            .eq("medicine_id", med["id"]) \
            .eq("whatsapp_status", "whatsapp") \
            .gte("sent_at", date.today().isoformat()) \
            .execute()

        if existing.data:
            print(f"[Scheduler] Already sent today for medicine {med['id']}, skipping")
            skipped += 1
            continue

        result = await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=pharmacy["name"],
        )

        log_status = result["channel"]  # "whatsapp" = success | "failed" = failure
        message_id = result.get("message_id", "")

        supabase.table("reminder_logs").insert({
            "medicine_id": med["id"],
            "patient_id": med["patient_id"],
            "pharmacy_id": med["pharmacy_id"],
            "whatsapp_status": log_status,
            "message_id": message_id,
            "patient_replied": False,
            "followup_sent": False,          # track whether follow-up was sent
        }).execute()

        if log_status == "whatsapp":
            print(f"[Scheduler] ✓ Sent to {patient['name']} for {med['name']}")
            sent += 1
        else:
            print(f"[Scheduler] ✗ FAILED for {patient['name']}: {result.get('error')}")
            failed += 1

    print(f"[Scheduler] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Follow-up job (NEW)
# ─────────────────────────────────────────────────────────────────────────────

async def send_followup_reminders():
    """
    Auto follow-up system:
    - Runs daily at 10 AM IST (1 hour after main job)
    - Finds all reminder_logs that:
        * were sent successfully (whatsapp_status = "whatsapp")
        * patient has NOT yet replied (patient_replied = False)
        * follow-up has NOT been sent yet (followup_sent = False)
        * were sent at least 20+ hours ago (so 9 AM yesterday's batch gets caught)
    - Sends a gentle nudge asking the patient to reply YES or NO

    This ensures pharmacies always know patient intent before the refill date.
    """
    # Look for logs sent before today (yesterday's 9am batch)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    today = date.today().isoformat()

    print(f"[FollowUp] Checking for unreplied reminders sent before {cutoff}")

    logs = supabase.table("reminder_logs") \
        .select(
            "id, medicine_id, patient_id, pharmacy_id, sent_at, "
            "medicines(name, status, is_paused, is_deleted, "
            "patients(name, phone, opted_out), pharmacies(name))"
        ) \
        .eq("whatsapp_status", "whatsapp") \
        .eq("patient_replied", False) \
        .eq("followup_sent", False) \
        .lt("sent_at", cutoff) \
        .execute()

    pending = logs.data
    print(f"[FollowUp] Found {len(pending)} unreplied reminders")

    sent = skipped = failed = 0

    for log in pending:
        med = log.get("medicines")
        if not med:
            skipped += 1
            continue

        # Skip if medicine is now inactive / paused / deleted
        if med.get("is_paused") or med.get("is_deleted") or med.get("status") == "reorder_requested":
            supabase.table("reminder_logs") \
                .update({"followup_sent": True}) \
                .eq("id", log["id"]) \
                .execute()
            skipped += 1
            continue

        patient = med.get("patients")
        pharmacy = med.get("pharmacies")

        if not patient or patient.get("opted_out"):
            skipped += 1
            continue

        follow_up_text = (
            f"Hi {patient['name']}! 👋 This is a follow-up reminder from "
            f"{pharmacy['name']}.\n\n"
            f"Your *{med['name']}* refill is due in 2 days.\n\n"
            "Please reply:\n"
            "✅ *YES* — to reorder\n"
            "❌ *NO* — to skip this cycle\n\n"
            "Reply *STOP* anytime to stop receiving reminders."
        )

        result = await send_text_message(
            phone=patient["phone"],
            text=follow_up_text,
        )

        # Mark follow-up as sent regardless of success
        # (we don't want infinite retries of follow-ups)
        supabase.table("reminder_logs") \
            .update({
                "followup_sent": True,
                "followup_status": result["channel"],
            }) \
            .eq("id", log["id"]) \
            .execute()

        if result["channel"] == "whatsapp":
            print(f"[FollowUp] ✓ Follow-up sent to {patient['name']} for {med['name']}")
            sent += 1
        else:
            print(f"[FollowUp] ✗ Failed for {patient['name']}: {result.get('error')}")
            failed += 1

    print(f"[FollowUp] Done — sent: {sent}, skipped: {skipped}, failed: {failed}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Retry failed sends
# ─────────────────────────────────────────────────────────────────────────────

async def retry_failed_reminders():
    """Runs 2 hours after main job to retry any failed sends from today."""
    today = date.today().isoformat()

    failed_logs = supabase.table("reminder_logs") \
        .select(
            "*, medicines(name, patient_id, pharmacy_id, is_paused, is_deleted, "
            "patients(name, phone, opted_out), pharmacies(name))"
        ) \
        .eq("whatsapp_status", "failed") \
        .gte("sent_at", today) \
        .execute()

    print(f"[Retry] Found {len(failed_logs.data)} failed reminders to retry")

    for log in failed_logs.data:
        med = log.get("medicines")
        if not med or med.get("is_paused") or med.get("is_deleted"):
            continue

        patient = med["patients"]
        if patient.get("opted_out"):
            continue

        result = await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=med["pharmacies"]["name"],
        )

        new_status = f"retry_{result['channel']}"
        supabase.table("reminder_logs") \
            .update({"whatsapp_status": new_status}) \
            .eq("id", log["id"]) \
            .execute()

        print(f"[Retry] {patient['name']} → {new_status}")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Subscription expiry check
# ─────────────────────────────────────────────────────────────────────────────

async def check_subscription_expiry():
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


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler setup
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler():
    # 1. Main reminder job — 9:00 AM IST
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger=CronTrigger(hour=9, minute=0, timezone=IST),
        id="daily_reminders",
    )

    # 2. Follow-up job — 10:00 AM IST (catches yesterday's unreplied messages)
    scheduler.add_job(
        lambda: asyncio.create_task(send_followup_reminders()),
        trigger=CronTrigger(hour=10, minute=0, timezone=IST),
        id="followup_reminders",
    )

    # 3. Retry failed sends — 11:00 AM IST
    scheduler.add_job(
        lambda: asyncio.create_task(retry_failed_reminders()),
        trigger=CronTrigger(hour=11, minute=0, timezone=IST),
        id="retry_failed",
    )

    # 4. Subscription expiry — midnight IST
    scheduler.add_job(
        lambda: asyncio.create_task(check_subscription_expiry()),
        trigger=CronTrigger(hour=0, minute=0, timezone=IST),
        id="check_expiry",
    )

    scheduler.start()
    print("[Scheduler] Started — 9 AM reminders | 10 AM follow-ups | 11 AM retries | midnight expiry check")
