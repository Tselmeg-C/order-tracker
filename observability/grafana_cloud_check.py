"""Check that Order Tracker telemetry and the 5xx alert work in Grafana Cloud.

Run after grafana_cloud_setup.py, with the app sending telemetry to the stack:

    python observability/grafana_cloud_check.py before-errors   # 200/404 visible, alert Normal
    python observability/grafana_cloud_check.py after-errors    # 500 visible, alert Firing
"""

import json
import sys
import time
import urllib.parse

from grafana_cloud_setup import api, find_datasources


RULE_TITLE = "Order Tracker 5xx responses"
TIMEOUT_SECONDS = 300


def proxy(uid, path, **params):
    status, body = api("GET", f"/api/datasources/proxy/uid/{uid}{path}?{urllib.parse.urlencode(params)}")
    if status != 200:
        raise RuntimeError(f"{path} returned {status}: {str(body)[:300]}")
    return body


def status_codes(prom_uid):
    body = proxy(prom_uid, "/api/v1/query",
                 query='sum by (http_response_status_code) (http_server_requests_total'
                       '{job="order-tracker", http_route="/api/orders/{order_id}"})')
    return {r["metric"].get("http_response_status_code"): r["value"][1] for r in body["data"]["result"]}


def log_lines(loki_uid, needle):
    now = time.time()
    body = proxy(loki_uid, "/loki/api/v1/query_range",
                 query=f'{{service_name="order-tracker"}} |= "{needle}"',
                 start=int((now - 3600) * 1e9), end=int(now * 1e9), limit=5)
    return [(s["stream"].get("severity_text"), s["stream"].get("trace_id"), v[1][:120])
            for s in body["data"]["result"] for v in s["values"]]


def traces(tempo_uid, path):
    now = int(time.time())
    body = proxy(tempo_uid, "/api/search",
                 q=f'{{ resource.service.name = "order-tracker" && span.url.path = "{path}" }}',
                 start=now - 3600, end=now, limit=5)
    return [(t["traceID"], t.get("rootTraceName")) for t in body.get("traces", [])]


def alert_state():
    status, body = api("GET", "/api/prometheus/grafana/api/v1/rules")
    if status != 200:
        raise RuntimeError(f"rules API returned {status}: {str(body)[:300]}")
    for group in body["data"]["groups"]:
        for rule in group["rules"]:
            if rule["name"] == RULE_TITLE:
                alerts = [(a.get("labels", {}).get("http_route"), a["state"]) for a in rule.get("alerts", [])]
                return rule["state"], rule.get("health"), alerts
    return "missing", None, []


def wait_for(description, check):
    deadline = time.time() + TIMEOUT_SECONDS
    last = None
    while time.time() < deadline:
        try:
            ok, last = check()
            if ok:
                print(f"PASS {description}: {json.dumps(last)}")
                return True
        except Exception as exc:
            last = repr(exc)
        time.sleep(15)
    print(f"FAIL {description}: {json.dumps(last, default=str)}")
    return False


def main(stage):
    uids = find_datasources()
    results = []
    if stage == "before-errors":
        results.append(wait_for("metric shows 200 and 404 for order lookups", lambda: (
            (codes := status_codes(uids["prometheus"])).keys() >= {"200", "404"}, codes)))
        results.append(wait_for("log for standard-1002 in Loki", lambda: (
            bool(lines := log_lines(uids["loki"], "standard-1002")), lines)))
        results.append(wait_for("trace for standard-1002 in Tempo", lambda: (
            bool(found := traces(uids["tempo"], "/api/orders/standard-1002")), found)))
        results.append(wait_for("alert is Normal (inactive) with no 5xx", lambda: (
            (state := alert_state())[0] == "inactive" and state[1] == "ok", state)))
    else:
        results.append(wait_for("metric shows 500 for order lookups", lambda: (
            "500" in (codes := status_codes(uids["prometheus"])), codes)))
        results.append(wait_for("error log for express-1002 in Loki", lambda: (
            bool(lines := log_lines(uids["loki"], "express-1002")), lines)))
        results.append(wait_for("alert is Firing", lambda: (
            (state := alert_state())[0] == "firing", state)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "before-errors")
