"""Read the upstream OpenJTS design_profile catalog and translate it.

design_profile/ is kept byte-for-byte compatible with door7302/openjts so new
upstream profiles can be merged in. This module turns those JSON files into
stock Telegraf processors plus a small metric spec used by the final Starlark
step (naming, field allowlist, counter vs gauge).

Processor order follows jtso: clone, pivot, rename, enum, strings, regex,
xreducer, filter, converter. jtso-only features are replaced:

  enrichment_list  -> inputs.gnmi tag_subscription (interface description)
                      plus a junos_device_info metric from targets
  rate_list        -> counters exported as cumulative sums, rate() in PromQL
  monitoring_list  -> Grafana alert rules (scripts/build_alerts.py)
  netconf_inputs   -> dropped (no NETCONF collector in this stack)
  enum_list        -> dropped (queue_id is kept, queue names are site-specific)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO / "design_profile"

METRIC_PREFIX = "junos_"

STAGES = ["clone", "pivot", "rename", "enum", "strings", "regex", "xreducer", "filter", "converter"]

JUNIPER_HEADER_FIELDS = {"_component_id": "component_id", "_subcomponent_id": "sub_component_id"}


class ProfileError(Exception):
    pass


def clean_name(raw: str) -> str:
    """Field or tag key as it appears in Prometheus. Mirrors clean_field in finalize.star."""
    last = raw.rstrip("/").split("/")[-1].split(":")[-1].lstrip("@")
    return last.replace("-", "_").replace(".", "_").lower()


def measurement_prefix(measurement: str) -> str:
    """JUNOS_TWAMP -> junos_twamp_, INTERFACES -> junos_interfaces_. Mirrors finalize.star."""
    return f"{METRIC_PREFIX}{measurement.lower().removeprefix(METRIC_PREFIX)}_"


def metric_name(measurement: str, fieldname: str) -> str:
    return measurement_prefix(measurement) + clean_name(fieldname)


def profiles() -> list[str]:
    return sorted(p.name for p in PROFILE_DIR.iterdir() if (p / "definition.json").is_file())


def definition(profile: str) -> dict:
    path = PROFILE_DIR / profile / "definition.json"
    if not path.is_file():
        raise ProfileError(f"unknown profile {profile!r} (have: {', '.join(profiles())})")
    return json.loads(path.read_text(encoding="utf-8"))


def _version_tuple(text: str) -> tuple[int, int | None]:
    m = re.match(r"\s*(\d+)(?:\.(\d+))?", text)
    if not m:
        raise ProfileError(f"cannot parse Junos version {text!r}")
    return int(m.group(1)), int(m.group(2)) if m.group(2) else None


def version_matches(rule: str, version: str) -> bool:
    rule = rule.strip()
    if rule == "all":
        return True
    m = re.fullmatch(r"(>=|<=|==|>|<)\s*(\d+)(?:\.(\d+))?R?", rule)
    if not m:
        raise ProfileError(f"unsupported version rule {rule!r}")
    op, rmaj, rmin = m.group(1), int(m.group(2)), m.group(3)
    vmaj, vmin = _version_tuple(version)
    if rmin is None:
        left, right = vmaj, rmaj
    else:
        left, right = (vmaj, vmin or 0), (rmaj, int(rmin))
    return {
        ">=": left >= right,
        "<=": left <= right,
        "==": left == right,
        ">": left > right,
        "<": left < right,
    }[op]


def select_conf(profile: str, family: str, version: str) -> Path | None:
    """First matching variant for a family/version, as jtso does. None if the profile has no entry."""
    variants = (definition(profile).get("telegraf") or {}).get(family)
    if not variants:
        return None
    for variant in variants:
        if version_matches(variant["version"], version):
            path = PROFILE_DIR / profile / variant["conf"]
            if path.is_file():
                return path
            # Upstream definition.json has a few stale names (bgp/vjunos_bgp.json -> vjunos.json).
            fallback = PROFILE_DIR / profile / f"{family}.json"
            if fallback.is_file():
                return fallback
            raise ProfileError(f"{profile}: {variant['conf']} listed for {family} but missing")
    return None


def load_conf(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def all_confs() -> list[Path]:
    seen: list[Path] = []
    for profile in profiles():
        for variants in (definition(profile).get("telegraf") or {}).values():
            for variant in variants:
                path = PROFILE_DIR / profile / variant["conf"]
                if not path.is_file():
                    family_guess = variant["conf"].split("_")[0].removesuffix(".json")
                    path = PROFILE_DIR / profile / f"{family_guess}.json"
                if path.is_file() and path not in seen:
                    seen.append(path)
    return seen


def prefix_globs(pattern: str) -> list[str]:
    """jtso filter regexes are prefix alternations. Turn them into Telegraf globs or refuse."""
    literal = r"[A-Za-z0-9_\-/:.]+"
    m = re.fullmatch(r"\^\((" + literal + r"(?:\|" + literal + r")*)\)\.\*", pattern)
    if m:
        return [alt + "*" for alt in m.group(1).split("|")]
    m = re.fullmatch(r"\^(" + literal + r")\.\*", pattern)
    if m:
        return [m.group(1) + "*"]
    raise ProfileError(f"filter pattern {pattern!r} is not a prefix alternation; extend prefix_globs()")


@dataclass
class Pipeline:
    """Everything one router needs. Routers with the same conf set share a pipeline."""

    pid: str
    confs: tuple[Path, ...]
    subscriptions: dict[tuple[str, str], dict] = field(default_factory=dict)
    aliases: list[tuple[str, str]] = field(default_factory=list)
    alias_renames: list[tuple[str, str]] = field(default_factory=list)
    processors: list[tuple[str, dict]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def build_pipeline(pid: str, confs: tuple[Path, ...]) -> Pipeline:
    pipe = Pipeline(pid=pid, confs=confs)
    staged: dict[str, list[dict]] = {s: [] for s in STAGES}
    filters: dict[str, dict[str, set[str]]] = {}

    for path in confs:
        conf = load_conf(path)
        rel = path.relative_to(PROFILE_DIR).as_posix()

        for gi in conf.get("gnmi_inputs", []):
            for sub in gi.get("subscriptions", []):
                key = (sub["name"], sub["path"])
                interval = int(sub.get("interval", 60))
                mode = sub.get("mode", "sample")
                prev = pipe.subscriptions.get(key)
                if prev is None or interval < prev["interval"]:
                    pipe.subscriptions[key] = {"mode": mode, "interval": interval}
            for alias in gi.get("aliases", []):
                for i, prefix in enumerate(alias.get("prefix_list", [])):
                    name = alias["name"] if i == 0 else f"{alias['name']}__alias{i}"
                    if (name, prefix) in pipe.aliases:
                        continue
                    if any(n == name and p != prefix for n, p in pipe.aliases):
                        name = f"{alias['name']}__alias{len(pipe.aliases)}"
                    pipe.aliases.append((name, prefix))
                    if name != alias["name"]:
                        pipe.alias_renames.append((name, alias["name"]))

        for item in conf.get("clone_list", []):
            staged["clone"].append({"namepass": item["namepass"], "name_override": item["override"]})
        for item in conf.get("pivot_list", []):
            staged["pivot"].append({"namepass": item["namepass"], "tag_key": item["tag"], "value_key": item["field"]})
        for item in conf.get("rename_list", []):
            replaces = []
            for e in item["entries"]:
                kind = {0: "tag", 1: "field"}.get(e["type"])
                if kind is None:
                    raise ProfileError(f"{rel}: rename type {e['type']}")
                replaces.append({kind: e["from"], "dest": e["to"]})
            staged["rename"].append({"namepass": item["namepass"], "replace": replaces})
        for item in conf.get("enum_list", []):
            pipe.skipped.append(f"{rel}: enum_list {item['namepass']} (queue names are site-specific; queue_id kept)")
        for item in conf.get("strings_list", []):
            ops: dict[str, list[dict]] = {}
            for e in item["entries"]:
                method = {0: "lowercase", 1: "uppercase"}.get(e["method"])
                kind = {0: "tag", 1: "field"}.get(e["type"])
                if method is None or kind is None:
                    raise ProfileError(f"{rel}: strings entry {e}")
                ops.setdefault(method, []).append({kind: e["data"]})
            staged["strings"].append({"namepass": item["namepass"], **ops})
        for item in conf.get("regex_list", []):
            out: dict[str, list[dict]] = {}
            for e in item["entries"]:
                t = e["type"]
                if t == 0:
                    out.setdefault("tag_rename", []).append({"pattern": e["pattern"], "replacement": e["replacement"]})
                elif t == 1:
                    out.setdefault("field_rename", []).append({"pattern": e["pattern"], "replacement": e["replacement"]})
                elif t in (2, 3):
                    entry = {"key": e["key"], "pattern": e["pattern"], "replacement": e["replacement"]}
                    if e.get("result_key"):
                        entry["result_key"] = e["result_key"]
                    out.setdefault("tags" if t == 2 else "fields", []).append(entry)
                else:
                    raise ProfileError(f"{rel}: regex type {t}")
            staged["regex"].append({"namepass": item["namepass"], **out})
        for item in conf.get("xreducer_list", []):
            if item.get("tags") not in (["all"], []) or item.get("fields") not in (["all"], []):
                raise ProfileError(f"{rel}: xreducer only supports 'all' ({item})")
            out = {}
            if item.get("tags") == ["all"]:
                out["tag_rename"] = [{"pattern": "^.*/([^/]+)$", "replacement": "${1}"}]
            if item.get("fields") == ["all"]:
                out["field_rename"] = [{"pattern": "^.*/([^/]+)$", "replacement": "${1}"}]
            staged["xreducer"].append({"namepass": item["namepass"], **out})
        for item in conf.get("filtering_list", []):
            for f in item["filters"]:
                if f["type"] != 0 or f.get("action", "accept") != "accept":
                    raise ProfileError(f"{rel}: only tag accept filters are supported ({f})")
                for meas in item["namepass"]:
                    filters.setdefault(meas, {}).setdefault(f["key"], set()).update(prefix_globs(f["pattern"]))
        for item in conf.get("converter_list", []):
            fields: dict[str, list[str]] = {}
            for key, values in item.items():
                if not key.endswith("_type"):
                    continue
                kind = key.removesuffix("_type")
                if kind not in {"integer", "float", "tag", "string", "boolean", "unsigned"}:
                    raise ProfileError(f"{rel}: converter {key}")
                keep = [v for v in values if v not in JUNIPER_HEADER_FIELDS]
                if keep:
                    fields[kind] = keep
            if fields:
                staged["converter"].append({"namepass": item["namepass"], "fields": fields})
        if conf.get("netconf_inputs"):
            pipe.skipped.append(f"{rel}: netconf_inputs (no NETCONF collector; use gNMI /system/alarms)")

    # One filter per distinct (key, glob set): the union across profiles, so the
    # traffic profile's ae* is not dropped by the power profile's narrower list.
    grouped: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for meas, keys in filters.items():
        for key, globs in keys.items():
            grouped.setdefault((key, tuple(sorted(globs))), []).append(meas)
    for (key, globs), meas in sorted(grouped.items()):
        staged["filter"].append({"namepass": sorted(meas), "key": key, "globs": list(globs)})

    for stage in STAGES:
        for cfg in _dedupe(staged[stage]):
            pipe.processors.append((stage, cfg))
    return pipe


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def conf_measurements(conf: dict) -> set[str]:
    names: set[str] = set()
    for gi in conf.get("gnmi_inputs", []):
        names.update(s["name"] for s in gi.get("subscriptions", []))
        names.update(a["name"] for a in gi.get("aliases", []))
    for key, val in conf.items():
        if not key.endswith("_list"):
            continue
        for item in val:
            names.update(item.get("namepass", []))
            if key == "clone_list":
                names.add(item["override"])
    return names


def metric_spec() -> dict[str, dict[str, list[str]]]:
    """measurement -> fields to export and which of them are counters, across every profile."""
    spec: dict[str, dict[str, set[str]]] = {}
    for path in all_confs():
        conf = load_conf(path)
        keep: set[str] = set()
        for out in conf.get("influx_outputs", []):
            for f in out.get("fieldpass", []):
                if f == "exception":
                    continue
                keep.add(clean_name(f.removesuffix("_rate")) if f.endswith("_rate") and _is_rate(conf, f) else clean_name(f))
        counters: dict[str, set[str]] = {}
        for item in conf.get("rate_list", []):
            for meas in item["namepass"]:
                counters.setdefault(meas, set()).update(clean_name(f) for f in item["fields"])
        for meas in conf_measurements(conf):
            entry = spec.setdefault(meas, {"keep": set(), "counters": set()})
            entry["keep"].update(keep)
            entry["counters"].update(counters.get(meas, set()))
    return {
        meas: {"keep": sorted(v["keep"]), "counters": sorted(v["counters"])}
        for meas, v in sorted(spec.items())
    }


def _is_rate(conf: dict, fieldname: str) -> bool:
    base = fieldname.removesuffix("_rate")
    return any(base in item["fields"] for item in conf.get("rate_list", []))


def probes() -> list[dict]:
    """monitoring_list probes across every profile, deduplicated by name and expression."""
    out: dict[tuple, dict] = {}
    for path in all_confs():
        conf = load_conf(path)
        rel = path.relative_to(PROFILE_DIR).as_posix()
        rates = {f for item in conf.get("rate_list", []) for f in item["fields"]}
        for item in conf.get("monitoring_list", []):
            for p in item["probes"]:
                fieldname = p["field"]
                is_rate = fieldname.endswith("_rate") and fieldname.removesuffix("_rate") in rates
                base = fieldname.removesuffix("_rate") if is_rate else fieldname
                key = (p["name"], base, p["operator"], p["threshold"])
                entry = out.setdefault(key, {
                    "name": p["name"],
                    "field": clean_name(base),
                    "rate": is_rate,
                    "operator": p["operator"],
                    "threshold": p["threshold"],
                    "measurements": set(),
                    "labels": set(),
                    "sources": set(),
                })
                entry["measurements"].update(item["namepass"])
                entry["labels"].update(p.get("tags", []))
                entry["sources"].add(rel)
    return [
        {**v, "measurements": sorted(v["measurements"]), "labels": sorted(v["labels"]), "sources": sorted(v["sources"])}
        for v in out.values()
    ]
