#!/usr/bin/env python3
"""Push the converted dashboards and alert rules to a Grafana Cloud stack.

Reads GRAFANA_URL, GRAFANA_TOKEN (service account token: dashboards:write,
folders:write, alert.provisioning:write), GRAFANA_FOLDER and GRAFANA_PROM_UID
from the environment or .env.

  python3 scripts/push_grafana.py dashboards
  python3 scripts/push_grafana.py alerts
  python3 scripts/push_grafana.py all --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDER_UID = "openjts"


def load_env() -> None:
    path = REPO / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if m and not line.lstrip().startswith("#"):
            os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


class Grafana:
    def __init__(self, url: str, token: str, dry_run: bool):
        self.url, self.token, self.dry_run = url.rstrip("/"), token, dry_run

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        if self.dry_run and method != "GET":
            print(f"  [dry-run] {method} {path}")
            return {}
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise SystemExit(f"{method} {path}: HTTP {exc.code} {detail}") from None

    def ensure_folder(self, title: str) -> str:
        try:
            self.call("GET", f"/api/folders/{FOLDER_UID}")
            return FOLDER_UID
        except SystemExit as exc:
            if "HTTP 404" not in str(exc):
                raise
        self.call("POST", "/api/folders", {"uid": FOLDER_UID, "title": title})
        print(f"created folder {title!r} ({FOLDER_UID})")
        return FOLDER_UID


def push_dashboards(g: Grafana, folder: str, prom_uid: str) -> None:
    files = sorted((REPO / "dashboards").glob("*.json"))
    if not files:
        raise SystemExit("dashboards/ is empty; run scripts/convert_dashboards.py")
    for f in files:
        dash = json.loads(f.read_text(encoding="utf-8"))
        for v in dash["templating"]["list"]:
            if v["name"] == "datasource":
                v["current"] = {"value": prom_uid}
        g.call("POST", "/api/dashboards/db", {"dashboard": dash, "folderUid": folder, "overwrite": True,
                                              "message": "openjts push_grafana.py"})
        print(f"  dashboard {dash['uid']}: {dash['title']}")


def push_alerts(g: Grafana, folder: str, prom_uid: str) -> None:
    path = REPO / "alerts" / "openjts.json"
    if not path.is_file():
        raise SystemExit("alerts/openjts.json missing; run scripts/build_alerts.py")
    group = json.loads(path.read_text(encoding="utf-8"))
    for rule in group["rules"]:
        rule["folderUID"] = folder
        rule["ruleGroup"] = group["title"]
        for q in rule["data"]:
            if q["datasourceUid"] != "__expr__":
                q["datasourceUid"] = prom_uid
    # X-Disable-Provenance keeps the rules editable in the Grafana UI.
    g.call("PUT", f"/api/v1/provisioning/folder/{folder}/rule-groups/{group['title']}", group,
           headers={"X-Disable-Provenance": "true"})
    print(f"  rule group {group['title']}: {len(group['rules'])} rules")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["dashboards", "alerts", "all"])
    ap.add_argument("--dry-run", action="store_true", help="read-only: check the folder, print the writes")
    args = ap.parse_args()
    load_env()
    url, token = os.environ.get("GRAFANA_URL", ""), os.environ.get("GRAFANA_TOKEN", "")
    if not url or not token or "<" in url + token:
        raise SystemExit("set GRAFANA_URL and GRAFANA_TOKEN in .env (see .env.sample)")
    prom_uid = os.environ.get("GRAFANA_PROM_UID") or "grafanacloud-prom"
    g = Grafana(url, token, args.dry_run)
    print(f"{url} -> folder {os.environ.get('GRAFANA_FOLDER') or 'OpenJTS'}, datasource {prom_uid}")
    folder = FOLDER_UID if args.dry_run else g.ensure_folder(os.environ.get("GRAFANA_FOLDER") or "OpenJTS")
    if args.what in ("dashboards", "all"):
        push_dashboards(g, folder, prom_uid)
    if args.what in ("alerts", "all"):
        push_alerts(g, folder, prom_uid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
