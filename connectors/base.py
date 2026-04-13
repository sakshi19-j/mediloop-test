"""
connectors/base.py — Abstract base class for all POS billing connectors.

WHY THIS EXISTS:
    Every pharmacy POS system (Marg ERP, GoFrugal, eVitalRx, SWIL) stores
    billing data differently. Without a base class, each connector becomes
    a one-off script with different inputs, outputs, and error handling.

    The adapter pattern solves this:
    - Every connector implements the same 3 methods
    - The sync engine calls those 3 methods without knowing which POS it is
    - Adding a new POS = write one class, zero changes to anything else

CONNECTOR CONTRACT:
    Every connector must implement:
        get_connector_name()  → str
        fetch_new_sales()     → list[SaleRecord]
        test_connection()     → bool

    The sync engine (sync_engine.py) does the rest:
        - Calls fetch_new_sales()
        - Maps each SaleRecord to MediLoop patient + medicine
        - POSTs to /patients and /medicines/bulk
        - Logs success/failure

ADDING A NEW POS:
    1. Create connectors/your_pos_name.py
    2. Subclass POSConnector
    3. Implement the 3 required methods
    4. Register in connectors/__init__.py
    Zero changes to routes, scheduler, or any other file.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class SaleRecord:
    """
    Standardised sale record — the common language between all POS connectors
    and the MediLoop sync engine. Every connector maps its data to this.

    Fields:
        patient_name:   Customer name as stored in POS
        phone:          Mobile number (10 digits, will be normalised)
        medicine_name:  Medicine/product name from bill line item
        quantity:       Quantity sold (used to estimate refill_days)
        sale_date:      Date of sale (used as last_purchase_date)
        bill_number:    Optional bill reference for dedup
        dosage:         Optional dosage if parseable from medicine name
        disease_type:   Optional — defaults to "Chronic" if not provided
    """
    patient_name:  str
    phone:         str
    medicine_name: str
    quantity:      int
    sale_date:     datetime

    bill_number:   Optional[str] = None
    dosage:        Optional[str] = None
    disease_type:  Optional[str] = "Chronic"

    # Derived field — set by connector or auto-computed from quantity
    refill_days:   Optional[int] = None

    def __post_init__(self):
        # Auto-compute refill_days from quantity if not set
        # Assumes 1 tablet = 1 day (common for chronic meds)
        if self.refill_days is None:
            qty = self.quantity
            if 7 <= qty <= 90:
                self.refill_days = qty
            else:
                self.refill_days = 30  # safe default


@dataclass
class SyncResult:
    """
    Summary returned after a sync run — used for logging and the API response.
    """
    connector_name:   str
    started_at:       datetime
    finished_at:      Optional[datetime] = None
    records_fetched:  int = 0
    patients_created: int = 0
    patients_updated: int = 0
    medicines_added:  int = 0
    errors:           list = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class POSConnector(ABC):
    """
    Abstract base class all POS connectors must subclass.

    Subclasses must implement:
        get_connector_name() → str
        fetch_new_sales()    → list[SaleRecord]
        test_connection()    → bool
    """

    @abstractmethod
    def get_connector_name(self) -> str:
        """
        Human-readable name of this connector.
        Example: "Marg ERP (CSV)", "GoFrugal API", "eVitalRx Webhook"
        Used in logs and API responses.
        """
        ...

    @abstractmethod
    def fetch_new_sales(self, since: Optional[datetime] = None) -> list[SaleRecord]:
        """
        Fetch sales records from the POS system.

        Args:
            since: Only return records after this datetime.
                   If None, fetch all available records (use carefully).

        Returns:
            List of SaleRecord objects. Empty list if no new sales.

        Must NOT raise exceptions — return empty list on failure and
        log the error. The sync engine handles errors at a higher level.
        """
        ...

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Verify the connector can reach its data source.
        Called by the /integrations/test endpoint and on startup.

        Returns:
            True if connection is healthy, False otherwise.
        """
        ...

    def get_config_requirements(self) -> list[str]:
        """
        Optional — return list of env var names this connector needs.
        Used to show a helpful error if config is missing.
        Example: ["MARG_EXPORT_FOLDER", "MARG_PHARMACY_ID"]
        """
        return []