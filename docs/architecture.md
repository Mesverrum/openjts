# Architecture

```
targets/routers.yaml ─┐
design_profile/ ──────┴─▶ scripts/generate.py ─▶ generated/telegraf/telegraf.conf + openjts.star

routers ──gNMI──▶ Telegraf ──OTLP gRPC :4317──▶ Alloy ──OTLP/HTTP──▶ Grafana Cloud OTLP gateway ─▶ Mimir

design_profile/ ─▶ convert_dashboards.py ─▶ dashboards/*.json ─┐
                ─▶ build_alerts.py ───────▶ alerts/openjts.json ┴─▶ push_grafana.py ─▶ Grafana Cloud
```

Two containers run: stock `telegraf` and stock `grafana/alloy`. The Telegraf config is
rendered from the profiles, so there is no orchestrator at runtime.

## Telegraf

One `[[inputs.gnmi]]` per router, with:

- the subscriptions from the selected profile variants (`gnmi_inputs` in each profile JSON),
- `vendor_specific = ["juniper_header"]` so the Junos header extension is decoded into the
  `_component_id` / `_subcomponent_id` fields the profiles key on,
- `canonical_field_names` and `prefix_tag_key_with_path`, which give the full-path field and
  tag keys the profile processors expect,
- a `tag_subscription` on `/interfaces/interface/state/description` that attaches the
  interface description to every interface-keyed series,
- static `device` tag (router name) and an internal `jts_pipeline` tag.

Routers that run the same set of profile variants share a pipeline. Each pipeline's
processors are the profile's processor lists translated to stock Telegraf processors
(clone, pivot, rename, strings, regex, the xreducer equivalents, filter, converter),
in the order jtso applied them, scoped with `tagpass jts_pipeline`. `enum_list` is
skipped; see [migration.md](migration.md).

A `[[inputs.mock]]` per router emits `junos_device_info` with `family`, `version`, `model`,
`site` and `profiles` labels from the targets file. This replaces jtso's enrichment tags.
Dashboards join on it instead of carrying those tags on every series.

`[[inputs.internal]]` reports Telegraf's own health as `openjts_internal_*`, cut to the agent and
output counters. Every sample interval, the heartbeat on `on_change` paths and the mock and
internal inputs run at `min_sample_interval` (60s default) so no series costs more than 1 DPM.
See [cost.md](cost.md).

## finalize.star

The last processor. It turns Influx-shaped points (one measurement, many fields) into
Prometheus-shaped series:

- One series per field the profile's Influx output kept, named
  `junos_<measurement>_<field>`, lowercased, `-` and `.` replaced with `_`. A leading
  `JUNOS_` on the measurement is not doubled.
- Fields jtso rated (`rate_list`) are sent as cumulative counters. Grafana Cloud appends
  `_total`. Query them with `rate()`.
- Everything else is a gauge.
- String fields become a gauge of `1` with the text in a `value` label (state, vendor,
  model). Numeric strings are parsed as numbers.
- Path-style tag keys are shortened to their last two elements
  (`/interfaces/interface/@name` → `interface_name`). `port_id` is derived from `if_name`
  on component and Ethernet series when the profile did not set it.
- `path`, `source`, `host` and `jts_pipeline` are dropped.

Measurements and fields not in the profiles are dropped here, so the series count tracks
what the dashboards use.

## Alloy

`alloy/config.alloy`: OTLP receiver, memory limiter, batch, OTLP/HTTP exporter with basic
auth from `GC_OTLP_*`. Alloy also ships its own health as `job="integrations/self"`, filtered to
nine metric names (the exporter has 130+ families, each a billed series). The UI is on
`http://localhost:12345`.

Alloy is there so the Grafana Cloud credentials, batching and retries live in one place,
and so other signals (syslog, SNMP) can be added later without touching Telegraf.

## Labels

| Label | From |
|---|---|
| `job` | `openjts` (OTLP `service.name`) |
| `instance` | `collector` in `targets/routers.yaml` (OTLP `service.instance.id`) |
| `device` | router `name` |
| `if_name`, `if_unit`, `if_description`, `port_id`, `component_id`, ... | profile tags |
| `value` | text of a string field |

## Rates and probes

jtso computed rates in Telegraf and stored them as `*_rate` fields. This fork keeps the raw
counter and dashboards use `rate(x_total[$__rate_interval])`, which handles counter resets
and scrape gaps.

jtso's `monitoring_list` probes (thresholds evaluated in Telegraf, results written back as
`ALARMING` points) are Grafana alert rules here. See `alerts/openjts.json`.
