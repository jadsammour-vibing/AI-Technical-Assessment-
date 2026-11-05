"""Step 1 - Ingestion: an HTTP webhook that triggers the pipeline on every new message.

    POST /intake   {"source": "Email", "message": "...", "id": "optional-external-id"}

Any upstream channel (email parser, web form, support portal) can call this. It answers
202 Accepted immediately and processes the message on a background worker, so a slow LLM
never makes the caller time out.

Run:  python intake_server.py            (listens on http://localhost:8080)

Demo console: open http://localhost:8080 in a browser. The page (ui/index.html) sends
messages to /intake and polls /api/state to show each record as it is produced.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from triage.llm import LLMClient
from triage.pipeline import make_record_id, process_message
from triage.sinks import already_processed, load_records, write_record

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "ui" / "index.html"
SAMPLE_FILES = {"samples": ROOT / "data" / "samples.json", "edge_cases": ROOT / "data" / "extra_edge_cases.json"}

work: "queue.Queue[dict]" = queue.Queue()
llm = LLMClient()

# Live job status for the demo console (in memory only; the records on disk are the source of truth).
jobs: dict[int, dict] = {}
jobs_lock = threading.Lock()
job_ids = itertools.count(1)
MAX_JOBS_KEPT = 100


def set_job(jid: int, **fields) -> None:
    with jobs_lock:
        jobs.setdefault(jid, {"job_id": jid}).update(fields)
        for old in sorted(jobs)[:-MAX_JOBS_KEPT]:
            del jobs[old]


def worker() -> None:
    while True:
        job = work.get()
        jid = job["job_id"]
        try:
            rid = make_record_id(job["source"], job["message"])
            if already_processed(rid):
                set_job(jid, state="duplicate", finished_at=time.time())
                print(f"[{rid}] duplicate of an already-processed message, skipped (no LLM calls)", flush=True)
                continue
            set_job(jid, state="processing", started_at=time.time())
            rec = process_message(llm, job["message"], job["source"], job["received_at"], job.get("id"))
            new = write_record(rec)
            set_job(jid, state="done" if new else "duplicate", finished_at=time.time())
            flag = "ESCALATED" if rec["escalation_flag"] else "routed"
            print(f"[{rec['record_id']}] {rec['category']} / {rec['priority']} / "
                  f"conf={rec['confidence']} -> {rec['destination_queue']} ({flag})"
                  + ("" if new else "  [duplicate, skipped]"), flush=True)
        except Exception as e:  # never kill the worker
            set_job(jid, state="error", error=str(e), finished_at=time.time())
            print(f"[error] failed to process message: {e}", flush=True)
        finally:
            work.task_done()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/intake":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            message = str(body["message"]).strip()
            if not message:
                raise ValueError("empty message")
        except Exception as e:
            return self._send(400, {"error": f"invalid payload: {e}"})
        job = {
            "job_id": next(job_ids),
            "message": message,
            "source": str(body.get("source", "unknown")),
            "id": body.get("id"),
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        rid = make_record_id(job["source"], message)
        duplicate = already_processed(rid)
        set_job(job["job_id"], record_id=rid, source=job["source"],
                message=message, state="queued", queued_at=time.time())
        work.put(job)
        self._send(202, {"status": "accepted", "record_id": rid, "job_id": job["job_id"], "duplicate": duplicate})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, {"status": "ok", "queued": work.qsize(), "model": llm.model})
        if path in ("/", "/index.html"):
            if not UI_FILE.exists():
                return self._send(404, {"error": "ui/index.html not found"})
            return self._send_bytes(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            with jobs_lock:
                job_list = [dict(j) for _, j in sorted(jobs.items())]
            return self._send(200, {
                "provider": llm.provider, "model": llm.model, "queued": work.qsize(),
                "server_time": time.time(), "jobs": job_list, "records": load_records(),
            })
        if path == "/api/samples":
            out = {}
            for key, f in SAMPLE_FILES.items():
                out[key] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
            return self._send(200, out)
        self._send(404, {"error": "not found"})

    def _send(self, code: int, obj: dict):
        self._send_bytes(code, json.dumps(obj).encode(), "application/json")

    def _send_bytes(self, code: int, data: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the console clean for the demo
        pass


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    for _ in range(int(os.getenv("WORKERS", "1"))):
        threading.Thread(target=worker, daemon=True).start()
    # On Windows, SO_REUSEADDR lets a second server bind a port that is already in use, and
    # requests then go to the old one. Turn it off there so a stale server fails loudly.
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        raise SystemExit(f"Port {port} is already in use ({e}). Stop the other intake_server.py "
                         f"(Ctrl+C in its terminal), or start this one with a different PORT.")
    print(f"ArcVault intake listening on http://localhost:{port}/intake  "
          f"(provider={llm.provider}, model={llm.model})", flush=True)
    print(f"Demo console: http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
