"""Upload the Order Tracker dashboard and 5xx alert to a Grafana Cloud stack.

Reuses the local dashboard JSON and alert provisioning file, pointing them at the
stack's own Prometheus, Loki, and Tempo data sources. Grafana Cloud stores the
OTel `service.name` resource attribute as the `job` label on metrics, so the
Prometheus queries filter on `job` instead of `service_name`.

    GRAFANA_URL=https://<stack>.grafana.net GRAFANA_SA_TOKEN=... \
        uv run --with pyyaml python observability/grafana_cloud_setup.py

Set RESPONDER_WEBHOOK_URL to also send alert notifications to the incident
responder (it must be reachable from the internet, for example through a tunnel).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
GRAFANA_URL = os.environ["GRAFANA_URL"].rstrip("/")
TOKEN = os.environ["GRAFANA_SA_TOKEN"]
WEBHOOK_URL = os.getenv("RESPONDER_WEBHOOK_URL")
FOLDER_UID = "order-tracker"
LOCAL_SERVICE_FILTER = 'service_name="order-tracker"'
CLOUD_SERVICE_FILTER = 'job="order-tracker"'


def api(method, path, body=None, ok=(200, 201, 202, 204)):
    request = urllib.request.Request(
        f"{GRAFANA_URL}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            # keep provisioned resources editable in the Grafana UI
            "X-Disable-Provenance": "true",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        if err.code in ok:
            return err.code, None
        return err.code, text


def find_datasources():
    status, sources = api("GET", "/api/datasources")
    if status != 200:
        sys.exit(f"Could not list data sources ({status}): {sources}")
    found = {}
    # the stack's own data sources (not ML metrics, usage, or alert state history)
    suffixes = {"prometheus": "prom", "loki": "logs", "tempo": "traces"}
    for kind, suffix in suffixes.items():
        matches = [s for s in sources if s["type"] == kind]
        matches.sort(key=lambda s: (
            s["uid"] != f"grafanacloud-{suffix}",
            not (s["name"].startswith("grafanacloud-") and s["name"].endswith(f"-{suffix}")),
            s["name"],
        ))
        if not matches:
            sys.exit(f"No {kind} data source found in {GRAFANA_URL}")
        found[kind] = matches[0]["uid"]
        print(f"{kind}: {matches[0]['name']} ({matches[0]['uid']})")
    return found


def retarget(node, uids):
    """Point datasource references at the stack's uids and fix Prometheus label filters."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "datasource" and isinstance(value, dict) and value.get("type") in uids:
                out[key] = {**value, "uid": uids[value["type"]]}
            elif key == "datasourceUid" and value in uids:
                out[key] = uids[value]
            else:
                out[key] = retarget(value, uids)
        if out.get("datasource", {}).get("type") == "prometheus" or out.get("datasourceUid") == uids["prometheus"]:
            out = fix_prometheus(out)
        return out
    if isinstance(node, list):
        return [retarget(item, uids) for item in node]
    return node


def fix_prometheus(node):
    text = json.dumps(node).replace(LOCAL_SERVICE_FILTER.replace('"', '\\"'), CLOUD_SERVICE_FILTER.replace('"', '\\"'))
    return json.loads(text)


def ensure_folder():
    status, body = api("GET", f"/api/folders/{FOLDER_UID}")
    if status == 200:
        return
    status, body = api("POST", "/api/folders", {"uid": FOLDER_UID, "title": "Order Tracker"})
    print(f"folder: {status}")
    if status != 200:
        sys.exit(f"Could not create folder ({status}): {body}")


def upload_dashboard(uids):
    dashboard = json.loads((HERE / "grafana/dashboards/order-tracker.json").read_text())
    dashboard = retarget(dashboard, uids)
    dashboard.pop("id", None)
    status, body = api("POST", "/api/dashboards/db", {"dashboard": dashboard, "folderUid": FOLDER_UID, "overwrite": True})
    if status != 200:
        sys.exit(f"Dashboard upload failed ({status}): {body}")
    print(f"dashboard: {GRAFANA_URL}{body['url']}")
    return f"{GRAFANA_URL}{body['url']}"


def upload_alert(uids, dashboard_url):
    config = yaml.safe_load((HERE / "grafana/provisioning/alerting/order-tracker-alerts.yaml").read_text())
    group = config["groups"][0]
    rules = []
    for rule in group["rules"]:
        rule = retarget(rule, uids)
        rule["annotations"]["dashboard_url"] = dashboard_url
        rule.update({"folderUID": FOLDER_UID, "ruleGroup": group["name"], "orgID": 1})
        if WEBHOOK_URL:
            rule["notification_settings"] = {"receiver": "incident-responder"}
        rules.append(rule)

    if WEBHOOK_URL:
        contact = {"uid": "incident-responder-webhook", "name": "incident-responder", "type": "webhook",
                   "settings": {"url": WEBHOOK_URL, "httpMethod": "POST"}}
        status, body = api("PUT", f"/api/v1/provisioning/contact-points/{contact['uid']}", contact)
        if status not in (200, 202):
            status, body = api("POST", "/api/v1/provisioning/contact-points", contact)
        print(f"contact point: {status}")

    for rule in rules:
        status, body = api("POST", "/api/v1/provisioning/alert-rules", rule)
        if status == 409 or "already exists" in str(body):
            status, body = api("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", rule)
        if status not in (200, 201):
            sys.exit(f"Alert rule upload failed ({status}): {body}")
        print(f"alert rule: {rule['title']} ({status})")

    interval = int(str(group["interval"]).rstrip("s"))
    status, body = api("GET", f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{group['name']}")
    if status == 200:
        body["interval"] = interval
        status, body = api("PUT", f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{group['name']}", body)
        print(f"rule group interval {interval}s: {status}")


def main():
    uids = find_datasources()
    ensure_folder()
    dashboard_url = upload_dashboard(uids)
    upload_alert(uids, dashboard_url)


if __name__ == "__main__":
    main()
