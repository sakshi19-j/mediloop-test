import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard, billing, prescriptions
from routes.webhook_whatsapp import router as whatsapp_webhook_router

# ── Sentry — initialise before anything else ──────────────────────────────────
# Set SENTRY_DSN in Railway environment variables.
# Get your DSN from https://sentry.io → New Project → Python → FastAPI
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        # Capture 100% of errors, 10% of performance traces (tune later)
        traces_sample_rate=0.1,
        # Tag every event with environment so you can filter prod vs staging
        environment=os.getenv("ENVIRONMENT", "production"),
        # Release tag — set RAILWAY_GIT_COMMIT_SHA in Railway env vars for free
        release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        # Personally identifiable data — scrub before sending
        send_default_pii=False,
    )
    print(f"[Sentry] Initialised — env={os.getenv('ENVIRONMENT', 'production')}")
else:
    print("[Sentry] SENTRY_DSN not set — error tracking disabled")

# ── Redis queue — initialise connection on startup ────────────────────────────
# Set REDIS_URL in Railway environment variables.
# Provision a free Redis instance: Railway dashboard → New → Redis
from queue_worker import get_redis_connection
_redis_url = os.getenv("REDIS_URL")
if _redis_url:
    print(f"[Redis] Queue enabled — {_redis_url[:30]}...")
else:
    print("[Redis] REDIS_URL not set — falling back to direct WhatsApp sends")

# ── App ───────────────────────────────────────────────────────────────────────
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
    """
    Basic health check. Returns Redis + Sentry status so Railway
    and external monitors (UptimeRobot) can verify all systems.
    """
    redis_ok = False
    try:
        r = get_redis_connection()
        if r:
            r.ping()
            redis_ok = True
    except Exception:
        pass

    return {
        "status": "ok",
        "version": "1.0.0",
        "sentry": bool(_sentry_dsn),
        "redis_queue": redis_ok,
        "environment": os.getenv("ENVIRONMENT", "production"),
    }


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


@app.get("/test-queue")
async def test_queue():
    """
    Pushes a test job onto the Redis queue to verify the worker
    is connected and processing. Safe to call — uses a dummy phone
    that will fail at Meta but proves the queue is working.
    """
    from queue_worker import enqueue_reminder
    job_id = enqueue_reminder(
        phone="910000000000",
        patient_name="Queue Test",
        medicine_names=["Test Medicine"],
        pharmacy_name="Test Pharmacy",
        pharmacy_id="test",
        patient_id="test",
        reminder_type="test",
        medicine_ids=[]
    )
    return {"message": "Test job enqueued", "job_id": job_id}