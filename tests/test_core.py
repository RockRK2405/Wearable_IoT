import numpy as np
import pytest

from backend.app.detection import Measurements, evaluate
from backend.app.ecg_processing import analyze
from backend.app.ecg_synth import add_noise, next_rr, render
from backend.app.protocol import ProtocolError, parse_message
from backend.app.thresholds import ThresholdStore, Thresholds

FS = 250


def make_ecg(hr=72, cv=0.04, seconds=8, noise=0.015, pause_at=None, seed=1):
    rng = np.random.default_rng(seed)
    t, beats, prev, i = 0.3, [], 60.0 / hr, 0
    while t < seconds + 1:
        rr = 3.0 if i == pause_at else next_rr(rng, hr, cv, t)
        beats.append((t, prev))
        prev = rr
        t += rr
        i += 1
    ts = np.arange(seconds * FS) / FS
    return add_noise(render(ts, beats, 0.8), ts, rng, noise)


@pytest.mark.parametrize("hr", [48, 72, 110, 140])
def test_hr_estimation(hr):
    m = analyze(make_ecg(hr=hr), FS)
    assert m.hr_bpm == pytest.approx(hr, abs=4)
    assert m.quality > 0.7


def test_irregular_has_high_cv_and_regular_low():
    reg = analyze(make_ecg(cv=0.04), FS)
    irr = analyze(make_ecg(hr=90, cv=0.3, seed=3), FS)
    assert reg.rr_cv < 0.10
    assert irr.rr_cv > 0.15


def test_pause_detected():
    m = analyze(make_ecg(pause_at=4, seconds=10), FS)
    assert m.max_rr_ms > 2500


def test_flat_signal_quality_zero():
    assert analyze(np.zeros(2000), FS).quality == 0.0


def test_noise_lowers_quality():
    assert analyze(make_ecg(noise=0.4), FS).quality < 0.5


TH = Thresholds()


def meas(**kw):
    base = dict(ecg_state="ok", vitals_state="ok", ecg_hr=72, n_beats=10, rr_cv=0.04, max_rr_ms=900,
                ecg_quality=0.9, pulse_bpm=72, spo2=98, finger=True)
    base.update(kw)
    return Measurements(**base)


def codes(ev):
    return {f.code: f.severity for f in ev.findings}


def test_normal():
    ev = evaluate(meas(), TH)
    assert ev.status == "normal" and not ev.findings and ev.hr_source == "ecg"


def test_hr_thresholds():
    assert codes(evaluate(meas(ecg_hr=105), TH)) == {"HR_HIGH": "warning"}
    assert codes(evaluate(meas(ecg_hr=140), TH)) == {"HR_HIGH": "critical"}
    assert codes(evaluate(meas(ecg_hr=45), TH)) == {"HR_LOW": "warning"}
    assert codes(evaluate(meas(ecg_hr=35), TH)) == {"HR_LOW": "critical"}


def test_spo2_thresholds():
    assert codes(evaluate(meas(spo2=93), TH)) == {"SPO2_LOW": "warning"}
    assert codes(evaluate(meas(spo2=88), TH)) == {"SPO2_LOW": "critical"}


def test_ecg_heuristics_labelled():
    ev = evaluate(meas(rr_cv=0.2), TH)
    assert codes(ev) == {"ECG_IRREGULAR": "warning"} and ev.ecg_irregular
    assert "not a diagnosis" in ev.findings[0].message
    assert codes(evaluate(meas(max_rr_ms=3200), TH))["ECG_PAUSE"] == "critical"


def test_sensor_fallbacks():
    ev = evaluate(meas(ecg_state="leads_off", ecg_hr=None, ecg_quality=None, pulse_bpm=120), TH)
    assert ev.hr_source == "ppg" and "LEADS_OFF" in codes(ev) and "HR_HIGH" in codes(ev)
    ev = evaluate(meas(finger=False, spo2=None), TH)
    assert "NO_FINGER" in codes(ev) and "SPO2_LOW" not in codes(ev)
    ev = evaluate(meas(ecg_quality=0.2, rr_cv=0.5), TH)
    assert "ECG_POOR_SIGNAL" in codes(ev) and "ECG_IRREGULAR" not in codes(ev)


def test_threshold_validation_and_persistence(tmp_path):
    st = ThresholdStore(tmp_path / "t.json")
    st.update({"heart_rate": {"warn_high": 110}})
    assert ThresholdStore(tmp_path / "t.json").get().heart_rate.warn_high == 110
    with pytest.raises(Exception):
        st.update({"heart_rate": {"warn_high": 30}})


def test_protocol():
    m = parse_message('{"type":"ecg","dev":"d","fs":250,"lo":0,"s":[1,2,5000]}')
    assert m["s"] == [1, 2, 4095] and m["sim"] is False
    assert parse_message("# debug text") is None
    assert parse_message('{"type":"vitals","spo2":97,"spo2_valid":0,"pulse":70,"finger":1}')["spo2"] is None
    for bad in ["not json", '{"type":"ecg","s":[]}', '{"type":"x"}', '{"type":"ecg","s":["a"]}']:
        with pytest.raises(ProtocolError):
            parse_message(bad)
