#!/usr/bin/env python3
"""Motorbike weather verdicts from several sources, no API keys. Core library + one-shot CLI.
Sources: 5 Open-Meteo models (ECMWF, GFS, ICON, Meteo-France, UK Met Office) + MET Norway.
Every source is scored per hour; an hour's score is the median across sources. Trips (your
commute windows) are judged on their worst hour; a dangerous hour reported by 2+ sources is a stop.
Usage: ride.py "Salamanca" [--country ES] [--date YYYY-MM-DD] [--dry]
Env:   RIDE_PLACE, RIDE_COUNTRY, RIDE_TRIPS, WEBHOOK_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Target day: before 12:00 local = today, after = tomorrow. The Telegram bot is in bot.py.
"""
import argparse, json, os, re, signal, statistics as st, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

DAY_HOURS = range(8, 21)  # the "Day" summary line, local time
FORECAST_DAYS = 8         # today + 7
RUN_TIMEOUT = 180  # s, whole one-shot run; the per-request timeout alone does not stop a slow-drip server
RETRIES = (1, 3)  # s waits between attempts
UA = {"User-Agent": "ride-forecast/2.0 github.com/n0nuser"}  # MET Norway requires an identifying UA
MODELS = {"ecmwf_ifs025": "ECMWF", "gfs_seamless": "GFS", "icon_seamless": "ICON",
          "meteofrance_seamless": "MeteoFr", "ukmo_seamless": "UKMO"}
VARS = ["temperature_2m", "relative_humidity_2m", "precipitation_probability", "precipitation", "wind_gusts_10m", "weather_code"]
BANDS = [(80, "Great day to ride"), (65, "Good day, ride"), (50, "Mixed: ride in the best window"),
         (35, "Marginal: short ride only if you must"), (0, "Stay home")]
TRIP_BANDS = [(70, "OK"), (45, "CAUTION"), (0, "STAY HOME")]
ICONS = {"OK": "🟢", "CAUTION": "🟡", "STAY HOME": "🔴"}
MIXED_MAX = BANDS[1][0] - 1  # a good window rescues a mediocre day, never above "mixed"
STOP_CAP = BANDS[2][0] - 1   # any dangerous hour caps the day at "marginal"
SPREAD_LIMIT, SPREAD_DIVISOR = 30, 5  # sources this far apart (points) lower the score by spread/5
STORM, SNOW_ICE, FOG = (95, 96, 99), (56, 57, 66, 67, 71, 73, 75, 77, 85, 86), (45, 48)
GUST_STOP = 70   # km/h: a hard stop on two wheels
ICE_T = 2        # deg C: at or below this, wet or damp air means ice risk
GUST_FACTOR = 1.5  # ponytail: MET Norway has no gusts; gust ~ 1.5 x mean wind. Crude, tune if it matters.
COLD_GEAR, RAIN_GEAR_MM, WIND_NOTE = 8, 0.2, 45  # gear hints: deg C, mm/h, km/h
DEFAULT_TRIPS = "work 07:30-09:00 mon-fri;home 17:00-19:00 mon-fri;out 20:00-23:00"
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}


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
        raise LookupError(f"place not found: {place}" + (f" in {country}" if country else ""))
    return res[0]


def place_label(g):
    return ", ".join(x for x in (g["name"], g.get("admin1"), g.get("country_code")) if x)


# ---- scoring -------------------------------------------------------------------------------

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


def penalties(h):
    pop = h.pop if h.pop is not None else min(90, h.mm * 150)  # no rain chance from the source: infer, smoothly
    return {"cold": min(60, max(0, 18 - h.t) * 3), "hot": min(60, max(0, h.t - 26) * 4),
            "rain": min(70, pop * 0.35 + h.mm * 40), "wind": min(30, max(0, (h.gust or 0) - 40) * 1.5),
            "damp": min(15, max(0, h.rh - 80) * 0.75), "fog": 20 if h.code in FOG else 0}


def hour_score(h):
    """100 = perfect; each factor subtracts; a stop reason is 0."""
    return 0 if stop_reason(h) else max(0, round(100 - sum(penalties(h).values())))


def main_reason(h):
    """The factor that costs this hour the most, in words."""
    if r := stop_reason(h):
        return r
    k, v = max(penalties(h).items(), key=lambda kv: kv[1])
    if v < 8:
        return "fine"
    return {"cold": f"cold {h.t:.0f}°C", "hot": f"hot {h.t:.0f}°C", "wind": f"gusts {h.gust or 0:.0f} km/h",
            "damp": f"damp {h.rh:.0f}%", "fog": "fog",
            "rain": f"rain {h.mm:.1f} mm" if h.mm >= 0.1 else f"{h.pop or 0:.0f}% rain chance"}[k]


# ---- fetching ------------------------------------------------------------------------------

def valid(h):
    return None not in (h.t, h.rh, h.mm, h.code)


def met_code(sym):  # MET Norway symbol -> the WMO codes this script cares about
    return STORM[0] if "thunder" in sym else SNOW_ICE[4] if ("snow" in sym or "sleet" in sym) else FOG[0] if sym == "fog" else 0


def open_meteo(g):
    w = get("https://api.open-meteo.com/v1/forecast", latitude=g["latitude"], longitude=g["longitude"], timezone=g["timezone"],
            forecast_days=FORECAST_DAYS, models=",".join(MODELS), hourly=",".join(VARS))
    h, out = w["hourly"], {}
    for m, name in MODELS.items():
        if f"temperature_2m_{m}" not in h:
            continue
        rows = {ts: Hour(*(h[f"{v}_{m}"][i] for v in VARS)) for i, ts in enumerate(h["time"])}
        out[name] = {ts: x for ts, x in rows.items() if valid(x)}
    return {n: s for n, s in out.items() if s}


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


def fetch_sources(g):
    tz = ZoneInfo(g["timezone"])
    sources = {}
    for name, fetch in (("Open-Meteo", lambda: open_meteo(g)), ("MET.no", lambda: {"MET.no": met_norway(g, tz)})):
        try:
            sources.update(fetch())
        except Exception as e:  # one provider down must not kill the report
            print(f"{name} skipped: {type(e).__name__}: {e}", file=sys.stderr)
    if not sources:
        raise RuntimeError("no weather source answered")
    return sources


# ---- trips ---------------------------------------------------------------------------------

def _days(spec):
    out = set()
    for part in spec.lower().split(","):
        a, _, b = part.partition("-")
        if a not in DAYS or (b and b not in DAYS):
            raise ValueError(f"bad day '{part}': use mon-fri, sat,sun, ...")
        i, j = DAYS[a], DAYS[b or a]
        out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
    return out


def _hm(s):
    h, _, m = s.partition(":")
    return int(h), int(m or 0)


def parse_trips(spec):
    """'work 07:30-09:00 mon-fri;home 17:00-19:00 mon-fri;out 20:00-23:00' -> list of trips. 'none' = no trips."""
    spec = spec.strip()
    if spec.lower() in ("", "none"):
        return []
    trips = []
    for entry in spec.split(";"):
        tok = entry.split()
        if not 2 <= len(tok) <= 3 or not re.fullmatch(r"\w+", tok[0]) or not re.fullmatch(r"\d{1,2}(:\d\d)?-\d{1,2}(:\d\d)?", tok[1]):
            raise ValueError(f"bad trip '{entry.strip()}': use NAME HH:MM-HH:MM [mon-fri]")
        a, b = (_hm(x) for x in tok[1].split("-"))
        if not (0 <= a[0] < 24 and 0 <= b[0] <= 24 and a[1] < 60 and b[1] < 60 and a < b and b <= (24, 0)):
            raise ValueError(f"bad times in '{entry.strip()}': start must be before end, same day")
        trips.append(dict(name=tok[0], start=a, end=b, days=_days(tok[2]) if len(tok) == 3 else set(range(7))))
    return trips


def trip_hours(t):
    return range(t["start"][0], t["end"][0] + (1 if t["end"][1] else 0))


def trip_window(t):
    return f"{t['start'][0]:02d}:{t['start'][1]:02d}-{t['end'][0]:02d}:{t['end'][1]:02d}"


# ---- analysis ------------------------------------------------------------------------------

def _row(hr, items):
    """One hour: the score of each source, their median, and a consensus weather for the description."""
    per = {n: hour_score(h) for n, h in items.items()}
    hs = list(items.values())
    reasons = [r for h in hs if (r := stop_reason(h))]
    stop = Counter(reasons).most_common(1)[0][0] if len(reasons) >= min(2, len(hs)) else None  # 2 sources agree
    pops, gusts = [h.pop for h in hs if h.pop is not None], [h.gust for h in hs if h.gust is not None]
    cons = Hour(st.median(h.t for h in hs), st.median(h.rh for h in hs), max(pops) if pops else None,
                st.median(h.mm for h in hs), max(gusts) if gusts else None, Counter(h.code for h in hs).most_common(1)[0][0])
    return dict(hour=hr, score=0 if stop else st.median(per.values()), stop=stop, per=per, w=cons, n=len(hs),
                reason=stop or main_reason(cons))


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


def eval_trip(t, by_hour):
    rows = [by_hour[h] for h in trip_hours(t) if h in by_hour]
    if not rows:
        return None
    worst = min(rows, key=lambda r: r["score"])
    spread = max(max(r["per"].values()) - min(r["per"].values()) for r in rows)
    score = worst["score"] - (round(spread / SPREAD_DIVISOR) if spread > SPREAD_LIMIT else 0)
    stop = next((r for r in rows if r["stop"]), None)
    verdict = "STAY HOME" if stop else next(v for m, v in TRIP_BANDS if score >= m)
    where = stop or worst
    return dict(name=t["name"], window=trip_window(t), score=max(0, score), verdict=verdict, spread=spread,
                tmin=min(r["w"].t for r in rows), tmax=max(r["w"].t for r in rows),
                reason="fine" if where["reason"] == "fine" else f"{where['reason']} at {where['hour']:02d}:00",
                rain=any(r["w"].mm >= RAIN_GEAR_MM for r in rows), fog=any(r["w"].code in FOG for r in rows),
                gust=max((r["w"].gust or 0) for r in rows), hours=[r["hour"] for r in rows])


def day_summary(rows):
    """The old whole-day view over DAY_HOURS: average, best 3 hours, disagreement, stop cap."""
    rs = [r for r in rows if r["hour"] in DAY_HOURS]
    if len(rs) < 3:
        return None
    hourly = [r["score"] for r in rs]
    best = max(range(len(hourly) - 2), key=lambda i: sum(hourly[i:i + 3]))
    avg, win = round(st.mean(hourly)), round(st.mean(hourly[best:best + 3]))
    day_scores = {n: round(st.mean(r["per"][n] for r in rs if n in r["per"])) for n in {n for r in rs for n in r["per"]}}
    spread = max(day_scores.values()) - min(day_scores.values())
    score = max(avg, min(win - 15, MIXED_MAX))
    if spread > SPREAD_LIMIT:
        score = max(0, score - round(spread / SPREAD_DIVISOR))
    if any(r["stop"] for r in rs):
        score = min(score, STOP_CAP)
    start = rs[best]["hour"]
    return dict(score=score, verdict=next(v for m, v in BANDS if score >= m), window=f"{start:02d}:00-{start + 3:02d}:00",
                window_score=win, day_scores=day_scores, spread=spread, n=min(r["n"] for r in rs))


def target_day(now):
    """Before noon local: today (late catch-up run). Otherwise tomorrow."""
    return (now.date() if now.hour < 12 else now.date() + timedelta(days=1)).isoformat()


def build_report(g, day, sources, trips):
    """Everything about one day: text for chat, plus the data a chart needs."""
    d = date.fromisoformat(day)
    rows = hourly_rows(sources, day)
    by_hour = {r["hour"]: r for r in rows}
    ev = [e for t in trips if d.weekday() in t["days"] if (e := eval_trip(t, by_hour))]
    summ = day_summary(rows)
    lines = [f"{d.strftime('%a %d %b')} · {place_label(g)}"]
    for e in ev:
        lines.append(f"{ICONS[e['verdict']]} {e['name']} {e['window']}: {e['verdict']}, {e['tmin']:.0f}-{e['tmax']:.0f}°C, {e['reason']}"
                     + (", sources disagree" if e["spread"] > SPREAD_LIMIT else ""))
    if summ:
        lines.append(f"Day 08-20: {summ['verdict']} ({summ['score']}/100), best {summ['window']} ({summ['window_score']})")
    gear_rows = [by_hour[h] for e in ev for h in e["hours"]] or [r for r in rows if r["hour"] in DAY_HOURS]
    gear = ([f"warm layers and gloves ({min(r['w'].t for r in gear_rows):.0f}°C)"] if gear_rows and min(r["w"].t for r in gear_rows) < COLD_GEAR else []) \
        + (["rain gear"] if any(r["w"].mm >= RAIN_GEAR_MM for r in gear_rows) else []) \
        + (["clear visor, fog"] if any(r["w"].code in FOG for r in gear_rows) else []) \
        + ([f"strong wind, {max(r['w'].gust or 0 for r in gear_rows):.0f} km/h gusts"] if any((r["w"].gust or 0) >= WIND_NOTE for r in gear_rows) else [])
    if gear:
        lines.append("Gear: " + ", ".join(gear))
    if summ:
        sc = summ["day_scores"]
        agree = ("ONLY 1 source, low confidence" if len(sc) == 1 else "sources agree" if summ["spread"] <= 15
                 else "some disagreement" if summ["spread"] <= 30 else "SOURCES DISAGREE, low confidence")
        lines.append(f"{len(sc)} sources, {agree}: " + " · ".join(f"{n} {s}" for n, s in sorted(sc.items(), key=lambda x: -x[1])))
    if not ev and not summ:
        lines.append("Not enough data for this day yet.")
    return dict(text="\n".join(lines), rows=rows, trips=ev, summary=summ, day=day, label=place_label(g))


# ---- delivery ------------------------------------------------------------------------------

def send(text, score=None):
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
    r = build_report(g, day, fetch_sources(g), parse_trips(os.environ.get("RIDE_TRIPS", DEFAULT_TRIPS)))
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
