# OpenJTS for Grafana Cloud

Juniper gNMI telemetry into Grafana Cloud, using the profiles and dashboards from
[door7302/openjts](https://github.com/door7302/openjts).

Upstream OpenJTS runs a full local stack: a custom Telegraf build, jtso (the orchestrator),
InfluxDB, Kapacitor, Chronograf and Grafana. This fork keeps the part that took real Junos
knowledge to build, the per-family telemetry profiles and the 15 dashboards, and swaps
everything else for stock components that ship to Grafana Cloud:

```
Junos routers ──gNMI──▶ Telegraf (stock) ──OTLP──▶ Alloy ──OTLP/HTTP──▶ Grafana Cloud
                         profiles as config                              Prometheus (Mimir)
                         processors                                      dashboards, alert rules
```

Nothing runs locally except two containers. No InfluxDB, no Grafana, no orchestrator.

> **Status:** the metric names and every dashboard query are derived from the profiles and
> tested against a real Telegraf and Alloy with synthetic input, not against a live router.
> Read [docs/juniper-caveats.md](docs/juniper-caveats.md) before rolling this out.

## Quick start

You need Docker with Compose, Python 3.10+, `make`, and gNMI enabled on the routers
(`set system services extension-service request-response grpc`, port 9339 by default).

```bash
pip install -r requirements.txt
cp .env.sample .env                              # Grafana Cloud OTLP + router credentials
cp targets/routers.yaml.sample targets/routers.yaml
# put CA / client certs in certs/ if the routers use TLS

make up        # renders generated/telegraf/telegraf.conf, starts Telegraf + Alloy
make logs
```

In Grafana Cloud, check data is arriving:

```promql
count by (device) (junos_device_info)
```

Then load the dashboards and alert rules into the stack. Create a service account token
with dashboard, folder and alert rule write access and set `GRAFANA_URL` / `GRAFANA_TOKEN`
in `.env`:

```bash
python3 scripts/push_grafana.py all --dry-run    # shows what would change
make push
```

Everything lands in an **OpenJTS** folder. You can also import `dashboards/*.json` by hand
and pick your Prometheus datasource from the `datasource` variable.

### Where the values come from

| `.env` | Where to find it |
|---|---|
| `GC_OTLP_URL`, `GC_OTLP_ACCOUNT`, `GC_OTLP_KEY` | Grafana Cloud portal, your stack, **Connections → OpenTelemetry**. Token needs `metrics:write`. |
| `JUNOS_USERNAME`, `JUNOS_PASSWORD` | Your gNMI user. Routers can point at other variable pairs with `username_env` / `password_env`. |
| `GRAFANA_URL`, `GRAFANA_TOKEN` | Stack URL and a service account token. Only for `make push`. |
| `GRAFANA_PROM_UID` | Prometheus datasource UID for alert rules. `grafanacloud-prom` on most stacks. |

## Routers and profiles

`targets/routers.yaml` replaces the jtso web UI. One entry per router with `family`,
`version` and the profiles it should run:

```yaml
routers:
  - name: edge1-par
    address: 192.0.2.10
    family: mx
    version: "23.4R2"
    site: par
    profiles: [health, traffic, bgp]
```

`make generate` picks the profile variant for each router from
`design_profile/<profile>/definition.json` the same way jtso did, and prints anything it
skipped because a profile has no variant for that family or release. Change the file,
then `make restart`.

Profiles: `bgp`, `ddos`, `filter`, `health`, `optic`, `power`, `srmpls`, `traffic`, `twamp`.
Families: `mx`, `ptx`, `acx`, `qfx`, `ex`, `crpd`, `vmx`, `vjunos`, `vevo`.

## What's in the repo

| Path | What it is |
|---|---|
| `design_profile/` | Upstream profiles, unchanged. The source for everything else. |
| `scripts/generate.py` | Renders the Telegraf config from `targets/routers.yaml` and the profiles. |
| `scripts/finalize.star` | Last Telegraf processor. Turns Influx-shaped fields into Prometheus series. |
| `alloy/config.alloy` | OTLP in, batch, OTLP/HTTP out to Grafana Cloud. |
| `dashboards/` | The 15 upstream dashboards, converted to PromQL. Generated, committed. |
| `alerts/openjts.json` | Grafana alert rules from the profiles' probes. Generated, committed. |
| `docs/` | Architecture, migration notes, metrics catalog, caveats. |
| `tests/` | Processor chain in real Telegraf, PromQL checks with promtool, OTLP end to end. |

## Make targets

```
make up | down | restart | logs | status
make content           regenerate dashboards/, alerts/, docs/metrics.md from design_profile/
make test              processor chain + every PromQL expression (needs Docker)
make e2e               Telegraf -> Alloy -> Prometheus OTLP receiver, check names
make check             content is in sync with design_profile/ (what CI runs)
make push              push dashboards and alerts to GRAFANA_URL
```

Tests run Telegraf, Alloy and promtool in Docker. From a Windows checkout with the engine in
WSL, set `OPENJTS_DOCKER=wsl`.

## Docs

- [Architecture](docs/architecture.md): the pipeline, naming rules, labels.
- [Coming from OpenJTS](docs/migration.md): what changed, what was dropped and why.
- [Metrics catalog](docs/metrics.md): every series the profiles can produce.
- [Dashboard conversion](docs/dashboards.md): how InfluxQL became PromQL, and the panels that were rewritten by hand.
- [Juniper caveats](docs/juniper-caveats.md): what has not been checked on a router.

## Credit and license

The profiles, dashboards and the original idea come from
[OpenJTS](https://github.com/door7302/openjts) by David Roy and contributors, BSD 3-Clause
(Copyright NetSampler). This fork keeps that [license](LICENSE). The Grafana Cloud
plumbing, converter and tests are new.
