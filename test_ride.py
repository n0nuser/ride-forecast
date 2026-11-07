import contextlib, io, os
from datetime import datetime
from zoneinfo import ZoneInfo
import ride
from ride import Hour, hour_score, stop_reason, target_day, main, parse_trips, trip_hours, hourly_rows, eval_trip, day_summary, build_report

OK = Hour(22, 50, 0, 0.0, 10, 0)
DAY, MON, SUN = "2026-01-02", "2026-11-09", "2026-11-08"   # DAY is a Friday; MON/SUN are Monday/Sunday
G = {"name": "X", "country_code": "ES", "timezone": "Europe/Madrid", "latitude": 1, "longitude": 1}
day_of = lambda day=DAY, **kw: {f"{day}T{h:02d}:00": OK._replace(**kw) for h in range(24)}
with_hours = lambda src, hours, day=DAY, **kw: {**src, **{f"{day}T{h:02d}:00": OK._replace(**kw) for h in hours}}
TRIPS = parse_trips(ride.DEFAULT_TRIPS)

# --- hour_score: graded, with hard stops
assert hour_score(OK) == 100
assert hour_score(OK._replace(pop=90, mm=3, code=61)) < 35                 # rain
assert hour_score(OK._replace(t=2, rh=90)) == 0 and hour_score(OK._replace(t=0, rh=95)) == 0   # ice risk
assert hour_score(OK._replace(code=95)) == 0                               # thunderstorm
assert hour_score(OK._replace(gust=100)) == 0 and hour_score(OK._replace(gust=69)) > 0   # gust stop at 70
assert 40 < hour_score(OK._replace(t=30, pop=10, gust=35)) < 90            # hot: degraded, not binary
assert hour_score(OK._replace(pop=None, gust=None)) == 100                 # source without pop/gust
assert hour_score(OK._replace(pop=None, mm=0.5)) < 60                      # steady rain, no pop: not "good"
a, b = hour_score(OK._replace(pop=None, mm=0.29)), hour_score(OK._replace(pop=None, mm=0.30))
assert abs(a - b) <= 3, (a, b)                                             # no cliff at 0.3 mm
assert hour_score(OK._replace(code=45)) == 80                              # fog costs 20
assert stop_reason(OK) is None and stop_reason(OK._replace(code=71)) == "snow or freezing rain"
assert ride.main_reason(OK) == "fine" and "cold" in ride.main_reason(OK._replace(t=4)) and "gusts" in ride.main_reason(OK._replace(gust=60))

# --- trips: parsing and hours
assert [t["name"] for t in TRIPS] == ["work", "home", "out"]
assert list(trip_hours(TRIPS[0])) == [7, 8] and list(trip_hours(TRIPS[1])) == [17, 18] and list(trip_hours(TRIPS[2])) == [20, 21, 22]
assert list(trip_hours(parse_trips("a 17:00-18:30")[0])) == [17, 18]       # a half hour still touches that hour
assert TRIPS[0]["days"] == {0, 1, 2, 3, 4} and parse_trips("a 8-9 fri-mon")[0]["days"] == {4, 5, 6, 0}
assert parse_trips("none") == [] and parse_trips("a 8-9 sat,sun")[0]["days"] == {5, 6} and parse_trips("a 8-9 lun-vie")[0]["days"] == {0, 1, 2, 3, 4}
for bad in ("work", "work 9-7", "work 25:00-26:00", "work 07:30-09:00 funday", "wo rk 7-9", "w 7-9 mon-fri extra"):
    try:
        parse_trips(bad); raise SystemExit(f"expected failure: {bad}")
    except ValueError:
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
assert eval_trip(TRIPS[0], {r["hour"]: r for r in hourly_rows(ice, DAY)})["verdict"] == "STAY HOME"

# --- stop votes: 2 of 5 count, 1 of 5 is outvoted but visible, a sole source is believed
two = {**{n: with_hours(sun, [17], code=95) for n in "ab"}, **{n: sun for n in "cde"}}
assert {r["hour"]: r for r in hourly_rows(two, DAY)}[17]["stop"]
one = {"a": with_hours(sun, [17], code=95), **{n: sun for n in "bcde"}}
r17 = {r["hour"]: r for r in hourly_rows(one, DAY)}[17]
assert r17["stop"] is None and r17["per"]["a"] == 0 and r17["score"] == 100
assert {r["hour"]: r for r in hourly_rows({"a": with_hours(sun, [17], code=95)}, DAY)}[17]["stop"]

# --- disagreement lowers a trip's score and is said in the text
bad = {k: OK._replace(t=2, rh=95, mm=1.0, pop=90, code=61) for k in sun}
rep = build_report(G, DAY, {"a": sun, "b": bad}, TRIPS)
assert rep["trips"][0]["spread"] > 30 and "sources disagree" in rep["text"] and "DISAGREE" in rep["text"]
assert "ONLY 1 source" in build_report(G, DAY, {"a": sun}, TRIPS)["text"]

# --- trips only apply on their days: Monday has work and home, Sunday only out
mon = build_report(G, MON, {"a": day_of(MON), "b": day_of(MON)}, TRIPS)
sund = build_report(G, SUN, {"a": day_of(SUN), "b": day_of(SUN)}, TRIPS)
assert [t["name"] for t in mon["trips"]] == ["work", "home", "out"] and [t["name"] for t in sund["trips"]] == ["out"]
assert "🟢 work 07:30-09:00: OK, 22-22°C, fine" in mon["text"] and "Mon 09 Nov · X, ES" in mon["text"].splitlines()[0]
assert "Gear" not in mon["text"]
cold = build_report(G, MON, {"a": day_of(MON, t=4), "b": day_of(MON, t=4)}, TRIPS)["text"]
assert "warm layers and gloves (4°C)" in cold
rainy = build_report(G, MON, {"a": day_of(MON, mm=0.5), "b": day_of(MON, mm=0.5)}, TRIPS)["text"]
assert "rain gear" in rainy and "rain 0.5 mm" in rainy
assert "no trips" not in build_report(G, MON, {"a": day_of(MON)}, [])["text"] and build_report(G, MON, {"a": day_of(MON)}, [])["trips"] == []

# --- partial data: a source missing hours still helps for the hours it has; no data at all is an error
partial = {k: v for k, v in sun.items() if not k.endswith("T12:00")}
assert {r["hour"]: r for r in hourly_rows({"a": sun, "b": partial}, DAY)}[12]["n"] == 1
assert {r["hour"]: r for r in hourly_rows({"a": sun, "b": partial}, DAY)}[11]["n"] == 2
try:
    hourly_rows({"a": sun}, "2030-01-01"); raise SystemExit("expected failure")
except RuntimeError:
    pass
# Open-Meteo hours with a missing value are dropped, not scored as clear
fake = {"hourly": {"time": [f"{DAY}T00:00", f"{DAY}T01:00"], **{f"{v}_{m}": [1, 1] for m in ride.MODELS for v in ride.VARS}}}
fake["hourly"]["weather_code_gfs_seamless"] = [0, None]
ride.get = lambda url, **p: fake
assert list(ride.open_meteo(G)["GFS"]) == [f"{DAY}T00:00"] and len(ride.open_meteo(G)["ECMWF"]) == 2

# --- target day: late catch-up runs must not forecast the day after
tz = ZoneInfo("Europe/Madrid")
assert target_day(datetime(2026, 11, 7, 21, 0, tzinfo=tz)) == "2026-11-08"
assert target_day(datetime(2026, 11, 8, 0, 30, tzinfo=tz)) == "2026-11-08"   # after midnight: still the ride day

# --- sending: channels are independent, failures surface in the exit code and never leak the token
calls = []
class Boom(Exception): pass
def fake_open(req, timeout=None):
    calls.append("webhook"); raise Boom("500")
ride.urllib.request.urlopen = fake_open
ride.get = lambda url, **p: calls.append("telegram")
os.environ.update(WEBHOOK_URL="http://x", TELEGRAM_TOKEN="SECRET", TELEGRAM_CHAT_ID="1")
with contextlib.redirect_stderr(io.StringIO()):
    assert ride.send("hi", 50) == ["webhook"] and calls == ["webhook", "telegram"]   # webhook down, telegram still sent
def bad_get(url, **p): raise Boom("https://api.telegram.org/botSECRET/x")
ride.get = bad_get
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert ride.send("hi", 50) == ["webhook", "telegram"]
assert "SECRET" not in err.getvalue()

# --- CLI: unknown flags are errors; failures send a message and exit 1; a source down still reports
for badarg in (["Salamanca", "--dry-run"], ["Salamanca", "-dry"]):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            main(badarg); raise SystemExit("expected argparse error")
        except SystemExit as e:
            assert e.code == 2, badarg
for k in ("WEBHOOK_URL", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "RIDE_PLACE"):
    os.environ.pop(k, None)
with contextlib.redirect_stderr(io.StringIO()):
    try:
        main(["Salamanca"]); raise SystemExit("expected: no channel configured")
    except SystemExit as e:
        assert e.code == 2
sent = []
real_send, real_run = ride.send, ride.run
ride.send = lambda text, score=None: sent.append(text) or []
ride.run = lambda a: (_ for _ in ()).throw(Boom("api down"))
os.environ["WEBHOOK_URL"] = "http://x"
with contextlib.redirect_stderr(io.StringIO()):
    assert main(["Salamanca"]) == 1
assert sent and sent[0].startswith("ride-forecast FAILED"), sent
ride.send, ride.run = real_send, real_run
ride.geocode = lambda p, c=None: G
def om_down(g): raise Boom("503")
ride.open_meteo = om_down
ride.met_norway = lambda g, tz: day_of(MON)
with contextlib.redirect_stderr(io.StringIO()):
    text, score = ride.run(ride.argparse.Namespace(place="X", country=None, date=MON))
assert "1 source" in text and score == 100, text

# --- geocoding: the country filter picks the right Leon; a miss is a LookupError
ride.get = lambda url, **p: {"results": [{"name": "León", "country_code": "NI"}, {"name": "León", "country_code": "ES"}]}
import importlib; importlib.reload(ride)
ride.get = lambda url, **p: {"results": [{"name": "León", "country_code": "NI"}, {"name": "León", "country_code": "ES"}]}
assert ride.geocode("Leon", "es")["country_code"] == "ES" and ride.geocode("Leon")["country_code"] == "NI"
try:
    ride.geocode("Leon", "FR"); raise SystemExit("expected LookupError")
except LookupError:
    pass
print("ok")
