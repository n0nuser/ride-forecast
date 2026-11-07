import contextlib, io, json, os, time, urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import ride
from ride import Hour, hour_score, stop_reason, target_day, main, parse_trips, trip_hours, hourly_rows, eval_trip, day_summary, build_report

FIX = Path(__file__).parent / "tests" / "fixtures"
OK = Hour(22, 50, 0, 0.0, 10, 0)
DAY, MON, SUN = "2026-01-02", "2026-11-09", "2026-11-08"   # DAY is a Friday; MON/SUN are Monday/Sunday
G = {"name": "X", "country_code": "ES", "timezone": "Europe/Madrid", "latitude": 1, "longitude": 1}
day_of = lambda day=DAY, **kw: {f"{day}T{h:02d}:00": OK._replace(**kw) for h in range(24)}
with_hours = lambda src, hours, day=DAY, **kw: {**src, **{f"{day}T{h:02d}:00": OK._replace(**kw) for h in hours}}
TRIPS = parse_trips("work 07:30-09:00 mon-fri;home 17:00-19:00 mon-fri;out 20:00-23:00")   # test data, not a default
REAL_GET, REAL_SLEEP, REAL_URLOPEN = ride.get, time.sleep, ride.urllib.request.urlopen
quiet = lambda: contextlib.redirect_stderr(io.StringIO())

# --- trips are personal: nothing built in
assert not hasattr(ride, "DEFAULT_TRIPS")
os.environ.pop("RIDE_TRIPS", None)
assert ride.trips_from_env() == []
os.environ["RIDE_TRIPS"] = "gym 18:00-19:00 tue"
assert ride.trips_from_env()[0]["name"] == "gym"
os.environ.pop("RIDE_TRIPS")

# --- hour_score: graded, with hard stops
assert hour_score(OK) == 100
assert hour_score(OK._replace(pop=90, mm=3, code=61)) < 35                 # rain
assert hour_score(OK._replace(code=95)) == 0                               # thunderstorm
assert hour_score(OK._replace(gust=100)) == 0 and hour_score(OK._replace(gust=69)) > 0   # gust stop at 70
assert 40 < hour_score(OK._replace(t=30, pop=10, gust=35)) < 90            # hot: degraded, not binary
assert hour_score(OK._replace(pop=None, gust=None)) == 100                 # source without pop/gust
assert hour_score(OK._replace(pop=None, mm=0.5)) < 60                      # steady rain, no pop: not "good"
a, b = hour_score(OK._replace(pop=None, mm=0.29)), hour_score(OK._replace(pop=None, mm=0.30))
assert abs(a - b) <= 3, (a, b)                                             # no cliff at 0.3 mm
assert hour_score(OK._replace(code=45)) == 80                              # fog costs 20
assert stop_reason(OK) is None and stop_reason(OK._replace(code=71)) == "snow or freezing rain"
assert ride.main_reason(OK) == "fine" and "cold" in ride.main_reason(OK._replace(t=4, rh=40)) and "gusts" in ride.main_reason(OK._replace(gust=60))

# --- ice: cold plus damp or wet. A hard stop at 2 C, a graded cost up to 5 C, nothing when dry or mild
for t, rh in ((2, 90), (0, 95), (-3, 92), (1, 60)):
    assert hour_score(OK._replace(t=t, rh=rh, mm=0.1 if rh < 90 else 0)) == 0, (t, rh)
assert stop_reason(OK._replace(t=2, rh=89)) is None and stop_reason(OK._replace(t=2, rh=89, mm=0.1)) == "ice risk"
assert ride.describe_stop(OK._replace(t=1, rh=93), "ice risk") == "ice risk (1°C, 93% humidity)"
damp, dry = hour_score(OK._replace(t=4, rh=90)), hour_score(OK._replace(t=4, rh=40))
assert dry - damp >= 15, (dry, damp)                                       # same cold, damp costs a lot more
assert hour_score(OK._replace(t=4, rh=60)) == dry                          # humidity below 70% is no ice concern
assert hour_score(OK._replace(t=4, mm=0.3)) < hour_score(OK._replace(t=4, rh=90, mm=0)) <= hour_score(OK._replace(t=4, rh=80))   # rain is full moisture
assert ride.ice_risk(OK._replace(t=6, rh=100)) == 0 and ride.ice_risk(OK._replace(t=20, mm=2)) == 0   # mild: no frost whatever the humidity
assert 0 < ride.ice_risk(OK._replace(t=3, rh=85)) < ride.ice_risk(OK._replace(t=1, rh=85)) < ride.ice_risk(OK._replace(t=1, rh=95))
assert "frost risk (3°C, 95% humidity)" in ride.main_reason(OK._replace(t=3, rh=95))
assert ride.main_reason(OK._replace(t=4, rh=95)).startswith("cold")   # risk 0.2: not yet worth naming
frost = day_of(t=3, rh=92)   # not a stop, but the trip lines and the gear line say so
rep = build_report(G, DAY, {"a": frost, "b": frost}, TRIPS)
assert "92% humidity" in rep["text"] and "watch for ice on bridges and in shade" in rep["text"] and "warm layers and gloves (3°C)" in rep["text"]
assert "ice" not in build_report(G, DAY, {"a": day_of(t=4, rh=50)}, TRIPS)["text"]

# --- trips: parsing and hours
assert [t["name"] for t in TRIPS] == ["work", "home", "out"]
assert list(trip_hours(TRIPS[0])) == [7, 8] and list(trip_hours(TRIPS[1])) == [17, 18] and list(trip_hours(TRIPS[2])) == [20, 21, 22]
assert list(trip_hours(parse_trips("a 17:00-18:30")[0])) == [17, 18]       # a half hour still touches that hour
assert TRIPS[0]["days"] == {0, 1, 2, 3, 4} and parse_trips("a 8-9 fri-mon")[0]["days"] == {4, 5, 6, 0}
assert parse_trips("none") == [] and parse_trips("") == [] and parse_trips("a 8-9 sat,sun")[0]["days"] == {5, 6} and parse_trips("a 8-9 lun-vie")[0]["days"] == {0, 1, 2, 3, 4}
for bad in ("work", "work 9-7", "work 25:00-26:00", "work 07:30-09:00 funday", "wo rk 7-9", "w 7-9 mon-fri extra"):
    try:
        parse_trips(bad); raise SystemExit(f"expected failure: {bad}")
    except ride.UserError:
        pass

# --- a dangerous hour in a trip is a stop and is named; the same hour outside any trip still caps the day
sun = day_of()
storm = {n: with_hours(sun, [17], code=95) for n in "abc"}
rows = hourly_rows(storm, DAY)
by = {r["hour"]: r for r in rows}
e = eval_trip(TRIPS[1], by)
assert e["verdict"] == "STAY HOME" and e["reason"] == "thunderstorm at 17:00", e
assert eval_trip(TRIPS[0], by)["verdict"] == "OK"                          # the morning trip is unaffected
assert day_summary(rows)["score"] <= 49
ice = {n: with_hours(sun, [8, 9, 10], t=0, rh=95) for n in "abc"}
assert day_summary(hourly_rows(ice, DAY))["score"] <= 49
ev = eval_trip(TRIPS[0], {r["hour"]: r for r in hourly_rows(ice, DAY)})
assert ev["verdict"] == "STAY HOME" and ev["reason"] == "ice risk (0°C, 95% humidity) at 08:00", ev

# --- stop votes: 2 of 5 count, 1 of 5 is outvoted but visible, a sole source is believed
two = {**{n: with_hours(sun, [17], code=95) for n in "ab"}, **{n: sun for n in "cde"}}
assert {r["hour"]: r for r in hourly_rows(two, DAY)}[17]["stop"]
one = {"a": with_hours(sun, [17], code=95), **{n: sun for n in "bcde"}}
r17 = {r["hour"]: r for r in hourly_rows(one, DAY)}[17]
assert r17["stop"] is None and r17["per"]["a"] == 0 and r17["score"] == 100
assert {r["hour"]: r for r in hourly_rows({"a": with_hours(sun, [17], code=95)}, DAY)}[17]["stop"]

# --- disagreement lowers a trip's score and is said in the text; a partial source is not counted as a full one
bad = {k: OK._replace(t=2, rh=95, mm=1.0, pop=90, code=61) for k in sun}
rep = build_report(G, DAY, {"a": sun, "b": bad}, TRIPS)
assert rep["trips"][0]["spread"] > 30 and "sources disagree" in rep["text"] and "DISAGREE" in rep["text"]
assert "ONLY 1 source" in build_report(G, DAY, {"a": sun}, TRIPS)["text"]
morning_only = {k: v for k, v in sun.items() if int(k[11:13]) < 14}
assert "ONLY 1 source" in build_report(G, DAY, {"a": sun, "b": morning_only}, TRIPS)["text"]   # b lacks the afternoon: not a full source
assert "2 sources" in build_report(G, DAY, {"a": sun, "b": sun}, TRIPS)["text"]

# --- trips only apply on their days: Monday has work and home, Sunday only out; no trips still gives the day line
mon = build_report(G, MON, {"a": day_of(MON), "b": day_of(MON)}, TRIPS)
sund = build_report(G, SUN, {"a": day_of(SUN), "b": day_of(SUN)}, TRIPS)
assert [t["name"] for t in mon["trips"]] == ["work", "home", "out"] and [t["name"] for t in sund["trips"]] == ["out"]
assert "🟢 work 07:30-09:00: OK, 22°C, 50% humidity, fine" in mon["text"] and "Mon 09 Nov · X, ES" in mon["text"].splitlines()[0]
assert "Gear" not in mon["text"]
assert "warm layers and gloves (4°C)" in build_report(G, MON, {"a": day_of(MON, t=4, rh=40), "b": day_of(MON, t=4, rh=40)}, TRIPS)["text"]
rainy = build_report(G, MON, {"a": day_of(MON, mm=0.5), "b": day_of(MON, mm=0.5)}, TRIPS)["text"]
assert "rain gear" in rainy and "rain 0.5 mm" in rainy
notrips = build_report(G, MON, {"a": day_of(MON)}, [])
assert notrips["trips"] == [] and "Day 08-20" in notrips["text"]

# --- disagreement lowers the numbers, not just the label: a perfect source and a poor one
poor = next(OK._replace(pop=None, mm=m / 100) for m in range(1, 200) if 40 <= hour_score(OK._replace(pop=None, mm=m / 100)) <= 45)
half = {"a": sun, "b": {k: poor for k in sun}}
avg_hour = (100 + hour_score(poor)) / 2                                    # >= 70: "OK" if the sources were not so far apart
rows2 = hourly_rows(half, DAY)
assert all(r["score"] == avg_hour for r in rows2) and avg_hour >= 70
spread = 100 - hour_score(poor)
tr = eval_trip(TRIPS[0], {r["hour"]: r for r in rows2})
assert tr["score"] == avg_hour - round(spread / ride.SPREAD_DIVISOR) and tr["verdict"] == "CAUTION", tr   # the penalty flips OK to CAUTION
ds = day_summary(rows2)
assert ds["score"] == round(avg_hour) - round(ds["spread"] / ride.SPREAD_DIVISOR) and ds["spread"] == spread, ds

# --- partial data: a source missing hours still helps for the hours it has; no data at all is an error
partial = {k: v for k, v in sun.items() if not k.endswith("T12:00")}
assert {r["hour"]: r for r in hourly_rows({"a": sun, "b": partial}, DAY)}[12]["n"] == 1
assert {r["hour"]: r for r in hourly_rows({"a": sun, "b": partial}, DAY)}[11]["n"] == 2
try:
    hourly_rows({"a": sun}, "2030-01-01"); raise SystemExit("expected failure")
except RuntimeError:
    pass

# --- the three outside APIs, pinned to real captures (tests/fixtures): a changed response shape fails here
geo, om, met = (json.loads((FIX / n).read_text()) for n in ("geocoding.json", "open_meteo_gmt.json", "met_norway.json"))
ride.get = lambda url, **p: geo
g = ride.geocode("Salamanca", "ES")
assert g["name"] == "Salamanca" and g["country_code"] == "ES" and g["timezone"] == "Europe/Madrid" and {"latitude", "longitude", "admin1"} <= set(g)
ride.get = lambda url, **p: om
src = ride.open_meteo(g)
assert set(src) == set(ride.MODELS.values()), set(src)
first_utc = om["hourly"]["time"][0]                                      # "2026-11-07T00:00" in GMT
assert min(src["ECMWF"]) == "2026-11-07T01:00"                           # CET is UTC+1: each instant is converted, not relabelled
assert isinstance(src["GFS"]["2026-11-07T12:00"], Hour) and src["GFS"]["2026-11-07T12:00"].t == om["hourly"]["temperature_2m_gfs_seamless"][11]
ride.get = lambda url, **p: met
mn = ride.met_norway(g, ZoneInfo("Europe/Madrid"))
k0 = sorted(mn)[0]
assert mn[k0].t == met["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"] and mn[k0].gust is not None and mn[k0].pop is None
assert k0 == ride.local_key(met["properties"]["timeseries"][0]["time"], ZoneInfo("Europe/Madrid"))
ride.get = lambda url, **p: om if "open-meteo" in url else met
real = ride.fetch_sources(g)
assert "MET.no" in real and "ECMWF" in real
text = build_report(g, "2026-11-08", real, TRIPS)["text"]
assert text.startswith("Sun 08 Nov · Salamanca") and "sources" in text

# --- DST: every source is aligned on the real instant (Madrid: spring forward 2026-03-29, fall back 2026-10-25)
tz = ZoneInfo("Europe/Madrid")
assert ride.local_key("2026-03-29T00:00", tz) == "2026-03-29T01:00" and ride.local_key("2026-03-29T01:00", tz) == "2026-03-29T03:00"   # no 02:00 that day
assert ride.local_key("2026-07-01T10:00", tz) == "2026-07-01T12:00" and ride.local_key("2026-07-01T10:00:00Z", tz) == "2026-07-01T12:00"
assert ride.local_key("2026-10-25T00:00", tz) == "2026-10-25T02:00" == ride.local_key("2026-10-25T01:00", tz)   # 02:00 happens twice
fake = {"hourly": {"time": ["2026-07-01T10:00"], **{f"{v}_{m}": [1] for m in ride.MODELS for v in ride.VARS}}}
fake["hourly"]["weather_code_gfs_seamless"] = [None]
ride.get = lambda url, **p: fake
assert "GFS" not in ride.open_meteo(G) and list(ride.open_meteo(G)["ECMWF"]) == ["2026-07-01T12:00"]   # a missing code drops the hour, not "clear"

# --- retries: only transient failures, never a bug, never a message
calls = []
def flaky(*errors):
    seq = list(errors)
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        e = seq.pop(0) if seq else None
        if e:
            raise e
        return io.BytesIO(b'{"ok": 1}')
    return fake_urlopen
ride.get, time.sleep = REAL_GET, lambda s: None
url_err = urllib.error.URLError("down")
http = lambda code: urllib.error.HTTPError("u", code, "m", {}, None)
for errs, expected in (((url_err, url_err), 3), ((http(503),), 2), ((http(429),), 2), ((http(404),), 1), ((json.JSONDecodeError("x", "", 0),), 1), ((url_err, url_err, url_err, url_err), 3)):
    calls.clear(); ride.urllib.request.urlopen = flaky(*errs)
    try:
        ride.get("http://x")
    except Exception:
        pass
    assert len(calls) == expected, (errs, len(calls))
calls.clear(); ride.urllib.request.urlopen = flaky(url_err)
try:
    ride.get("http://x", retry=False)
except urllib.error.URLError:
    assert len(calls) == 1                                                  # a one-shot call is never repeated
ride.urllib.request.urlopen, time.sleep = REAL_URLOPEN, REAL_SLEEP

# --- deadline: a whole-call limit that a broad `except Exception` cannot swallow, and stuck threads cannot hold up
t0 = time.time()
try:
    with ride.deadline(1):
        try:
            time.sleep(5)
        except Exception:
            pass
    raise SystemExit("deadline did not fire")
except ride.Deadline:
    assert time.time() - t0 < 3
ride.open_meteo = lambda g: time.sleep(4) or {}
ride.met_norway = lambda g, tz: time.sleep(4) or {}
t0 = time.time()
try:
    with ride.deadline(1):
        ride.fetch_sources(G)
except ride.Deadline:
    assert time.time() - t0 < 3, "fetch_sources waited for its stuck threads"
ride.open_meteo = lambda g: (time.sleep(0.4), {"A": sun})[1]
ride.met_norway = lambda g, tz: (time.sleep(0.4), sun)[1]
t0 = time.time()
assert set(ride.fetch_sources(G)) == {"A", "MET.no"} and time.time() - t0 < 0.7   # providers run side by side, not one after the other

# --- logs never carry the token
os.environ["TELEGRAM_TOKEN"] = "123456:SECRETTOKEN"
err = io.StringIO()
with contextlib.redirect_stderr(err):
    ride.log("boom at https://api.telegram.org/bot123456:SECRETTOKEN/sendMessage and bot999:OTHER-secret and 123456:SECRETTOKEN # my bot")
assert "SECRETTOKEN" not in err.getvalue() and "OTHER-secret" not in err.getvalue() and "bot***" in err.getvalue()
os.environ.pop("TELEGRAM_TOKEN")

# --- target day: late catch-up runs must not forecast the day after
assert target_day(datetime(2026, 11, 7, 21, 0, tzinfo=tz)) == "2026-11-08"
assert target_day(datetime(2026, 11, 8, 0, 30, tzinfo=tz)) == "2026-11-08"   # after midnight: still the ride day

# --- sending: channels are independent, a message is sent once, failures surface and never leak the token
sent_calls = []
class Boom(Exception): pass
def fake_open(req, timeout=None):
    sent_calls.append("webhook"); raise Boom("500")
ride.urllib.request.urlopen = fake_open
def fake_get(url, **p):
    sent_calls.append(("telegram", p.get("retry"))); return {}
ride.get = fake_get
os.environ.update(WEBHOOK_URL="http://x", TELEGRAM_TOKEN="123456:SECRET", TELEGRAM_CHAT_ID="1")
with quiet():
    assert ride.send("hi", 50) == ["webhook"] and sent_calls == ["webhook", ("telegram", False)]   # webhook down, telegram still sent, no retry
def bad_get(url, **p): raise Boom("https://api.telegram.org/bot123456:SECRET/x")
ride.get = bad_get
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert ride.send("hi", 50) == ["webhook", "telegram"]
assert "SECRET" not in err.getvalue()
ride.urllib.request.urlopen = REAL_URLOPEN

# --- CLI: unknown flags are errors; failures send a message and exit 1; a source down still reports
for badarg in (["Salamanca", "--dry-run"], ["Salamanca", "-dry"]):
    with quiet():
        try:
            main(badarg); raise SystemExit("expected argparse error")
        except SystemExit as e:
            assert e.code == 2, badarg
for k in ("WEBHOOK_URL", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "RIDE_PLACE"):
    os.environ.pop(k, None)
with quiet():
    try:
        main(["Salamanca"]); raise SystemExit("expected: no channel configured")
    except SystemExit as e:
        assert e.code == 2
sent = []
real_send, real_run = ride.send, ride.run
ride.send = lambda text, score=None: sent.append(text) or []
ride.run = lambda a: (_ for _ in ()).throw(Boom("api down"))
os.environ["WEBHOOK_URL"] = "http://x"
with quiet():
    assert main(["Salamanca"]) == 1
assert sent and sent[0].startswith("ride-forecast FAILED"), sent
ride.send, ride.run = real_send, real_run
ride.geocode = lambda p, c=None: G
def om_down(g): raise Boom("503")
ride.open_meteo = om_down
ride.met_norway = lambda g, tz: day_of(MON)
with quiet():
    text, score = ride.run(ride.argparse.Namespace(place="X", country=None, date=MON))
assert "1 source" in text and score == 100, text

# --- geocoding: the country filter picks the right Leon; a miss is a user error, not a crash
ride.get = lambda url, **p: {"results": [{"name": "León", "country_code": "NI"}, {"name": "León", "country_code": "ES"}]}
import importlib; importlib.reload(ride)
ride.get = lambda url, **p: {"results": [{"name": "León", "country_code": "NI"}, {"name": "León", "country_code": "ES"}]}
assert ride.geocode("Leon", "es")["country_code"] == "ES" and ride.geocode("Leon")["country_code"] == "NI"
try:
    ride.geocode("Leon", "FR"); raise SystemExit("expected UserError")
except ride.UserError:
    pass
print("ok")
