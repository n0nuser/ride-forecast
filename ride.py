#!/usr/bin/env python3
"""Tomorrow's motorbike-riding forecast from several weather sources, no API keys.
Sources: 5 Open-Meteo models (ECMWF, GFS, ICON, Meteo-France, UK Met Office) + MET Norway.
Each source is scored per hour; the hour score is the median across sources. A dangerous
hour (storm, ice, snow, gusts) reported by 2+ sources caps the whole day at "Marginal".
Usage: ride.py "Salamanca" [--country ES] [--date YYYY-MM-DD] [--dry]
Env:   RIDE_PLACE, RIDE_COUNTRY (defaults for the args), WEBHOOK_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Target day: before 12:00 local = today, after = tomorrow (so a late catch-up run still works).
"""
import argparse, json, os, signal, statistics as st, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

HOURS = range(8, 21)  # riding hours, local time
RUN_TIMEOUT = 180  # s, whole run; the per-request timeout alone does not stop a slow-drip server
RETRIES = (1, 3)  # s waits between attempts
UA = {"User-Agent": "ride-forecast/1.0 github.com/n0nuser"}  # MET Norway requires an identifying UA
MODELS = {"ecmwf_ifs025": "ECMWF", "gfs_seamless": "GFS", "icon_seamless": "ICON",
          "meteofrance_seamless": "MeteoFr", "ukmo_seamless": "UKMO"}
VARS = ["temperature_2m", "relative_humidity_2m", "precipitation_probability", "precipitation", "wind_gusts_10m", "weather_code"]
BANDS = [(80, "Great day to ride"), (65, "Good day, ride"), (50, "Mixed: ride in the best window"),
         (35, "Marginal: short ride only if you must"), (0, "Stay home")]
MIXED_MAX = BANDS[1][0] - 1  # a good window rescues a mediocre day, never above "mixed"
STOP_CAP = BANDS[2][0] - 1   # any dangerous hour caps the day at "marginal"
SPREAD_LIMIT, SPREAD_DIVISOR = 30, 5  # sources this far apart (points) lower the score by spread/5
STORM, SNOW_ICE, FOG = (95, 96, 99), (56, 57, 66, 67, 71, 73, 75, 77, 85, 86), (45, 48)
GUST_STOP = 70   # km/h: a hard stop on two wheels
ICE_T = 2        # deg C: at or below this, wet or damp air means ice risk
GUST_FACTOR = 1.5  # ponytail: MET Norway has no gusts; gust ~ 1.5 x mean wind. Crude, tune if it matters.


class Hour(NamedTuple):
    t: float
    rh: float
    pop: Optional[float]   # rain chance %, None if the source has none
    mm: float
    gust: Optional[float]  # km/h, None if the source has none
    code: int              # WMO weather code


def get(url, **params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=UA)
    for wait in (*RETRIES, None):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if wait is None or (getattr(e, "code", 500) < 500 and getattr(e, "code", 0) != 429):
                raise  # out of retries, or a client error that retrying will not fix
            time.sleep(wait)


def geocode(place, country=None):
    res = get("https://geocoding-api.open-meteo.com/v1/search", name=place, count=10).get("results") or []
    if country:
        res = [r for r in res if r.get("country_code", "").upper() == country.upper()]
    if not res:
        sys.exit(f"place not found: {place}" + (f" in {country}" if country else ""))
    return res[0]


def stop_reason(h):
    """Why this hour is not rideable at all, or None."""
    if h.code in STORM:
        return "thunderstorm"
    if h.code in SNOW_ICE:
        return "snow or freezing rain"
    if h.gust is not None and h.gust >= GUST_STOP:
        return f"gusts {h.gust:.0f} km/h"
    if h.t <= ICE_T and (h.mm >= 0.1 or h.rh >= 90):
        return "ice risk"
    return None


def hour_score(h):
    """100 = perfect; each factor subtracts; a stop reason is 0."""
    if stop_reason(h):
        return 0
    pop = h.pop if h.pop is not None else min(90, h.mm * 150)  # no rain chance from the source: infer, smoothly
    p = min(60, max(0, 18 - h.t) * 3 + max(0, h.t - 26) * 4)  # temperature comfort 18-26
    p += min(70, pop * 0.35 + h.mm * 40)                      # rain: chance + amount
    p += min(30, max(0, (h.gust or 0) - 40) * 1.5)            # gusts over 40 km/h
    p += min(15, max(0, h.rh - 80) * 0.75)                    # damp air, mist
    p += 20 if h.code in FOG else 0
    return max(0, round(100 - p))


def met_code(sym):  # MET Norway symbol -> the WMO codes this script cares about
    return STORM[0] if "thunder" in sym else SNOW_ICE[4] if ("snow" in sym or "sleet" in sym) else FOG[0] if sym == "fog" else 0


def open_meteo(g):
    w = get("https://api.open-meteo.com/v1/forecast", latitude=g["latitude"], longitude=g["longitude"], timezone=g["timezone"],
            forecast_days=3, models=",".join(MODELS), hourly=",".join(VARS))
    h, out = w["hourly"], {}
    for m, name in MODELS.items():
        if h.get(f"temperature_2m_{m}", [None])[0] is None:
            continue
        out[name] = {ts: Hour(*(h[f"{v}_{m}"][i] for v in VARS)) for i, ts in enumerate(h["time"])
                     if h[f"temperature_2m_{m}"][i] is not None}
    return out


def met_norway(g, tz):
    d = get("https://api.met.no/weatherapi/locationforecast/2.0/compact", lat=round(g["latitude"], 4), lon=round(g["longitude"], 4))
    out = {}
    for e in d["properties"]["timeseries"]:
        n1 = e["data"].get("next_1_hours")
        if not n1:
            continue
        i = e["data"]["instant"]["details"]
        ts = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(tz).strftime("%Y-%m-%dT%H:00")
        gust = i["wind_speed"] * 3.6 * GUST_FACTOR if "wind_speed" in i else None
        out[ts] = Hour(i["air_temperature"], i["relative_humidity"], None, n1["details"].get("precipitation_amount", 0), gust,
                       met_code(n1["summary"]["symbol_code"]))
    return out


def target_day(now):
    """Before noon local: today (late catch-up run). Otherwise tomorrow."""
    return (now.date() if now.hour < 12 else now.date() + timedelta(days=1)).isoformat()


def analyse(sources, day):
    keys = [f"{day}T{h:02d}:00" for h in HOURS]
    sources = {n: s for n, s in sources.items() if all(k in s for k in keys)}  # drop sources missing any riding hour
    if not sources:
        raise RuntimeError("no source returned full data for the day")
    per = {n: [hour_score(s[k]) for k in keys] for n, s in sources.items()}
    need = min(2, len(sources))  # a dangerous hour counts when 2 sources agree (or the only one says so)
    stops = {}
    for k in keys:
        reasons = [r for s in sources.values() if (r := stop_reason(s[k]))]
        if len(reasons) >= need:
            stops[k] = Counter(reasons).most_common(1)[0][0]
    hourly = [0 if k in stops else st.median(v[i] for v in per.values()) for i, k in enumerate(keys)]
    best = max(range(len(hourly) - 2), key=lambda i: sum(hourly[i:i + 3]))
    avg, win = round(st.mean(hourly)), round(st.mean(hourly[best:best + 3]))
    day_scores = {n: round(st.mean(v)) for n, v in per.items()}
    spread = max(day_scores.values()) - min(day_scores.values())
    score = max(avg, min(win - 15, MIXED_MAX))
    if spread > SPREAD_LIMIT:  # sources disagree: lean cautious
        score = max(0, score - round(spread / SPREAD_DIVISOR))
    if stops:
        score = min(score, STOP_CAP)
    col = lambda i: [x for s in sources.values() for k in keys if (x := s[k][i]) is not None]
    start = HOURS[best]
    return dict(score=score, verdict=next(v for m, v in BANDS if score >= m), window=f"{start:02d}:00-{start + 3:02d}:00",
                window_score=win, day_scores=day_scores, spread=spread, n=len(sources),
                stops=[f"{k[11:16]} {r}" for k, r in stops.items()],
                tmin=st.median(min(s[k].t for k in keys) for s in sources.values()),
                tmax=st.median(max(s[k].t for k in keys) for s in sources.values()),
                pop=max(col(2) or [0]), mm=round(st.median(sum(s[k].mm for k in keys) for s in sources.values()), 1),
                gust=round(max(col(4) or [0])), rh=(round(min(col(1))), round(max(col(1)))))


def message(place, day, r):
    agree = ("ONLY 1 source, low confidence" if r["n"] == 1 else "sources agree" if r["spread"] <= 15
             else "some disagreement" if r["spread"] <= 30 else "SOURCES DISAGREE, low confidence")
    per = " · ".join(f"{n} {s}" for n, s in sorted(r["day_scores"].items(), key=lambda x: -x[1]))
    stop = f"\nDANGER: {', '.join(r['stops'])}" if r["stops"] else ""
    return (f"{r['verdict']} ({r['score']}/100)\n{place}, {day}{stop}\n"
            f"Best window {r['window']} ({r['window_score']}/100)\n"
            f"{r['tmin']:.0f}-{r['tmax']:.0f}°C, rain {r['mm']} mm (up to {r['pop']}% in some models), "
            f"gusts {r['gust']} km/h, humidity {r['rh'][0]}-{r['rh'][1]}%\n"
            f"{r['n']} sources, {agree}: {per}")


def send(text, score):
    """Try every channel; return the names of those that failed."""
    failed = []
    if url := os.environ.get("WEBHOOK_URL"):  # "text" = Slack, "content" = Discord
        try:
            req = urllib.request.Request(url, json.dumps({"text": text, "content": text, "score": score}).encode(),
                                         {"Content-Type": "application/json", **UA})
            urllib.request.urlopen(req, timeout=20)
        except Exception as e:
            failed.append("webhook")
            print(f"webhook failed: {type(e).__name__}: {e}", file=sys.stderr)
    if (tok := os.environ.get("TELEGRAM_TOKEN")) and (chat := os.environ.get("TELEGRAM_CHAT_ID")):
        try:
            get(f"https://api.telegram.org/bot{tok}/sendMessage", chat_id=chat, text=text)
        except Exception as e:
            failed.append("telegram")
            print(f"telegram failed: {type(e).__name__}", file=sys.stderr)  # not str(e): keep the token-bearing URL out of the journal
    return failed


def configured():
    return bool(os.environ.get("WEBHOOK_URL") or (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")))


def run(a):
    g = geocode(a.place, a.country)
    tz = ZoneInfo(g["timezone"])
    day = a.date or target_day(datetime.now(tz))
    sources = {}
    for name, fetch in (("Open-Meteo", lambda: open_meteo(g)), ("MET.no", lambda: {"MET.no": met_norway(g, tz)})):
        try:
            sources.update(fetch())
        except Exception as e:  # one provider down must not kill the report
            print(f"{name} skipped: {type(e).__name__}: {e}", file=sys.stderr)
    r = analyse(sources, day)
    place = ", ".join(x for x in (g["name"], g.get("admin1"), g.get("country_code")) if x)
    return message(place, day, r), r["score"]


def main(argv=None):
    p = argparse.ArgumentParser(description="Motorbike weather verdict for tomorrow.")
    p.add_argument("place", nargs="?", default=os.environ.get("RIDE_PLACE"))
    p.add_argument("--country", default=os.environ.get("RIDE_COUNTRY"), help="ISO code, e.g. ES; stops 'Leon' meaning Nicaragua")
    p.add_argument("--date", help="YYYY-MM-DD, default: see module docstring")
    p.add_argument("--dry", action="store_true", help="print only, send nothing")
    a = p.parse_args(argv)
    if not a.place:
        p.error("give a place or set RIDE_PLACE")
    if a.date:
        date.fromisoformat(a.date)
    if not a.dry and not configured():
        p.error("no channel configured: set WEBHOOK_URL or TELEGRAM_TOKEN + TELEGRAM_CHAT_ID (or use --dry)")
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError(f"run exceeded {RUN_TIMEOUT}s")))
    signal.alarm(RUN_TIMEOUT)
    try:
        text, score = run(a)
    except BaseException as e:  # a silent night is the worst outcome: say it failed
        signal.alarm(30)
        text, score = f"ride-forecast FAILED for {a.place}: {type(e).__name__}: {e}", -1
        print(text, file=sys.stderr)
        if not a.dry:
            send(text, score)
        return 1
    print(text)
    return 1 if (not a.dry and send(text, score)) else 0


if __name__ == "__main__":
    sys.exit(main())
