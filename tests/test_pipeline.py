#!/usr/bin/env python3
"""Run the generated processor chain in a real Telegraf container.

Feeds tests/fixtures/pipeline.lp (line protocol shaped like inputs.gnmi output
with canonical_field_names + prefix_tag_key_with_path + juniper_header) through
every profile pipeline and checks names, labels and counter/gauge typing.

  python3 tests/test_pipeline.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "pipeline.lp"
IMAGE = os.environ.get("TELEGRAF_IMAGE", "telegraf:1.36")

# (metric, labels that must match, field key, value)
EXPECT = [
    ("junos_interfaces_in_octets", {"device": "edge1", "if_name": "et-0/0/0", "if_description": "to-core1"}, "counter", 123456789),
    ("junos_interfaces_out_octets", {"if_name": "et-0/0/0"}, "counter", 987654321),
    ("junos_interfaces_in_errors", {"if_name": "et-0/0/0"}, "counter", 3),
    ("junos_interfaces_cos_red_dropped_packets", {"if_name": "et-0/0/0", "queue_id": "3"}, "counter", 7),
    ("junos_interfaces_cos_buffer_cur", {"queue_id": "3"}, "gauge", 1024),
    ("junos_subinterfaces_unit_in_octets", {"if_name": "ae1", "if_unit": "100"}, "counter", 18446744073709551000),
    ("junos_components_linecard_cpu", {"component_name": "FPC0:CPU0", "component_id": "1", "sub_component_id": "0"}, "gauge", 35),
    ("junos_components_idle_cpu", {"component_name": "Routing Engine0"}, "gauge", 92),
    ("junos_hw_npu_value", {"component_name": "FPC0:CPU0", "property_name": "cpu-utilization-total"}, "gauge", 35),
    ("junos_network_instance_bgp_session_state", {"instance_name": "master", "neighbor_address": "192.0.2.1", "value": "ESTABLISHED"}, "gauge", 1),
    ("junos_network_instance_bgp_session_peer_as", {"neighbor_address": "192.0.2.1"}, "gauge", 65001),
    ("junos_network_instance_bgp_safi_received", {"neighbor_afi_safi_name": "IPV4_UNICAST"}, "gauge", 1200),
    ("junos_network_instance_bgp_max_prefixes", {"neighbor_afi_safi_name": "IPV4_UNICAST"}, "gauge", 5000),
    ("junos_ddos_stats_arrival_rate", {"protocol": "arp", "sub_protocol": "aggregate", "location": "fpc2", "component_id": "2"}, "gauge", 15),
    ("junos_ddos_stats_dropped_aggregate_policer", {"location": "fpc2"}, "counter", 100),
    ("junos_ddos_stats_policer_state", {"value": "ok"}, "gauge", 1),
    ("junos_components_optic_vendor", {"port_id": "0/0/3", "value": "JUNIPER-FINISAR"}, "gauge", 1),
    ("junos_components_fec_corrected_bits", {"port_id": "0/0/3"}, "counter", 1000),
    ("junos_components_tx_dbm", {"if_name": "et-0/0/5", "port_id": "0/0/5", "lane_number": "0"}, "gauge", -2.1),
    ("junos_interfaces_eth_in_crc_errors", {"if_name": "et-0/0/5", "port_id": "0/0/5"}, "counter", 4),
    ("junos_firewall_drop_pkts", {"plcr_name": "POL1", "component_id": "0"}, "counter", 12),
]
FORBIDDEN_LABELS = {"jts_pipeline", "path", "source", "host"}
FORBIDDEN_VALUES = {"em0"}


def _wsl_docker() -> bool:
    return os.environ.get("OPENJTS_DOCKER", "").lower() == "wsl"


def docker_cmd() -> list[str]:
    # OPENJTS_DOCKER=wsl runs the Docker engine inside WSL from a Windows checkout.
    return ["wsl", "-e", "docker"] if _wsl_docker() else ["docker"]


def scratch_dir(prefix: str) -> Path:
    """Gitignored work dir under tests/, readable by the non-root users in the images."""
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=REPO / "tests"))
    d.chmod(0o755)
    return d


def host_path(p: Path) -> str:
    if not _wsl_docker():
        return str(p)
    drive, rest = p.drive, p.as_posix()[2:]
    return f"/mnt/{drive[0].lower()}{rest}"


def parse_line(line: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Minimal line-protocol parser for Telegraf's own output (escaped spaces/commas)."""
    parts, buf, esc, in_quote = [], "", False, False
    for ch in line:
        if esc:
            buf += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_quote = not in_quote
            buf += ch
        elif ch == " " and not in_quote:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    series, fields_raw = parts[0], parts[1]
    name, *tag_pairs = series.split(",")
    tags = dict(t.split("=", 1) for t in tag_pairs)
    fields = dict(f.split("=", 1) for f in fields_raw.split(","))
    return name, tags, fields


def main() -> int:
    out = scratch_dir("openjts-test-")
    try:
        gen = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "generate.py"), "--test-input", str(FIXTURE), "--out", str(out)],
            capture_output=True, text=True,
        )
        if gen.returncode != 0:
            print(gen.stdout, gen.stderr)
            return 1
        cmd = docker_cmd() + [
            "run", "--rm",
            "-v", f"{host_path(out)}:/etc/telegraf:ro",
            "-v", f"{host_path(FIXTURE)}:/test/input.lp:ro",
            IMAGE, "telegraf", "--config", "/etc/telegraf/telegraf.conf", "--once",
        ]
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if run.returncode != 0:
            print(run.stderr)
            return 1
        lines = [l for l in run.stdout.splitlines() if l.strip()]
        metrics = [parse_line(l) for l in lines]
    finally:
        shutil.rmtree(out, ignore_errors=True)

    failures = []
    for name, want_tags, key, value in EXPECT:
        hits = [
            (t, f) for n, t, f in metrics
            if n == name and all(t.get(k) == v for k, v in want_tags.items())
        ]
        if not hits:
            failures.append(f"missing {name} {want_tags}")
            continue
        tags, fields = hits[0]
        if list(fields) != [key]:
            failures.append(f"{name}: fields {fields}, want only {key!r}")
            continue
        got = fields[key].rstrip("iu")
        if float(got) != float(value):
            failures.append(f"{name}: {key}={got}, want {value}")
    for name, tags, fields in metrics:
        bad = FORBIDDEN_LABELS & set(tags)
        if bad:
            failures.append(f"{name}: leaked labels {sorted(bad)}")
        if FORBIDDEN_VALUES & set(tags.values()):
            failures.append(f"{name}: filtered interface leaked {tags}")
        if any("/" in k for k in tags):
            failures.append(f"{name}: unrenamed path label {tags}")

    print(f"{len(metrics)} series from {FIXTURE.name}")
    for line in lines:
        print("  " + line)
    if failures:
        print("\nFAIL")
        for f in failures:
            print("  " + f)
        return 1
    print(f"\nOK ({len(EXPECT)} expectations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
