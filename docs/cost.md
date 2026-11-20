# Cost: series, DPM and what this fork does about them

## How Grafana Cloud bills this

Metrics are billed on whichever is higher, **active series** or **total DPM divided by the DPM
included per series**, at the 95th percentile. Pro includes 1 DPM per series. A series is active
if it received a sample in the last 20 minutes. OTLP series cost the same as any other series.
Source: [Grafana Cloud metrics invoice](https://grafana.com/docs/grafana-cloud/cost-management-and-billing/understand-your-invoice/metrics-invoice/).

Two things follow:

- A series sampled every 30s is 2 DPM, so it bills as 2 series. Halving a sample interval doubles
  the cost of the same data, and nothing on a dashboard looks different.
- Cardinality (devices x interfaces x metrics) sets the floor. DPM only matters once it is over 1.

## Where the DPM would come from

The upstream profiles ask for more than 1 point per minute on some paths. Across every profile
variant in `design_profile/`, rendered for every family, there are 165 `sample` subscriptions and 9
`on_change` ones. 63 of the sample subscriptions ask for 30s (optic, twamp, srmpls, traffic and
health), 102 ask for 60s. A router running the optic or twamp profiles would have billed 2 DPM on those
series. The on_change subscriptions (BGP session state) send nothing in steady state, which has its
own problem: see the heartbeat below.

`python3 scripts/generate.py --all-profiles` renders that config and `tests/test_cost.py` checks it.

## What the collector does

| Setting | Where | Effect |
|---|---|---|
| `min_sample_interval` (default 60, allowed 1 to 300) | `targets/routers.yaml` | Every gNMI `sample_interval` is raised to at least this. `make generate` prints how many subscriptions it raised per router. |
| `heartbeat_interval` on `on_change` paths | same value | Re-sends the current value at the floor, so BGP session state does not go quiet, age out after 20 minutes and leave holes. Steady state is the same 1 point per minute as everything else. |
| `[agent] interval` | same value | Anything that polls rather than subscribes is held to the floor. |
| `inputs.mock` (`junos_device_info`) | same value | One series per router, at the floor. |
| `inputs.internal` | same value | Telegraf's own health, cut to `internal_agent` and `internal_write` (16 series for the whole collector: 6 agent, 10 output). `internal_gather` is one set per input plugin and `collect_memstats` is another 20 series, so both are off. `namepass` is matched against the name before `name_prefix` is added; `tests/test_cost.py` runs that in real Telegraf. |
| Alloy self metrics | `alloy/config.alloy` | `prometheus.exporter.self` has 130+ metric families and each one is a billed series. A `prometheus.relabel` keeps nine metric names: `up`, `alloy_build_info`, the exporter sent, failed and queue counters, and the receiver accepted and refused counters. Scraped every 60s. `target_info` is added by the OTLP translation, not by Alloy. |

Nothing here drops a series the dashboards read. It lowers the sample rate, and for collector
health, the number of series.

### Changing the floor

Lower it (30, or less) only for a short investigation and put it back. Raising it above 60 does
not lower the bill: under 1 DPM per series the billable number is the active series count, which a
slower sample rate does not change.

After changing it, run `make restart`. Panels that use `rate()` need two samples inside the
window. This has not been checked on a live stack at 60s, so confirm the rate panels on the first push.

### What this does not solve

- **Cardinality.** At 1 DPM the bill is the number of active series. The cost dashboard shows
  where they come from. Cutting them means dropping a profile from a router, or a path from a
  profile.
- **Router support for `heartbeat_interval`.** Telegraf sends it; whether a given Junos release
  honours it has not been checked on a router. If it ignores it, on_change series go quiet and
  age out. That lowers the bill and leaves gaps in the BGP session panels.
- **Routers that sample slower than asked.** If a router rejects or rounds a `sample_interval`,
  the DPM panels will show it. Compare the DPM panel with the floor.

## The cost dashboard

`dashboards/openjts-cost.json` (`OpenJTS / Cost and cardinality`), built by
`scripts/build_cost_dashboard.py`. It follows the active series and DPM panels in the Grafana Cloud
Billing/Usage dashboard and the Active Series by Metric table on the Alloy device dashboards.

| Row | Datasource | Panels |
|---|---|---|
| Stack billing | `grafanacloud-usage` (the `usage` variable) | Active series, DPM per series, included DPM, billable and included series. Active series against the series implied by DPM: when the dashed line is higher you are paying for sample rate. |
| OpenJTS series | your metrics datasource, `job="openjts"` | Active series now and over time, series per device, active series by profile area, by metric, by device and area, by interface. |
| Sample rate | same, plus Alloy's `job="integrations/self"` | Points per minute leaving Alloy, DPM per OpenJTS series, refused and failed points, Telegraf written, dropped and rejected, seconds since each router reported. |

The stack row covers the whole stack, not only OpenJTS. The OpenJTS rows count series with
`job="openjts"` using an instant query, which looks back 5 minutes, so it reads a little under the
20 minute window billing uses.

Every query in the OpenJTS and sample rate rows runs against a real Prometheus in
`tests/test_otlp_e2e.py` and returns data (rates excepted, they need two scrapes). The stack row
was run against a live usage datasource while building it.

## Reading the first week

1. **DPM per active series** on the Stack row. Over 1.05 on a stack that only runs OpenJTS means
   something is sampling faster than the floor.
2. **Active series by profile area.** The largest bar is the profile to look at first.
3. **Active series by interface.** Subinterface units and VOQ queues multiply fastest.
4. **Series per device** times the number of routers you plan to add.
