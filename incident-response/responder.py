"""Incident responder for Order Tracker.

Receives Grafana alert webhooks at POST /alerts (port 8001), saves the evidence
needed to understand the problem (alert, metrics, logs, traces) under
incident-response/incidents/, then starts a coding agent in headless mode to
investigate and fix it.

Run it on the host (the agent needs the repo, uv, and docker):

    uv run --project incident-response uvicorn responder:app --app-dir incident-response --port 8001
"""

import json
import os
import shlex
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request


REPO_DIR = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", REPO_DIR / "incident-response" / "incidents"))
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
LOOKBACK_SECONDS = int(os.getenv("EVIDENCE_LOOKBACK_SECONDS", "900"))

# The headless agent. Default: Claude Code in print mode, allowed to edit files,
# run tests, rebuild the app with Docker Compose, and query the telemetry APIs.
AGENT_COMMAND = os.getenv(
    "AGENT_COMMAND",
    "claude -p --permission-mode acceptEdits --allowedTools "
    "Read Edit Write Glob Grep "
    "'Bash(uv run:*)' 'Bash(docker compose:*)' 'Bash(curl:*)' "
    "'Bash(git diff:*)' 'Bash(git status:*)' 'Bash(git log:*)'",
)
APP_RESTART_COMMAND = os.getenv("APP_RESTART_COMMAND", "docker compose up --build -d --wait app")
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "1800"))

app = FastAPI(title="Order Tracker incident responder")
_running = set()
_lock = threading.Lock()


@app.get("/healthz")
def health():
    return {"status": "ok", "running": sorted(_running)}


@app.get("/incidents")
def incidents():
    if not INCIDENTS_DIR.exists():
        return []
    result = []
    for path in sorted(INCIDENTS_DIR.iterdir(), reverse=True):
        status_file = path / "status.json"
        if status_file.exists():
            result.append({"id": path.name, **json.loads(status_file.read_text())})
    return result


@app.post("/alerts", status_code=202)
async def alerts(request: Request, background: BackgroundTasks):
    payload = await request.json()
    accepted, skipped = [], []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            skipped.append({"reason": "not firing", "labels": alert.get("labels", {})})
            continue
        key = alert_key(alert)
        with _lock:
            if key in _running:
                skipped.append({"reason": "agent already running for this alert", "key": key})
                continue
            _running.add(key)
        incident_dir = create_incident(alert, payload)
        background.add_task(handle_incident, key, alert, incident_dir)
        accepted.append({"incident": incident_dir.name, "key": key})
    return {"accepted": accepted, "skipped": skipped}


def alert_key(alert):
    labels = alert.get("labels", {})
    return alert.get("fingerprint") or f"{labels.get('alertname')}:{labels.get('http_route', '')}"


def create_incident(alert, payload):
    labels = alert.get("labels", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = slug(labels.get("alertname", "alert"))
    incident_dir = INCIDENTS_DIR / f"{stamp}-{name}"
    incident_dir.mkdir(parents=True, exist_ok=True)
    write_json(incident_dir / "alert.json", {"alert": alert, "webhook": without_alerts(payload)})
    write_status(incident_dir, "collecting evidence", alert)
    return incident_dir


def handle_incident(key, alert, incident_dir):
    try:
        is_test = alert.get("labels", {}).get("test") == "true"
        evidence = {} if is_test else collect_evidence(alert)
        write_json(incident_dir / "evidence.json", evidence)
        (incident_dir / "evidence.md").write_text(evidence_markdown(alert, evidence, is_test))
        write_status(incident_dir, "agent running", alert)
        exit_code = run_agent(alert, incident_dir, is_test)
        write_status(incident_dir, "agent finished" if exit_code == 0 else f"agent failed ({exit_code})", alert)
    except Exception as exc:  # keep the responder alive whatever happens
        write_status(incident_dir, f"responder error: {exc!r}", alert)
    finally:
        with _lock:
            _running.discard(key)


def collect_evidence(alert):
    route = alert.get("labels", {}).get("http_route")
    end = time.time()
    start = end - LOOKBACK_SECONDS
    route_filter = f', http_route="{route}"' if route else ""
    evidence = {"endpoint": route, "window_seconds": LOOKBACK_SECONDS}

    evidence["metrics"] = safe(lambda: get_json(
        f"{PROMETHEUS_URL}/api/v1/query",
        query=f'sum by (http_route, http_response_status_code, url_path) '
              f'(increase(http_server_requests_total{{service_name="order-tracker"{route_filter}}}[15m]))',
    )["data"]["result"])

    logs = safe(lambda: get_json(
        f"{LOKI_URL}/loki/api/v1/query_range",
        query='{service_name="order-tracker"} | severity_text="ERROR"',
        start=int(start * 1e9), end=int(end * 1e9), limit=20, direction="backward",
    )["data"]["result"])
    evidence["error_logs"] = []
    trace_ids = []
    for stream in logs if isinstance(logs, list) else []:
        for ts, line in stream.get("values", []):
            trace_id = stream["stream"].get("trace_id")
            evidence["error_logs"].append({
                "time": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(),
                "trace_id": trace_id,
                "message": line,
                "attributes": stream["stream"],
            })
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)

    search = safe(lambda: get_json(
        f"{TEMPO_URL}/api/search",
        q='{ resource.service.name = "order-tracker" && status = error }',
        start=int(start), end=int(end), limit=10,
    ).get("traces", []))
    for trace in search if isinstance(search, list) else []:
        if trace["traceID"] not in trace_ids:
            trace_ids.append(trace["traceID"])

    evidence["traces"] = {}
    for trace_id in trace_ids[:3]:
        evidence["traces"][trace_id] = safe(lambda: summarize_trace(get_json(f"{TEMPO_URL}/api/traces/{trace_id}")))
    return evidence


def summarize_trace(trace):
    spans = []
    for batch in trace.get("batches", []) + trace.get("resourceSpans", []):
        for scope in batch.get("scopeSpans", []) + batch.get("instrumentationLibrarySpans", []):
            for span in scope.get("spans", []):
                spans.append({
                    "name": span.get("name"),
                    "status": span.get("status", {}),
                    "attributes": {a["key"]: next(iter(a["value"].values()), None) for a in span.get("attributes", [])},
                    "events": [
                        {"name": e.get("name"),
                         "attributes": {a["key"]: next(iter(a["value"].values()), None) for a in e.get("attributes", [])}}
                        for e in span.get("events", [])
                    ],
                })
    return spans


def evidence_markdown(alert, evidence, is_test):
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    lines = [
        f"# Incident: {labels.get('alertname', 'unknown alert')}",
        "",
        f"- Status: {alert.get('status')}",
        f"- Started: {alert.get('startsAt', 'unknown')}",
        f"- Endpoint: {labels.get('http_route') or annotations.get('endpoint') or 'unknown'}",
        f"- Summary: {annotations.get('summary', '')}",
        f"- Description: {annotations.get('description', '')}",
        f"- Dashboard: {alert.get('dashboardURL') or annotations.get('dashboard_url', '')}",
        f"- Labels: `{json.dumps(labels)}`",
        "",
    ]
    if is_test:
        lines.append("This is a test notification (label `test=true`). No evidence was collected.")
        return "\n".join(lines) + "\n"
    lines += ["## Metrics (last 15m)", ""]
    for series in evidence.get("metrics") or []:
        if isinstance(series, dict):
            lines.append(f"- `{json.dumps(series['metric'])}`: {series['value'][1]}")
    lines += ["", "## Error logs", ""]
    for entry in evidence.get("error_logs", [])[:10]:
        lines.append(f"- {entry['time']} trace `{entry['trace_id']}`")
        lines.append("")
        lines.append("  ```")
        lines += [f"  {line}" for line in entry["message"].splitlines()]
        lines.append("  ```")
    lines += ["", "## Traces", ""]
    for trace_id, spans in evidence.get("traces", {}).items():
        lines.append(f"### {trace_id}")
        for span in spans if isinstance(spans, list) else [spans]:
            lines.append(f"- `{json.dumps(span)}`")
    return "\n".join(lines) + "\n"


def agent_prompt(alert, incident_dir, is_test):
    rel = incident_dir.relative_to(REPO_DIR)
    if is_test:
        return (
            f"You are the on-call engineer for Order Tracker. A test alert was received; "
            f"it is described in {rel}/evidence.md and {rel}/alert.json. "
            "This is only a test of the incident responder: do not change any code. "
            "Read the alert, confirm what you received, and write a short note to "
            f"{rel}/report.md. Reply in two or three sentences. "
            "End your answer with the line: RESPONDER TEST OK: no incident to fix."
        )
    return f"""You are the on-call engineer for Order Tracker (FastAPI app in app/, tests in tests/).
A Grafana alert fired. Evidence (alert, metrics, error logs with stack traces, traces) is in
{rel}/evidence.md and {rel}/evidence.json. You can query more telemetry with curl:
Prometheus {PROMETHEUS_URL}, Loki {LOKI_URL}, Tempo {TEMPO_URL}. The app runs at {APP_URL}.

1. Read the evidence and find the root cause in the code.
2. Add a regression test in tests/ that reproduces the failure, and check it fails.
3. Make the smallest safe fix, then run `uv run --frozen pytest -q` until it passes.
4. Rebuild and restart the app with `{APP_RESTART_COMMAND}`.
5. Verify the failing request from the evidence now succeeds with curl.
6. Write {rel}/report.md with: root cause, fix, test, and verification output.

Do not commit or push. If you cannot fix it safely (the fix needs a data migration, a
product decision, or the tests keep failing), don't guess: write the reason in report.md
and end your answer with a line starting with "ESCALATE:". Otherwise end your answer
with a line starting with "RESOLVED:" and the root cause in one sentence."""


def run_agent(alert, incident_dir, is_test):
    prompt = agent_prompt(alert, incident_dir, is_test)
    (incident_dir / "agent-prompt.md").write_text(prompt + "\n")
    command = shlex.split(AGENT_COMMAND) + [prompt]
    with open(incident_dir / "agent-output.md", "w") as output:
        try:
            result = subprocess.run(
                command, cwd=REPO_DIR, stdout=output, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=AGENT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            output.write("\nESCALATE: agent timed out\n")
            return -1
    return result.returncode


def get_json(url, **params):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"error": repr(exc)}


def write_status(incident_dir, state, alert):
    write_json(incident_dir / "status.json", {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "alertname": alert.get("labels", {}).get("alertname"),
        "endpoint": alert.get("labels", {}).get("http_route"),
    })


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def without_alerts(payload):
    return {k: v for k, v in payload.items() if k != "alerts"}


def slug(text):
    return "".join(c if c.isalnum() else "-" for c in text).strip("-").lower()[:40] or "alert"
