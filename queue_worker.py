"""
queue_worker.py — Redis-backed job queue for WhatsApp reminder sends.

WHY THIS EXISTS:
  Previously the scheduler sent WhatsApp messages synchronously inside
  a loop. This meant:
    - If Meta's API was slow, the scheduler loop stalled
    - If Railway restarted mid-loop, in-flight sends were lost silently
    - At 10,000+ pharmacies the loop would take 20+ minutes and overlap

  Now the scheduler only ENQUEUES jobs (fast, ~1ms each).
  This worker PROCESSES jobs (slow, network calls to Meta).
  They run independently — the scheduler never waits for WhatsApp.

HOW IT WORKS:
  - Scheduler calls enqueue_reminder() → pushes JSON job to Redis list
  - Worker runs in a separate process (Procfile: worker: python queue_worker.py)
  - Worker pulls jobs one by one, calls send_bulk_reminder, logs result
  - If send fails → job goes to a dead-letter list for inspection
  - Redis persists jobs across restarts — no sends lost

QUEUE KEYS:
  mediloop:reminders:pending   — jobs waiting to be sent
  mediloop:reminders:dead      — jobs that failed after MAX_RETRIES
  mediloop:reminders:sent      — count of successfully sent (for monitoring)

SETUP:
  1. Add Redis to Railway: dashboard → New → Redis → copy REDIS_URL to env
  2. Add to Procfile:
       web: uvicorn main:app --host 0.0.0.0 --port $PORT
       worker: python queue_worker.py
  3. Set REDIS_URL environment variable in Railway
"""

import os
import json
import time
import asyncio
import logging
from datetime import date

import redis

logger = logging.getLogger(__name__)

# ── Redis config ──────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL")

QUEUE_PENDING = "mediloop:reminders:pending"
QUEUE_DEAD    = "mediloop:reminders:dead"
QUEUE_SENT    = "mediloop:reminders:sent"

MAX_RETRIES   = 3
POLL_INTERVAL = 2   # seconds between queue checks when idle


def get_redis_connection():
    """
    Returns a Redis connection or None if REDIS_URL is not set.
    None means the system falls back to direct sends (original behaviour).
    This lets existing deployments keep working without Redis configured.
    """
    if not REDIS_URL:
        return None
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
        return r
    except Exception as e:
        logger.error(f"[Queue] Redis connection failed: {e}")
        return None


# ── Enqueue (called by scheduler) ─────────────────────────────────────────────

def enqueue_reminder(
    phone: str,
    patient_name: str,
    medicine_names: list,
    pharmacy_name: str,
    pharmacy_id: str,
    patient_id: str,
    reminder_type: str,
    medicine_ids: list,
) -> str | None:
    """
    Push one reminder job onto the Redis queue.
    Returns the job_id if enqueued, None if Redis unavailable.
    Called by scheduler — synchronous, non-blocking (<1ms).
    """
    r = get_redis_connection()
    if not r:
        return None

    job_id = f"{pharmacy_id}:{patient_id}:{reminder_type}:{date.today().isoformat()}"

    job = {
        "job_id":        job_id,
        "phone":         phone,
        "patient_name":  patient_name,
        "medicine_names": medicine_names,
        "pharmacy_name": pharmacy_name,
        "pharmacy_id":   pharmacy_id,
        "patient_id":    patient_id,
        "reminder_type": reminder_type,
        "medicine_ids":  medicine_ids,
        "enqueued_at":   time.time(),
        "attempt":       0,
    }

    try:
        r.rpush(QUEUE_PENDING, json.dumps(job))
        logger.info(f"[Queue] Enqueued reminder for {patient_name} — {len(medicine_names)} medicines")
        return job_id
    except Exception as e:
        logger.error(f"[Queue] Failed to enqueue for {patient_name}: {e}")
        return None


def enqueue_consent(
    phone: str,
    patient_name: str,
    pharmacy_name: str,
    pharmacy_id: str,
    patient_id: str,
) -> str | None:
    """Push a consent request job onto the queue."""
    r = get_redis_connection()
    if not r:
        return None

    job_id = f"consent:{patient_id}:{time.time()}"

    job = {
        "job_id":       job_id,
        "type":         "consent",
        "phone":        phone,
        "patient_name": patient_name,
        "pharmacy_name": pharmacy_name,
        "pharmacy_id":  pharmacy_id,
        "patient_id":   patient_id,
        "enqueued_at":  time.time(),
        "attempt":      0,
    }

    try:
        r.rpush(QUEUE_PENDING, json.dumps(job))
        return job_id
    except Exception as e:
        logger.error(f"[Queue] Failed to enqueue consent for {patient_name}: {e}")
        return None


# ── Process one job (called by worker loop) ───────────────────────────────────

async def process_job(job: dict) -> bool:
    """
    Process a single job from the queue.
    Returns True if successful, False if should be retried/dead-lettered.
    """
    from whatsapp import send_bulk_reminder, send_consent_request
    from database import supabase

    job_type      = job.get("type", "reminder")
    patient_name  = job.get("patient_name", "Unknown")
    pharmacy_id   = job.get("pharmacy_id")
    patient_id    = job.get("patient_id")

    try:
        if job_type == "consent":
            result = await send_consent_request(
                phone=job["phone"],
                patient_name=patient_name,
                pharmacy_name=job["pharmacy_name"],
            )
        else:
            # Standard reminder
            result = await send_bulk_reminder(
                phone=job["phone"],
                patient_name=patient_name,
                medicine_names=job["medicine_names"],
                pharmacy_name=job["pharmacy_name"],
            )

        log_status = result.get("channel", "failed")
        success    = log_status != "failed"

        # Write reminder_logs for each medicine in the job
        if job_type != "consent" and pharmacy_id and patient_id:
            medicine_ids  = job.get("medicine_ids", [])
            reminder_type = job.get("reminder_type", "scheduled")

            for medicine_id in medicine_ids:
                try:
                    supabase.table("reminder_logs").insert({
                        "medicine_id":    medicine_id,
                        "patient_id":     patient_id,
                        "pharmacy_id":    pharmacy_id,
                        "whatsapp_status": log_status,
                        "reminder_type":  reminder_type,
                    }).execute()
                except Exception as e:
                    logger.error(f"[Queue] DB log failed for medicine {medicine_id}: {e}")

        if success:
            logger.info(f"[Queue] Sent to {patient_name} ({job['phone']}) — {log_status}")
        else:
            logger.warning(f"[Queue] Send failed for {patient_name}: {result.get('error')}")

        return success

    except Exception as e:
        logger.error(f"[Queue] Job error for {patient_name}: {e}")
        return False


# ── Worker loop ───────────────────────────────────────────────────────────────

async def run_worker():
    """
    Main worker loop. Runs forever, pulling jobs from Redis and processing them.
    Starts with: python queue_worker.py
    Or via Procfile: worker: python queue_worker.py
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    logger.info("[Queue Worker] Starting...")

    r = get_redis_connection()
    if not r:
        logger.error("[Queue Worker] No Redis connection — REDIS_URL not set. Exiting.")
        return

    logger.info(f"[Queue Worker] Connected to Redis. Watching: {QUEUE_PENDING}")

    while True:
        try:
            # BLPOP blocks up to POLL_INTERVAL seconds, returns (key, value) or None
            item = r.blpop(QUEUE_PENDING, timeout=POLL_INTERVAL)

            if not item:
                # Queue is empty — just loop
                continue

            _, raw = item
            job = json.loads(raw)
            attempt = job.get("attempt", 0)

            logger.info(f"[Queue Worker] Processing job {job.get('job_id')} (attempt {attempt + 1})")

            success = await process_job(job)

            if success:
                # Increment sent counter in Redis for monitoring
                r.incr(QUEUE_SENT)
            else:
                # Retry up to MAX_RETRIES times
                if attempt < MAX_RETRIES - 1:
                    job["attempt"] = attempt + 1
                    job["retry_at"] = time.time()
                    r.rpush(QUEUE_PENDING, json.dumps(job))
                    logger.warning(f"[Queue Worker] Job {job.get('job_id')} re-queued (attempt {attempt + 2}/{MAX_RETRIES})")
                else:
                    # Move to dead-letter queue for manual inspection
                    job["failed_at"] = time.time()
                    r.rpush(QUEUE_DEAD, json.dumps(job))
                    logger.error(f"[Queue Worker] Job {job.get('job_id')} dead-lettered after {MAX_RETRIES} attempts")

        except json.JSONDecodeError as e:
            logger.error(f"[Queue Worker] Invalid job JSON: {e}")
        except KeyboardInterrupt:
            logger.info("[Queue Worker] Shutting down")
            break
        except Exception as e:
            logger.error(f"[Queue Worker] Unexpected error: {e}")
            await asyncio.sleep(5)  # Brief pause before retrying on unknown errors


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_worker())