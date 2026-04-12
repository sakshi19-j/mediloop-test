from fastapi import APIRouter, HTTPException, Header
from database import supabase
from datetime import date, timedelta
from typing import Optional

router = APIRouter()


def calc_conversion(journeys: list) -> dict:
    """Helper — given a list of journey rows, return conversion metrics."""
    total = len(journeys)
    converted = sum(1 for j in journeys if j["conversion_flag"] == 1)
    active = sum(1 for j in journeys if j["status"] == "active")
    reminders = sum(j["total_reminders_sent"] for j in journeys)
    rate = round((converted / total * 100), 1) if total > 0 else 0
    avg = round(reminders / converted, 1) if converted > 0 else 0
    return {
        "total_journeys": total,
        "conversions": converted,
        "active": active,
        "reminders_sent": reminders,
        "conversion_rate_percent": rate,
        "avg_reminders_per_conversion": avg,
    }


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

        # ── Patients ──────────────────────────────────────────────────────
        patients = supabase.table("patients") \
            .select("id", count="exact") \
            .eq("pharmacy_id", pharmacy_id) \
            .eq("is_active", True) \
            .eq("is_deleted", False) \
            .execute()

        # ── Journeys for selected period ──────────────────────────────────
        journeys = supabase.table("journeys") \
            .select("id, status, conversion_flag, total_reminders_sent") \
            .eq("pharmacy_id", pharmacy_id) \
            .gte("start_date", start_date) \
            .execute()

        overall = calc_conversion(journeys.data)

        # ── This week vs last week breakdown ─────────────────────────────
        this_week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        last_week_start = (date.today() - timedelta(days=date.today().weekday() + 7)).isoformat()
        last_week_end = (date.today() - timedelta(days=date.today().weekday() + 1)).isoformat()

        this_week_journeys = supabase.table("journeys") \
            .select("id, status, conversion_flag, total_reminders_sent") \
            .eq("pharmacy_id", pharmacy_id) \
            .gte("start_date", this_week_start) \
            .execute()

        last_week_journeys = supabase.table("journeys") \
            .select("id, status, conversion_flag, total_reminders_sent") \
            .eq("pharmacy_id", pharmacy_id) \
            .gte("start_date", last_week_start) \
            .lte("start_date", last_week_end) \
            .execute()

        this_week = calc_conversion(this_week_journeys.data)
        last_week = calc_conversion(last_week_journeys.data)

        # Week-over-week conversion rate change
        wow_change = round(
            this_week["conversion_rate_percent"] - last_week["conversion_rate_percent"], 1
        )

        # ── Upcoming refills this week ────────────────────────────────────
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

        # ── Unrecognised replies — pending chemist review ─────────────────
        try:
            unrecognised = supabase.table("unrecognised_replies") \
                .select("id", count="exact") \
                .eq("pharmacy_id", pharmacy_id) \
                .eq("reviewed", False) \
                .execute()
            unrecognised_count = unrecognised.count or 0
        except Exception:
            # Table may not exist yet on older deployments
            unrecognised_count = 0

        # ── Subscription ──────────────────────────────────────────────────
        sub = supabase.table("subscriptions") \
            .select("plan, patient_limit, ends_at") \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        subscription = sub.data[0] if sub.data else {}

        return {
            # Overall for selected period
            "total_patients": patients.count,
            "active_journeys": overall["active"],
            "successful_conversions": overall["conversions"],
            "conversion_rate_percent": overall["conversion_rate_percent"],
            "total_reminders_sent": overall["reminders_sent"],
            "avg_reminders_per_conversion": overall["avg_reminders_per_conversion"],
            "upcoming_this_week": upcoming.count,
            "period": period,
            "subscription": subscription,

            # Week-over-week breakdown
            "weekly_breakdown": {
                "this_week": this_week,
                "last_week": last_week,
                "wow_change_percent": wow_change,
                "trending_up": wow_change >= 0,
            },

            # Unrecognised replies needing attention
            "unrecognised_replies_pending": unrecognised_count,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))