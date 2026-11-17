"""Read-only contract for BCO's production new-entry pause status.

Distinguish an operationally healthy live service from enabled new entries.
Unknown pause telemetry must never be displayed as 'entries enabled'.
"""

def verify_bco_pause(payload):
    if not isinstance(payload, dict):
        raise ValueError("BCO entry pause response not an object")
    if payload.get("ok") is not True:
        raise ValueError("BCO entry pause status unavailable")
    if payload.get("scope") != "production_trade_creation_only":
        raise ValueError("BCO entry pause scope mismatch")
    if not isinstance(payload.get("paused"), bool):
        raise ValueError("BCO entry pause status not boolean")
    return {
        "ok": True,
        "paused": payload["paused"],
        "scope": payload["scope"],
        "candidate_signal_preserved_for_research": payload.get("candidate_signal_preserved_for_research") is True,
        "management_preserved": payload.get("management_preserved") is True,
    }
