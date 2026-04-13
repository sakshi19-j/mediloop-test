import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
import os

# ── Logging — must be first so all modules get JSON formatter ─────────────────
from logger_config import setup_logging, get_logger
setup_logging()
logger = get_logger(__name__)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scheduler import start_scheduler
from routes import auth, patients, medicines, dashboard, billing, prescriptions
from routes.webhook_whatsapp import router as whatsapp_webhook_router

# ── Sentry ────────────────────────────────────────────────────────────────────
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=0.1,
        environment=os.getenv("ENVIRONMENT", "production"),
        release=os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
        send_default_pii=False,
    )
    logger.info("Sentry initialised", extra={"environment": os.getenv("ENVIRONMENT", "production")})
else:
    logger.warning("SENTRY_DSN not set — error tracking disabled")

# ── Redis ─────────────────────────────────────────────────────────────────────
from queue_worker import get_redis_connection
_redis_url = os.getenv("REDIS_URL")
if _redis_url:
    logger.info("Redis queue enabled", extra={"url_prefix": _redis_url[:30]})
else:
    logger.warning("REDIS_URL not set — falling back to direct WhatsApp sends")

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
    logger.info("MediLoop API starting up", extra={
        "version": "1.0.0",
        "environment": os.getenv("ENVIRONMENT", "production"),
    })
    start_scheduler()


@app.get("/health")
def health():
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