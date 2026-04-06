from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard, billing

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