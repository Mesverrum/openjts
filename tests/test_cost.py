#!/usr/bin/env python3
"""The collector must not out-run the 1 DPM per series that Grafana Cloud includes.

Renders the production config (not the test fixture config) from targets/routers.yaml.sample and
checks every interval in it against min_sample_interval, then, when Docker is reachable, has real
Telegraf load the result (`telegraf config check`) so a key Telegraf does not know fails here.

  python3 tests/test_cost.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import REPO, docker_cmd, host_path, scratch_dir  # noqa: E402

SAMPLE = REPO / "targets" / "routers.yaml.sample"
IMAGE = os.environ.get("TELEGRAF_IMAGE", "telegraf:1.36")
failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    if not ok:
        failures.append(msg)
        print("FAIL", msg)


def targets_with(floor: object | None, dest: Path) -> Path:
    text = SAMPLE.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^min_sample_interval:.*\n", "", text)
    if floor is not None:
        text = text.replace("defaults:", f"min_sample_interval: {floor}\n\ndefaults:", 1)
    dest.write_text(text, encoding="utf-8", newline="\n")
    return dest


def render(targets: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "scripts" / "generate.py"), "--targets", str(targets),
                           "--out", str(out)], capture_output=True, text=True)


def seconds(v: str) -> int:
    assert v.endswith("s"), v
    return int(v[:-1])


def audit(conf: str, floor: int) -> dict[str, int]:
    """Check every interval in a rendered config. Returns counts for the summary line."""
    agent = re.search(r'\[agent\]\n\s+interval = "(\d+)s"', conf)
    check(bool(agent) and int(agent.group(1)) == floor, f"[agent] interval is not {floor}s")

    internal = re.search(r"\[\[inputs\.internal\]\](.*?)(?=\n\[\[|\Z)", conf, re.S)
    check(bool(internal), "inputs.internal missing")
    if internal:
        body = internal.group(1)
        check(f'interval = "{floor}s"' in body, f"inputs.internal interval is not {floor}s")
        check("collect_memstats = false" in body, "inputs.internal still collects memstats")
        check("namepass" in body, "inputs.internal has no namepass allowlist")

    for m in re.finditer(r"\[\[inputs\.mock\]\](.*?)(?=\n\[\[|\Z)", conf, re.S):
        check(f'interval = "{floor}s"' in m.group(1), f"inputs.mock interval is not {floor}s")

    sample = on_change = 0
    for block in re.split(r"(?m)^  \[\[inputs\.gnmi\.subscription\]\]\n", conf)[1:]:
        body = block.split("\n  [", 1)[0].split("\n\n", 1)[0]
        mode = re.search(r'subscription_mode = "(\w+)"', body).group(1)
        name = re.search(r'name = "([^"]+)"', body).group(1)
        if mode == "sample":
            sample += 1
            v = seconds(re.search(r'sample_interval = "([^"]+)"', body).group(1))
            check(v >= floor, f"{name}: sample_interval {v}s is under the {floor}s floor")
        else:
            on_change += 1
            hb = re.search(r'heartbeat_interval = "([^"]+)"', body)
            check(bool(hb), f"{name}: on_change without heartbeat_interval")
            if hb:
                check(seconds(hb.group(1)) == floor, f"{name}: heartbeat {hb.group(1)} is not {floor}s")
    check(sample > 0, "no sample subscriptions rendered")
    return {"sample": sample, "on_change": on_change}


def telegraf_loads(out: Path) -> bool | None:
    """True/False from real Telegraf, None when Docker is not reachable."""
    try:
        probe = subprocess.run([*docker_cmd(), "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    run = subprocess.run([*docker_cmd(), "run", "--rm", "-e", "JUNOS_USERNAME=u", "-e", "JUNOS_PASSWORD=p",
                          "-e", "JUNOS_PASSWORD_LYO=p", "-e", "OTLP_ENDPOINT=localhost:4317",
                          "-v", f"{host_path(out)}:/etc/telegraf:ro", IMAGE,
                          "telegraf", "config", "check", "--config", "/etc/telegraf/telegraf.conf"],
                         capture_output=True, text=True)
    if run.returncode != 0:
        print((run.stdout + run.stderr)[-3000:])
    return run.returncode == 0


def internal_names(conf: str, work: Path) -> set[str] | None:
    """Run only the rendered inputs.internal block in real Telegraf and return the measurement names it emits."""
    block = re.search(r"\[\[inputs\.internal\]\].*?(?=\n\[\[|\Z)", conf, re.S).group(0)
    d = scratch_dir("openjts-test-internal-")
    try:
        (d / "t.conf").write_text('[agent]\n  omit_hostname = true\n[[outputs.file]]\n  files = ["stdout"]\n'
                                  '  data_format = "influx"\n' + block + "\n", encoding="utf-8", newline="\n")
        run = subprocess.run([*docker_cmd(), "run", "--rm", "-v", f"{host_path(d / 't.conf')}:/t.conf:ro", IMAGE,
                              "telegraf", "--config", "/t.conf", "--test"], capture_output=True, text=True, encoding="utf-8")
        return {l.lstrip("> ").split(",")[0].split(" ")[0] for l in run.stdout.splitlines() if l.startswith(">")}
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="openjts-cost-"))
    work = scratch_dir("openjts-test-cost-")
    try:
        for floor_arg, floor in ((None, 60), (120, 120), (30, 30)):
            t = targets_with(floor_arg, tmp / "routers.yaml")
            out = work / f"out{floor}" / "telegraf"
            r = render(t, out)
            check(r.returncode == 0, f"render failed for floor {floor_arg}: {r.stdout}{r.stderr}")
            if r.returncode:
                continue
            conf = (out / "telegraf.conf").read_text(encoding="utf-8")
            counts = audit(conf, floor)
            if floor_arg is None:
                check("min_sample_interval=60s raised" in r.stdout, "generate.py did not report the raised subscriptions")
            print(f"floor {floor}s: {counts['sample']} sample, {counts['on_change']} on_change subscriptions OK")

        for bad in (0, 301, '"60"', "true", "1.5"):
            r = render(targets_with(bad, tmp / "bad.yaml"), work / "bad" / "telegraf")
            check(r.returncode != 0 and "min_sample_interval" in (r.stdout + r.stderr),
                  f"min_sample_interval: {bad} was accepted")

        # Same targets with TLS off, only because the sample's CA file does not exist here.
        text = (tmp / "routers.yaml").read_text(encoding="utf-8")
        text = text.replace("enable: true", "enable: false").replace("    ca: certs/ca.pem\n", "")
        (tmp / "notls.yaml").write_text(text, encoding="utf-8", newline="\n")
        r = render(tmp / "notls.yaml", work / "notls" / "telegraf")
        check(r.returncode == 0, f"render without TLS failed: {r.stdout}{r.stderr}")
        loaded = telegraf_loads(work / "notls" / "telegraf") if r.returncode == 0 else False

        # Every profile x family, so a heartbeat or interval Telegraf rejects on one gNMI path cannot hide.
        allp = work / "all" / "telegraf"
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "generate.py"), "--all-profiles", "--out", str(allp)],
                           capture_output=True, text=True)
        check(r.returncode == 0, f"--all-profiles render failed: {r.stdout}{r.stderr}")
        if r.returncode == 0:
            counts = audit((allp / "telegraf.conf").read_text(encoding="utf-8"), 60)
            print(f"all profiles x families at 60s: {counts['sample']} sample, {counts['on_change']} on_change subscriptions OK")
            if loaded is not None:
                ok = telegraf_loads(allp)
                check(bool(ok), "real Telegraf rejected the all-profiles production config")
                if ok:
                    print("real Telegraf loaded the all-profiles production config")
        if loaded is None:
            print("skip: Docker not reachable, real Telegraf load not checked")
        else:
            check(loaded, "real Telegraf rejected the production config")
            if loaded:
                print("real Telegraf loaded the production config")
                got = internal_names((work / "notls" / "telegraf" / "telegraf.conf").read_text(encoding="utf-8"), work)
                want = {"openjts_internal_agent", "openjts_internal_write"}
                check(got == want, f"inputs.internal emits {sorted(got)}, want {sorted(want)}")
                print("inputs.internal emits", sorted(got))
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nOK" if not failures else f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
