import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import weather_ensemble as wx


def test_dist_center_and_monotonic():
    d = wx.Dist([(98, 1), (100, 1), (102, 1)], wx._lead_kernel(24))
    assert abs(d.center - 100.0) < 1e-9
    assert d.prob_at_least(95) > d.prob_at_least(100) > d.prob_at_least(105)


def test_disagreement_widens_tails():
    wide = wx.Dist([(90, 1), (110, 1)], wx._lead_kernel(24))     # models disagree
    tight = wx.Dist([(100, 1), (100, 1)], wx._lead_kernel(24))   # models agree
    # same center (100) but the disagreeing set assigns more probability to a tail
    assert wide.prob_at_least(106) > tight.prob_at_least(106)


def test_empty_dist_is_none():
    assert wx.Dist([], wx._lead_kernel(24)).prob_at_least(100) is None
    assert wx.Dist([], wx._lead_kernel(24)).center is None


def test_forecast_pools_all_sources(monkeypatch):
    monkeypatch.setattr(wx, "fetch_openmeteo_models", lambda la, lo, d: {
        "gfs_seamless": {"max": 100.0, "min": 70.0},
        "icon_seamless": {"max": 104.0, "min": 72.0},
        "ecmwf_ifs025": {"max": 102.0, "min": 71.0}})
    monkeypatch.setattr(wx, "fetch_gfs_ensemble", lambda la, lo, d: {
        "max": [99.0, 101.0, 103.0], "min": [69.0, 71.0, 73.0]})
    monkeypatch.setattr(wx, "fetch_nws", lambda la, lo, d: {"max": 103.0, "min": 72.0})
    monkeypatch.setattr(wx, "_fetch_keyed", lambda la, lo, d: {})
    fc = wx.forecast("phoenix", "2026-07-05", 33.4, -112.0, 24, log=False)
    # 3 deterministic + nws + gfs_ens = 5 named sources
    assert fc["n_sources"] == 5
    assert 100 <= fc["max"].center <= 104
    p = fc["max"].prob_at_least(102)
    assert 0.2 < p < 0.8            # genuinely uncertain, not a 0/1 megabet


def test_forecast_graceful_when_all_down(monkeypatch):
    monkeypatch.setattr(wx, "fetch_openmeteo_models", lambda la, lo, d: {})
    monkeypatch.setattr(wx, "fetch_gfs_ensemble", lambda la, lo, d: None)
    monkeypatch.setattr(wx, "fetch_nws", lambda la, lo, d: {"max": None, "min": None})
    monkeypatch.setattr(wx, "_fetch_keyed", lambda la, lo, d: {})
    fc = wx.forecast("x", "2026-07-05", 1.0, 2.0, 24, log=False)
    assert fc["n_sources"] == 0 and not fc["max"].ok()
    pr, ctr, n = wx.prob("x", "2026-07-05", 1.0, 2.0, 100, False, log=False)
    assert pr is None and n == 0


def test_keyed_sources_skipped_without_keys(monkeypatch):
    for k in ("WEATHERAPI_KEY", "VISUALCROSSING_KEY", "TOMORROW_KEY"):
        monkeypatch.setattr(os, "environ", {k2: v for k2, v in os.environ.items() if k2 != k}) \
            if False else os.environ.pop(k, None)
    assert wx._fetch_keyed(33.4, -112.0, "2026-07-05") == {}


def _run():
    import traceback
    class _MP:
        def setattr(self, obj, name, val): setattr(obj, name, val)
    names = sorted(n for n in globals() if n.startswith("test_"))
    p = 0
    for n in names:
        try:
            fn = globals()[n]
            fn(_MP()) if fn.__code__.co_argcount else fn()
            print("PASS " + n); p += 1
        except Exception:
            print("FAIL " + n); traceback.print_exc()
    print("%d/%d ensemble tests passed" % (p, len(names)))
    return 0 if p == len(names) else 1


if __name__ == "__main__":
    sys.exit(_run())
