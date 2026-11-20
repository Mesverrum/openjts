#!/usr/bin/env python3
"""Render generated/telegraf/ from a targets file and design_profile/.

Usage:
  python3 scripts/generate.py                       # targets/routers.yaml
  python3 scripts/generate.py --targets targets/lab.yaml
  python3 scripts/generate.py --test-input tests/fixtures/sample.lp

--test-input swaps the gNMI inputs for a line-protocol file and the OTLP output
for stdout, so the processor chain can be checked with a real Telegraf binary
and no router (see tests/test_pipeline.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profiles as P  # noqa: E402

REPO = P.REPO
OUT_DIR = REPO / "generated" / "telegraf"
STAR_SRC = REPO / "scripts" / "finalize.star"
DESCRIPTION_SUB = "/interfaces/interface/state/description"

# One data point per series per minute is what Grafana Cloud includes. Profiles ask for 30s
# on traffic, optics, TWAMP and SR-MPLS; the floor raises those. It stays under the 5 minute
# Prometheus staleness window so a slow series never drops out of a graph.
DEFAULT_FLOOR = 60
MAX_FLOOR = 300


def q(value) -> str:
    """TOML scalar. JSON string escaping is valid TOML basic-string escaping."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(q(v) for v in value) + "]"
    return json.dumps(str(value))


def load_targets(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"{path} not found. Copy targets/routers.yaml.sample to {path} and edit it.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    routers = data.get("routers") or []
    if not routers:
        sys.exit(f"{path}: no routers listed")
    defaults = data.get("defaults") or {}
    names = set()
    out = []
    for i, r in enumerate(routers):
        merged = {**defaults, **r}
        merged["tls"] = {**(defaults.get("tls") or {}), **(r.get("tls") or {})}
        for key in ("name", "address", "family", "version"):
            if not merged.get(key):
                sys.exit(f"{path}: routers[{i}] is missing {key!r}")
        merged["version"] = str(merged["version"])
        if merged["name"] in names:
            sys.exit(f"{path}: duplicate router name {merged['name']!r}")
        names.add(merged["name"])
        if not merged.get("profiles"):
            sys.exit(f"{path}: {merged['name']} has no profiles (set defaults.profiles or per router)")
        out.append(merged)
    floor = data.get("min_sample_interval", DEFAULT_FLOOR)
    if not isinstance(floor, int) or isinstance(floor, bool) or not 1 <= floor <= MAX_FLOOR:
        sys.exit(f"{path}: min_sample_interval must be whole seconds between 1 and {MAX_FLOOR}, got {floor!r}")
    return {"collector": data.get("collector", "openjts"), "min_sample_interval": floor, "routers": out}


def plan(routers: list[dict]) -> tuple[dict[tuple, P.Pipeline], dict[str, P.Pipeline], list[str]]:
    pipelines: dict[tuple, P.Pipeline] = {}
    by_router: dict[str, P.Pipeline] = {}
    notes: list[str] = []
    for r in routers:
        confs = []
        for profile in r["profiles"]:
            conf = P.select_conf(profile, r["family"], r["version"])
            if conf is None:
                notes.append(f"{r['name']}: profile {profile} has no variant for {r['family']} {r['version']}, skipped")
                continue
            confs.append(conf)
        if not confs:
            notes.append(f"{r['name']}: nothing to collect, router left out")
            continue
        key = tuple(confs)
        if key not in pipelines:
            pipelines[key] = P.build_pipeline("+".join(c.stem for c in confs), key)
        by_router[r["name"]] = pipelines[key]
    return pipelines, by_router, notes


def render_processor(lines: list[str], stage: str, cfg: dict, order: int, pid: str) -> None:
    ns = cfg["namepass"]
    tagpass = [f"  [processors.{{kind}}.tagpass]", f"    jts_pipeline = {q([pid])}"]

    def head(kind: str) -> None:
        lines.append(f"[[processors.{kind}]]")
        lines.append(f"  order = {order}")
        lines.append(f"  namepass = {q(ns)}")

    def tail(kind: str) -> None:
        lines.extend(t.format(kind=kind) for t in tagpass)
        lines.append("")

    if stage == "clone":
        head("clone")
        lines.append(f"  name_override = {q(cfg['name_override'])}")
        tail("clone")
    elif stage == "pivot":
        head("pivot")
        lines.append(f"  tag_key = {q(cfg['tag_key'])}")
        lines.append(f"  value_key = {q(cfg['value_key'])}")
        tail("pivot")
    elif stage == "rename":
        head("rename")
        for rep in cfg["replace"]:
            lines.append("  [[processors.rename.replace]]")
            for k, v in rep.items():
                lines.append(f"    {k} = {q(v)}")
        tail("rename")
    elif stage == "strings":
        head("strings")
        for method in ("lowercase", "uppercase"):
            for op in cfg.get(method, []):
                lines.append(f"  [[processors.strings.{method}]]")
                for k, v in op.items():
                    lines.append(f"    {k} = {q(v)}")
        tail("strings")
    elif stage in ("regex", "xreducer"):
        head("regex")
        for section in ("tags", "fields", "tag_rename", "field_rename"):
            for op in cfg.get(section, []):
                lines.append(f"  [[processors.regex.{section}]]")
                for k, v in op.items():
                    lines.append(f"    {k} = {q(v)}")
        tail("regex")
    elif stage == "filter":
        head("filter")
        lines.append('  default = "drop"')
        lines.append("  [[processors.filter.rule]]")
        lines.append(f"    tags = {{ {q(cfg['key'])} = {q(cfg['globs'])} }}")
        lines.append('    action = "pass"')
        tail("filter")
    elif stage == "converter":
        head("converter")
        lines.append("  [processors.converter.fields]")
        for kind, values in cfg["fields"].items():
            lines.append(f"    {kind} = {q(values)}")
        tail("converter")
    else:
        raise ValueError(stage)


def tls_lines(tls: dict) -> list[str]:
    out = []
    if "enable" in tls:
        out.append(f"  tls_enable = {q(bool(tls['enable']))}")
    for key, opt in (("ca", "tls_ca"), ("cert", "tls_cert"), ("key", "tls_key")):
        if tls.get(key):
            out.append(f"  {opt} = {q('/certs/' + Path(tls[key]).name)}")
    if tls.get("server_name"):
        out.append(f"  tls_server_name = {q(tls['server_name'])}")
    if tls.get("insecure_skip_verify"):
        out.append("  insecure_skip_verify = true")
    return out


def render(targets: dict, test_input: Path | None, test_otlp: bool = False) -> tuple[str, list[str]]:
    pipelines, by_router, notes = plan(targets["routers"])
    floor = targets.get("min_sample_interval", DEFAULT_FLOOR)
    lines = [
        "# Generated by scripts/generate.py from the targets file and design_profile/.",
        "# Do not edit. Re-run `make generate` instead.",
        "",
        "[agent]",
        f'  interval = "{floor}s"',
        "  round_interval = true",
        "  metric_batch_size = 5000",
        "  metric_buffer_limit = 200000",
        '  flush_interval = "10s"',
        '  flush_jitter = "2s"',
        "  omit_hostname = true",
        "  skip_processors_after_aggregators = true",
        "",
    ]
    # Collector health only: one set of agent and output counters. internal_gather is one set per
    # input plugin (two per router), and memstats is another 20 series, neither worth billing for.
    # Per-router liveness comes from junos_device_info instead.
    internal = [
        "[[inputs.internal]]",
        f'  interval = "{floor}s"',
        '  name_prefix = "openjts_"',
        "  collect_memstats = false",
        # namepass sees the name before name_prefix is applied.
        '  namepass = ["internal_agent", "internal_write"]',
        "",
    ]
    if test_input:
        if test_otlp:
            lines += [
                "[[outputs.opentelemetry]]",
                '  service_address = "${OTLP_ENDPOINT}"',
                "  [outputs.opentelemetry.attributes]",
                '    "service.name" = "openjts"',
                '    "service.instance.id" = "test"',
                "",
                *internal,
                # The per-router info series, so the end-to-end test can run the dashboards that read it.
                "[[inputs.mock]]",
                f'  interval = "{floor}s"',
                '  metric_name = "junos_device"',
                "  [[inputs.mock.constant]]",
                '    name = "info"',
                "    value = 1",
                "  [inputs.mock.tags]",
                '    device = "edge1"',
                '    family = "mx"',
                '    version = "23.4R2"',
                '    model = "MX304"',
                '    site = "par"',
                '    profiles = "health,traffic"',
                "",
            ]
        else:
            lines += ["[[outputs.file]]", '  files = ["stdout"]', '  data_format = "influx"', ""]
        lines += [
            "[[inputs.file]]",
            '  files = ["/test/input.lp"]',
            '  data_format = "influx"',
            "",
        ]
    else:
        lines += [
            "[[outputs.opentelemetry]]",
            '  service_address = "${OTLP_ENDPOINT}"',
            '  compression = "gzip"',
            "  [outputs.opentelemetry.attributes]",
            '    "service.name" = "openjts"',
            f'    "service.instance.id" = {q(targets["collector"])}',
            "",
            *internal,
        ]
        for r in targets["routers"]:
            pipe = by_router.get(r["name"])
            if pipe is None:
                continue
            lines += [
                "[[inputs.gnmi]]",
                f"  alias = {q(r['name'])}",
                f"  addresses = {q([str(r['address']) + ':' + str(r.get('port', 9339))])}",
                f"  username = {q('${' + r.get('username_env', 'JUNOS_USERNAME') + '}')}",
                f"  password = {q('${' + r.get('password_env', 'JUNOS_PASSWORD') + '}')}",
                f"  encoding = {q(r.get('encoding', 'proto'))}",
                '  redial = "10s"',
                "  canonical_field_names = true",
                "  prefix_tag_key_with_path = true",
                '  vendor_specific = ["juniper_header"]',
                *tls_lines(r.get("tls") or {}),
                "  [inputs.gnmi.tags]",
                f"    device = {q(r['name'])}",
                f"    jts_pipeline = {q(pipe.pid)}",
            ]
            if pipe.aliases:
                lines.append("  [inputs.gnmi.aliases]")
                for name, prefix in pipe.aliases:
                    lines.append(f"    {name} = {q(prefix)}")
            raised = 0
            for (name, path), sub in pipe.subscriptions.items():
                lines += [
                    "  [[inputs.gnmi.subscription]]",
                    f"    name = {q(name)}",
                    f"    path = {q(path)}",
                    f"    subscription_mode = {q(sub['mode'])}",
                ]
                if sub["mode"] == "sample":
                    interval = max(int(sub["interval"]), floor)
                    raised += interval != int(sub["interval"])
                    lines.append(f"    sample_interval = {q(str(interval) + 's')}")
                else:
                    # on_change alone goes quiet, the series ages out after 20 minutes and
                    # graphs show holes. The heartbeat re-sends at the floor, so steady state
                    # is the same one point per minute as everything else.
                    lines.append(f"    heartbeat_interval = {q(str(floor) + 's')}")
            if raised:
                notes.append(f"{r['name']}: min_sample_interval={floor}s raised {raised} of "
                             f"{len(pipe.subscriptions)} subscriptions (profiles ask for less)")
            lines += [
                "  [[inputs.gnmi.tag_subscription]]",
                '    name = "ifdesc"',
                f"    path = {q(DESCRIPTION_SUB)}",
                '    subscription_mode = "on_change"',
                '    elements = ["interface"]',
                "",
                "[[inputs.mock]]",
                f"  alias = {q('device_info_' + r['name'])}",
                f'  interval = "{floor}s"',
                '  metric_name = "junos_device"',
                "  [[inputs.mock.constant]]",
                '    name = "info"',
                "    value = 1",
                "  [inputs.mock.tags]",
                f"    device = {q(r['name'])}",
                f"    family = {q(r['family'])}",
                f"    version = {q(r['version'])}",
                f"    model = {q(r.get('model', ''))}",
                f"    site = {q(r.get('site', ''))}",
                f"    profiles = {q(','.join(r['profiles']))}",
                "",
            ]

    order = 1
    for pipe in pipelines.values():
        if pipe.alias_renames:
            lines += ["[[processors.rename]]", f"  order = {order}"]
            for src, dst in pipe.alias_renames:
                lines += ["  [[processors.rename.replace]]", f"    measurement = {q(src)}", f"    dest = {q(dst)}"]
            lines += ["  [processors.rename.tagpass]", f"    jts_pipeline = {q([pipe.pid])}", ""]
            order += 1
    for pipe in pipelines.values():
        lines.append(f"# pipeline {pipe.pid}: " + ", ".join(c.relative_to(P.PROFILE_DIR).as_posix() for c in pipe.confs))
        for stage, cfg in pipe.processors:
            render_processor(lines, stage, cfg, order, pipe.pid)
            order += 1
        notes.extend(f"{pipe.pid} {s}" for s in pipe.skipped)

    lines += [
        "[[processors.starlark]]",
        "  order = 100000",
        '  script = "/etc/telegraf/openjts.star"',
        "  [processors.starlark.tagpass]",
        '    jts_pipeline = ["*"]',
        "",
    ]
    return "\n".join(lines), notes


def render_test_targets() -> dict:
    """Pipelines for every profile x family, so the fixture can exercise any of them."""
    routers = []
    for profile in P.profiles():
        for family, variants in (P.definition(profile).get("telegraf") or {}).items():
            for variant in variants:
                version = "1.0" if variant["version"] == "all" else variant["version"].lstrip("<>=").rstrip("R")
                routers.append({"name": f"{profile}-{family}-{version}", "address": "test", "family": family,
                                "version": version, "profiles": [profile], "tls": {}})
    return {"collector": "test", "routers": routers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--targets", default=str(REPO / "targets" / "routers.yaml"))
    parser.add_argument("--test-input", help="line-protocol fixture; renders a stdout-only test config")
    parser.add_argument("--test-otlp", action="store_true", help="with --test-input, send to ${OTLP_ENDPOINT} instead of stdout")
    parser.add_argument("--all-profiles", action="store_true",
                        help="test hook: the production config for a router per profile x family, to prove Telegraf loads all of it")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    test_input = Path(args.test_input) if args.test_input else None
    if args.all_profiles:
        targets = render_test_targets()
    else:
        targets = render_test_targets() if test_input else load_targets(Path(args.targets))
    try:
        conf, notes = render(targets, test_input, args.test_otlp)
    except P.ProfileError as e:
        sys.exit(f"profile error: {e}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "telegraf.conf").write_text(conf + "\n", encoding="utf-8", newline="\n")
    spec = P.metric_spec()
    star = "SPEC = " + json.dumps(spec, indent=1) + "\n\n" + STAR_SRC.read_text(encoding="utf-8")
    (out / "openjts.star").write_text(star, encoding="utf-8", newline="\n")

    if not test_input:
        cred_vars = sorted({r.get(k, d) for r in targets["routers"]
                            for k, d in (("username_env", "JUNOS_USERNAME"), ("password_env", "JUNOS_PASSWORD"))})
        override = ["# Generated by scripts/generate.py. Credential variables named in the targets file.",
                    "services:", "  telegraf:", "    environment:"]
        override += [f"      {v}: ${{{v}:?set {v} in .env}}" for v in cred_vars]
        (out.parent / "compose.credentials.yaml").write_text("\n".join(override) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {out / 'telegraf.conf'} and {out / 'openjts.star'}")
    for note in sorted(set(notes)):
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
