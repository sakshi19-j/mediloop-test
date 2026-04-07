from fastapi import APIRouter, HTTPException, Header, Request
from database import supabase
import razorpay
import os
from datetime import datetime, timedelta

router = APIRouter()

# ── Plan definitions ──────────────────────────────────────────────────────────
# Prices in paise (INR × 100). Monthly base prices.
PLANS = {
    "basic":    {"monthly_price": 49900,  "patient_limit": 50,    "label": "Basic"},
    "standard": {"monthly_price": 99900,  "patient_limit": 200,   "label": "Standard"},
    "premium":  {"monthly_price": 199900, "patient_limit": 99999, "label": "Premium"},
}

YEARLY_DISCOUNT = 0.15   # 15% off for annual billing
YEARLY_MONTHS   = 12


def get_plan_price(plan: str, billing_cycle: str) -> int:
    """
    Returns final price in paise.
    billing_cycle: "monthly" | "yearly"
    Yearly = monthly × 12 × 0.85
    """
    base = PLANS[plan]["monthly_price"]
    if billing_cycle == "yearly":
        return int(base * YEARLY_MONTHS * (1 - YEARLY_DISCOUNT))
    return base


def get_razorpay_client():
    return razorpay.Client(
        auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET"))
    )


# ── Coupon validation helper ──────────────────────────────────────────────────

def validate_coupon(code: str, pharmacy_id: str) -> dict:
    """
    Returns {"valid": True, "discount_pct": 100} or raises HTTPException.
    Also checks the pharmacy hasn't already used a coupon.
    """
    code = code.strip().upper()

    # 1. Fetch coupon row
    result = supabase.table("coupons") \
        .select("*") \
        .eq("code", code) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=400, detail="Invalid coupon code")

    coupon = result.data[0]

    # 2. Check if already fully redeemed
    if coupon["used_count"] >= coupon["max_uses"]:
        raise HTTPException(status_code=400, detail="Coupon has already been fully redeemed")

    # 3. Check this pharmacy hasn't used it before
    used_check = supabase.table("coupon_redemptions") \
        .select("id") \
        .eq("coupon_code", code) \
        .eq("pharmacy_id", pharmacy_id) \
        .execute()

    if used_check.data:
        raise HTTPException(status_code=400, detail="You have already used this coupon")

    return {
        "valid": True,
        "discount_pct": coupon["discount_pct"],  # e.g. 100 for free
        "coupon_id": coupon["id"],
    }


def record_coupon_use(code: str, pharmacy_id: str):
    """Increments used_count and logs the redemption."""
    code = code.strip().upper()

    # Log redemption
    supabase.table("coupon_redemptions").insert({
        "coupon_code": code,
        "pharmacy_id": pharmacy_id,
        "redeemed_at": datetime.utcnow().isoformat(),
    }).execute()

    # Increment used_count
    result = supabase.table("coupons").select("used_count").eq("code", code).execute()
    current = result.data[0]["used_count"]
    supabase.table("coupons") \
        .update({"used_count": current + 1}) \
        .eq("code", code) \
        .execute()


def activate_subscription(pharmacy_id: str, plan: str, billing_cycle: str,
                           payment_id: str = None, order_id: str = None,
                           coupon_code: str = None):
    """
    Central function — updates subscriptions + pharmacies tables.
    Works for both paid and 100%-off coupon flows.
    """
    days = 365 if billing_cycle == "yearly" else 30
    ends_at = (datetime.utcnow() + timedelta(days=days)).isoformat()
    plan_data = PLANS[plan]

    payload = {
        "plan": plan,
        "billing_cycle": billing_cycle,
        "status": "active",
        "patient_limit": plan_data["patient_limit"],
        "ends_at": ends_at,
    }
    if payment_id:
        payload["razorpay_payment_id"] = payment_id
    if order_id:
        payload["razorpay_order_id"] = order_id
    if coupon_code:
        payload["coupon_code"] = coupon_code.strip().upper()

    # Upsert so it works even if no subscription row exists yet
    existing = supabase.table("subscriptions") \
        .select("id") \
        .eq("pharmacy_id", pharmacy_id) \
        .execute()

    if existing.data:
        supabase.table("subscriptions") \
            .update(payload) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()
    else:
        payload["pharmacy_id"] = pharmacy_id
        supabase.table("subscriptions").insert(payload).execute()

    supabase.table("pharmacies") \
        .update({"subscription_plan": plan}) \
        .eq("id", pharmacy_id) \
        .execute()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/plans")
def get_plans():
    """Returns plans with both monthly and yearly prices."""
    result = {}
    for key, data in PLANS.items():
        monthly = data["monthly_price"]
        yearly  = int(monthly * YEARLY_MONTHS * (1 - YEARLY_DISCOUNT))
        result[key] = {
            **data,
            "monthly_price": monthly,
            "yearly_price":  yearly,
            "yearly_saving": int(monthly * YEARLY_MONTHS - yearly),
        }
    return result


@router.post("/validate-coupon")
def validate_coupon_route(
    code: str,
    plan: str,
    billing_cycle: str = "monthly",
    pharmacy_id: str = Header(...)
):
    """
    Validates a coupon without redeeming it.
    Returns the discounted price so the frontend can show it.
    """
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if billing_cycle not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail="billing_cycle must be monthly or yearly")

    coupon_info = validate_coupon(code, pharmacy_id)  # raises if invalid
    base_price  = get_plan_price(plan, billing_cycle)
    discount    = coupon_info["discount_pct"]
    final_price = int(base_price * (1 - discount / 100))

    return {
        "valid":         True,
        "discount_pct":  discount,
        "original_price": base_price,
        "final_price":   final_price,
        "message":       f"{discount}% discount applied!",
    }


@router.post("/create-order")
def create_order(
    plan: str,
    billing_cycle: str = "monthly",
    coupon_code: str = None,
    pharmacy_id: str = Header(...)
):
    """
    Creates a Razorpay order.
    If a valid 100% coupon is applied, returns amount=0 and skips Razorpay.
    """
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    if billing_cycle not in ("monthly", "yearly"):
        raise HTTPException(status_code=400, detail="Invalid billing_cycle")

    base_price  = get_plan_price(plan, billing_cycle)
    final_price = base_price
    coupon_info = None

    # Validate coupon if provided
    if coupon_code and coupon_code.strip():
        coupon_info = validate_coupon(coupon_code, pharmacy_id)
        discount    = coupon_info["discount_pct"]
        final_price = int(base_price * (1 - discount / 100))

    # 100% discount — activate directly, no Razorpay needed
    if final_price == 0:
        record_coupon_use(coupon_code, pharmacy_id)
        activate_subscription(
            pharmacy_id   = pharmacy_id,
            plan          = plan,
            billing_cycle = billing_cycle,
            coupon_code   = coupon_code,
        )
        return {
            "free":          True,
            "message":       "Plan activated with coupon — no payment required!",
            "plan":          plan,
            "billing_cycle": billing_cycle,
        }

    # Paid flow — create Razorpay order
    try:
        client = get_razorpay_client()
        order  = client.order.create({
            "amount":   final_price,
            "currency": "INR",
            "notes": {
                "pharmacy_id":   pharmacy_id,
                "plan":          plan,
                "billing_cycle": billing_cycle,
                "coupon_code":   coupon_code or "",
            }
        })

        return {
            "free":          False,
            "order_id":      order["id"],
            "amount":        final_price,
            "original_amount": base_price,
            "currency":      "INR",
            "plan":          plan,
            "billing_cycle": billing_cycle,
            "key":           os.getenv("RAZORPAY_KEY_ID"),
            "coupon_applied": coupon_info is not None,
            "discount_pct":  coupon_info["discount_pct"] if coupon_info else 0,
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
    billing_cycle: str = "monthly",
    coupon_code: str = None,
    pharmacy_id: str = Header(...)
):
    """
    Verifies Razorpay signature and activates subscription.
    Also records coupon redemption if one was used.
    """
    try:
        client = get_razorpay_client()
        client.utility.verify_payment_signature({
            "razorpay_order_id":   razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature":  razorpay_signature,
        })

        # Record coupon use (if partial discount coupon was applied)
        if coupon_code and coupon_code.strip():
            try:
                record_coupon_use(coupon_code, pharmacy_id)
            except Exception:
                pass  # Don't fail payment verification over this

        activate_subscription(
            pharmacy_id   = pharmacy_id,
            plan          = plan,
            billing_cycle = billing_cycle,
            payment_id    = razorpay_payment_id,
            order_id      = razorpay_order_id,
            coupon_code   = coupon_code,
        )

        return {
            "message":       "Payment verified and plan activated",
            "plan":          plan,
            "billing_cycle": billing_cycle,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/subscription")
def get_subscription(pharmacy_id: str = Header(...)):
    try:
        result = supabase.table("subscriptions") \
            .select("*") \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        if not result.data:
            raise HTTPException(status_code=404, detail="No subscription found")

        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/renew")
def renew_subscription(
    razorpay_payment_id: str,
    razorpay_order_id: str,
    razorpay_signature: str,
    plan: str,
    billing_cycle: str = "monthly",
    pharmacy_id: str = Header(...)
):
    try:
        client = get_razorpay_client()
        client.utility.verify_payment_signature({
            "razorpay_order_id":   razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature":  razorpay_signature,
        })

        days    = 365 if billing_cycle == "yearly" else 30
        new_end = (datetime.utcnow() + timedelta(days=days)).isoformat()

        supabase.table("subscriptions") \
            .update({
                "status":              "active",
                "billing_cycle":       billing_cycle,
                "ends_at":             new_end,
                "next_billing_date":   (datetime.utcnow() + timedelta(days=days)).date().isoformat(),
                "razorpay_payment_id": razorpay_payment_id,
            }) \
            .eq("pharmacy_id", pharmacy_id) \
            .execute()

        return {"message": "Subscription renewed", "next_billing": new_end}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))