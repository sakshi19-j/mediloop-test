"""
connectors/__init__.py — Connector registry.

To register a new connector, import it here and add to CONNECTORS dict.
The sync engine and API use this registry — no other file needs to change.
"""

from connectors.marg_csv import MargCSVConnector

# Registry — key is the connector_id used in the API
CONNECTORS = {
    "marg_csv": MargCSVConnector,
}


def get_connector(connector_id: str):
    """
    Returns an initialised connector instance by ID.
    Raises ValueError if connector_id is not registered.
    """
    cls = CONNECTORS.get(connector_id)
    if not cls:
        raise ValueError(
            f"Unknown connector '{connector_id}'. "
            f"Available: {list(CONNECTORS.keys())}"
        )
    return cls()


def list_connectors() -> list[dict]:
    """Returns metadata about all registered connectors."""
    result = []
    for connector_id, cls in CONNECTORS.items():
        instance = cls()
        result.append({
            "id":           connector_id,
            "name":         instance.get_connector_name(),
            "requires":     instance.get_config_requirements(),
        })
    return result