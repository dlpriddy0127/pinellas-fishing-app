#!/usr/bin/env python3
"""Scheduled job: fetch live data, run the water clarity model for every site, write clarity.json.

Run by GitHub Actions every 3 hours (see .github/workflows/update-clarity.yml). Standard library only.
Usage:  python update_clarity.py [--offline] [--as-of 2026-10-12T19]
  --offline  skip all network calls (writes typical-condition estimates only; used for the first placeholder file)
"""
import csv, io, json, math, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
import clarity_model as cm

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS = cm.load_params(os.path.join(HERE, "model_params.json"))
UA = {"User-Agent": "pinellas-fishing-forecaster (github actions)"}
BUOY_URL = "https://www.ndbc.noaa.gov/data/realtime2/42098.txt"
# Live (near-real-time) satellite clarity sources, tried in order until one answers. The server sometimes drops a dataset for a while.
SAT_CANDIDATES = [("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNkd490Daily_Lon0360"),
                  ("https://coastwatch.pfeg.noaa.gov/erddap", "nesdisVHNkd490Daily"),
                  ("https://coastwatch.noaa.gov/erddap", "noaacwNPPVIIRSkd490Daily")]
MARINE_POINT = (27.60, -82.95)          # offshore point used for the 24 h wave outlook
# app card prefix -> model site id
APP_SITES = {"gandy": "gandy", "skyway": "skyway_pier", "johnspass": "johns_pass", "desotogulf": "desoto_gulf_pier",
             "desotobay": "desoto_bay_pier", "desotobeach": "desoto_north_beach", "desotoflats": "desoto_east_flats",
             "honeymoon": "honeymoon_north", "honeymoonbay": "honeymoon_bay_flats", "honeymoonsouth": "honeymoon_south"}


def http_get(url, timeout=60, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                      # network hiccup: wait and retry
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url[:80]}... failed: {last}")


# ---------------- buoy waves ----------------
def parse_buoy(text):
    """NDBC realtime2 standard met text -> list of (hour_utc, Hs_m, Tp_s), newest data included."""
    lines = [l for l in text.splitlines() if l.strip()]
    hdr = lines[0].lstrip("#").split()
    ix = {k: hdr.index(k) for k in ("WVHT", "DPD")}
    by_hour = {}
    for l in lines[1:]:
        if l.startswith("#"):
            continue
        p = l.split()
        try:
            y, mo, d, h = (int(x) for x in p[:4])
            y = y + 2000 if y < 100 else y
            hs, tp = p[ix["WVHT"]], p[ix["DPD"]]
            if hs in ("MM", "99.00", "99.0") or tp in ("MM", "99.00", "99.0"):
                continue
            by_hour.setdefault(datetime(y, mo, d, h), []).append((float(hs), float(tp)))
        except (ValueError, IndexError):
            continue
    return sorted((t, sum(a for a, _ in v) / len(v), sum(b for _, b in v) / len(v)) for t, v in by_hour.items())


# ---------------- rain and wind ----------------
def fetch_weather(lat, lon):
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": "precipitation,wind_speed_10m", "past_days": 14,
                                "forecast_days": 1, "wind_speed_unit": "ms", "timezone": "GMT"})
    j = json.loads(http_get("https://api.open-meteo.com/v1/forecast?" + q))
    h = j["hourly"]
    times = [datetime.strptime(t, "%Y-%m-%dT%H:%M") for t in h["time"]]
    rain = [(t, v) for t, v in zip(times, h["precipitation"])]
    wind = [(t, v) for t, v in zip(times, h["wind_speed_10m"])]
    return rain, wind


def fetch_marine_forecast(as_of):
    q = urllib.parse.urlencode({"latitude": MARINE_POINT[0], "longitude": MARINE_POINT[1], "hourly": "wave_height,wave_period",
                                "forecast_days": 3, "timezone": "GMT"})
    j = json.loads(http_get("https://marine-api.open-meteo.com/v1/marine?" + q))
    h = j["hourly"]
    out = []
    for t, hs, tp in zip(h["time"], h["wave_height"], h["wave_period"]):
        t = datetime.strptime(t, "%Y-%m-%dT%H:%M")
        if as_of < t <= as_of + timedelta(hours=24) and hs is not None and tp is not None:
            out.append((t, float(hs), float(tp)))
    return out


# ---------------- satellite ----------------
def erddap_info(server, dataset):
    rows = list(csv.DictReader(io.StringIO(http_get(f"{server}/info/{dataset}/index.csv"))))
    dims = [r["Variable Name"] for r in rows if r["Row Type"] == "dimension"]
    vars_ = [r["Variable Name"] for r in rows if r["Row Type"] == "variable"]
    var = next((v for v in vars_ if "kd" in v.lower() or "k490" in v.lower()), vars_[0])
    use360 = False
    for r in rows:
        if r["Variable Name"] == "longitude" and r["Attribute Name"] == "actual_range":
            try:
                use360 = float(r["Value"].split(",")[1]) > 180
            except (ValueError, IndexError):
                pass
    return {"server": server, "dataset": dataset, "dims": dims, "var": var, "use360": use360}


def fetch_satellite(lat, lon, src):
    """Newest cloud-free raw Kd490 at the pixel nearest (lat, lon) in the last week -> (date, kd) or None."""
    x = lon + 360 if src["use360"] and lon < 0 else lon
    parts = []
    for d in src["dims"]:
        if d == "time": parts.append("[(last-6):1:(last)]")
        elif d in ("altitude", "depth", "elevation"): parts.append("[(0.0):1:(0.0)]")
        elif d == "latitude": parts.append(f"[({lat}):1:({lat})]")
        elif d == "longitude": parts.append(f"[({x}):1:({x})]")
    text = http_get(f"{src['server']}/griddap/{src['dataset']}.csv?{src['var']}" + "".join(parts))
    rows = list(csv.reader(io.StringIO(text)))[2:]          # skip header and units
    best = None
    for r in rows:
        try:
            v = float(r[-1])
        except ValueError:
            continue
        if v > 0 and v == v:
            best = (datetime.strptime(r[0][:10], "%Y-%m-%d").date(), v)
    return best


def fetch_all_satellite(pixels, errors):
    """Try each live source in turn. Returns ({pixel: (date, raw_kd) or None}, dataset_name) or ({}, None)."""
    for server, ds in SAT_CANDIDATES:
        try:
            src = erddap_info(server, ds)
            return {pk: fetch_satellite(pk[0], pk[1], src) for pk in pixels}, ds
        except Exception as e:
            errors.append(f"satellite {ds}: {e}")
    return {}, None


def log_nrt(results, ds):
    """Keep a small running log of the live satellite values so they can be compared with the science-quality data later."""
    path = os.path.join(HERE, "nrt_log.csv")
    seen = set()
    if os.path.exists(path):
        seen = {(r["date"], r["pixel_lat"], r["pixel_lon"]) for r in csv.DictReader(open(path))}
    new = [(v[0].isoformat(), pk[0], pk[1], round(v[1], 4), ds) for pk, v in results.items() if v and (v[0].isoformat(), str(pk[0]), str(pk[1])) not in seen]
    if not new:
        return
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if write_header: w.writerow(["date", "pixel_lat", "pixel_lon", "kd490", "source"])
        w.writerows(new)


# ---------------- main ----------------
def main():
    offline = "--offline" in sys.argv
    if "--as-of" in sys.argv:
        as_of = datetime.strptime(sys.argv[sys.argv.index("--as-of") + 1], "%Y-%m-%dT%H")
    else:
        as_of = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    status = {"buoy_ok": False, "buoy_latest_utc": None, "weather_ok": False, "satellite_ok": False, "satellite_latest": None,
              "marine_forecast_ok": False, "errors": []}
    waves, forecast_waves, weather, sat = [], [], {}, {}
    if not offline:
        try:
            waves = [w for w in parse_buoy(http_get(BUOY_URL)) if w[0] <= as_of]
            status["buoy_ok"] = bool(waves); status["buoy_latest_utc"] = waves[-1][0].isoformat() + "Z" if waves else None
        except Exception as e:
            status["errors"].append(f"buoy: {e}")
        try:
            forecast_waves = fetch_marine_forecast(as_of); status["marine_forecast_ok"] = bool(forecast_waves)
        except Exception as e:
            status["errors"].append(f"marine forecast: {e}")
        cache_w = {}
        for sid, st in PARAMS["sites"].items():
            key = (round(st["lat"], 1), round(st["lon"], 1))
            try:
                if key not in cache_w:
                    cache_w[key] = fetch_weather(st["lat"], st["lon"])
                weather[sid] = cache_w[key]
            except Exception as e:
                status["errors"].append(f"weather {sid}: {e}")
        pixels = sorted({tuple(st["pixel"][:2]) for st in PARAMS["sites"].values()})
        sat_by_pixel, sat_ds = fetch_all_satellite(pixels, status["errors"])
        if sat_ds:
            try:
                log_nrt(sat_by_pixel, sat_ds)
            except Exception as e:
                status["errors"].append(f"nrt log: {e}")
        bias = math.exp(PARAMS.get("nrt_bias_ln", 0.0))       # correction once live vs science-quality data have been compared
        for sid, st in PARAMS["sites"].items():
            v = sat_by_pixel.get(tuple(st["pixel"][:2]))
            if v:
                sat[sid] = (v[0], v[1] * bias)
        status["satellite_source"] = sat_ds
        status["weather_ok"] = bool(weather); status["satellite_ok"] = bool(sat)
        status["satellite_latest"] = max((v[0] for v in sat.values()), default=None)
        status["satellite_latest"] = status["satellite_latest"].isoformat() if status["satellite_latest"] else None

    out_sites = {}
    for prefix, sid in APP_SITES.items():
        rain, wind = weather.get(sid, ([], []))
        last_sat = sat.get(sid)
        r = cm.nowcast(PARAMS, sid, as_of, waves, rain, wind, last_sat)
        try:
            trend = cm.trend_24h(PARAMS, sid, as_of, waves, rain, wind, last_sat, forecast_waves or None)
        except Exception:
            trend = "holding"
        out_sites[prefix] = {"site_id": sid, "name": PARAMS["sites"][sid]["name"], "class": r["class"], "class_index": r["class_index"],
                             "confidence": r["confidence"], "tags": r["tags"], "why": r["why"], "trend_24h": trend,
                             "kd490": r["kd490_estimate"], "stirring_percentile": r["stirring_percentile"]}
    result = {"updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "as_of_utc": as_of.isoformat() + "Z",
              "model_version": PARAMS["version"], "status": PARAMS["status"], "data": status, "sites": out_sites}
    if not offline and not (status["buoy_ok"] or status["weather_ok"] or status["satellite_ok"]):
        print("No live data could be fetched; leaving the previous clarity.json in place.", status["errors"])
        sys.exit(1)
    path = os.path.join(HERE, "clarity.json")
    json.dump(result, open(path, "w"), indent=1)
    print(f"Wrote {path}. data status: {json.dumps(status)}")
    for p, v in out_sites.items():
        print(f"  {p:15s} {v['class']:8s} conf={v['confidence']:6s} trend={v['trend_24h']:10s} {', '.join(v['tags'])}")


if __name__ == "__main__":
    main()
