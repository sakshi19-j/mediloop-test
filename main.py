from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard, billing, prescriptions

app = FastAPI(
    title="MediLoop API",
    version="1.0.0",
    description="Pharmacy recurring revenue management"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])
app.include_router(patients.router, prefix="/api/v1/patients", tags=["Patients"])
app.include_router(medicines.router, prefix="/api/v1/medicines", tags=["Medicines"])
app.include_router(dashboard.router, prefix="/api/v1/dashboard", tags=["Dashboard"])
app.include_router(billing.router, prefix="/api/v1/billing", tags=["Billing"])
app.include_router(prescriptions.router, prefix="/api/v1/prescriptions", tags=["Prescriptions"])

@app.on_event("startup")
async def startup():
    start_scheduler()

@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}

@app.get("/test-whatsapp")
async def test_whatsapp():
    from whatsapp import send_reminder
    result = await send_reminder(
        phone="918237007450",
        patient_name="Sudesh Dahale",
        medicine_name="Metformin 500mg",
        pharmacy_name="SahilMedical"
    )
    return result

from fastapi import Request, Form

@app.post("/webhook/twilio")
async def twilio_webhook(
    From: str = Form(...),
    Body: str = Form(...)
):
    """Twilio calls this when patient replies to WhatsApp"""
    message = Body.strip().upper()
    phone = From.replace("whatsapp:+", "").replace("+", "")

    if message in ["STOP", "UNSUBSCRIBE", "CANCEL", "QUIT"]:
        from database import supabase
        supabase.table("patients")\
            .update({"opted_out": True})\
            .eq("phone", phone)\
            .execute()

        supabase.table("opt_outs").upsert({"phone": phone}).execute()
        print(f"[Webhook] Opted out: {phone}")

    # Twilio expects TwiML response
    return {"message": "ok"}

@app.get("/test-scheduler")
async def test_scheduler():
    """Manually trigger the daily reminder job"""
    from scheduler import send_due_reminders
    await send_due_reminders()
    return {"message": "Scheduler triggered"}

@app.get("/test-retry")
async def test_retry():
    """Manually trigger the retry job"""
    from scheduler import retry_failed_reminders
    await retry_failed_reminders()
    return {"message": "Retry triggered"}