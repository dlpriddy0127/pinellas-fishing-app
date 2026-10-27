"""Fishing App 2.0 - water clarity nowcast, version 0.1 (PROVISIONAL).

Pure Python (standard library only) so it can be ported to the app's language later.
All numbers come from model_params.json, built by build_params.py.

Public functions
    load_params(path)
    nowcast(params, site_id, as_of, waves, rain, wind, last_satellite=None)  -> dict
    trend_24h(params, site_id, as_of, waves, rain, wind, last_satellite=None, forecast_waves=None) -> str

Inputs (all times are UTC, naive datetimes on the hour)
    waves : list of (time, Hs_m, Tp_s)   hourly, buoy 42098, at least the last 14 days
    rain  : list of (time, mm)           hourly precipitation, at least the last 14 days
    wind  : list of (time, m/s)          hourly 10 m wind speed, at least the last 14 days
    last_satellite : optional (date, Kd490) newest cloud-free satellite value near the site
"""
import json, math, bisect
from datetime import datetime, timedelta

G = 9.81
NAMES = ["Muddy", "Murky", "Fair", "Clear", "Crystal"]
LOOKBACK_H = 14 * 24


def load_params(path="model_params.json"):
    return json.load(open(path))


def _orbital_velocity(hs, tp, depth):
    if hs is None or tp is None or hs != hs or tp != tp or tp <= 1:
        return None
    om = 2 * math.pi / tp
    k = om * om / G
    for _ in range(40):
        k = om * om / (G * math.tanh(k * depth))
    return math.pi * hs / (tp * math.sinh(k * depth))


def _hourly_grid(series, as_of, max_gap_h=6):
    """series: list of (time, value) -> dict hour_index(-LOOKBACK..0) -> value, forward-filled over short gaps."""
    by_time = {t: v for t, v in series if v is not None and v == v}
    out, last, gap = {}, None, 0
    for i in range(-LOOKBACK_H, 1):
        t = as_of + timedelta(hours=i)
        if t in by_time:
            last, gap = by_time[t], 0
            out[i] = last
        elif last is not None and gap < max_gap_h:
            gap += 1
            out[i] = last
    return out


def stirring(params, waves, as_of):
    """Wave-stirring stores at as_of. Returns (fast, slow, coverage_last_72h)."""
    sp = params["stirring"]
    wv = {t: (hs, tp) for t, hs, tp in waves}
    fast = slow = 0.0
    af, as_ = math.exp(-1 / sp["tau_fast_h"]), math.exp(-1 / sp["tau_slow_h"])
    have, total, last_ub, gap = 0, 0, None, 0
    for i in range(-LOOKBACK_H, 1):
        t = as_of + timedelta(hours=i)
        ub = _orbital_velocity(*wv.get(t, (None, None)), sp["depth_m"])
        if ub is None and last_ub is not None and gap < 6:
            ub, gap = last_ub, gap + 1
        elif ub is not None:
            last_ub, gap = ub, 0
        ex = max(0.0, ub - sp["u_crit_ms"]) if ub is not None else 0.0
        fast, slow = fast * af + ex, slow * as_ + ex
        if i > -72:
            total += 1
            have += ub is not None
    return fast, slow, have / max(total, 1)


def _ewm(series, as_of, tau_h):
    g = _hourly_grid(series, as_of)
    a, acc = math.exp(-1 / tau_h), 0.0
    for i in range(-LOOKBACK_H, 1):
        acc = acc * a + (1 - a) * g.get(i, 0.0)
    return acc


def percentile_of(params, score):
    table = params["stirring"]["pctile_table_0_to_100"]
    return min(100, bisect.bisect_right(table, score) - 1 if score > table[0] else 0)


def baseline_lnkd(site, when):
    """Smooth monthly baseline: linear between month centres (day 15)."""
    b = site["baseline_lnkd"]
    d = when.day + when.hour / 24.0
    m = when.month - 1
    if d >= 15:
        a, c, f = m, (m + 1) % 12, (d - 15) / 30.0
    else:
        a, c, f = (m - 1) % 12, m, (d + 15) / 30.0
    return b[a] * (1 - f) + b[c] * f


def adjustment(params, site, as_of, waves, rain, wind):
    """Model-based departure from baseline (log Kd units) from recent waves, rain and wind."""
    fast, slow, cover = stirring(params, waves, as_of)
    f = {"stir_fast": math.log1p(fast), "stir_slow": math.log1p(slow),
         "rain": _ewm(rain, as_of, params["weather"]["tau_rain_h"]),
         "wind": _ewm(wind, as_of, params["weather"]["tau_wind_h"])}
    adj = site["intercept"] + sum(site["coef"][k] * (f[k] - site["feat_mean"][k]) / site["feat_sd"][k] for k in f)
    # no weather adjustment when buoy wave data is patchy: missing waves would otherwise read as calm water and push the estimate clearer
    return (adj if site["adjust_used"] and cover >= 0.6 else 0.0), fast, slow, cover


def _carry(params, age_days):
    rows = params["persistence"]["by_lag"]
    lag = max(1, int(round(age_days)))
    return max(0.0, rows[lag - 1][2]) if lag <= len(rows) else 0.0


def _class_from_kd(params, kd):
    cut = params["classes"]["kd_cutoffs_high_to_low"]
    return 0 if kd > cut[0] else 1 if kd > cut[1] else 2 if kd > cut[2] else 3 if kd > cut[3] else 4


def tide_level_m(params, when):
    t = params["tide"]
    t0 = datetime.strptime(t["t0"], "%Y-%m-%dT%H:%M:%SZ")
    h = (when - t0).total_seconds() / 3600.0
    c, tot = t["coef"], t["coef"][0]
    for i, p in enumerate(t["periods_h"]):
        om = 2 * math.pi / p
        tot += c[1 + 2 * i] * math.cos(om * h) + c[2 + 2 * i] * math.sin(om * h)
    return tot


def nowcast(params, site_id, as_of, waves, rain, wind, last_satellite=None, use_override=True):
    site = params["sites"][site_id]
    why, tags = [], []
    adj, fast, slow, cover = adjustment(params, site, as_of, waves, rain, wind)
    lnkd = baseline_lnkd(site, as_of) + adj
    base_cls = _class_from_kd(params, math.exp(baseline_lnkd(site, as_of)))
    why.append(f"Typical for {NAMES[base_cls]} this time of year")
    if site["adjust_used"] and abs(adj) >= 0.05:
        why.append(f"Recent waves/wind/rain push it {'murkier' if adj > 0 else 'clearer'} (log Kd {adj:+.2f})")
    sat_age = None
    if last_satellite is not None:
        sat_date, kd_sat = last_satellite
        sat_age = (as_of - datetime(sat_date.year, sat_date.month, sat_date.day, 18)).total_seconds() / 86400.0
        if 0 <= sat_age <= 7:
            t_sat = datetime(sat_date.year, sat_date.month, sat_date.day, 18)
            adj_sat = adjustment(params, site, t_sat, waves, rain, wind)[0]
            resid = math.log(max(kd_sat, 0.03)) - baseline_lnkd(site, t_sat) - adj_sat
            w = _carry(params, sat_age)
            lnkd += w * resid
            why.append(f"Satellite reading {sat_age:.0f} day(s) old carries {w:.0%} of its surprise")
    kd = math.exp(lnkd)
    cls = _class_from_kd(params, kd)
    score = fast + slow
    pct = percentile_of(params, score)
    sp = params["stirring"]
    overridden = False
    if use_override and site["gulf_facing"]:
        if pct >= sp["override_muddy_pct"]:
            cls, overridden = 0, True
            why.append(f"Storm rule: wave stirring is in the top {100 - pct}% of the 3-year record -> Muddy")
            tags.append("Storm aftermath")
        elif pct >= sp["override_murky_pct"] and cls > 1:
            cls, overridden = 1, True
            why.append(f"Rough-day rule: wave stirring is in the top {100 - pct}% -> no better than Murky")
            tags.append("Rough water")
    if site_id in ("johns_pass", "skyway_pier"):
        try:
            nxt, prv = tide_level_m(params, as_of + timedelta(minutes=30)), tide_level_m(params, as_of - timedelta(minutes=30))
            tags.append("Tide: rising" if nxt > prv else "Tide: falling")
        except Exception:
            pass
    if cover < 0.6:
        conf = "low"; why.append("Wave data from the buoy is patchy")
    elif overridden:
        conf = "medium"
    elif sat_age is not None and 0 <= sat_age <= 2:
        conf = "high" if site["adjust_used"] else "medium"
    elif sat_age is not None and sat_age <= 5:
        conf = "medium"
    else:
        conf = "low"
    return {"site": site_id, "as_of": as_of.isoformat(), "class": NAMES[cls], "class_index": cls, "kd490_estimate": round(kd, 3),
            "confidence": conf, "tags": tags, "why": why, "stirring_percentile": pct, "buoy_coverage_72h": round(cover, 2),
            "status": params["status"]}


def trend_24h(params, site_id, as_of, waves, rain, wind, last_satellite=None, forecast_waves=None):
    """improving / holding / worsening over the next 24 h. forecast_waves, if given, extends the wave list; otherwise no new wave energy is assumed."""
    now = nowcast(params, site_id, as_of, waves, rain, wind, last_satellite)
    later_t = as_of + timedelta(hours=24)
    w2 = list(waves) + list(forecast_waves or [(as_of + timedelta(hours=h), 0.1, 4.0) for h in range(1, 25)])
    later = nowcast(params, site_id, later_t, w2, rain, wind, last_satellite)
    d = later["kd490_estimate"] / now["kd490_estimate"]
    if later["class_index"] > now["class_index"] or d < 0.9: return "improving"
    if later["class_index"] < now["class_index"] or d > 1.1: return "worsening"
    return "holding"
