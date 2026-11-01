"""Threshold-based abnormality detection.

IMPORTANT: everything here is a configurable *threshold / heuristic screening rule*.
ECG rhythm flags (irregular R-R, long pause) are NOT a clinically validated diagnosis.
"""
from __future__ import annotations

from dataclasses import dataclass

from .seizure_ml import predict_seizure
from .thresholds import Thresholds

SEVERITY = {"normal": 0, "warning": 1, "critical": 2}

# code -> (category, short description)
CODE_INFO = {
    "HR_HIGH": ("vital", "Heart rate above limit"),
    "HR_LOW": ("vital", "Heart rate below limit"),
    "SPO2_LOW": ("vital", "SpO2 below limit"),
    "TEMP_HIGH": ("vital", "Body temperature above limit (fever)"),
    "TEMP_LOW": ("vital", "Body temperature below limit (hypothermia)"),
    "SEIZURE_CLONIC": ("seizure", "Clonic seizure detected (tremors + tachycardia + desaturation)"),
    "FEBRILE_SEIZURE": ("seizure", "Febrile seizure risk (high fever + tremor)"),
    "TREMOR_DETECTED": ("seizure", "Abnormal rhythmic muscle tremor detected"),
    "ECG_IRREGULAR": ("ecg_heuristic", "Irregular R-R intervals (heuristic)"),
    "ECG_PAUSE": ("ecg_heuristic", "Long R-R pause (heuristic)"),
    "LEADS_OFF": ("sensor", "ECG leads off"),
    "ECG_POOR_SIGNAL": ("sensor", "Poor ECG signal quality"),
    "NO_FINGER": ("sensor", "No finger on SpO2 sensor"),
    "NO_ECG_DATA": ("sensor", "No ECG data from device"),
    "NO_PPG_DATA": ("sensor", "No SpO2/pulse data from device"),
}
NOTES = {
    "vital": "Threshold alert on a configurable limit - not a diagnosis.",
    "ecg_heuristic": "Heuristic R-R flag - not a clinically validated ECG diagnosis.",
    "seizure": "Epilepsy / seizure screening alert based on multi-sensor physiological markers and ML inference.",
    "sensor": "Sensor / signal-quality notice - readings may be unreliable.",
}


def category_of(code: str) -> str:
    return CODE_INFO.get(code, ("sensor", ""))[0]


@dataclass
class Measurements:
    ecg_state: str = "ok"      # ok | leads_off | warming | stale
    vitals_state: str = "ok"   # ok | stale
    ecg_hr: float | None = None
    n_beats: int = 0
    rr_cv: float | None = None
    max_rr_ms: float | None = None
    ecg_quality: float | None = None
    pulse_bpm: float | None = None
    spo2: float | None = None
    finger: bool | None = None
    temperature_c: float | None = None
    vibration_intensity: float | None = None
    rmssd_ms: float | None = None


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    value: float | None = None
    limit: float | None = None

    @property
    def category(self) -> str:
        return category_of(self.code)

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "category": self.category,
                "message": self.message, "value": self.value, "limit": self.limit,
                "note": NOTES[self.category]}


@dataclass
class Evaluation:
    status: str
    findings: list[Finding]
    hr_bpm: float | None
    hr_source: str
    ecg_irregular: bool
    seizure_detected: bool = False
    seizure_prob: float = 0.0
    seizure_class: str = "normal"


def format_codes(findings: list[Finding]) -> str:
    return "|".join(f"{f.code}:{f.severity}" for f in findings)


def parse_codes(s) -> list[tuple[str, str]]:
    if not isinstance(s, str) or not s:
        return []
    out = []
    for part in s.split("|"):
        code, _, sev = part.partition(":")
        out.append((code, sev or "warning"))
    return out


def evaluate(m: Measurements, th: Thresholds) -> Evaluation:
    F: list[Finding] = []
    hr, sp, ec = th.heart_rate, th.spo2, th.ecg

    # ---- sensor / signal state ----
    if m.ecg_state == "stale":
        F.append(Finding("NO_ECG_DATA", "warning", "No ECG data received from the device."))
    elif m.ecg_state == "leads_off":
        F.append(Finding("LEADS_OFF", "warning", "ECG leads off - attach electrodes. ECG-derived values unavailable."))
    ecg_good = False
    if m.ecg_state == "ok" and m.ecg_quality is not None:
        if m.ecg_quality < ec.min_quality:
            F.append(Finding("ECG_POOR_SIGNAL", "warning",
                             f"Poor ECG signal quality ({m.ecg_quality:.2f}); ECG-derived values ignored.",
                             m.ecg_quality, ec.min_quality))
        else:
            ecg_good = True

    if m.vitals_state == "stale":
        F.append(Finding("NO_PPG_DATA", "warning", "No data from the SpO2/pulse sensor."))
    elif m.finger is False:
        F.append(Finding("NO_FINGER", "warning", "No finger detected on SpO2 sensor - SpO2/pulse unavailable."))
    ppg_ok = m.vitals_state == "ok" and m.finger is not False

    # ---- heart rate selection ----
    hr_val, src = None, "none"
    if ecg_good and m.ecg_hr and hr.valid_min <= m.ecg_hr <= hr.valid_max:
        hr_val, src = m.ecg_hr, "ecg"
    elif ppg_ok and m.pulse_bpm and hr.valid_min <= m.pulse_bpm <= hr.valid_max:
        hr_val, src = m.pulse_bpm, "ppg"

    if hr_val is not None:
        via = "" if src == "ecg" else " (pulse from SpO2 sensor)"
        if hr_val < hr.crit_low:
            F.append(Finding("HR_LOW", "critical", f"Heart rate {hr_val:.0f} bpm below {hr.crit_low:.0f}{via}", hr_val, hr.crit_low))
        elif hr_val < hr.warn_low:
            F.append(Finding("HR_LOW", "warning", f"Heart rate {hr_val:.0f} bpm below {hr.warn_low:.0f}{via}", hr_val, hr.warn_low))
        elif hr_val > hr.crit_high:
            F.append(Finding("HR_HIGH", "critical", f"Heart rate {hr_val:.0f} bpm above {hr.crit_high:.0f}{via}", hr_val, hr.crit_high))
        elif hr_val > hr.warn_high:
            F.append(Finding("HR_HIGH", "warning", f"Heart rate {hr_val:.0f} bpm above {hr.warn_high:.0f}{via}", hr_val, hr.warn_high))

    # ---- SpO2 ----
    if ppg_ok and m.spo2 is not None:
        if m.spo2 < sp.crit_below:
            F.append(Finding("SPO2_LOW", "critical", f"SpO2 {m.spo2:.0f}% below {sp.crit_below:.0f}%", m.spo2, sp.crit_below))
        elif m.spo2 < sp.warn_below:
            F.append(Finding("SPO2_LOW", "warning", f"SpO2 {m.spo2:.0f}% below {sp.warn_below:.0f}%", m.spo2, sp.warn_below))

    # ---- ECG heuristics (not diagnoses) ----
    irregular = False
    if ecg_good:
        if m.max_rr_ms is not None and m.max_rr_ms >= ec.pause_warn_ms:
            sev = "critical" if m.max_rr_ms >= ec.pause_crit_ms else "warning"
            lim = ec.pause_crit_ms if sev == "critical" else ec.pause_warn_ms
            F.append(Finding("ECG_PAUSE", sev, f"Long R-R gap of {m.max_rr_ms / 1000:.1f} s detected (heuristic flag, not a diagnosis)", m.max_rr_ms, lim))
            irregular = True
        if m.n_beats >= ec.min_beats and m.rr_cv is not None and m.rr_cv > ec.rr_cv_warn:
            sev = "critical" if m.rr_cv > ec.rr_cv_crit else "warning"
            lim = ec.rr_cv_crit if sev == "critical" else ec.rr_cv_warn
            F.append(Finding("ECG_IRREGULAR", sev, f"Irregular R-R intervals (variability {m.rr_cv:.2f}) - heuristic flag, not a diagnosis", m.rr_cv, lim))
            irregular = True

    # ---- Temperature ----
    ep_th = getattr(th, "epilepsy", None)
    if m.temperature_c is not None and ep_th is not None:
        tc = m.temperature_c
        if tc >= ep_th.temp_crit_high:
            F.append(Finding("TEMP_HIGH", "critical", f"Critical high fever {tc:.1f}°C above {ep_th.temp_crit_high:.1f}°C", tc, ep_th.temp_crit_high))
        elif tc >= ep_th.temp_warn_high:
            F.append(Finding("TEMP_HIGH", "warning", f"Elevated temperature {tc:.1f}°C above {ep_th.temp_warn_high:.1f}°C", tc, ep_th.temp_warn_high))
        elif tc <= ep_th.temp_warn_low:
            F.append(Finding("TEMP_LOW", "warning", f"Low temperature {tc:.1f}°C below {ep_th.temp_warn_low:.1f}°C", tc, ep_th.temp_warn_low))

    # ---- Vibration / Tremors ----
    if m.vibration_intensity is not None and ep_th is not None:
        vib = m.vibration_intensity
        if vib >= ep_th.vibration_tremor_crit:
            F.append(Finding("TREMOR_DETECTED", "critical", f"Intense muscle tremor/vibration {vib:.2f} m/s² above {ep_th.vibration_tremor_crit:.2f} m/s²", vib, ep_th.vibration_tremor_crit))
        elif vib >= ep_th.vibration_tremor_warn:
            F.append(Finding("TREMOR_DETECTED", "warning", f"Abnormal muscle tremor {vib:.2f} m/s² above {ep_th.vibration_tremor_warn:.2f} m/s²", vib, ep_th.vibration_tremor_warn))

    # ---- Machine Learning Seizure Detection ----
    seizure_detected = False
    seizure_prob = 0.0
    seizure_class = "normal"
    prob_th = ep_th.seizure_prob_threshold if ep_th else 0.60
    if m.vibration_intensity is not None or hr_val is not None:
        try:
            pred = predict_seizure(
                heart_rate=hr_val,
                vibration_intensity=m.vibration_intensity,
                spo2=m.spo2 if ppg_ok else None,
                temperature_c=m.temperature_c,
                rmssd_ms=m.rmssd_ms,
                threshold=prob_th,
            )
            seizure_prob = pred.probability
            seizure_class = pred.classification
            if pred.is_seizure:
                seizure_detected = True
                code = "FEBRILE_SEIZURE" if pred.classification == "febrile_seizure" else "SEIZURE_CLONIC"
                F.append(Finding(code, "critical", pred.explanation, value=pred.probability, limit=prob_th))
        except Exception:
            pass

    status = "normal"
    for f in F:
        if SEVERITY[f.severity] > SEVERITY[status]:
            status = f.severity
    return Evaluation(status, F, hr_val, src, irregular, seizure_detected, seizure_prob, seizure_class)
