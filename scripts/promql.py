"""PromQL building blocks shared by the dashboard converter and the alert builder."""
from __future__ import annotations

import profiles as P

# Grafana Cloud's OTLP translation appends this to monotonic sums.
COUNTER_SUFFIX = "_total"

_SPEC: dict | None = None


def spec() -> dict:
    global _SPEC
    if _SPEC is None:
        _SPEC = P.metric_spec()
    return _SPEC


def is_counter(measurement: str, fieldname: str) -> bool:
    return P.clean_name(fieldname) in spec().get(measurement, {}).get("counters", [])


def is_known(measurement: str, fieldname: str) -> bool:
    return P.clean_name(fieldname) in spec().get(measurement, {}).get("keep", [])


def series_name(measurement: str, fieldname: str) -> str:
    name = P.metric_name(measurement, fieldname)
    return name + COUNTER_SUFFIX if is_counter(measurement, fieldname) else name


def selector(name: str, matchers: list[str]) -> str:
    return f"{name}{{{', '.join(matchers)}}}" if matchers else name


def field_ref(measurement: str, fieldname: str, matchers: list[str], window: str = "$__rate_interval") -> tuple[str, bool]:
    """PromQL for a jtso field. A trailing _rate on a counter becomes rate(). Returns (expr, is_rate)."""
    if fieldname.endswith("_rate"):
        base = fieldname[: -len("_rate")]
        if is_counter(measurement, base):
            return f"rate({selector(series_name(measurement, base), matchers)}[{window}])", True
    return selector(series_name(measurement, fieldname), matchers), False


OPS = {"gt": ">", "lt": "<", "ge": ">=", "le": "<=", "eq": "==", "ne": "!="}


def probe_operator(probe: dict) -> str:
    op = OPS.get(probe["operator"])
    if op is None:
        raise P.ProfileError(f"probe {probe['name']}: operator {probe['operator']}")
    return op


def probe_values(probe: dict, matchers: list[str], window: str = "5m") -> list[str]:
    """The probed value per measurement (a rate for counters), without the threshold."""
    parts = []
    for meas in probe["measurements"]:
        if not is_known(meas, probe["field"]):
            continue
        if probe["rate"]:
            parts.append(f"rate({selector(P.metric_name(meas, probe['field']) + COUNTER_SUFFIX, matchers)}[{window}])")
        else:
            parts.append(selector(series_name(meas, probe["field"]), matchers))
    if not parts:
        raise P.ProfileError(f"probe {probe['name']}: field {probe['field']} not exported by {probe['measurements']}")
    return parts


def probe_value(probe: dict, matchers: list[str], window: str = "5m") -> str:
    parts = probe_values(probe, matchers, window)
    return parts[0] if len(parts) == 1 else " or ".join(parts)


def probe_expr(probe: dict, matchers: list[str], window: str = "5m") -> str:
    """One monitoring_list probe as a PromQL condition (series present = probe firing)."""
    op = probe_operator(probe)
    parts = [f"{v} {op} {probe['threshold']}" for v in probe_values(probe, matchers, window)]
    return parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"
