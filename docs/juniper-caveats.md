# Juniper caveats

This fork has been built and tested without a Junos router. What is verified and what is not:

## Verified

- The rendered Telegraf config loads in stock Telegraf 1.36 for every profile and family
  (`telegraf config check` on `generate.py --all-profiles`, in `tests/test_cost.py`), with every
  sample interval at or above `min_sample_interval`.
- The processor chain, run in real Telegraf against line-protocol fixtures shaped like gNMI
  output (`tests/fixtures/pipeline.lp`), produces the expected series names, labels and types.
- Telegraf → Alloy → an OTLP receiver produces the expected Prometheus names, including `_total`.
- Every dashboard and alert expression parses in `promtool` and references only series the
  pipeline can export.

## Not verified

- **Field and tag keys from a live router.** The fixtures follow the paths in the profiles,
  with `canonical_field_names` and `prefix_tag_key_with_path`. Junos releases differ
  (OpenConfig vs native paths, `state/` vs no `state/`), and upstream notes older releases
  rename counters. Expect some series names to need a fix in the first week.
- **Juniper header extension.** `_component_id` / `_subcomponent_id` depend on Telegraf's
  `juniper_header` decoding. Profiles that key on them (NPU, VOQ, some health panels) are the
  most likely to come up empty.
- **`heartbeat_interval` on a router.** Telegraf sends it on `on_change` paths. Whether a Junos
  release honours it is untested; if not, BGP session state goes quiet and ages out ([cost.md](cost.md)).
- **Sample rates a router actually delivers.** The config asks for the floor. The cost dashboard's
  DPM panels show what arrives.
- **`junos_device_info`** comes from `inputs.mock`. The end-to-end test runs one mock for `edge1`,
  not the per-router set `generate.py` renders from your targets file.
- **Throughput at scale.** Batch sizes are Telegraf and Alloy defaults tuned up, not measured.

## First router checklist

1. `make up`, then `make logs`. Look for gNMI subscription errors per path.
2. In Grafana Explore:
   ```promql
   count by (__name__) ({job="openjts", device="<router>"})
   ```
   Compare with [metrics.md](metrics.md) for the profiles that router runs.
3. Open each dashboard for that router. Empty panels point at a name mismatch.
4. To see what Telegraf receives before `finalize.star`, add a temporary
   `[[outputs.file]]` with `data_format = "influx"` to `generated/telegraf/telegraf.conf`
   and restart. Fix the profile mapping in `scripts/`, not the generated file.
