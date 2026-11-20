# Coming from OpenJTS

If you run upstream OpenJTS today, this is what changes.

## Kept

- `design_profile/` exactly as upstream. Profile fixes from upstream can be merged straight in,
  then `make content` regenerates dashboards, alerts and the metrics catalog.
- The profile selection logic (family, version rules, first match wins).
- All 15 dashboards, same layout and panels, queries rewritten in PromQL.
- The probe thresholds, as Grafana alert rules.

## Replaced

| OpenJTS | This fork | Why |
|---|---|---|
| `jts_telegraf` (custom Telegraf build) | stock `telegraf:1.36` | Stock Telegraf decodes the Juniper gNMI header and covers the processors the profiles use. One less image to maintain and patch. |
| jtso (web UI, SQLite, config generator) | `targets/routers.yaml` + `make generate` | The router list is a file you can review and version. Nothing to back up. |
| InfluxDB | Grafana Cloud metrics (Mimir) | |
| Grafana, Chronograf (local) | Grafana Cloud | |
| `influx_outputs` | `outputs.opentelemetry` → Alloy → OTLP | Grafana Cloud's native ingest path. |
| enrichment (jtso-supplied interface description, model, version, family tags) | gNMI `tag_subscription` for the description, `junos_device_info` from the targets file for the rest | Description stays live from the router. The rest are static facts you know when you add a router, joined in PromQL. |
| `rate_list` (`*_rate` fields) | raw counters + `rate()` | Prometheus rates handle resets and gaps; one series instead of two. |
| `monitoring_list` + `ALARMING` measurement | Grafana alert rules (`alerts/openjts.json`) | Alerting with routing, silences and history instead of a measurement on a dashboard. |
| Power dashboards' Kapacitor aggregation | PromQL `sum by (device)` over the power series | |

## Dropped

- **Chassis alarms over NETCONF.** jtso polled them separately from gNMI. The panel is now a
  note. If you need them, Junos can send them as syslog, which Alloy can forward to Loki.
- **`enum_list` queue name lookups.** They map queue numbers to names that are specific to
  each network's CoS config. Series keep `queue_id`; add a Grafana value mapping if you want names.
- **The Flowcharting panel plugin and committed `grafana.db`.** Not needed in Grafana Cloud.
- **The MkDocs site and release workflow.** This repo's docs are the Markdown in `docs/`.

## Queries

Influx measurement plus field becomes one series name:

| InfluxQL | PromQL |
|---|---|
| `SELECT mean("in-octets_rate") FROM "INTERFACES" WHERE device = 'r1' GROUP BY "if_name"` | `avg by (if_name) (rate(junos_interfaces_in_octets_total{device="r1"}[$__rate_interval]))` |
| `SELECT last("state") FROM "NETWORK_INSTANCE_BGP"` | `junos_network_instance_bgp_session_state` (text in `value`) |
| `WHERE "MODEL" =~ /MX/` | `and on(device) junos_device_info{model=~".*MX.*"}` |

Tag renames: `SHORTNAME` → `device`, `LINKNAME` → `if_name` or `port_id`. The full rules are in
[dashboards.md](dashboards.md).
