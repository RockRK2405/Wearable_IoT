"""CSV logging (append) and CSV-backed history queries."""
from __future__ import annotations

import csv
import io
import math
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .detection import CODE_INFO, parse_codes

COLUMNS = [
    "timestamp", "epoch_ms", "data_source", "patient_id", "device_id",
    "hr_bpm", "hr_source", "pulse_bpm", "spo2_pct",
    "rr_mean_ms", "sdnn_ms", "rmssd_ms", "rr_cv", "beats_in_window", "ecg_quality",
    "leads_off", "finger_detected", "ecg_irregular_flag",
    "status", "alert_codes", "alert_message", "scenario", "ecg_fs", "ecg_snippet",
]
STATUS_NUM = {"normal": 0, "warning": 1, "critical": 2}
DATASETS = ("synthetic", "simulated", "real")


def _fmt(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".")
    return v


class CsvLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, row: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists() or self.path.stat().st_size == 0
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, COLUMNS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({k: _fmt(row.get(k)) for k in COLUMNS})


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if v is pd.NaT:
        return None
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    cols = list(df.columns)
    return [{c: _clean(v) for c, v in zip(cols, row)} for row in df.itertuples(index=False, name=None)]


class History:
    def __init__(self, paths: dict[str, Path], ttl: float = 2.0):
        self.paths = {k: Path(v) for k, v in paths.items()}
        self.ttl = ttl
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------- loading ----------
    def exists(self, ds: str) -> bool:
        return ds in self.paths and self.paths[ds].exists()

    def load(self, ds: str) -> pd.DataFrame | None:
        if ds not in self.paths:
            raise KeyError(ds)
        path = self.paths[ds]
        if not path.exists():
            return None
        with self._lock:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            c = self._cache.get(ds)
            if c and (c["key"] == key or (ds != "synthetic" and time.time() - c["at"] < self.ttl)):
                return c["df"]
            df = pd.read_csv(path, low_memory=False, on_bad_lines="skip")
            for col in COLUMNS:
                if col not in df.columns:
                    df[col] = np.nan
            df = df.dropna(subset=["epoch_ms", "patient_id"])
            df["epoch_ms"] = df["epoch_ms"].astype("int64")
            df["sev"] = df["status"].map(STATUS_NUM).fillna(0).astype(int)
            df["alert_codes"] = df["alert_codes"].fillna("")
            df = df.sort_values(["patient_id", "epoch_ms"]).reset_index(drop=True)
            self._cache[ds] = {"key": key, "at": time.time(), "df": df}
            return df

    def _filtered(self, ds, patient_id=None, start_ms=None, end_ms=None, statuses=None, code=None):
        df = self.load(ds)
        if df is None:
            return None
        m = np.ones(len(df), dtype=bool)
        if patient_id:
            m &= (df["patient_id"] == patient_id).to_numpy()
        if start_ms is not None:
            m &= (df["epoch_ms"] >= start_ms).to_numpy()
        if end_ms is not None:
            m &= (df["epoch_ms"] <= end_ms).to_numpy()
        if statuses:
            m &= df["status"].isin(statuses).to_numpy()
        if code:
            m &= df["alert_codes"].str.contains(code, regex=False).to_numpy()
        return df[m]

    # ---------- queries ----------
    def meta(self, ds: str) -> dict:
        df = self.load(ds)
        if df is None or df.empty:
            return {"dataset": ds, "available": False, "rows": 0, "patients": []}
        counts = df["status"].value_counts().to_dict()
        pats = []
        for pid, g in df.groupby("patient_id"):
            pats.append({"patient_id": pid, "rows": int(len(g)), "min_ms": int(g["epoch_ms"].min()),
                         "max_ms": int(g["epoch_ms"].max())})
        sources = [str(x) for x in df["data_source"].dropna().unique()]
        return {"dataset": ds, "available": True, "rows": int(len(df)),
                "min_ms": int(df["epoch_ms"].min()), "max_ms": int(df["epoch_ms"].max()),
                "status_counts": {k: int(v) for k, v in counts.items()},
                "patients": pats, "data_sources": sources}

    def series(self, ds, patient_id=None, start_ms=None, end_ms=None, statuses=None, max_points=1200) -> dict:
        df = self._filtered(ds, patient_id, start_ms, end_ms, statuses)
        empty = {"t": [], "hr": [], "hr_min": [], "hr_max": [], "spo2": [], "spo2_min": [], "sev": [], "n": [], "bucket_ms": 0, "rows": 0}
        if df is None or df.empty:
            return empty
        d = df
        bucket = 0
        if len(d) > max_points:
            span = int(d["epoch_ms"].max() - d["epoch_ms"].min()) or 1
            bucket = int(math.ceil(span / (max_points - 1)))
            g = d.assign(b=(d["epoch_ms"] // bucket) * bucket).groupby("b")
            a = g.agg(hr=("hr_bpm", "mean"), hr_min=("hr_bpm", "min"), hr_max=("hr_bpm", "max"),
                      spo2=("spo2_pct", "mean"), spo2_min=("spo2_pct", "min"),
                      sev=("sev", "max"), n=("sev", "size")).reset_index().rename(columns={"b": "t"})
        else:
            a = pd.DataFrame({"t": d["epoch_ms"], "hr": d["hr_bpm"], "hr_min": d["hr_bpm"], "hr_max": d["hr_bpm"],
                              "spo2": d["spo2_pct"], "spo2_min": d["spo2_pct"], "sev": d["sev"], "n": 1})
        if patient_id is None and a["t"].duplicated().any():
            a = a.groupby("t").agg(hr=("hr", "mean"), hr_min=("hr_min", "min"), hr_max=("hr_max", "max"),
                                   spo2=("spo2", "mean"), spo2_min=("spo2_min", "min"),
                                   sev=("sev", "max"), n=("n", "sum")).reset_index()
        a = a.sort_values("t")
        out = {c: [_clean(v) for v in a[c].tolist()] for c in a.columns}
        out["bucket_ms"] = bucket
        out["rows"] = int(len(d))
        return out

    def rows(self, ds, patient_id=None, start_ms=None, end_ms=None, statuses=None, code=None,
             limit=50, offset=0, order="desc", include_ecg=False) -> dict:
        df = self._filtered(ds, patient_id, start_ms, end_ms, statuses, code)
        if df is None:
            return {"total": 0, "rows": []}
        df = df.sort_values("epoch_ms", ascending=(order == "asc"), kind="stable")
        total = int(len(df))
        page = df.iloc[offset: offset + limit]
        cols = [c for c in COLUMNS if include_ecg or c != "ecg_snippet"]
        recs = _records(page[cols])
        if include_ecg:
            for r in recs:
                r["ecg_snippet"] = self.parse_snippet(r.get("ecg_snippet"))
        return {"total": total, "rows": recs}

    @staticmethod
    def parse_snippet(s) -> list[float]:
        if not isinstance(s, str) or not s.strip():
            return []
        return [int(v) / 100.0 for v in s.split()]

    def export_csv(self, ds, patient_id=None, start_ms=None, end_ms=None, statuses=None, code=None) -> str:
        df = self._filtered(ds, patient_id, start_ms, end_ms, statuses, code)
        if df is None:
            return ""
        buf = io.StringIO()
        df.sort_values("epoch_ms")[COLUMNS].to_csv(buf, index=False)
        return buf.getvalue()

    def alert_episodes(self, ds, patient_id=None, start_ms=None, end_ms=None, category=None,
                       min_severity="warning", limit=500) -> list[dict]:
        df = self._filtered(ds, patient_id, start_ms, end_ms)
        if df is None or df.empty:
            return []
        min_sev = STATUS_NUM.get(min_severity, 1)
        eps: list[dict] = []
        for pid, g in df.groupby("patient_id"):
            t = g["epoch_ms"].to_numpy()
            step = float(np.median(np.diff(t))) if len(t) > 1 else 1000.0
            gap = max(2.5 * step, 2500.0)
            sub = g[g["alert_codes"] != ""]
            open_eps: dict[str, dict] = {}

            def close(code):
                eps.append(open_eps.pop(code))

            for r in sub.itertuples(index=False):
                ts = int(r.epoch_ms)
                present = dict(parse_codes(r.alert_codes))
                for code in list(open_eps):
                    if code not in present or ts - open_eps[code]["end_ms"] > gap:
                        close(code)
                for code, sev in present.items():
                    sv = STATUS_NUM.get(sev, 1)
                    ep = open_eps.get(code)
                    hr = _clean(r.hr_bpm)
                    sp = _clean(r.spo2_pct)
                    if ep is None:
                        ep = open_eps[code] = {
                            "patient_id": pid, "code": code, "category": CODE_INFO.get(code, ("sensor", ""))[0],
                            "description": CODE_INFO.get(code, ("", code))[1], "severity": sev,
                            "start_ms": ts, "end_ms": ts, "n_readings": 0,
                            "hr_min": hr, "hr_max": hr, "spo2_min": sp, "data_source": r.data_source}
                    ep["end_ms"] = ts
                    ep["n_readings"] += 1
                    if sv > STATUS_NUM[ep["severity"]]:
                        ep["severity"] = sev
                    if hr is not None:
                        ep["hr_min"] = hr if ep["hr_min"] is None else min(ep["hr_min"], hr)
                        ep["hr_max"] = hr if ep["hr_max"] is None else max(ep["hr_max"], hr)
                    if sp is not None:
                        ep["spo2_min"] = sp if ep["spo2_min"] is None else min(ep["spo2_min"], sp)
            for code in list(open_eps):
                close(code)
        out = []
        for e in eps:
            if STATUS_NUM[e["severity"]] < min_sev:
                continue
            if category and e["category"] != category:
                continue
            e["duration_s"] = round((e["end_ms"] - e["start_ms"]) / 1000.0, 1)
            out.append(e)
        out.sort(key=lambda e: e["start_ms"], reverse=True)
        return out[:limit]
