#!/usr/bin/env python3
"""Build Grafana-managed alert rules from the profiles' monitoring_list probes.

jtso evaluated these probes in its own process and wrote hits to an ALARMING
measurement. Here each probe becomes one multi-dimensional Grafana alert rule
(one alert instance per device/interface/queue) in alerts/openjts.json, in the
rule-group shape that the provisioning API accepts:

  PUT /api/v1/provisioning/folder/<folderUid>/rule-groups/openjts

scripts/push_grafana.py alerts does that call and swaps in GRAFANA_PROM_UID.

  python3 scripts/build_alerts.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profiles as P  # noqa: E402
import promql as Q  # noqa: E402

OUT = P.REPO / "alerts" / "openjts.json"
GROUP = "openjts"
PROM_UID = "grafanacloud-prom"
EVAL_INTERVAL = 60
PENDING = "5m"

SUMMARIES = {
    "BLOCK_ERROR": "PCS block errors on {{ $labels.if_name }}",
    "CRC_ERROR": "CRC errors on {{ $labels.if_name }}",
    "CPU_HIGH_MPC": "Linecard CPU high on {{ $labels.component_name }}",
    "CPU_HIGH_RE": "Routing Engine CPU high on {{ $labels.component_name }}",
    "MEM_HIGH_MPC": "Linecard memory high on {{ $labels.component_name }}",
    "MEM_HIGH_RE": "Routing Engine memory high on {{ $labels.component_name }}",
    "ETH_DROP_IN": "Input errors on {{ $labels.if_name }}",
    "ETH_DROP_OUT": "Output errors on {{ $labels.if_name }}",
    "RED_DROP": "WRED drops on {{ $labels.if_name }} queue {{ $labels.queue_id }}",
    "TAIL_DROP": "Tail drops on {{ $labels.if_name }} queue {{ $labels.queue_id }}",
}


def rule(probe: dict) -> dict:
    value = Q.probe_value(probe, [], window="5m")
    op = Q.probe_operator(probe)
    unit = "/s" if probe["rate"] else ""
    name = probe["name"]
    return {
        "uid": f"openjts-{name.lower().replace('_', '-')}",
        "title": f"OpenJTS {name}",
        "condition": "C",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": PROM_UID,
             "model": {"refId": "A", "expr": value, "instant": True, "range": False, "editorMode": "code"}},
            {"refId": "B", "datasourceUid": "__expr__",
             "model": {"refId": "B", "type": "reduce", "expression": "A", "reducer": "last",
                       "settings": {"mode": "dropNN"}}},
            {"refId": "C", "datasourceUid": "__expr__",
             "model": {"refId": "C", "type": "math", "expression": f"$B {op} {probe['threshold']}"}},
        ],
        "noDataState": "OK",
        "execErrState": "Error",
        "for": PENDING,
        "labels": {"source": "openjts", "probe": name, "severity": "warning"},
        "annotations": {
            "summary": SUMMARIES.get(name, name) + " ({{ $labels.device }})",
            "description": (f"{probe['field']}{' rate' if probe['rate'] else ''} {op} {probe['threshold']}{unit}. "
                            f"From the monitoring_list probe {name} in {', '.join(probe['sources'])}."),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    rules = []
    for probe in sorted(P.probes(), key=lambda p: p["name"]):
        try:
            rules.append(rule(probe))
        except P.ProfileError as exc:
            print(f"skip {probe['name']}: {exc}")
    uids = [r["uid"] for r in rules]
    if len(set(uids)) != len(uids):
        raise SystemExit(f"duplicate probe names with different expressions: {uids}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"title": GROUP, "interval": EVAL_INTERVAL, "rules": rules}
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"{args.out.relative_to(P.REPO).as_posix()}: {len(rules)} rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
