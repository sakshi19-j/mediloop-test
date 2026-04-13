"""
connectors/sync_engine.py — Runs any connector and syncs data to MediLoop.

The sync engine is the bridge between POS connectors and the MediLoop API.
It takes SaleRecord objects from any connector and:
    1. Creates or finds the patient via POST /patients
    2. Adds medicines via POST /medicines/bulk
    3. Returns a SyncResult with counts and any errors

Called by:
    - routes/billing.py POST /api/v1/integrations/sync  (manual trigger)
    - Standalone cron agent (mediloop_sync.py) on chemist's PC
"""

import logging
import os
import httpx
from datetime import datetime
from typing import Optional

from connectors.base import POSConnector, SaleRecord, SyncResult

logger = logging.getLogger(__name__)

MEDILOOP_API_URL = os.getenv("MEDILOOP_API_URL", "https://mediloop-production-1b1e.up.railway.app")
API_TIMEOUT      = 15.0  # seconds per request


def normalize_phone(phone: str) -> str:
    phone = phone.strip().lstrip("+").replace(" ", "").replace("-", "")
    if not phone.startswith("91") and len(phone) == 10:
        phone = f"91{phone}"
    return phone


async def run_sync(
    connector: POSConnector,
    pharmacy_id: str,
    api_token: str,
    since: Optional[datetime] = None,
) -> SyncResult:
    """
    Run a full sync cycle for one connector.

    Args:
        connector:   Any POSConnector subclass
        pharmacy_id: MediLoop pharmacy UUID
        api_token:   JWT token for this pharmacy
        since:       Only fetch records after this datetime

    Returns:
        SyncResult with counts and any errors
    """
    result = SyncResult(
        connector_name=connector.get_connector_name(),
        started_at=datetime.utcnow(),
    )

    logger.info(
        "Sync started",
        extra={"connector": connector.get_connector_name(), "pharmacy_id": pharmacy_id}
    )

    # ── Step 1: Fetch records from POS ───────────────────────────────────────
    try:
        records = connector.fetch_new_sales(since=since)
        result.records_fetched = len(records)
        logger.info(
            "Records fetched from POS",
            extra={"count": len(records), "connector": connector.get_connector_name()}
        )
    except Exception as e:
        result.errors.append(f"Fetch failed: {str(e)}")
        result.finished_at = datetime.utcnow()
        logger.error("Fetch failed", extra={"error": str(e)})
        return result

    if not records:
        result.finished_at = datetime.utcnow()
        return result

    # ── Step 2: Group records by patient (phone number) ───────────────────────
    # Multiple medicines from the same bill → one patient create + bulk medicine add
    patients_map: dict[str, dict] = {}

    for rec in records:
        phone = normalize_phone(rec.phone)
        if phone not in patients_map:
            patients_map[phone] = {
                "name":         rec.patient_name,
                "phone":        phone,
                "disease_type": rec.disease_type or "Chronic",
                "medicines":    [],
            }
        patients_map[phone]["medicines"].append({
            "name":               rec.medicine_name,
            "dosage":             rec.dosage or "as prescribed",
            "refill_days":        rec.refill_days or 30,
            "last_purchase_date": rec.sale_date.date().isoformat(),
            "notes":              rec.bill_number or "",
        })

    headers = {
        "pharmacy-id":   pharmacy_id,
        "Authorization": f"Bearer {api_token}",
        "Content-Type":  "application/json",
    }

    # ── Step 3: Create/find patients and add medicines ─────────────────────────
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        for phone, patient_data in patients_map.items():
            try:
                # Create or find patient
                patient_payload = {
                    "name":         patient_data["name"],
                    "phone":        patient_data["phone"],
                    "disease_type": patient_data["disease_type"],
                }

                patient_res = await client.post(
                    f"{MEDILOOP_API_URL}/api/v1/patients",
                    json=patient_payload,
                    headers=headers,
                )

                if patient_res.status_code not in (200, 201):
                    err = f"Patient create failed for {phone}: {patient_res.text}"
                    result.errors.append(err)
                    logger.warning(err)
                    continue

                patient = patient_res.json()
                patient_id = patient["id"]

                if patient.get("already_exists"):
                    result.patients_updated += 1
                else:
                    result.patients_created += 1

                # Add medicines in bulk
                medicines = patient_data["medicines"]
                if medicines:
                    # Attach patient_id and fix model shape
                    med_payload = {
                        "patient_id": patient_id,
                        "medicines": [
                            {
                                "patient_id":          patient_id,
                                "name":                m["name"],
                                "dosage":              m["dosage"],
                                "refill_days":         m["refill_days"],
                                "last_purchase_date":  m["last_purchase_date"],
                                "notes":               m.get("notes"),
                            }
                            for m in medicines
                        ]
                    }

                    med_res = await client.post(
                        f"{MEDILOOP_API_URL}/api/v1/medicines/bulk",
                        json=med_payload,
                        headers=headers,
                    )

                    if med_res.status_code == 200:
                        result.medicines_added += med_res.json().get("added", 0)
                    else:
                        err = f"Medicine add failed for {phone}: {med_res.text}"
                        result.errors.append(err)
                        logger.warning(err)

            except httpx.TimeoutException:
                err = f"Timeout processing patient {phone}"
                result.errors.append(err)
                logger.error(err)
            except Exception as e:
                err = f"Error processing patient {phone}: {str(e)}"
                result.errors.append(err)
                logger.error(err)

    result.finished_at = datetime.utcnow()

    logger.info(
        "Sync completed",
        extra={
            "connector":        connector.get_connector_name(),
            "pharmacy_id":      pharmacy_id,
            "records_fetched":  result.records_fetched,
            "patients_created": result.patients_created,
            "patients_updated": result.patients_updated,
            "medicines_added":  result.medicines_added,
            "errors":           len(result.errors),
            "duration_seconds": result.duration_seconds,
        }
    )

    return result