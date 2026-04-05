from fastapi import APIRouter, Header
from database import supabase
from datetime import date, timedelta

router = APIRouter()

@router.get("/stats")
def get_stats(pharmacy_id: str = Header(...)):
    # Total active patients
    patients = supabase.table("patients")\
        .select("id", count="exact")\
        .eq("pharmacy_id", pharmacy_id)\
        .eq("is_active", True)\
        .execute()

    # Reminders sent this month
    month_start = date.today().replace(day=1).isoformat()
    reminders = supabase.table("reminder_logs")\
        .select("id, converted", count="exact")\
        .eq("pharmacy_id", pharmacy_id)\
        .gte("sent_at", month_start)\
        .execute()

    total_sent = reminders.count or 0
    converted = sum(1 for r in reminders.data if r["converted"])
    conversion_rate = round((converted / total_sent * 100), 1) if total_sent > 0 else 0

    # Upcoming this week
    in_7 = (date.today() + timedelta(days=7)).isoformat()
    upcoming = supabase.table("medicines")\
        .select("id", count="exact")\
        .eq("pharmacy_id", pharmacy_id)\
        .eq("status", "active")\
        .gte("next_due_date", date.today().isoformat())\
        .lte("next_due_date", in_7)\
        .execute()

    return {
        "total_patients": patients.count,
        "reminders_sent_this_month": total_sent,
        "conversions_this_month": converted,
        "conversion_rate_percent": conversion_rate,
        "upcoming_this_week": upcoming.count
    }