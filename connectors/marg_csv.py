"""
connectors/marg_csv.py — Marg ERP CSV export connector.

HOW MARG EXPORTS WORK:
    Marg ERP can auto-export daily sales as a CSV to a local folder.
    The chemist configures this once:
        Reports → Sales Register → Schedule → Export as CSV → C:/MargExports/

    This connector watches that folder, reads new CSVs, and maps
    each row to a SaleRecord. The sync engine does the rest.

CONFIGURATION (set in Railway env vars):
    MARG_EXPORT_FOLDER   — path to watch (default: /mnt/marg_exports)
    MARG_PHARMACY_ID     — the MediLoop pharmacy_id this connector belongs to

    For the Windows CSV watcher agent, these are set in the agent's .env file.
    For direct server-side integration, set in Railway.

COLUMN MAPPING:
    Marg CSV columns vary slightly between versions.
    Update COLUMN_MAP below after inspecting a real export from the chemist.
    Common column names seen in Marg v9/v10:
        Patient name: "Party Name", "Customer Name", "Patient Name"
        Phone:        "Mobile", "Mobile No", "Phone"
        Medicine:     "Item Name", "Product Name", "Medicine Name"
        Quantity:     "Qty", "Quantity", "Pcs"
        Date:         "Date", "Bill Date", "Invoice Date"
        Bill no:      "Bill No", "Invoice No", "Voucher No"
"""

import os
import csv
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from connectors.base import POSConnector, SaleRecord

logger = logging.getLogger(__name__)

# ── Column mapping — UPDATE after seeing a real Marg CSV ──────────────────────
# Key = SaleRecord field, Value = Marg CSV column header
# Add multiple possible column names in a list — connector tries each in order
COLUMN_MAP = {
    "patient_name": ["Party Name", "Customer Name", "Patient Name", "Name"],
    "phone":        ["Mobile", "Mobile No", "Phone", "Contact"],
    "medicine":     ["Item Name", "Product Name", "Medicine Name", "Description"],
    "quantity":     ["Qty", "Quantity", "Pcs", "Units"],
    "date":         ["Date", "Bill Date", "Invoice Date", "Voucher Date"],
    "bill_number":  ["Bill No", "Invoice No", "Voucher No", "Bill Number"],
}

# Date formats Marg uses (tries each in order)
DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"]


class MargCSVConnector(POSConnector):

    def __init__(self):
        self.export_folder = Path(
            os.getenv("MARG_EXPORT_FOLDER", "/mnt/marg_exports")
        )
        # SQLite DB to track which files have been processed
        # Prevents re-importing the same CSV on restart
        self._state_db = os.getenv("MARG_STATE_DB", "/tmp/marg_sync_state.db")
        self._init_state_db()

    def get_connector_name(self) -> str:
        return "Marg ERP (CSV Export)"

    def get_config_requirements(self) -> list[str]:
        return ["MARG_EXPORT_FOLDER", "MARG_PHARMACY_ID"]

    def test_connection(self) -> bool:
        """Check that the export folder exists and is readable."""
        try:
            if not self.export_folder.exists():
                logger.warning(
                    "Marg export folder not found",
                    extra={"folder": str(self.export_folder)}
                )
                return False
            # Try listing the folder
            list(self.export_folder.glob("*.csv"))
            return True
        except Exception as e:
            logger.error("Marg connector test failed", extra={"error": str(e)})
            return False

    def fetch_new_sales(self, since: Optional[datetime] = None) -> list[SaleRecord]:
        """
        Scans the export folder for new CSV files and parses them.
        Skips files already processed (tracked in SQLite state DB).
        """
        if not self.export_folder.exists():
            logger.warning(
                "Marg export folder missing — skipping sync",
                extra={"folder": str(self.export_folder)}
            )
            return []

        csv_files = sorted(self.export_folder.glob("*.csv"))
        new_files = [f for f in csv_files if not self._is_processed(f.name)]

        if not new_files:
            logger.info("No new Marg CSV files to process")
            return []

        all_records = []

        for filepath in new_files:
            logger.info(
                "Processing Marg CSV",
                extra={"file": filepath.name}
            )
            try:
                records = self._parse_csv(filepath)
                all_records.extend(records)
                self._mark_processed(filepath.name, len(records))
                logger.info(
                    "Marg CSV parsed",
                    extra={"file": filepath.name, "records": len(records)}
                )
            except Exception as e:
                logger.error(
                    "Marg CSV parse failed",
                    extra={"file": filepath.name, "error": str(e)}
                )
                # Don't mark as processed — will retry next run

        return all_records

    def _parse_csv(self, filepath: Path) -> list[SaleRecord]:
        """Parse one CSV file into SaleRecord objects."""
        records = []

        with open(filepath, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

            for row_num, row in enumerate(reader, start=2):
                try:
                    record = self._map_row(row, headers)
                    if record:
                        records.append(record)
                except Exception as e:
                    logger.warning(
                        "Skipping Marg CSV row",
                        extra={"file": filepath.name, "row": row_num, "error": str(e)}
                    )
                    continue

        return records

    def _map_row(self, row: dict, headers: list) -> Optional[SaleRecord]:
        """Map one CSV row to a SaleRecord. Returns None if row should be skipped."""

        def get_col(field_name: str) -> str:
            """Try each possible column name for this field."""
            for col in COLUMN_MAP.get(field_name, []):
                if col in headers and row.get(col, "").strip():
                    return row[col].strip()
            return ""

        patient_name = get_col("patient_name")
        phone        = get_col("phone")
        medicine     = get_col("medicine")
        qty_str      = get_col("quantity")
        date_str     = get_col("date")
        bill_num     = get_col("bill_number")

        # Skip rows with missing required fields
        if not patient_name or not phone or not medicine:
            return None

        # Skip non-medicine items (common in Marg exports)
        SKIP_KEYWORDS = ["surgical", "cosmetic", "baby", "diaper", "soap", "shampoo"]
        if any(kw in medicine.lower() for kw in SKIP_KEYWORDS):
            return None

        # Parse quantity
        try:
            quantity = int(float(qty_str.replace(",", ""))) if qty_str else 30
        except ValueError:
            quantity = 30

        # Parse date
        sale_date = datetime.today()
        for fmt in DATE_FORMATS:
            try:
                sale_date = datetime.strptime(date_str.strip(), fmt)
                break
            except (ValueError, AttributeError):
                continue

        # Extract dosage from medicine name if present
        # e.g. "Metformin 500mg" → dosage = "500mg"
        dosage = None
        import re
        match = re.search(r"\d+\s*(?:mg|ml|mcg|g|iu|units?)", medicine, re.IGNORECASE)
        if match:
            dosage = match.group(0).strip()

        return SaleRecord(
            patient_name=patient_name,
            phone=phone,
            medicine_name=medicine,
            quantity=quantity,
            sale_date=sale_date,
            bill_number=bill_num or None,
            dosage=dosage,
        )

    # ── State DB (tracks processed files) ────────────────────────────────────

    def _init_state_db(self):
        try:
            conn = sqlite3.connect(self._state_db)
            conn.execute("""
                create table if not exists processed_files (
                    filename     text primary key,
                    processed_at text,
                    record_count int
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Marg state DB init failed", extra={"error": str(e)})

    def _is_processed(self, filename: str) -> bool:
        try:
            conn = sqlite3.connect(self._state_db)
            row = conn.execute(
                "select 1 from processed_files where filename = ?", (filename,)
            ).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def _mark_processed(self, filename: str, record_count: int):
        try:
            conn = sqlite3.connect(self._state_db)
            conn.execute(
                "insert or replace into processed_files values (?, ?, ?)",
                (filename, datetime.now().isoformat(), record_count)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to mark file processed", extra={"error": str(e)})