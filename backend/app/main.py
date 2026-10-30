"""FastAPI application: REST + WebSocket + static dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .emergency import EmergencyNotifier
from .hub import Hub
from .patients import PatientStore
from .pipeline import Pipeline
from .scenarios import SCENARIOS
from .settings import ROOT, Settings, load_settings
from .storage import DATASETS, History
from .thresholds import ThresholdStore

DISCLAIMER = ("Threshold-based screening alerts only. Heuristic ECG rhythm flags are NOT a clinically "
              "validated diagnosis. This is not a medical device.")


def build_source(s: Settings, pipeline: Pipeline):
    if s.mode == "simulated":
        from .sources.simulator import SimulatorSource
        return SimulatorSource(s, pipeline)
    if s.mode == "serial":
        from .sources.serial_source import SerialSource
        return SerialSource(s, pipeline)
    if s.mode == "cloud":
        from .sources.cloud_source import CloudSource
        return CloudSource(s, pipeline)
    from .sources.mqtt_source import MqttSource
    return MqttSource(s, pipeline)


def parse_time(v) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except ValueError:
        raise HTTPException(422, f"cannot parse time {v!r}")


def create_app(settings: Settings | None = None, start_source: bool = True) -> FastAPI:
    s = settings or load_settings()
    logging.basicConfig(level=getattr(logging, s.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    thresholds = ThresholdStore(s.thresholds_path)
    patients = PatientStore(s.patients_path, default_id=s.patient_id, default_name=s.patient_name)
    notifier = EmergencyNotifier(s, patients.get)
    hub = Hub()
    pipeline = Pipeline(s, thresholds, hub, patients=patients, notifier=notifier)
    history = History({"synthetic": s.results_csv, "simulated": s.simulated_csv, "real": s.real_csv})
    source = build_source(s, pipeline)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        pipeline.bind_loop(loop)
        tick = asyncio.create_task(pipeline.run_ticks())
        if start_source:
            source.start(loop)
        yield
        source.stop()
        notifier.shutdown()
        tick.cancel()

    app = FastAPI(title="CardioWatch Cloud Health IoT Monitor", lifespan=lifespan)
    app.state.settings, app.state.pipeline, app.state.history = s, pipeline, history
    app.state.thresholds, app.state.hub, app.state.source = thresholds, hub, source
    app.state.patients, app.state.notifier = patients, notifier

    def ds_check(ds: str) -> str:
        if ds not in DATASETS:
            raise HTTPException(422, f"dataset must be one of {DATASETS}")
        return ds

    def need(ds: str):
        if not history.exists(ds):
            hint = ("run: python scripts/generate_dataset.py" if ds == "synthetic"
                    else "no readings have been logged yet in this mode")
            raise HTTPException(404, f"dataset '{ds}' not found ({hint})")

    # ---------------- status / config ----------------
    @app.get("/api/status")
    def status():
        st = source.status() if hasattr(source, "status") else {}
        return {"mode": s.mode, "is_real": s.is_real, "server_time_ms": int(time.time() * 1000),
                "source": st, "stats": pipeline.stats, "disclaimer": DISCLAIMER,
                "scenarios": SCENARIOS if s.mode == "simulated" else [],
                "datasets": {d: history.exists(d) for d in DATASETS},
                "notifier": {"provider": notifier.provider.name,
                             "simulated": notifier.provider.name == "simulated",
                             "notify_on_simulated": s.notify_on_simulated},
                "ingest": {"enabled": s.allow_ingest or s.mode == "cloud",
                           "token_required": bool(s.ingest_token)}}

    @app.get("/api/thresholds")
    def get_thresholds():
        return thresholds.get().model_dump()

    @app.put("/api/thresholds")
    def put_thresholds(patch: dict = Body(...)):
        try:
            return thresholds.update(patch).model_dump()
        except ValidationError as e:
            raise HTTPException(422, "; ".join(x["msg"].replace("Value error, ", "") for x in e.errors()))

    @app.post("/api/thresholds/reset")
    def reset_thresholds():
        return thresholds.reset().model_dump()

    # ---------------- cloud ingest (device -> cloud over HTTP) ----------------
    def _check_token(tok: str | None) -> None:
        if s.ingest_token and tok != s.ingest_token:
            raise HTTPException(401, "invalid or missing X-Device-Token")

    @app.post("/api/ingest")
    def ingest(body: dict = Body(...), x_device_token: str | None = Header(default=None)):
        """Accept one reading from a device over the internet.

        Combined shape (what the ESP32 sends each second):
          {"dev","pid"?,"pname"?,"fs","leads_off"?,"ecg":[raw...],
           "spo2"?,"pulse"?,"finger"?,"lat"?,"lng"?}
        or a raw internal message with an explicit "type":"ecg"|"vitals".
        """
        if not (s.allow_ingest or s.mode == "cloud"):
            raise HTTPException(409, "HTTP ingest is disabled (set ALLOW_INGEST=1)")
        _check_token(x_device_token)
        dev = str(body.get("dev") or body.get("device_id") or s.device_id)
        pid = body.get("pid") or s.patient_id
        accepted = []

        if "type" in body:  # raw internal message passthrough
            msg = dict(body)
            msg["sim"] = False
            msg.setdefault("dev", dev)
            pipeline.handle(msg)
            accepted.append(body["type"])
        else:
            fs = int(body.get("fs") or s.ecg_fs)
            ecg = body.get("ecg")
            if ecg is not None:
                pipeline.handle({"type": "ecg", "dev": dev, "pid": pid, "pname": body.get("pname"),
                                 "fs": fs, "lo": 1 if body.get("leads_off") else 0,
                                 "s": list(ecg), "sim": False})
                accepted.append("ecg")
            if any(k in body for k in ("spo2", "pulse", "finger")):
                pipeline.handle({"type": "vitals", "dev": dev, "pid": pid,
                                 "spo2": body.get("spo2"), "pulse": body.get("pulse"),
                                 "finger": bool(body.get("finger", True)), "sim": False})
                accepted.append("vitals")

        # optional device GPS -> update the patient's stored coordinates (keep label)
        lat, lng = body.get("lat"), body.get("lng")
        if lat is not None and lng is not None:
            try:
                patients.update(pid, {"location": {"lat": float(lat), "lng": float(lng)}})
            except (ValidationError, ValueError):
                pass
        if hasattr(source, "note_ingest"):
            source.note_ingest(dev)
        if not accepted:
            raise HTTPException(422, "no ecg or vitals fields in body")
        return {"ok": True, "accepted": accepted, "patient_id": pid}

    # ---------------- patients / emergency contact ----------------
    @app.get("/api/patients")
    def get_patients():
        return [p.model_dump() for p in patients.all()]

    @app.get("/api/patients/{pid}")
    def get_patient(pid: str):
        p = patients.get(pid)
        if p is None:
            raise HTTPException(404, f"unknown patient {pid}")
        return p.model_dump()

    @app.put("/api/patients/{pid}")
    def put_patient(pid: str, patch: dict = Body(...)):
        try:
            return patients.update(pid, patch).model_dump()
        except ValidationError as e:
            raise HTTPException(422, "; ".join(x["msg"].replace("Value error, ", "") for x in e.errors()))

    # ---------------- emergency notifications ----------------
    @app.get("/api/notifications")
    def get_notifications(limit: int = Query(50, ge=1, le=200)):
        return {"provider": notifier.provider.name,
                "simulated": notifier.provider.name == "simulated",
                "items": notifier.recent(limit)}

    @app.post("/api/notifications/test")
    def test_notification(body: dict = Body(default={})):
        pid = body.get("patient_id") or s.patient_id
        res = notifier.send_test(pid)
        if not res.get("ok") and res.get("error"):
            raise HTTPException(422, res["error"])
        if res.get("notification"):
            hub.publish({"type": "notification", "notification": res["notification"]})
        return res

    # ---------------- live ----------------
    @app.get("/api/live/patients")
    def live_patients():
        return pipeline.patients()

    @app.get("/api/live/alerts")
    def live_alerts(limit: int = 100):
        return pipeline.alerts.recent(limit)

    @app.post("/api/simulator/inject")
    def inject(body: dict = Body(...)):
        if s.mode != "simulated":
            raise HTTPException(409, "scenario injection exists only in simulated mode")
        scenario = body.get("scenario")
        if scenario not in SCENARIOS:
            raise HTTPException(422, f"scenario must be one of {SCENARIOS}")
        dur = float(body.get("duration_s", 30))
        if not source.inject(body.get("patient_id"), scenario, max(2.0, min(300.0, dur))):
            raise HTTPException(404, "unknown patient_id")
        return {"ok": True}

    # ---------------- history (CSV) ----------------
    def common(dataset, patient_id, start, end, status_):
        ds_check(dataset)
        need(dataset)
        statuses = [x for x in (status_ or "").split(",") if x in ("normal", "warning", "critical")] or None
        return dict(ds=dataset, patient_id=patient_id or None, start_ms=parse_time(start), end_ms=parse_time(end),
                    statuses=statuses)

    @app.get("/api/history/meta")
    def h_meta(dataset: str = "synthetic"):
        ds_check(dataset)
        return history.meta(dataset)

    @app.get("/api/history/series")
    def h_series(dataset: str = "synthetic", patient_id: str = "", start: str = "", end: str = "",
                 status: str = "", max_points: int = Query(1200, ge=50, le=5000)):
        return history.series(**common(dataset, patient_id, start, end, status), max_points=max_points)

    @app.get("/api/history/rows")
    def h_rows(dataset: str = "synthetic", patient_id: str = "", start: str = "", end: str = "",
               status: str = "", code: str = "", limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
               order: str = "desc", include_ecg: bool = False):
        return history.rows(**common(dataset, patient_id, start, end, status), code=code or None,
                            limit=limit, offset=offset, order=order, include_ecg=include_ecg)

    @app.get("/api/history/alerts")
    def h_alerts(dataset: str = "synthetic", patient_id: str = "", start: str = "", end: str = "",
                 category: str = "", min_severity: str = "warning", limit: int = Query(500, ge=1, le=5000)):
        c = common(dataset, patient_id, start, end, "")
        c.pop("statuses")
        return history.alert_episodes(**c, category=category or None, min_severity=min_severity, limit=limit)

    @app.get("/api/history/export.csv")
    def h_export(dataset: str = "synthetic", patient_id: str = "", start: str = "", end: str = "",
                 status: str = "", code: str = ""):
        text = history.export_csv(**common(dataset, patient_id, start, end, status), code=code or None)
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{dataset}_export.csv"'})

    # ---------------- websocket ----------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        q = hub.subscribe()
        try:
            await ws.send_text(json.dumps({**pipeline.snapshot(), "disclaimer": DISCLAIMER,
                                           "scenarios": SCENARIOS if s.mode == "simulated" else []}, default=str))

            async def sender():
                while True:
                    await ws.send_text(await q.get())

            async def receiver():
                while True:
                    await ws.receive_text()

            tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                t.exception()
        except Exception:
            pass
        finally:
            hub.unsubscribe(q)

    frontend = ROOT / "frontend"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="ui")
    return app


def app_factory() -> FastAPI:
    return create_app()
