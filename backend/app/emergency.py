"""Emergency notifier.

When an abnormal episode (low SpO2, dangerous heart rate, long ECG pause, ...)
opens and reaches the configured severity, this sends the patient's emergency
contact an SMS - and optionally a voice call - that includes the patient's
location as a Google Maps link.

Two providers:
  * Twilio, used when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER
    are set and the `twilio` package is installed.
  * a simulated provider (the default) that records the message to
    data/notifications.log and the in-memory feed, so the whole flow is
    demoable with no account and no cost.

Sends run on a small thread pool so the live pipeline is never blocked, and each
(patient, alert-code) episode only notifies once per cooldown window.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .detection import SEVERITY
from .patients import PatientProfile
from .settings import Settings

log = logging.getLogger("emergency")

# Only these categories represent the patient's condition; sensor/signal notices
# (leads off, no finger, no data) never trigger an emergency call-out.
NOTIFY_CATEGORIES = ("vital", "ecg_heuristic")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BaseProvider:
    name = "base"
    configured = False

    def send_sms(self, to: str, body: str) -> tuple[bool, str]:
        raise NotImplementedError

    def place_call(self, to: str, body: str) -> tuple[bool, str]:
        raise NotImplementedError


class SimulatedProvider(BaseProvider):
    """No network. Writes the message to a log file so a demo can show it."""
    name = "simulated"
    configured = True

    def __init__(self, log_path):
        self.log_path = log_path

    def _write(self, kind: str, to: str, body: str) -> tuple[bool, str]:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{_iso(time.time())}] {kind.upper()} -> {to}\n{body}\n\n")
        except Exception:  # logging must never break a send
            pass
        log.warning("[SIMULATED %s] to %s: %s", kind, to, body.replace("\n", " "))
        return True, "simulated"

    def send_sms(self, to: str, body: str) -> tuple[bool, str]:
        return self._write("sms", to, body)

    def place_call(self, to: str, body: str) -> tuple[bool, str]:
        return self._write("call", to, body)


class TwilioProvider(BaseProvider):
    name = "twilio"

    def __init__(self, sid: str, token: str, from_number: str):
        self.from_number = from_number
        self.configured = False
        self._client = None
        try:
            from twilio.rest import Client  # type: ignore
            self._client = Client(sid, token)
            self.configured = bool(sid and token and from_number)
        except Exception as e:  # package missing or bad creds
            log.warning("Twilio not available (%s); falling back to simulated notifier", e)

    def send_sms(self, to: str, body: str) -> tuple[bool, str]:
        try:
            msg = self._client.messages.create(to=to, from_=self.from_number, body=body)
            return True, getattr(msg, "sid", "sent")
        except Exception as e:
            return False, str(e)

    def place_call(self, to: str, body: str) -> tuple[bool, str]:
        try:
            # Minimal TwiML: speak the alert text.
            safe = body.replace("&", "and").replace("<", "").replace(">", "")
            twiml = f"<Response><Say voice=\"alice\">{safe}</Say></Response>"
            call = self._client.calls.create(to=to, from_=self.from_number, twiml=twiml)
            return True, getattr(call, "sid", "called")
        except Exception as e:
            return False, str(e)


def build_provider(s: Settings) -> BaseProvider:
    if s.twilio_sid and s.twilio_token and s.twilio_from:
        p = TwilioProvider(s.twilio_sid, s.twilio_token, s.twilio_from)
        if p.configured:
            log.info("Emergency notifier: Twilio provider active (from %s)", s.twilio_from)
            return p
    log.info("Emergency notifier: simulated provider (no Twilio creds; messages logged, not sent)")
    return SimulatedProvider(s.notifications_log)


class EmergencyNotifier:
    def __init__(self, settings: Settings, get_profile, max_keep: int = 200):
        self.s = settings
        self.get_profile = get_profile          # callable(pid) -> PatientProfile | None
        self.provider = build_provider(settings)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
        self._lock = threading.Lock()
        self._last_sent: "OrderedDict[tuple, float]" = OrderedDict()  # (pid, code) -> ts
        self.feed: deque[dict] = deque(maxlen=max_keep)
        self._seq = 0

    # ---------- message ----------
    def compose(self, ep: dict, prof: PatientProfile) -> str:
        name = prof.name or ep.get("patient_id", "patient")
        bits = [f"CardioWatch ALERT: {name}", ep.get("message") or ep.get("code", "abnormal reading")]
        vit = []
        if ep.get("hr_bpm") is not None:
            vit.append(f"HR {ep['hr_bpm']:.0f}")
        if ep.get("spo2_pct") is not None:
            vit.append(f"SpO2 {ep['spo2_pct']:.0f}%")
        if vit:
            bits.append(", ".join(vit))
        url = prof.location.maps_url()
        bits.append(f"Location: {url}" if url else f"Location: {prof.location.describe()}")
        bits.append("Automated alert - not a medical diagnosis.")
        return "\n".join(bits)

    # ---------- eligibility + dispatch ----------
    def maybe_notify(self, ep: dict, now: float | None = None) -> dict | None:
        """Call for every *active* alert episode. Returns a record if a send was
        triggered, else None (ineligible, deduped, or within cooldown)."""
        now = now or time.time()
        if ep.get("status") != "active":
            return None
        if ep.get("category") not in NOTIFY_CATEGORIES:
            return None
        pid = ep.get("patient_id")
        prof = self.get_profile(pid) if pid else None
        if prof is None:
            return None
        em = prof.emergency
        if not em.notify_enabled or not em.contact_phone:
            return None
        if SEVERITY.get(ep.get("severity", "warning"), 1) < SEVERITY.get(em.min_severity, 2):
            return None
        key = (pid, ep.get("code"))
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < em.cooldown_s:
                return None
            self._last_sent[key] = now
            while len(self._last_sent) > 2000:
                self._last_sent.popitem(last=False)
            self._seq += 1
            rec = {
                "id": f"N{self._seq:06d}", "ts": _iso(now), "ts_ms": int(now * 1000),
                "patient_id": pid, "patient_name": prof.name or pid, "code": ep.get("code"),
                "severity": ep.get("severity"), "to": em.contact_phone,
                "contact_name": em.contact_name, "channel": "sms", "status": "sending",
                "provider": self.provider.name, "maps_url": prof.location.maps_url(),
                "message": self.compose(ep, prof), "detail": "", "call": bool(em.call_enabled),
                "simulated": self.provider.name == "simulated",
            }
            self.feed.append(rec)
        self._pool.submit(self._dispatch, rec, em.call_enabled)
        return rec

    def _dispatch(self, rec: dict, call: bool) -> None:
        ok, detail = self.provider.send_sms(rec["to"], rec["message"])
        rec["status"] = "sent" if ok else "failed"
        rec["detail"] = detail
        if call and ok:
            cok, cdetail = self.provider.place_call(rec["to"], rec["message"])
            rec["channel"] = "sms+call"
            rec["detail"] = f"{detail}; call: {cdetail if cok else 'failed ' + cdetail}"

    # ---------- dashboard helpers ----------
    def recent(self, n: int = 50) -> list[dict]:
        return list(self.feed)[-n:][::-1]

    def send_test(self, pid: str) -> dict:
        """Dashboard 'send test alert' button: bypasses cooldown + severity gate."""
        prof = self.get_profile(pid)
        if prof is None:
            return {"ok": False, "error": "unknown patient"}
        em = prof.emergency
        if not em.contact_phone:
            return {"ok": False, "error": "no emergency contact phone set for this patient"}
        now = time.time()
        with self._lock:
            self._seq += 1
            url = prof.location.maps_url()
            body = (f"CardioWatch TEST alert for {prof.name or pid}. "
                    f"This is a test of the emergency notification. "
                    + (f"Location: {url}" if url else f"Location: {prof.location.describe()}"))
            rec = {
                "id": f"N{self._seq:06d}", "ts": _iso(now), "ts_ms": int(now * 1000),
                "patient_id": pid, "patient_name": prof.name or pid, "code": "TEST",
                "severity": "test", "to": em.contact_phone, "contact_name": em.contact_name,
                "channel": "sms", "status": "sending", "provider": self.provider.name,
                "maps_url": url, "message": body, "detail": "", "call": False,
                "simulated": self.provider.name == "simulated",
            }
            self.feed.append(rec)
        self._dispatch(rec, False)
        return {"ok": rec["status"] == "sent", "notification": rec}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
