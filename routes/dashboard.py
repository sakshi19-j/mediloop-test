from fastapi import APIRouter, HTTPException, Header
from database import supabase
from datetime import date, timedelta
from typing import Optional

router = APIRouter()

@router.get("/stats")
def get_stats(
    pharmacy_id: str = Header(...),
    period: Optional[str] = "month"
):
    try:
        if period == "week":
            start_date = (date.today() - timedelta(days=7)).isoformat()
        elif period == "quarter":
            start_date = (date.today() - timedelta(days=90)).isoformat()
        elif period == "year":
            start_date = (date.today() - timedelta(days=365)).isoformat()
        else:
            start_date = date.today().replace(day=1).isoformat()

        patients = supabase.table("patients")\
            .select("id", count="exact")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("is_active", True)\
            .eq("is_deleted", False)\
            .execute()

        reminders = supabase.table("reminder_logs")\
            .select("id, converted")\
            .eq("pharmacy_id", pharmacy_id)\
            .gte("sent_at", start_date)\
            .execute()

        total_sent = len(reminders.data)
        converted = sum(1 for r in reminders.data if r["converted"])
        conversion_rate = round((converted / total_sent * 100), 1) if total_sent > 0 else 0

        in_7_days = (date.today() + timedelta(days=7)).isoformat()
        today = date.today().isoformat()

        upcoming = supabase.table("medicines")\
            .select("id", count="exact")\
            .eq("pharmacy_id", pharmacy_id)\
            .eq("status", "active")\
            .eq("is_deleted", False)\
            .eq("is_paused", False)\
            .gte("next_due_date", today)\
            .lte("next_due_date", in_7_days)\
            .execute()

        sub = supabase.table("subscriptions")\
            .select("plan, patient_limit, ends_at")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        subscription = sub.data[0] if sub.data else {}

        return {
            "total_patients": patients.count,
            "reminders_sent": total_sent,
            "conversions": converted,
            "conversion_rate_percent": conversion_rate,
            "upcoming_this_week": upcoming.count,
            "period": period,
            "subscription": subscription
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))