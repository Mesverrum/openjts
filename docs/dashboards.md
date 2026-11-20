# Dashboard conversion

`scripts/convert_dashboards.py` reads the upstream dashboards that ship with each profile
(`design_profile/<profile>/dashboard-*.json`), rewrites every InfluxQL query to PromQL and
writes `dashboards/openjts-<name>.json`. Run `make dashboards`; `make check` fails if the
committed output is stale.

The output is plain dashboard JSON with a `datasource` variable, so it imports into any
Grafana with a Prometheus datasource. UIDs are `openjts-<name>` and links between dashboards
are remapped to them.

## Query rules

- Raw and builder-mode InfluxQL are both handled. Builder targets are rendered to InfluxQL first.
- `measurement` + `field` → `junos_<measurement>_<field>`, `_total` on counters.
- A `*_rate` field becomes `rate(x_total[$__rate_interval])`.
- Aggregates become series aggregation over the `GROUP BY` tags:
  `mean`→`avg`, `last`/`first`→`max`, `sum`/`count`→`sum`, `min`, `max`.
- Tables, and stat/gauge/bar/pie panels without `GROUP BY time()`, are instant queries over
  the dashboard range: `avg_over_time(...[$__range])` and friends. Arithmetic across fields
  uses subqueries (`[$__range:]`).
- `WHERE` with `AND`/`OR`, regex and comparisons becomes label matchers. A comparison on a
  field becomes a PromQL filter (`> 0`).
- One level of subquery (`SELECT ... FROM (SELECT ...)`) is supported.
- A regex that is only a variable (`=~ /^$rtr$/`) is an exact multi-value variable match.
- String fields match on the `value` label. Stat panels showing text use the series name.

## Tags

| Influx tag | Label |
|---|---|
| `SHORTNAME` | `device` |
| `LINKNAME` | `if_name` on interface measurements, `port_id` on component and Ethernet measurements |
| `_component_id` | `component_id` |
| `MODEL`, `VERSION`, `FAMILY` | joined from `junos_device_info` |
| `HAS_CHANNEL` | `port_id=~".*:.*"` |
| `OPTIC_DESC` | joined from `junos_components_optic_model` |

## Tables

Tables get `merge` and `organize` transforms that rename `Value #A` back to the Influx
column alias and Prometheus labels back to the upstream column names, then the original
table transforms run. Boomtable panels are standard tables; graph panels are time series.

## Rewritten by hand

These panels had no direct PromQL equivalent. The converter replaces them:

| Panel | Upstream | Now |
|---|---|---|
| Router Info / Chassis Info | NETCONF enrichment tags | `junos_device_info` table |
| Chassis Alarms | NETCONF poll | text note (see [migration.md](migration.md)) |
| ALARMING panels | `ALARMING` measurement written by Telegraf | the probe expressions, labelled `alarm_type` |
| Power, Power aggregation | Kapacitor-summed series | `sum by (device)` of `junos_components_power_(system\|zone_0\|zone_1)_usage` |
| Watts per Gbps | Kapacitor | power ÷ `rate(junos_interfaces_in_octets_total)` |
| In/Out SR-MPLS ratio | Influx math across measurements | SR octets ÷ interface octets per `if_name` |

## Tests

`make test-dashboards` collects every expression from the dashboards, variables and alert
rules, checks each `junos_*` name against what `finalize.star` can export, and runs them all
through `promtool` for syntax.
