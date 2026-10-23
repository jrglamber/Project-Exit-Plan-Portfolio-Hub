"""Portfolio Hub production entrypoint with complete daily-accounting semantics.

The Hub must never turn a missing producer `realised_today_gbp` into a plausible
£0.00. Once all live producers supply the field, their UK-day values are summed.
Presentation/aggregation only; no trading or broker authority.
"""
from __future__ import annotations

from typing import Any, Dict

import hub_entrypoint as base

app = base.app
core = base.core
VERSION = "hub_daily_realised_contract_v1_2026_10_23"
_original_aggregate = core.aggregate_snapshot


def aggregate_snapshot() -> Dict[str, Any]:
    snap = _original_aggregate()
    sources = snap.get("sources") or {}
    missing = []
    values = []
    for key in ("indices", "metals", "bco"):
        source = sources.get(key) or {}
        data = source.get("data") if isinstance(source, dict) else None
        if not isinstance(data, dict) or str(data.get("mode") or "").lower() != "live":
            continue
        accounting = data.get("accounting") or {}
        value = accounting.get("realised_today_gbp")
        try:
            if value is None or value == "":
                raise ValueError("missing")
            values.append(float(value))
        except Exception:
            missing.append(key)

    portfolio = snap.setdefault("portfolio", {})
    if missing:
        portfolio["realised_today_gbp"] = None
        portfolio["realised_today_complete"] = False
        portfolio["realised_today_missing_sources"] = missing
    else:
        portfolio["realised_today_gbp"] = round(sum(values), 2) if values else None
        portfolio["realised_today_complete"] = bool(values)
        portfolio["realised_today_missing_sources"] = []
    portfolio["realised_today_contract_version"] = VERSION
    return snap


core.aggregate_snapshot = aggregate_snapshot
print(
    f"HUB_DAILY_REALISED_CONTRACT installed=True version={VERSION} execution_authority=False",
    flush=True,
)
