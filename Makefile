PY      ?= python3
COMPOSE ?= docker compose -f compose.yaml -f generated/compose.credentials.yaml

.PHONY: help generate up down restart logs status dashboards alerts catalog content test test-pipeline test-dashboards e2e check push-dashboards push-alerts push

help:
	@echo "Collector"
	@echo "  generate          targets/routers.yaml -> generated/telegraf/telegraf.conf"
	@echo "  up / down         start or stop Telegraf + Alloy (runs generate first)"
	@echo "  logs / status     follow logs, show containers"
	@echo "Grafana content"
	@echo "  content           dashboards/, alerts/ and docs/metrics.md from design_profile/"
	@echo "  push              push dashboards and alert rules (GRAFANA_URL / GRAFANA_TOKEN in .env)"
	@echo "Checks"
	@echo "  test              processor chain in Telegraf + PromQL parse/name checks"
	@echo "  e2e               Telegraf -> Alloy -> Prometheus OTLP receiver, check series names"
	@echo "  check             content is up to date with design_profile/ (CI)"

generate:
	$(PY) scripts/generate.py

up: generate
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart: generate
	$(COMPOSE) up -d --force-recreate

logs:
	$(COMPOSE) logs -f --tail 100

status:
	$(COMPOSE) ps

dashboards:
	$(PY) scripts/convert_dashboards.py

alerts:
	$(PY) scripts/build_alerts.py

catalog:
	$(PY) scripts/metrics_catalog.py

content: dashboards alerts catalog

test: test-pipeline test-dashboards

test-pipeline:
	$(PY) tests/test_pipeline.py

test-dashboards:
	$(PY) tests/test_dashboards.py

e2e:
	$(PY) tests/test_otlp_e2e.py

check: content
	git diff --exit-code -- dashboards alerts docs/metrics.md

push-dashboards:
	$(PY) scripts/push_grafana.py dashboards

push-alerts:
	$(PY) scripts/push_grafana.py alerts

push:
	$(PY) scripts/push_grafana.py all
