#!/usr/bin/env python3
"""Motorbike weather verdicts from several sources, no API keys. Core library + one-shot CLI.
Sources: 5 Open-Meteo models (ECMWF, GFS, ICON, Meteo-France, UK Met Office) + MET Norway.
Every source is rated per hour on four levels (GO, CAUTION, RISKY, DON'T RIDE): the worst real hazard wins
(ice, rain, gusts, fog/visibility, heat, storm), and two CAUTION factors make RISKY. A 0-100 score is derived from the
level, so verdicts, charts and rankings always agree. An hour's score is the median across sources; trips are judged
on their worst hour; a DON'T RIDE hour reported by 2+ sources is a stop. Thresholds and their sources: docs/research/.
Usage: ride.py "Salamanca" [--country ES] [--date YYYY-MM-DD] [--dry]
Env:   RIDE_PLACE, RIDE_COUNTRY, RIDE_TRIPS, WEBHOOK_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Target day: before 12:00 local = today, after = tomorrow. The Telegram bot is in bot.py.
"""
from __future__ import annotations

import argparse, contextlib, json, os, random, re, signal, statistics as st, sys, time, traceback
import urllib.error, urllib.parse, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

DAY_HOURS = range(8, 21)  # the "Day" summary line, local time
WINDOW_HOURS = 3          # length of the "best window" in the Day line
FORECAST_DAYS = 8         # today + 7
RUN_TIMEOUT = 180  # s, whole one-shot run; the per-request timeout alone does not stop a slow-drip server
REQUEST_TIMEOUT = 15  # s per socket operation
RETRIES = (1, 3)  # s waits between attempts
UA = {"User-Agent": "ride-forecast/2.0 github.com/n0nuser/ride-forecast"}  # MET Norway requires an identifying UA
MODELS = {"ecmwf_ifs025": "ECMWF", "gfs_seamless": "GFS", "icon_seamless": "ICON",
          "meteofrance_seamless": "MeteoFr", "ukmo_seamless": "UKMO"}
VARS = ["temperature_2m", "relative_humidity_2m", "precipitation_probability", "precipitation", "wind_gusts_10m",
        "weather_code", "visibility"]  # same order as Hour's fields
# One scale for everything: name, icon, lowest score of the level. An hour's 0-100 score is kept inside its level's band.
LEVELS = [("GO", "🟢", 70), ("CAUTION", "🟡", 45), ("RISKY", "🟠", 20), ("DON'T RIDE", "🔴", 0)]
STOP = len(LEVELS) - 1
STOP_CAP = LEVELS[1][2] - 1    # any DON'T RIDE hour in the day caps the day at RISKY
MIXED_MAX = LEVELS[0][2] - 1   # a good window rescues a mediocre day, never above CAUTION
SPREAD_LIMIT = 30  # sources this far apart (points): the report says "sources disagree"
AGREE_SPREAD = 15  # up to this, the day line says "sources agree"
STORM, SNOW_ICE, FOG = (95, 96, 99), (56, 57, 66, 67, 71, 73, 75, 77, 85, 86), (45, 48)
# Thresholds (sources and judgment calls: docs/research/motorcycle-weather-thresholds.md)
ICE_DAMP_RH = 85            # %: at or above this the road counts as damp for ice
ICE_DRY_STOP_T, ICE_WET_STOP_T, FROST_T = 0, 1, 3  # deg C: <=0 DON'T RIDE; <=1 and damp too; <=3 and damp RISKY, dry CAUTION
RAIN_LEVELS = (1, 4, 15)    # mm/h: from CAUTION, RISKY, DON'T RIDE (Met Office: 4 is heavy; AEMET: 15 is a yellow warning)
GUST_FROM = 40              # km/h: worth a tip
GUST_LEVELS = (50, 60, 70)  # km/h: CAUTION, RISKY, DON'T RIDE (70 = AEMET yellow gust warning; 50 and 60 are judgment)
VIS_LEVELS = (1000, 200, 100)  # m: below these CAUTION (fog), RISKY, DON'T RIDE (100 m: UK fog-light limit)
VIS_NOTE = 2000             # m: below this a tip ("mist")
HEAT_LEVELS = (32, 36, 40)  # deg C: above 32 CAUTION, from 36 RISKY, from 40 DON'T RIDE (judgment)
ICE_SOFT_T = 5   # deg C: below this, damp or wet air starts to cost score points (frost on bridges and shade)
ICE_WET_FROM, ICE_WET_FULL = 70, 95  # % humidity where the moisture factor starts and reaches 1
ICE_MAX_PENALTY, ICE_NOTE = 50, 0.3  # points at full risk; risk above which the gear line warns
GUST_FACTOR = 1.5  # ponytail: MET Norway has no gusts; gust ~ 1.5 x mean wind. Crude, tune if it matters.
COLD_GEAR, RAIN_GEAR_MM, WIND_NOTE = 8, 0.2, 45  # gear hints: deg C, mm/h, km/h
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}


class UserError(ValueError):
    """The user asked for something we can't do (bad day, bad trip, unknown place). Say it plainly."""


class Deadline(BaseException):
    """Raised by deadline(). A BaseException so a broad `except Exception` can't swallow it."""


class Hour(NamedTuple):
    t: float
    rh: float
    pop: Optional[float]   # rain chance %, None if the source has none
    mm: float
    gust: Optional[float]  # km/h, None if the source has none
    code: int              # WMO weather code
    vis: Optional[float] = None  # visibility in metres, None if the source has none


# ---- plumbing ------------------------------------------------------------------------------

def scrub(text):
    """Remove the Telegram token (in a URL or as an env value) from anything that gets logged."""
    for k in ("TELEGRAM_TOKEN", "WEBHOOK_URL"):
        if v := os.environ.get(k):
            text = text.replace(v, "***")
    return re.sub(r"bot\d+:[\w-]+", "bot***", text)


def log(msg, exc=False):
    print(scrub(msg + ("\n" + traceback.format_exc() if exc else "")), file=sys.stderr)


@contextlib.contextmanager
def deadline(seconds):
    """Whole-call time limit (main thread, Unix). Socket timeouts only bound each read, not a slow drip."""
    def fire(*_):
        raise Deadline(f"exceeded {seconds}s")
    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429
    return isinstance(e, OSError)  # URLError, timeouts, resets. A JSON error is a bug or a changed API: not retried.


def get(url, retry=True, **params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=UA)
    for wait in (*RETRIES, None) if retry else (None,):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.load(r)
        except Exception as e:
            if wait is None or not retryable(e):
                raise
            time.sleep(wait + random.random())


def geocode(place, country=None):
    res = get("https://geocoding-api.open-meteo.com/v1/search", name=place, count=10).get("results") or []
    if country:
        res = [r for r in res if r.get("country_code", "").upper() == country.upper()]
    if not res:
        raise UserError(f"place not found: {place}" + (f" in {country}" if country else ""))
    return res[0]


def place_label(g):
    return ", ".join(x for x in (g["name"], g.get("admin1"), g.get("country_code")) if x)


# ---- scoring -------------------------------------------------------------------------------

def _clamp(x):
    return max(0.0, min(1.0, x))


def level_of(score):
    """Score (0-100) -> level index 0 (GO) .. 3 (DON'T RIDE). The one place a score becomes a verdict."""
    return next(i for i, (_, _, lo) in enumerate(LEVELS) if score >= lo)


def ice_risk(h):
    """0 to 1: cold enough for frost and damp or wet enough to form it. Rain counts as fully wet."""
    wet = 1.0 if h.mm >= 0.1 else _clamp((h.rh - ICE_WET_FROM) / (ICE_WET_FULL - ICE_WET_FROM))
    return _clamp((ICE_SOFT_T - h.t) / ICE_SOFT_T) * wet


def _step(value, limits):
    """How many of the ascending limits value has reached (0 to len)."""
    return sum(value >= x for x in limits)


PRIORITY = ("storm", "snow", "ice", "wind", "rain", "fog", "heat", "cold")  # the order ties are named in
CATEGORY = {"storm": "thunderstorm", "snow": "snow or freezing rain", "ice": "ice risk", "wind": "gusts",
            "rain": "heavy rain", "fog": "poor visibility", "heat": "extreme heat"}


def hazards(h):
    """[(level, key)] for one hour of one source, worst first. Level 0 is a tip only; 3 is DON'T RIDE."""
    out = []
    wet = h.mm >= 0.1 or h.rh >= ICE_DAMP_RH
    if h.code in STORM:
        out.append((3, "storm"))
    if h.code in SNOW_ICE:
        out.append((3, "snow"))
    if h.t <= ICE_DRY_STOP_T or (h.t <= ICE_WET_STOP_T and wet):
        out.append((3, "ice"))
    elif h.t <= FROST_T:
        out.append((2, "ice") if wet else (1, "cold"))
    elif h.t < COLD_GEAR:
        out.append((0, "cold"))
    if h.mm >= 0.1:
        out.append((_step(h.mm, RAIN_LEVELS), "rain"))
    if h.gust is not None and h.gust >= GUST_FROM:
        out.append((_step(h.gust, GUST_LEVELS), "wind"))
    fog = 1 if h.code in FOG else 0
    if h.vis is not None:
        fog = max(fog, sum(h.vis < x for x in VIS_LEVELS))
        if fog or h.vis < VIS_NOTE:
            out.append((fog, "fog"))
    elif fog:
        out.append((fog, "fog"))
    if h.t > HEAT_LEVELS[0]:
        out.append((1 + (h.t >= HEAT_LEVELS[1]) + (h.t >= HEAT_LEVELS[2]), "heat"))
    return sorted(out, key=lambda x: (-x[0], PRIORITY.index(x[1])))


def level(h):
    """One hour of one source: the worst hazard's level; two CAUTION factors together make RISKY."""
    hz = hazards(h)
    lv = hz[0][0] if hz else 0
    return 2 if lv == 1 and sum(l == 1 for l, _ in hz) >= 2 else lv


def stop_reason(h):
    """Category of danger that makes this hour not rideable at all, or None."""
    return next((CATEGORY[k] for l, k in hazards(h) if l == STOP), None)


def reason_text(key, lv, h):
    return {"storm": "thunderstorm", "snow": "snow or freezing rain",
            "ice": f"{'ice' if lv == STOP else 'frost'} risk ({h.t:.0f}°C, {h.rh:.0f}% humidity)",
            "cold": f"cold {h.t:.0f}°C", "wind": f"gusts {h.gust or 0:.0f} km/h", "heat": f"hot {h.t:.0f}°C",
            "rain": f"{'light rain' if lv == 0 else 'rain'} {h.mm:.1f} mm/h",
            "fog": ("fog" if lv else "mist") + (f", visibility {h.vis:.0f} m" if h.vis is not None else "")}[key]


def describe_stop(h, category):
    key = next(k for k, c in CATEGORY.items() if c == category)
    return reason_text(key, STOP, h)


def penalties(h):
    """Soft costs: they order hours inside a level. The level itself comes from hazards()."""
    pop = h.pop if h.pop is not None else min(90, h.mm * 150)  # no rain chance from the source: infer, smoothly
    return {"cold": min(60, max(0, 18 - h.t) * 3), "hot": min(60, max(0, h.t - HEAT_LEVELS[0]) * 4),
            "rain": min(70, pop * 0.35 + h.mm * 40), "wind": min(30, max(0, (h.gust or 0) - GUST_FROM) * 1.5),
            "damp": min(15, max(0, h.rh - 80) * 0.75), "fog": 20 if h.code in FOG else 0,
            "vis": 0 if h.vis is None else min(30, max(0, 4000 - h.vis) / 4000 * 30),
            "ice": ICE_MAX_PENALTY * ice_risk(h)}


def hour_score(h):
    """0-100, always inside the band of the hour's level (GO 70-100 ... RISKY 20-44), DON'T RIDE is 0."""
    lv = level(h)
    if lv == STOP:
        return 0
    lo, hi = LEVELS[lv][2], LEVELS[lv - 1][2] - 1 if lv else 100
    return round(lo + (hi - lo) * max(0, 100 - sum(penalties(h).values())) / 100)  # soft costs order hours inside the band


def main_reason(h, n=2):
    """The factors that matter for this hour (worst first, at most n), in words."""
    return ", ".join(reason_text(k, l, h) for l, k in hazards(h)[:n]) or "fine"


# ---- fetching ------------------------------------------------------------------------------

def valid(h):
    return None not in (h.t, h.rh, h.mm, h.code)


def met_code(sym):  # MET Norway symbol -> the WMO codes this script cares about
    return STORM[0] if "thunder" in sym else SNOW_ICE[4] if ("snow" in sym or "sleet" in sym) else FOG[0] if sym == "fog" else 0


def local_key(utc_iso, tz):
    """A UTC timestamp -> the place's local hour key. Converting each instant keeps sources aligned across DST changes."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(tz).strftime("%Y-%m-%dT%H:00")


def open_meteo(g):
    tz = ZoneInfo(g["timezone"])
    w = get("https://api.open-meteo.com/v1/forecast", latitude=g["latitude"], longitude=g["longitude"], timezone="GMT",
            forecast_days=FORECAST_DAYS, models=",".join(MODELS), hourly=",".join(VARS))
    h, out = w["hourly"], {}
    keys = [local_key(ts, tz) for ts in h["time"]]
    for m, name in MODELS.items():
        if f"temperature_2m_{m}" not in h:
            continue
        col = {v: h.get(f"{v}_{m}") or [None] * len(keys) for v in VARS}  # visibility may be missing for a model
        rows = {keys[i]: Hour(*(col[v][i] for v in VARS)) for i in range(len(keys))}
        out[name] = {k: x for k, x in rows.items() if valid(x)}
    return {n: s for n, s in out.items() if s}


def met_norway(g, tz):
    d = get("https://api.met.no/weatherapi/locationforecast/2.0/compact", lat=round(g["latitude"], 4), lon=round(g["longitude"], 4))
    out = {}
    for e in d["properties"]["timeseries"]:
        n1 = e["data"].get("next_1_hours")
        if not n1:
            continue
        i = e["data"]["instant"]["details"]
        gust = i["wind_speed"] * 3.6 * GUST_FACTOR if "wind_speed" in i else None
        out[local_key(e["time"], tz)] = Hour(i["air_temperature"], i["relative_humidity"], None,
                                             n1["details"].get("precipitation_amount", 0), gust, met_code(n1["summary"]["symbol_code"]))
    return out


def fetch_sources(g):
    """All providers at once. One being down must not kill the report; all being down must say so."""
    tz = ZoneInfo(g["timezone"])
    sources = {}
    ex = ThreadPoolExecutor(2)
    try:
        futs = {"Open-Meteo": ex.submit(open_meteo, g), "MET.no": ex.submit(lambda: {"MET.no": met_norway(g, tz)})}
        for name, f in futs.items():
            try:
                sources.update(f.result())
            except Exception as e:
                log(f"{name} skipped: {type(e).__name__}: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)  # a Deadline must not wait for stuck threads
    if not sources:
        raise RuntimeError("no weather source answered")
    return sources


# ---- trips ---------------------------------------------------------------------------------

def _days(spec):
    out = set()
    for part in spec.lower().split(","):
        a, _, b = part.partition("-")
        if a not in DAYS or (b and b not in DAYS):
            raise UserError(f"bad day '{part}': use mon-fri, sat,sun, ...")
        i, j = DAYS[a], DAYS[b or a]
        out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
    return out


def _hm(s):
    h, _, m = s.partition(":")
    return int(h), int(m or 0)


def parse_trips(spec):
    """'work 07:30-09:00 mon-fri;home 17:00-19:00 mon-fri' -> list of trips. Empty or 'none' = no trips."""
    spec = spec.strip()
    if spec.lower() in ("", "none"):
        return []
    trips = []
    for entry in spec.split(";"):
        tok = entry.split()
        if not 2 <= len(tok) <= 3 or not re.fullmatch(r"\w+", tok[0]) or not re.fullmatch(r"\d{1,2}(:\d\d)?-\d{1,2}(:\d\d)?", tok[1]):
            raise UserError(f"bad trip '{entry.strip()}': use NAME HH:MM-HH:MM [mon-fri]")
        a, b = (_hm(x) for x in tok[1].split("-"))
        if not (0 <= a[0] < 24 and 0 <= b[0] <= 24 and a[1] < 60 and b[1] < 60 and a < b and b <= (24, 0)):
            raise UserError(f"bad times in '{entry.strip()}': start must be before end, same day")
        trips.append(dict(name=tok[0], start=a, end=b, days=_days(tok[2]) if len(tok) == 3 else set(range(7))))
    return trips


def trips_from_env():
    """Trips are personal: there are no built-in ones. RIDE_TRIPS or the bot's /trips supply them."""
    return parse_trips(os.environ.get("RIDE_TRIPS", ""))


def trip_hours(t):
    return range(t["start"][0], t["end"][0] + (1 if t["end"][1] else 0))


def trip_window(t):
    return f"{t['start'][0]:02d}:{t['start'][1]:02d}-{t['end'][0]:02d}:{t['end'][1]:02d}"


# ---- analysis ------------------------------------------------------------------------------

def _row(hr, items):
    """One hour: the score of each source, their median, and a consensus weather for the description."""
    per = {n: hour_score(h) for n, h in items.items()}
    hs = list(items.values())
    votes = [r for h in hs if (r := stop_reason(h))]
    pops, gusts, viss = ([x for h in hs if (x := getattr(h, f)) is not None] for f in ("pop", "gust", "vis"))
    cons = Hour(st.median(h.t for h in hs), st.median(h.rh for h in hs), max(pops) if pops else None,
                st.median(h.mm for h in hs), max(gusts) if gusts else None, Counter(h.code for h in hs).most_common(1)[0][0],
                min(viss) if viss else None)  # the worst visibility any source sees
    stop = Counter(votes).most_common(1)[0][0] if len(votes) >= min(2, len(hs)) else None  # 2 sources agree
    med = st.median(per.values())
    ref = min(per, key=lambda n: (abs(per[n] - med), per[n]))  # the source nearest the median explains the hour
    score = 0 if stop else max(LEVELS[STOP - 1][2], med)  # without a stop vote the hour stays at RISKY or better
    return dict(hour=hr, score=score, level=STOP if stop else level_of(score), stop=stop, per=per, w=cons, n=len(hs),
                reason=describe_stop(cons, stop) if stop else main_reason(items[ref]))


def hourly_rows(sources, day):
    rows = []
    for hr in range(24):
        key = f"{day}T{hr:02d}:00"
        items = {n: s[key] for n, s in sources.items() if key in s}
        if items:
            rows.append(_row(hr, items))
    if not rows:
        raise RuntimeError(f"no source has data for {day}")
    return rows


def _rng(a, b, unit):
    a, b = round(a), round(b)
    return f"{a}{unit}" if a == b else f"{a} to {b}{unit}"


def eval_trip(t, by_hour):
    rows = [by_hour[h] for h in trip_hours(t) if h in by_hour]
    if not rows:
        return None
    worst = min(rows, key=lambda r: r["score"])
    spread = max(max(r["per"].values()) - min(r["per"].values()) for r in rows)
    stop = next((r for r in rows if r["stop"]), None)
    score = worst["score"]
    lv = STOP if stop else level_of(score)
    where = stop or worst
    return dict(name=t["name"], window=trip_window(t), score=score, level=lv, verdict=LEVELS[lv][0], spread=spread,
                temp=_rng(min(r["w"].t for r in rows), max(r["w"].t for r in rows), "°C"),
                humidity=_rng(min(r["w"].rh for r in rows), max(r["w"].rh for r in rows), "%"),
                reason="fine" if where["reason"] == "fine" else f"{where['reason']} at {where['hour']:02d}:00",
                hours=[r["hour"] for r in rows])


def day_summary(rows):
    """The whole-day view over DAY_HOURS: average, best window, worst hour, stop cap. Only full-coverage sources count."""
    rs = [r for r in rows if r["hour"] in DAY_HOURS]
    if len(rs) < WINDOW_HOURS:
        return None
    names = set.intersection(*(set(r["per"]) for r in rs)) or {n for r in rs for n in r["per"]}  # partial sources only if none is full
    hourly = [r["score"] for r in rs]
    span = range(len(hourly) - WINDOW_HOURS + 1)
    best = max(span, key=lambda i: sum(hourly[i:i + WINDOW_HOURS]))
    avg, win = round(st.mean(hourly)), round(st.mean(hourly[best:best + WINDOW_HOURS]))
    day_scores = {n: round(st.mean(r["per"][n] for r in rs if n in r["per"])) for n in names}
    spread = max(day_scores.values()) - min(day_scores.values())
    score = max(avg, min(win - 15, MIXED_MAX))
    if any(r["stop"] for r in rs):
        score = min(score, STOP_CAP)
    start, worst = rs[best]["hour"], min(rs, key=lambda r: (r["level"] < STOP, r["score"]))
    lv = level_of(score)
    return dict(score=score, level=lv, verdict=LEVELS[lv][0], window=f"{start:02d}:00-{start + WINDOW_HOURS:02d}:00",
                window_score=win, window_level=level_of(win), day_scores=day_scores, spread=spread,
                worst=worst if worst["level"] > 0 else None)


def target_day(now):
    """Before noon local: today (late catch-up run). Otherwise tomorrow."""
    return (now.date() if now.hour < 12 else now.date() + timedelta(days=1)).isoformat()


def _gear(rows):
    if not rows:
        return []
    cold = min(r["w"].t for r in rows)
    return ([f"warm layers and gloves ({cold:.0f}°C)"] if cold < COLD_GEAR else []) \
        + (["watch for ice on bridges and in shade"] if any(ice_risk(r["w"]) >= ICE_NOTE for r in rows) else []) \
        + (["rain gear"] if any(r["w"].mm >= RAIN_GEAR_MM for r in rows) else []) \
        + (["clear visor, fog"] if any(r["w"].code in FOG or (r["w"].vis is not None and r["w"].vis < VIS_LEVELS[0]) for r in rows) else []) \
        + ([f"strong wind, {max(r['w'].gust or 0 for r in rows):.0f} km/h gusts"] if any((r["w"].gust or 0) >= WIND_NOTE for r in rows) else []) \
        + ([f"hot, vented gear and water ({max(r['w'].t for r in rows):.0f}°C)"] if any(r["w"].t > HEAT_LEVELS[0] for r in rows) else [])


TIPS = {"rain": "wet roads, brake early, 3 s gap, first minutes slippery", "wind": "grip firmly, give trucks room on open roads",
        "fog": "lights on, slow down, stop within what you see", "cold": "cold tyres, gentle first km"}


def _tips(rows):
    keys = {k for r in rows for _, k in hazards(r["w"])}
    return [t for k, t in TIPS.items() if k in keys]


def build_report(g, day, sources, trips):
    """Everything about one day: text for chat, plus the data a chart needs."""
    d = date.fromisoformat(day)
    rows = hourly_rows(sources, day)
    by_hour = {r["hour"]: r for r in rows}
    ev = [e for t in trips if d.weekday() in t["days"] if (e := eval_trip(t, by_hour))]
    summ = day_summary(rows)
    lines = [f"{d.strftime('%a %d %b')} · {place_label(g)}"]
    for e in ev:
        lines.append(f"{LEVELS[e['level']][1]} {e['name']} {e['window']}: {e['verdict']}, {e['temp']}, {e['humidity']} humidity, {e['reason']}"
                     + (", sources disagree" if e["spread"] > SPREAD_LIMIT else ""))
    if summ:
        lines.append(f"Whole day 08:00-20:00: {LEVELS[summ['level']][1]} {summ['verdict']}, {summ['score']}/100\n"
                     f"Best 3 hours: {summ['window']}, {LEVELS[summ['window_level']][1]} {LEVELS[summ['window_level']][0]}, {summ['window_score']}/100")
        if w := summ["worst"]:
            lines.append(f"Worst hour: {w['hour']:02d}:00, {LEVELS[w['level']][1]} {LEVELS[w['level']][0]}, {w['reason']}")
    used = [by_hour[h] for e in ev for h in e["hours"]] or [r for r in rows if r["hour"] in DAY_HOURS]
    if gear := _gear(used):
        lines.append("Gear: " + ", ".join(gear))
    if tips := _tips(used):
        lines.append("Ride: " + "; ".join(tips))
    if summ:
        sc = summ["day_scores"]
        agree = ("ONLY 1 source, low confidence" if len(sc) == 1 else "sources agree" if summ["spread"] <= AGREE_SPREAD
                 else "some disagreement" if summ["spread"] <= SPREAD_LIMIT else "SOURCES DISAGREE, low confidence")
        lines.append(f"{len(sc)} sources, {agree}: " + " · ".join(f"{n} {s}" for n, s in sorted(sc.items(), key=lambda x: -x[1])))
    if not ev and not summ:
        lines.append("Not enough data for this day yet.")
    return dict(text="\n".join(lines), rows=rows, trips=ev, summary=summ, day=day, label=place_label(g))


def week_line(rep):
    """One line for a day in the week view: level and score, best window, temperature range, rain, gusts, visibility."""
    d, summ = date.fromisoformat(rep["day"]), rep["summary"]
    head = d.strftime("%a %d %b")
    if not summ:
        return f"{head}: not enough data yet"
    rs = [r for r in rep["rows"] if r["hour"] in DAY_HOURS]
    t, rain, gust = [r["w"].t for r in rs], max(r["w"].mm for r in rs), max((r["w"].gust or 0) for r in rs)
    vis = min((r["w"].vis for r in rs if r["w"].vis is not None), default=None)
    return (f"{LEVELS[summ['level']][1]} {head}: {summ['verdict']} {summ['score']}/100\n    best {summ['window']}, {min(t):.0f}-{max(t):.0f}°C"
            + (f", rain {rain:.1f} mm/h" if rain >= RAIN_GEAR_MM else "") + (f", gusts {gust:.0f} km/h" if gust >= WIND_NOTE else "")
            + (f", visibility {vis:.0f} m" if vis is not None and vis < VIS_LEVELS[0] else ""))


def week_reports(g, today, sources, trips, days=7):
    return [build_report(g, (today + timedelta(days=i)).isoformat(), sources, trips) for i in range(days)]


# ---- delivery ------------------------------------------------------------------------------

def send(text, score=None):
    """Try every channel; return the names of those that failed. A message is sent once, never retried."""
    failed = []
    if url := os.environ.get("WEBHOOK_URL"):  # "text" = Slack, "content" = Discord
        try:
            req = urllib.request.Request(url, json.dumps({"text": text, "content": text, "score": score}).encode(),
                                         {"Content-Type": "application/json", **UA})
            urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            failed.append("webhook")
            log(f"webhook failed: {type(e).__name__}: {e}")
    if (tok := os.environ.get("TELEGRAM_TOKEN")) and (chat := os.environ.get("TELEGRAM_CHAT_ID")):
        try:
            get(f"https://api.telegram.org/bot{tok}/sendMessage", retry=False, chat_id=chat, text=text)
        except Exception as e:
            failed.append("telegram")
            log(f"telegram failed: {type(e).__name__}")
    return failed


def configured():
    return bool(os.environ.get("WEBHOOK_URL") or (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")))


def run(a):
    g = geocode(a.place, a.country)
    day = a.date or target_day(datetime.now(ZoneInfo(g["timezone"])))
    r = build_report(g, day, fetch_sources(g), trips_from_env())
    return r["text"], (r["summary"] or {}).get("score")


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
    try:
        with deadline(RUN_TIMEOUT):
            text, score = run(a)
    except BaseException as e:  # a silent night is the worst outcome: say it failed
        text, score = f"ride-forecast FAILED for {a.place}: {type(e).__name__}: {e}", -1
        log(text)
        if not a.dry:
            with deadline(30):
                send(text, score)
        return 1
    print(text)
    return 1 if (not a.dry and send(text, score)) else 0


if __name__ == "__main__":
    sys.exit(main())
