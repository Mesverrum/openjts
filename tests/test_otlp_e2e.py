#!/usr/bin/env python3
"""Telegraf -> alloy/config.alloy -> Prometheus OTLP receiver, no router needed.

Prometheus 3 translates OTLP the same way the Grafana Cloud gateway does
(monotonic sums get _total), so this shows the metric names dashboards use.

  python3 tests/test_otlp_e2e.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import FIXTURE, REPO, docker_cmd, host_path, scratch_dir  # noqa: E402

TELEGRAF = os.environ.get("TELEGRAF_IMAGE", "telegraf:1.36")
ALLOY = os.environ.get("ALLOY_IMAGE", "grafana/alloy:v1.17.1")
PROM = os.environ.get("PROM_IMAGE", "prom/prometheus:v3.5.0")

EXPECT = {
    "junos_interfaces_in_octets_total",
    "junos_interfaces_cos_buffer_cur",
    "junos_components_linecard_cpu",
    "junos_network_instance_bgp_session_state",
    "junos_ddos_stats_dropped_aggregate_policer_total",
    "junos_firewall_drop_pkts_total",
}


# alloy/config.alloy keeps only these Alloy self-metrics (names as they land in Prometheus).
# The self exporter has 130+ families; anything else reaching the stack is a billed series.
ALLOY_EXPECT = {"otelcol_exporter_sent_metric_points_total"}
ALLOY_ALLOWED = re.compile(
    r"target_info|up|alloy_build_info|otelcol_exporter_(sent|send_failed|enqueue_failed)_metric_points_total"
    r"|otelcol_receiver_(accepted|refused)_metric_points_total|otelcol_exporter_queue_(size|capacity)")


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(docker_cmd() + list(args), capture_output=True, text=True, encoding="utf-8")
    if check and res.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {res.stderr.strip()}")
    return res


def prom_get(container: str, path: str) -> dict:
    res = docker("exec", container, "wget", "-qO-", f"http://localhost:9090{path}")
    return json.loads(res.stdout)


def main() -> int:
    tag = "openjts-e2e-" + uuid.uuid4().hex[:6]
    work = scratch_dir("openjts-test-e2e-")
    prom, alloy = f"{tag}-prom", f"{tag}-alloy"
    try:
        gen = subprocess.run([sys.executable, str(REPO / "scripts" / "generate.py"), "--test-input", str(FIXTURE),
                              "--test-otlp", "--out", str(work / "telegraf")], capture_output=True, text=True)
        if gen.returncode != 0:
            print(gen.stdout, gen.stderr)
            return 1
        # Drop fixture timestamps so Prometheus accepts the samples as current.
        lines = [l.rsplit(" ", 1)[0] for l in FIXTURE.read_text(encoding="utf-8").splitlines() if l.strip()]
        (work / "input.lp").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

        docker("network", "create", tag)
        docker("run", "-d", "--name", prom, "--network", tag, PROM,
               "--config.file=/etc/prometheus/prometheus.yml", "--web.enable-otlp-receiver")
        docker("run", "-d", "--name", alloy, "--network", tag,
               "-e", f"GC_OTLP_URL=http://{prom}:9090/api/v1/otlp", "-e", "GC_OTLP_ACCOUNT=test", "-e", "GC_OTLP_KEY=test",
               "-v", f"{host_path(REPO / 'alloy' / 'config.alloy')}:/etc/alloy/config.alloy:ro",
               ALLOY, "run", "/etc/alloy/config.alloy")
        time.sleep(10)
        run = docker("run", "--rm", "--network", tag, "-e", f"OTLP_ENDPOINT={alloy}:4317",
                     "-v", f"{host_path(work / 'telegraf')}:/etc/telegraf:ro",
                     "-v", f"{host_path(work / 'input.lp')}:/test/input.lp:ro",
                     TELEGRAF, "telegraf", "--config", "/etc/telegraf/telegraf.conf", "--once", check=False)
        if run.returncode != 0:
            print(run.stderr)
            return 1

        names: set[str] = set()
        # Alloy scrapes itself every 60s, so wait for that first scrape too.
        for _ in range(30):
            time.sleep(5)
            names = set(prom_get(prom, "/api/v1/label/__name__/values")["data"])
            if EXPECT <= names and ALLOY_EXPECT <= names:
                break
        junos = sorted(n for n in names if n.startswith("junos_"))
        print("metric names in Prometheus:")
        for n in junos:
            print("  " + n)
        # The Prometheus under test scrapes itself too, so pick Alloy's series by job, not by name.
        # prometheus.exporter.self targets carry job="integrations/self", which wins over job_name.
        match = urllib.parse.quote('{job="integrations/self"}')
        alloy_series = prom_get(prom, f"/api/v1/series?match[]={match}")["data"]
        alloy_names = sorted({s["__name__"] for s in alloy_series})
        print(f"Alloy self-metrics after the allowlist ({len(alloy_series)} series):")
        for n in alloy_names:
            print("  " + n)
        extra = sorted(n for n in alloy_names if not ALLOY_ALLOWED.fullmatch(n))
        if extra:
            print("FAIL Alloy self-metrics outside the allowlist:", extra)
            return 1
        if not ALLOY_EXPECT <= set(alloy_names):
            print("FAIL missing Alloy self-metrics:", sorted(ALLOY_EXPECT - set(alloy_names)))
            return 1
        internal = sorted(n for n in names if n.startswith("openjts_internal_"))
        print("collector health metrics:")
        for n in internal:
            print("  " + n)
        if not internal:
            print("FAIL no openjts_internal_* metrics from inputs.internal")
            return 1
        # Every query on the cost dashboard that reads this stack's metrics datasource has to run, and the
        # ones that are not rates have to return something (a rate needs two scrapes, this run has one).
        bad = []
        for name in ("openjts-cost.json", "openjts-series-per-device.json"):
            cost = json.loads((REPO / "dashboards" / name).read_text(encoding="utf-8"))
            for p in cost["panels"]:
                for t in p.get("targets") or []:
                    if t["datasource"]["uid"] != "${datasource}":
                        continue
                    expr = t["expr"].replace("$device", ".+")  # the variable's All value
                    try:
                        res = prom_get(prom, "/api/v1/query?query=" + urllib.parse.quote(expr))["data"]["result"]
                    except Exception as e:  # noqa: BLE001
                        bad.append(f"{name}: {p['title']}: {e}")
                        continue
                    if not res and "rate(" not in expr:
                        bad.append(f"{name}: {p['title']}: no data for {expr}")
        if bad:
            print("FAIL cost dashboard queries:")
            for b in bad:
                print("  " + b)
            return 1
        print("cost dashboard queries OK")
        fleet = next(t["expr"] for p in json.loads((REPO / "dashboards" / "openjts-series-per-device.json").read_text(encoding="utf-8"))["panels"]
                     if p["title"] == "Series per router" for t in p["targets"])
        for r in prom_get(prom, "/api/v1/query?query=" + urllib.parse.quote(fleet))["data"]["result"]:
            print("  series per router:", json.dumps(r["metric"], sort_keys=True), r["value"][1])
        sample = prom_get(prom, "/api/v1/query?query=junos_interfaces_in_octets_total")["data"]["result"]
        if sample:
            print("labels on junos_interfaces_in_octets_total:", json.dumps(sample[0]["metric"], sort_keys=True))
        missing = EXPECT - names
        if missing:
            print("FAIL missing:", sorted(missing))
            print(docker("logs", alloy, check=False).stderr[-3000:])
            return 1
        print(f"\nOK ({len(junos)} junos_* metrics)")
        return 0
    finally:
        for c in (alloy, prom):
            docker("rm", "-f", c, check=False)
        docker("network", "rm", tag, check=False)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
