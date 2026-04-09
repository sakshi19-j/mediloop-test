from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard, billing, prescriptions
from routes.webhook_whatsapp import router as whatsapp_webhook_router

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
app.include_router(whatsapp_webhook_router)

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

@app.get("/test-scheduler")
async def test_scheduler():
    from scheduler import send_due_reminders
    await send_due_reminders()
    return {"message": "Scheduler triggered"}

@app.get("/test-retry")
async def test_retry():
    from scheduler import retry_failed_reminders
    await retry_failed_reminders()
    return {"message": "Retry triggered"}