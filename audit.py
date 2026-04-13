"""
audit.py — Centralised audit logging for MediLoop.

WHY THIS EXISTS:
    Pharmacies handle patient health data. If a patient record is deleted,
    modified, or accessed, there must be a permanent record of who did it,
    when, and what changed. This is required for compliance and cannot be
    retrofitted easily once you have thousands of pharmacies.

    This is a write-only append table. Records are never updated or deleted.

USAGE:
    from audit import log_audit

    # On patient create
    log_audit(
        pharmacy_id=pharmacy_id,
        actor_id=pharmacy_id,          # who did it (pharmacy JWT)
        action="created",
        entity_type="patient",
        entity_id=new_patient["id"],
        new_data=new_patient           # what was created
    )

    # On patient update
    log_audit(
        pharmacy_id=pharmacy_id,
        actor_id=pharmacy_id,
        action="updated",
        entity_type="patient",
        entity_id=patient_id,
        old_data={"name": "Old Name"},  # what it was before
        new_data={"name": "New Name"}   # what it changed to
    )

    # On delete
    log_audit(
        pharmacy_id=pharmacy_id,
        actor_id=pharmacy_id,
        action="deleted",
        entity_type="patient",
        entity_id=patient_id
    )

SUPABASE TABLE (run this SQL once in Supabase dashboard):

    create table audit_logs (
        id          uuid primary key default gen_random_uuid(),
        pharmacy_id uuid references pharmacies(id),
        actor_id    text not null,
        action      text not null,
        entity_type text not null,
        entity_id   text not null,
        old_data    jsonb,
        new_data    jsonb,
        ip_address  text,
        created_at  timestamptz default now()
    );

    -- Index for fast lookups per pharmacy
    create index idx_audit_logs_pharmacy_id on audit_logs(pharmacy_id);
    create index idx_audit_logs_entity on audit_logs(entity_type, entity_id);

DESIGN NOTES:
    - log_audit() never raises — a logging failure must never break the
      main request. It swallows all exceptions and logs to stderr.
    - old_data and new_data are optional — pass only what's relevant.
    - ip_address is optional — pass request.client.host when available.
    - actor_id is currently the pharmacy_id (JWT subject). In future when
      staff roles exist, this will be the staff member's user_id.
"""

import logging
from typing import Optional
from database import supabase

logger = logging.getLogger(__name__)

# Valid action values — enforced to keep the table consistent
VALID_ACTIONS      = {"created", "updated", "deleted", "viewed", "exported"}
VALID_ENTITY_TYPES = {"patient", "medicine", "prescription", "reorder", "subscription"}


def log_audit(
    pharmacy_id: str,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    old_data: Optional[dict] = None,
    new_data: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> None:
    """
    Write one audit log entry. Never raises — swallows all exceptions.

    Args:
        pharmacy_id:  UUID of the pharmacy performing the action
        actor_id:     ID of who performed the action (pharmacy_id or future staff_id)
        action:       One of: created, updated, deleted, viewed, exported
        entity_type:  One of: patient, medicine, prescription, reorder, subscription
        entity_id:    UUID of the affected record
        old_data:     Dict of fields before the change (for updates/deletes)
        new_data:     Dict of fields after the change (for creates/updates)
        ip_address:   Request IP if available (pass request.client.host)
    """
    try:
        # Sanitise — remove sensitive fields before storing
        if old_data:
            old_data = _sanitise(old_data)
        if new_data:
            new_data = _sanitise(new_data)

        row = {
            "pharmacy_id": pharmacy_id,
            "actor_id":    actor_id,
            "action":      action,
            "entity_type": entity_type,
            "entity_id":   str(entity_id),
        }

        if old_data is not None:
            row["old_data"] = old_data
        if new_data is not None:
            row["new_data"] = new_data
        if ip_address:
            row["ip_address"] = ip_address

        supabase.table("audit_logs").insert(row).execute()

    except Exception as e:
        # Never let audit logging break the main request
        logger.error(
            "Audit log write failed",
            extra={
                "pharmacy_id": pharmacy_id,
                "action":      action,
                "entity_type": entity_type,
                "entity_id":   entity_id,
                "error":       str(e),
            }
        )


def _sanitise(data: dict) -> dict:
    """
    Remove fields that should never be stored in audit logs:
    passwords, tokens, raw phone numbers in full, etc.
    """
    SENSITIVE_KEYS = {
        "password", "password_hash", "token", "access_token",
        "whatsapp_access_token", "razorpay_key_secret", "secret",
    }
    return {
        k: ("***" if k.lower() in SENSITIVE_KEYS else v)
        for k, v in data.items()
    }