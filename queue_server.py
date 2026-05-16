"""
Central queue server — one instance coordinates all workers across all machines.

Deploy on any always-on machine (a local PC, t3.micro EC2, etc.).
Workers on every machine call this HTTP API instead of touching SQLite directly.

Start server:
    pip install fastapi uvicorn
    python queue_server.py --db work_queue.db --port 8000

    # With a secret key so random internet traffic can't mess with your queue:
    python queue_server.py --db work_queue.db --port 8000 --key mysecret123

Workers then use:
    python run_village_workers.py --workers 20 --queue-url http://<server-ip>:8000 --key mysecret123 ...

Endpoints:
    POST /claim          → claim next pending village
    POST /complete       → mark village done
    POST /fail           → mark village failed (auto-retry up to 3x)
    POST /checkpoint     → save progress within a village (resume support)
    POST /heartbeat      → keep claim alive (prevents timeout reclaim)
    GET  /stats          → overall progress
    GET  /districts      → per-district breakdown
    GET  /health         → liveness check
"""

import argparse
import logging
import os
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import work_queue as wq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Bhulekh Queue Server", version="1.0")

# Globals set at startup
_db_path: str = "work_queue.db"
_api_key: Optional[str] = None


# ── Auth ─────────────────────────────────────────────────────────────────────

def check_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if _api_key and x_api_key != _api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ── Request / response models ─────────────────────────────────────────────────

class ClaimRequest(BaseModel):
    worker_id: str
    district_codes: Optional[list[int]] = None


class CompleteRequest(BaseModel):
    village_id: int
    khatiyans_fetched: int


class FailRequest(BaseModel):
    village_id: int
    error_msg: str


class CheckpointRequest(BaseModel):
    village_id: int
    khatiyans_fetched: int
    last_khatiyan_no: str


class HeartbeatRequest(BaseModel):
    village_id: int


class PriorityRequest(BaseModel):
    district_codes: list[int]
    priority: int = 10


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "db": _db_path}


@app.post("/claim", dependencies=[Depends(check_key)])
def claim(req: ClaimRequest):
    village = wq.claim_village(
        _db_path,
        worker_id=req.worker_id,
        district_codes=req.district_codes,
    )
    if village is None:
        return JSONResponse(status_code=204, content={"detail": "no work available"})
    log.info("CLAIM  worker=%s village=%s (%s)", req.worker_id,
             village["village_name"], village["village_code"])
    return village


@app.post("/complete", dependencies=[Depends(check_key)])
def complete(req: CompleteRequest):
    wq.complete_village(_db_path, req.village_id, req.khatiyans_fetched)
    log.info("DONE   village_id=%d khatiyans=%d", req.village_id, req.khatiyans_fetched)
    return {"ok": True}


@app.post("/fail", dependencies=[Depends(check_key)])
def fail(req: FailRequest):
    wq.fail_village(_db_path, req.village_id, req.error_msg)
    log.info("FAIL   village_id=%d err=%s", req.village_id, req.error_msg[:80])
    return {"ok": True}


@app.post("/checkpoint", dependencies=[Depends(check_key)])
def checkpoint(req: CheckpointRequest):
    wq.checkpoint_village(
        _db_path, req.village_id, req.khatiyans_fetched, req.last_khatiyan_no
    )
    return {"ok": True}


@app.post("/heartbeat", dependencies=[Depends(check_key)])
def heartbeat(req: HeartbeatRequest):
    wq.heartbeat(_db_path, req.village_id)
    return {"ok": True}


@app.post("/priority", dependencies=[Depends(check_key)])
def priority(req: PriorityRequest):
    n = wq.set_priority(_db_path, req.district_codes, req.priority)
    log.info("PRIORITY=%d set for %d villages in districts %s",
             req.priority, n, req.district_codes)
    return {"villages_updated": n}


@app.get("/stats", dependencies=[Depends(check_key)])
def stats():
    return wq.get_stats(_db_path)


@app.get("/districts", dependencies=[Depends(check_key)])
def districts():
    rows = wq.list_districts(_db_path)
    return [
        {"district_code": r[0], "district_name": r[1], "villages": r[2], "done": r[3]}
        for r in rows
    ]


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _db_path, _api_key

    parser = argparse.ArgumentParser(description="Bhulekh central queue server")
    parser.add_argument("--db",   default="work_queue.db", help="SQLite queue file")
    parser.add_argument("--port", type=int, default=8000,  help="HTTP port (default 8000)")
    parser.add_argument("--host", default="0.0.0.0",       help="Bind address (default 0.0.0.0)")
    parser.add_argument("--key",  default=None,             help="Optional API key for auth")
    args = parser.parse_args()

    _db_path = args.db
    _api_key = args.key or os.environ.get("QUEUE_API_KEY")

    if not os.path.exists(_db_path):
        print(f"ERROR: Queue DB not found: {_db_path}")
        print("Run first: python soap_enumerator.py --db", _db_path)
        raise SystemExit(1)

    log.info("Queue server starting — db=%s port=%d auth=%s",
             _db_path, args.port, "yes" if _api_key else "no")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
