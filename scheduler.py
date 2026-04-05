from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import date, timedelta
from database import supabase
from whatsapp import send_reminder
import asyncio

scheduler = AsyncIOScheduler()

async def send_due_reminders():
    target_date = (date.today() + timedelta(days=2)).isoformat()
    
    # Fetch all medicines due in 2 days that are active
    result = supabase.table("medicines")\
        .select("*, patients(name, phone), pharmacies(name)")\
        .eq("next_due_date", target_date)\
        .eq("status", "active")\
        .execute()
    
    medicines = result.data
    print(f"[Scheduler] Found {len(medicines)} reminders to send")
    
    for med in medicines:
        patient = med["patients"]
        pharmacy = med["pharmacies"]
        
        # Check if reminder already sent today for this medicine
        existing = supabase.table("reminder_logs")\
            .select("id")\
            .eq("medicine_id", med["id"])\
            .gte("sent_at", date.today().isoformat())\
            .execute()
        
        if existing.data:
            continue  # already sent today, skip
        
        # Send WhatsApp
        await send_reminder(
            phone=patient["phone"],
            patient_name=patient["name"],
            medicine_name=med["name"],
            pharmacy_name=pharmacy["name"]
        )
        
        # Log it
        supabase.table("reminder_logs").insert({
            "medicine_id": med["id"],
            "patient_id": med["patient_id"],
            "pharmacy_id": med["pharmacy_id"],
            "whatsapp_status": "sent"
        }).execute()

def start_scheduler():
    scheduler.add_job(
        lambda: asyncio.create_task(send_due_reminders()),
        trigger="cron",
        hour=9,
        minute=0
    )
    scheduler.start()