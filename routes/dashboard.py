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

        # Patients
        patients = supabase.table("patients") \
            .select("id", count="exact") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("is_active", True) \
            .eq("is_deleted", False) \
            .execute()

        # Journey-based metrics (correct conversion logic)
        journeys = supabase.table("journeys") \
            .select("id, status, conversion_flag, total_reminders_sent") \
            .eq("pharmacy_id", pharmacy_id) \
            .gte("start_date", start_date) \
            .execute()

        total_journeys = len(journeys.data)
        successful_conversions = sum(1 for j in journeys.data if j["conversion_flag"] == 1)
        active_journeys = sum(1 for j in journeys.data if j["status"] == "active")
        total_reminders_sent = sum(j["total_reminders_sent"] for j in journeys.data)

        conversion_rate = round((successful_conversions / total_journeys * 100), 1) if total_journeys > 0 else 0
        avg_reminders_per_conversion = round(
            total_reminders_sent / successful_conversions, 1
        ) if successful_conversions > 0 else 0

        # Upcoming refills this week
        in_7_days = (date.today() + timedelta(days=7)).isoformat()
        today = date.today().isoformat()

        upcoming = supabase.table("medicines") \
            .select("id", count="exact") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("status", "active") \
            .eq("is_deleted", False) \
            .eq("is_paused", False) \
            .gte("next_due_date", today) \
            .lte("next_due_date", in_7_days) \
            .execute()

        # Subscription
        sub = supabase.table("subscriptions") \
            .select("plan, patient_limit, ends_at") \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        subscription = sub.data[0] if sub.data else {}

        return {
            "total_patients": patients.count,
            "active_journeys": active_journeys,
            "successful_conversions": successful_conversions,
            "conversion_rate_percent": conversion_rate,
            "total_reminders_sent": total_reminders_sent,
            "avg_reminders_per_conversion": avg_reminders_per_conversion,
            "upcoming_this_week": upcoming.count,
            "period": period,
            "subscription": subscription
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))