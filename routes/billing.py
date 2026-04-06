from fastapi import APIRouter, HTTPException, Header, Request
from database import supabase
import razorpay
import os

router = APIRouter()

PLANS = {
    "basic": {"price": 49900, "patient_limit": 50, "label": "Basic"},
    "standard": {"price": 99900, "patient_limit": 200, "label": "Standard"},
    "premium": {"price": 199900, "patient_limit": 99999, "label": "Premium"}
}

def get_razorpay_client():
    return razorpay.Client(
        auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET"))
    )

@router.post("/create-order")
def create_order(plan: str, pharmacy_id: str = Header(...)):
    try:
        if plan not in PLANS:
            raise HTTPException(status_code=400, detail="Invalid plan")

        client = get_razorpay_client()
        plan_data = PLANS[plan]

        order = client.order.create({
            "amount": plan_data["price"],
            "currency": "INR",
            "notes": {
                "pharmacy_id": pharmacy_id,
                "plan": plan
            }
        })

        return {
            "order_id": order["id"],
            "amount": plan_data["price"],
            "currency": "INR",
            "plan": plan,
            "key": os.getenv("RAZORPAY_KEY_ID")
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/verify-payment")
def verify_payment(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
    plan: str,
    pharmacy_id: str = Header(...)
):
    try:
        client = get_razorpay_client()

        client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature
        })

        plan_data = PLANS[plan]

        from datetime import datetime, timedelta
        ends_at = (datetime.utcnow() + timedelta(days=30)).isoformat()

        supabase.table("subscriptions")\
            .update({
                "plan": plan,
                "status": "active",
                "patient_limit": plan_data["patient_limit"],
                "razorpay_payment_id": razorpay_payment_id,
                "razorpay_order_id": razorpay_order_id,
                "ends_at": ends_at
            })\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        supabase.table("pharmacies")\
            .update({"subscription_plan": plan})\
            .eq("id", pharmacy_id)\
            .execute()

        return {"message": "Payment verified", "plan": plan}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/plans")
def get_plans():
    return PLANS

@router.get("/subscription")
def get_subscription(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("subscriptions")\
            .select("*")\
            .eq("pharmacy_id", pharmacy_id)\
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="No subscription found")

        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))