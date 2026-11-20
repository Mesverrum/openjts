#!/usr/bin/env python3
"""Build dashboards/openjts-cost.json: what OpenJTS costs in Grafana Cloud metrics, and where it comes from.

Not converted from jtso. Written for this fork, modelled on the active series and DPM panels
in the Grafana Cloud "Billing/Usage" dashboard.

Three rows:
  Stack billing    grafanacloud-usage datasource: active series, DPM, included DPM, billable series.
  OpenJTS series   this stack's Prometheus datasource: active series by metric, device, profile area.
  Sample rate      what leaves the collector (Alloy's exporter counters, Telegraf's own write counters).

  python3 scripts/build_cost_dashboard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "dashboards" / "openjts-cost.json"

DS = {"type": "prometheus", "uid": "${datasource}"}
USAGE = {"type": "prometheus", "uid": "${usage}"}
JOB = 'job="openjts"'
ALLOY = 'job="integrations/self"'
# junos_interfaces_in_octets_total -> "interfaces"; junos_device_info -> "device"
AREA = 'label_replace({%s, __name__=~"junos_.+"}, "area", "$1", "__name__", "junos_([a-z0-9]+)_.+")' % JOB

DOCS = "https://grafana.com/docs/grafana-cloud/cost-management-and-billing/understand-your-invoice/metrics-invoice/"

_id = 0


def nid() -> int:
    global _id
    _id += 1
    return _id


def target(expr: str, legend: str = "", ref: str = "A", ds: dict = DS, instant: bool = False, fmt: str | None = None) -> dict:
    t = {"datasource": ds, "expr": expr, "refId": ref, "legendFormat": legend}
    if instant:
        t.update({"instant": True, "range": False})
    if fmt:
        t["format"] = fmt
    return t


def panel(kind: str, title: str, desc: str, targets: list[dict], x: int, y: int, w: int, h: int,
          unit: str = "short", ds: dict = DS, decimals: int | None = None, thresholds: list | None = None,
          options: dict | None = None, overrides: list | None = None, transformations: list | None = None,
          min_: float | None = 0) -> dict:
    defaults: dict = {"unit": unit, "color": {"mode": "palette-classic"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if min_ is not None:
        defaults["min"] = min_
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        if kind in ("stat", "bargauge", "gauge"):
            defaults["color"] = {"mode": "thresholds"}
    p = {"id": nid(), "type": kind, "title": title, "description": desc, "datasource": ds, "targets": targets,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []}, "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def row(title: str, y: int) -> dict:
    return {"id": nid(), "type": "row", "title": title, "collapsed": False, "panels": [], "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


def stat(*a, **kw) -> dict:
    kw["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "value", "graphMode": "area", "textMode": "auto"}
    return panel("stat", *a, **kw)


def ts(*a, **kw) -> dict:
    kw["options"] = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}}
    return panel("timeseries", *a, **kw)


def table(title: str, desc: str, expr: str, x: int, y: int, w: int, h: int, label_cols: list[str], ds: dict = DS) -> dict:
    """Instant table, one row per label set, largest first."""
    rename = {"Value": "Series"}
    return panel("table", title, desc, [target(expr, instant=True, fmt="table")], x, y, w, h, ds=ds,
                 options={"showHeader": True, "sortBy": [{"displayName": "Series", "desc": True}], "footer": {"show": False}},
                 transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                                  "renameByName": rename,
                                                                  "indexByName": {c: i for i, c in enumerate(label_cols + ["Series"])}}}],
                 min_=None)


def bars(title: str, desc: str, expr: str, legend: str, x: int, y: int, w: int, h: int) -> dict:
    return panel("bargauge", title, desc, [target(expr, legend, instant=True)], x, y, w, h,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                          "valueMode": "color"})


def build() -> dict:
    global _id
    _id = 0
    panels: list[dict] = []

    panels.append({"id": nid(), "type": "text", "title": "", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
                   "options": {"mode": "markdown", "content": (
                       "**What this costs.** Grafana Cloud bills metrics on whichever is higher, active series or total "
                       "data points per minute (DPM) divided by the DPM included per series (1 on Pro), at the 95th "
                       f"percentile ([metrics invoice]({DOCS})). Halve the sample interval and the same series cost twice as much. "
                       "Row 1 is the stack as billed (`grafanacloud-usage`), row 2 is where OpenJTS series come from, "
                       "row 3 is what the collector sends. The collector raises every gNMI sample interval to "
                       "`min_sample_interval` (60s by default), see `docs/cost.md`.")}})

    y = 4
    panels.append(row("Stack billing (grafanacloud-usage datasource, whole stack, not only OpenJTS)", y)); y += 1
    mid = 'id=~"$metrics_id"'
    panels.append(stat("Active series", "Series that received a sample in the last 20 minutes, summed over the selected metrics instances. "
                       "Source: grafanacloud_instance_active_series.",
                       [target(f"sum(max by (id) (grafanacloud_instance_active_series{{{mid}}}))", ds=USAGE)], 0, y, 5, 4, ds=USAGE))
    panels.append(stat("DPM per active series (average)", "60 x samples per second / active series. Over 1 you are paying for data points, not series. "
                       "Source: grafanacloud_instance_samples_per_second, grafanacloud_instance_active_series.",
                       [target(f"60 * sum(max by (id) (grafanacloud_instance_samples_per_second{{{mid}}})) / sum(max by (id) (grafanacloud_instance_active_series{{{mid}}}))", ds=USAGE)],
                       5, y, 5, 4, ds=USAGE, decimals=2,
                       thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.05}, {"color": "red", "value": 1.5}]))
    panels.append(stat("Included DPM per series", "What the plan includes. grafanacloud_org_metrics_included_dpm_per_series.",
                       [target("max(grafanacloud_org_metrics_included_dpm_per_series)", ds=USAGE)], 10, y, 4, 4, ds=USAGE))
    panels.append(stat("Billable series (org)", "max(active series, total DPM / included DPM) at the 95th percentile. grafanacloud_org_metrics_billable_series.",
                       [target("max(grafanacloud_org_metrics_billable_series)", ds=USAGE)], 14, y, 5, 4, ds=USAGE))
    panels.append(stat("Included series (org)", "grafanacloud_org_metrics_included_series.",
                       [target("max(grafanacloud_org_metrics_included_series)", ds=USAGE)], 19, y, 5, 4, ds=USAGE))
    y += 4
    panels.append(ts("Active series vs series implied by DPM",
                     "Billing takes the higher line. When the dashed DPM line is above active series you are paying for sample rate, "
                     "not cardinality, and the fix is a longer scrape or sample interval.",
                     [target(f"sum(max by (id) (grafanacloud_instance_active_series{{{mid}}}))", "active series", "A", USAGE),
                      target(f"60 * sum(max by (id) (grafanacloud_instance_samples_per_second{{{mid}}})) / max(grafanacloud_org_metrics_included_dpm_per_series)",
                             "series implied by DPM", "B", USAGE)],
                     0, y, 12, 8, ds=USAGE,
                     overrides=[{"matcher": {"id": "byName", "options": "series implied by DPM"},
                                 "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]))
    panels.append(ts("DPM per active series",
                     "Average data points per minute per series against the included DPM.",
                     [target(f"60 * sum(max by (id) (grafanacloud_instance_samples_per_second{{{mid}}})) / sum(max by (id) (grafanacloud_instance_active_series{{{mid}}}))", "DPM per series", "A", USAGE),
                      target("max(grafanacloud_org_metrics_included_dpm_per_series)", "included", "B", USAGE)],
                     12, y, 12, 8, ds=USAGE, decimals=2,
                     overrides=[{"matcher": {"id": "byName", "options": "included"},
                                 "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                                                {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]}]))
    y += 8

    panels.append(row("OpenJTS series (this stack's metrics datasource, job=openjts)", y)); y += 1
    panels.append(stat("OpenJTS active series", "Series with job=openjts seen in the last 5 minutes (instant query lookback), "
                       "so a little under the 20 minute billing window. Includes the collector's own health metrics.",
                       [target(f"count({{{JOB}}})", instant=True)], 0, y, 6, 4))
    panels.append(stat("Devices reporting", "Distinct device label on junos_device_info.",
                       [target(f"count(group by (device) (junos_device_info{{{JOB}}}))", instant=True)], 6, y, 6, 4))
    panels.append(stat("Series per device", "OpenJTS series divided by devices. Multiply by your device count before adding a profile.",
                       [target(f"count({{{JOB}}}) / count(group by (device) (junos_device_info{{{JOB}}}))", instant=True)],
                       12, y, 6, 4, decimals=0))
    panels.append(stat("Series from the collector itself", "openjts_internal_* from Telegraf. Alloy's own series are job=integrations/self and not counted here.",
                       [target(f'count({{{JOB}, __name__=~"openjts_internal_.+"}})', instant=True)], 18, y, 6, 4))
    y += 4
    panels.append(ts("OpenJTS active series over time", "A step up after a profile or router change is the cost of that change.",
                     [target(f"count({{{JOB}}})", "series")], 0, y, 12, 8))
    panels.append(ts("Active series by device", "Which routers carry the series. A router far above its peers usually has many more interfaces or optics lanes.",
                     [target(f'count by (device) ({{{JOB}, device!=""}})', "{{device}}")], 12, y, 12, 8))
    y += 8
    panels.append(bars("Active series by profile area",
                       "Second word of the metric name: interfaces, components, bgp, srmpls, twamp, and so on. This is the profile you would drop or thin to save series.",
                       f"sum by (area) (count by (__name__, area) ({AREA}))", "{{area}}", 0, y, 8, 12))
    panels.append(table("Active series by metric (top 25)", "Same view as Active Series by Metric on the Alloy device dashboards, for the whole OpenJTS job.",
                        f"topk(25, count by (__name__) ({{{JOB}}}))", 8, y, 16, 12, ["__name__"]))
    y += 12
    panels.append(table("Active series by device and profile area (top 25)", "Where the series come from, router by router.",
                        f"topk(25, count by (device, area) ({AREA}))", 0, y, 12, 10, ["device", "area"]))
    panels.append(table("Active series by interface (top 25)", "Interface labelled series per device. Subinterface units and VOQ queues multiply these fastest.",
                        f'topk(25, count by (device, if_name) ({{{JOB}, if_name!=""}}))', 12, y, 12, 10, ["device", "if_name"]))
    y += 10

    panels.append(row("Sample rate leaving the collector", y)); y += 1
    panels.append(ts("Data points per minute sent to Grafana Cloud",
                     "Alloy's exporter counter, all of it (junos_* plus the allowlisted Alloy and Telegraf health series). "
                     "Source: otelcol_exporter_sent_metric_points_total, job=integrations/self.",
                     [target(f"sum(rate(otelcol_exporter_sent_metric_points_total{{{ALLOY}}}[5m])) * 60", "sent per minute")],
                     0, y, 12, 8))
    panels.append(stat("DPM per OpenJTS series (collector side)",
                       "Points per minute leaving Alloy divided by OpenJTS series. At the default 60s floor this sits near 1. "
                       "Read it as an estimate: Alloy's own few series are in the numerator.",
                       [target(f"sum(rate(otelcol_exporter_sent_metric_points_total{{{ALLOY}}}[5m])) * 60 / count({{{JOB}}})", instant=True)],
                       12, y, 6, 8, decimals=2,
                       thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.05}, {"color": "red", "value": 1.5}]))
    panels.append(stat("Refused or failed points per minute",
                       "Receiver refusals plus exporter send and enqueue failures. Anything above zero is data you are not getting.",
                       [target(f"(sum(rate(otelcol_receiver_refused_metric_points_total{{{ALLOY}}}[5m])) or vector(0)) * 60"
                               f" + (sum(rate(otelcol_exporter_send_failed_metric_points_total{{{ALLOY}}}[5m])) or vector(0)) * 60"
                               f" + (sum(rate(otelcol_exporter_enqueue_failed_metric_points_total{{{ALLOY}}}[5m])) or vector(0)) * 60", instant=True)],
                       18, y, 6, 8, decimals=1,
                       thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]))
    y += 8
    panels.append(ts("Telegraf points written per minute", "openjts_internal_write_metrics_written and the agent counters, rate of the cumulative values.",
                     [target(f"sum(rate(openjts_internal_write_metrics_written{{{JOB}}}[5m])) * 60", "written"),
                      target(f"sum(rate(openjts_internal_agent_metrics_gathered{{{JOB}}}[5m])) * 60", "gathered", "B")],
                     0, y, 12, 8))
    panels.append(ts("Telegraf dropped and rejected per minute", "Buffer overflow (dropped) or output refusals (rejected). Both should be flat at zero.",
                     [target(f"sum(rate(openjts_internal_write_metrics_dropped{{{JOB}}}[5m])) * 60", "dropped by output", "A"),
                      target(f"sum(rate(openjts_internal_write_metrics_rejected{{{JOB}}}[5m])) * 60", "rejected by output", "B"),
                      target(f"sum(rate(openjts_internal_agent_metrics_dropped{{{JOB}}}[5m])) * 60", "dropped by agent", "C")],
                     12, y, 12, 8))
    y += 8
    panels.append(ts("Seconds since each router last reported", "From junos_device_info, which is emitted at the sample floor. Above about 3 x min_sample_interval the router is not reporting.",
                     [target(f"time() - max by (device) (timestamp(junos_device_info{{{JOB}}}))", "{{device}}")],
                     0, y, 24, 7, unit="s"))
    y += 7

    templating = [
        {"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus", "current": {},
         "hide": 0, "refresh": 1, "regex": "", "options": []},
        {"name": "usage", "label": "Usage data source", "type": "datasource", "query": "prometheus", "current": {},
         "hide": 0, "refresh": 1, "regex": "/.*usage.*/", "options": []},
        {"name": "metrics_id", "label": "Metrics instance", "type": "query", "datasource": USAGE,
         "query": {"query": "label_values(grafanacloud_instance_info, id)", "refId": "metrics_id"},
         "definition": "label_values(grafanacloud_instance_info, id)", "refresh": 1, "includeAll": True, "multi": True,
         "allValue": ".+", "current": {}, "hide": 0, "options": [], "sort": 1},
    ]
    return {"uid": "openjts-cost", "title": "OpenJTS / Cost and cardinality", "tags": ["openjts", "junos", "cost"],
            "schemaVersion": 39, "version": 1, "editable": True, "graphTooltip": 2, "refresh": "1m",
            "time": {"from": "now-24h", "to": "now"}, "timezone": "browser",
            "annotations": {"list": []}, "links": [], "panels": panels, "templating": {"list": templating}}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dash = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    n = sum(len(p.get("targets") or []) for p in dash["panels"])
    print(f"{OUT.name:40} {n:3} targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
