#!/usr/bin/env python3
"""Check the converted dashboards and alert rules.

1. Every PromQL expression parses (promtool check rules, in the prom/prometheus image).
2. Every junos_* series a query names is one the Telegraf pipeline exports,
   with _total exactly on counters.

  python3 tests/test_dashboards.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import profiles as P  # noqa: E402
import promql as Q  # noqa: E402

IMAGE = os.environ.get("PROMETHEUS_IMAGE", "prom/prometheus:v3.5.0")
GRAFANA_VARS = {
    "$__rate_interval": "1m", "$__range": "1h", "$__interval": "1m", "$__range_s": "3600",
}
EXTRA_SERIES = {"junos_device_info"}

sys.path.insert(0, str(REPO / "tests"))
from test_pipeline import docker_cmd, host_path, scratch_dir  # noqa: E402


def exported_series() -> set[str]:
    names = set(EXTRA_SERIES)
    for meas, spec in Q.spec().items():
        for f in spec["keep"]:
            names.add(P.metric_name(meas, f) + (Q.COUNTER_SUFFIX if f in spec["counters"] else ""))
    return names


def substitute(expr: str) -> str:
    for k, v in GRAFANA_VARS.items():
        expr = expr.replace(k, v)
    expr = re.sub(r"\$\{(\w+)(?::\w+)?\}", "x", expr)
    return re.sub(r"\$(\w+)", "x", expr)


def walk(panels):
    for p in panels:
        yield p
        yield from walk(p.get("panels") or [])


def collect() -> list[tuple[str, str]]:
    out = []
    for f in sorted((REPO / "dashboards").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        for p in walk(d["panels"]):
            for t in p.get("targets") or []:
                if t.get("expr"):
                    out.append((f"{f.name}: {p.get('title')}", t["expr"]))
        for v in d["templating"]["list"]:
            q = v.get("query")
            q = q.get("query") if isinstance(q, dict) else q
            m = re.match(r"label_values\((.*),\s*\w+\)$", q or "")
            if m:
                out.append((f"{f.name}: var {v['name']}", m.group(1)))
    for f in sorted((REPO / "alerts").glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        for rule in doc.get("rules", []):
            for q in rule.get("data", []):
                expr = (q.get("model") or {}).get("expr")
                if expr:
                    out.append((f"{f.name}: {rule['title']}", expr))
    return out


def main() -> int:
    exprs = collect()
    if not exprs:
        print("no expressions found; run scripts/convert_dashboards.py first")
        return 1
    known = exported_series()
    failures = []
    for where, expr in exprs:
        for name in set(re.findall(r"\bjunos_[a-z0-9_]+\b", expr)):
            if name not in known and not expr.count(f'"{name}'):
                failures.append(f"{where}: {name} is not exported")

    tmp = scratch_dir("openjts-test-")
    try:
        rules = {"groups": [{"name": "dashboards", "rules": [
            {"record": f"check:expr_{i}", "expr": substitute(e)} for i, (_, e) in enumerate(exprs)]}]}
        (tmp / "rules.yml").write_text(json.dumps(rules), encoding="utf-8")
        run = subprocess.run(docker_cmd() + [
            "run", "--rm", "--entrypoint", "promtool", "-v", f"{host_path(tmp)}:/check:ro",
            IMAGE, "check", "rules", "/check/rules.yml"], capture_output=True, text=True, encoding="utf-8")
        if run.returncode != 0:
            text = run.stdout + run.stderr
            failures.append("promtool:\n" + text[-3000:])
            for m in re.finditer(r"expr_(\d+)", text):
                i = int(m.group(1))
                failures.append(f"  {exprs[i][0]}: {exprs[i][1]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"{len(exprs)} expressions checked")
    if failures:
        print("FAIL")
        for f in failures:
            print("  " + f)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
