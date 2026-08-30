"""
SRE Copilot — Live Demo Runner
================================
Triggers a real LangGraph analysis through the running FastAPI server.
WebSocket events stream live to the browser dashboard as each agent runs.

No Go backend required — all evidence is controlled by the demo path.

SETUP (run once):
  uvicorn main:app --reload --port 9000

USAGE:
  python demo.py path_a   # Autonomous happy path
  python demo.py path_b   # Human review — low confidence
  python demo.py path_c   # Human review — no runbook
  python demo.py path_d   # Degraded backend

Then watch the browser dashboard update live.
"""

import sys
import time
import uuid
import webbrowser

import httpx

BASE_URL = "http://127.0.0.1:9000"

PATHS = {
    "path_a": {
        "label": "PATH A — Autonomous Happy Path",
        "description": "All evidence strong. Confidence 1.0. No human needed.",
        "color": "\033[92m",  # green
    },
    "path_b": {
        "label": "PATH B — Human Review (Low Confidence)",
        "description": "Weak logs, unknown root cause. Confidence ~0.50. Graph pauses for human input.",
        "color": "\033[93m",  # yellow
    },
    "path_c": {
        "label": "PATH C — Human Review (No Runbook)",
        "description": "Strong evidence but no matching runbook. Graph pauses at RAG stage.",
        "color": "\033[93m",  # yellow
    },
    "path_d": {
        "label": "PATH D — Degraded Backend",
        "description": "Go backend unreachable. System completes autonomously with available data.",
        "color": "\033[91m",  # red
    },
}

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def print_header(path: str):
    info = PATHS[path]
    color = info["color"]
    print(f"\n{color}{BOLD}{'=' * 60}{RESET}")
    print(f"{color}{BOLD}  SRE Copilot Demo{RESET}")
    print(f"{color}{BOLD}  {info['label']}{RESET}")
    print(f"{DIM}  {info['description']}{RESET}")
    print(f"{color}{BOLD}{'=' * 60}{RESET}\n")


def check_server():
    try:
        r = httpx.get(f"{BASE_URL}/api/v1/health", timeout=3.0)
        status = r.json().get("status", "unknown")
        print(f"  Server: {BOLD}{status}{RESET}")
        return True
    except Exception:  # noqa: BLE001  # any connection error means server isn't running
        print(f"\n  {BOLD}ERROR:{RESET} FastAPI server is not running.")
        print("  Start it first:\n")
        print("    uvicorn main:app --reload --port 9000\n")
        return False


def open_dashboard(analysis_id: str):
    url = f"{BASE_URL}/dashboard/{analysis_id}"
    print(f"\n  Dashboard: {BOLD}{url}{RESET}")
    print("  Opening browser...")
    webbrowser.open(url)
    print("  Waiting 3 seconds for browser to load...")
    time.sleep(3)


def trigger_analysis(analysis_id: str, path: str) -> dict:
    print("\n  Triggering analysis... (watch the browser!)\n")
    r = httpx.post(
        f"{BASE_URL}/api/v1/demo",
        json={"analysis_id": analysis_id, "path": path},
        timeout=120.0,
    )
    return r.json()


def resume_analysis(analysis_id: str, path: str) -> dict:
    human_contexts = {
        "path_b": "This looks like the Redis connection leak from last Tuesday — connection pool is not being released after timeouts. Check pool settings and restart the connection manager.",
        "path_c": "This is a new failure mode introduced in last night's deploy — the payment gateway timeout value was changed from 5s to 30s which holds DB connections open much longer, exhausting the pool.",
    }
    context = human_contexts.get(
        path, "Manual investigation confirms connection pool exhaustion as root cause."
    )

    print(f"\n  {BOLD}Injecting human context:{RESET}")
    print(f'  {DIM}"{context}"{RESET}\n')
    print("  Resuming analysis...")

    r = httpx.post(
        f"{BASE_URL}/api/v1/demo/{analysis_id}/resume",
        json={"human_context": context},
        timeout=120.0,
    )
    return r.json()


def print_result(result: dict, label: str = "Result"):
    print(f"\n  {BOLD}{label}:{RESET}")
    print(f"    Status:     {BOLD}{result.get('status', 'unknown')}{RESET}")
    if result.get("confidence") is not None:
        conf = result["confidence"]
        conf_pct = f"{conf * 100:.0f}%"
        color = "\033[92m" if conf >= 0.75 else "\033[93m"
        print(f"    Confidence: {color}{BOLD}{conf_pct}{RESET}")
    if result.get("waiting_at"):
        print(f"    Paused at:  {BOLD}{result['waiting_at']}{RESET}")
    if result.get("error"):
        print(f"    Error:      \033[91m{result['error']}{RESET}")


def main():
    path = sys.argv[1].lower() if len(sys.argv) > 1 else "path_a"

    if path not in PATHS:
        print(f"\nUnknown path: {path}")
        print(f"Available: {', '.join(PATHS.keys())}")
        sys.exit(1)

    print_header(path)

    if not check_server():
        sys.exit(1)

    analysis_id = f"demo-{path}-{uuid.uuid4().hex[:6]}"
    print(f"  Analysis ID: {DIM}{analysis_id}{RESET}")

    open_dashboard(analysis_id)

    result = trigger_analysis(analysis_id, path)
    print_result(result, "First run")

    # Handle HITL paths
    if result.get("requires_human") and path in ("path_b", "path_c"):
        pause_reason = {
            "path_b": "confidence below 0.75 threshold",
            "path_c": "no matching runbook found (RAG pause)",
        }
        print(f"\n  {'=' * 50}")
        print(
            f"  {BOLD}GRAPH PAUSED{RESET} — {pause_reason.get(path, 'human input required')}"
        )
        print(f"  {'=' * 50}")
        print("\n  The dashboard is now showing the HITL pause state.")
        input("\n  Press ENTER to inject human context and resume...\n")

        resume_result = resume_analysis(analysis_id, path)
        print_result(resume_result, "After resume")

    print(f"\n  {'=' * 60}")
    print(f"  {BOLD}Demo complete.{RESET} Full report visible in the browser.")
    print(f"  Dashboard: {BASE_URL}/dashboard/{analysis_id}")
    print(f"  {'=' * 60}\n")


if __name__ == "__main__":
    main()
