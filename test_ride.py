import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import ride
from ride import Hour, hour_score, analyse, stop_reason, target_day, message, main

OK = Hour(22, 50, 0, 0.0, 10, 0)
DAY = "2026-01-02"
day_of = lambda **kw: {f"{DAY}T{h:02d}:00": OK for h in range(8, 21)}
with_hour = lambda src, h, **kw: {**src, f"{DAY}T{h:02d}:00": OK._replace(**kw)}

# --- hour_score: graded, with hard stops
assert hour_score(OK) == 100
assert hour_score(OK._replace(pop=90, mm=3, code=61)) < 35                 # rain
assert hour_score(OK._replace(t=2, rh=90)) == 0                            # ice risk
assert hour_score(OK._replace(t=0, rh=95)) == 0                            # the 0 C / 95% case
assert hour_score(OK._replace(code=95)) == 0                               # thunderstorm
assert hour_score(OK._replace(gust=100)) == 0 and hour_score(OK._replace(gust=69)) > 0   # gust stop at 70
assert 40 < hour_score(OK._replace(t=30, pop=10, gust=35)) < 90            # hot: degraded, not binary
assert hour_score(OK._replace(pop=None, gust=None)) == 100                 # source without pop/gust
assert hour_score(OK._replace(pop=None, mm=0.5)) < 60                      # steady rain, no pop: not "good"
a, b = hour_score(OK._replace(pop=None, mm=0.29)), hour_score(OK._replace(pop=None, mm=0.30))
assert abs(a - b) <= 3, (a, b)                                             # no cliff at 0.3 mm
assert stop_reason(OK) is None and stop_reason(OK._replace(code=71)) == "snow or freezing rain"

# --- a single dangerous hour must not hide in the mean (all sources agree)
sun = day_of()
storm = {n: with_hour(sun, 17, code=95) for n in "abc"}
r = analyse(storm, DAY)
assert r["score"] <= 49 and r["stops"] == ["17:00 thunderstorm"], r
assert "DANGER: 17:00 thunderstorm" in message("X", DAY, r)
ice = {n: {**sun, **{f"{DAY}T{h:02d}:00": OK._replace(t=0, rh=95) for h in (8, 9, 10)}} for n in "abc"}
assert analyse(ice, DAY)["score"] <= 49
assert analyse({n: sun for n in "abc"}, DAY)["score"] == 100 and not analyse({"a": sun}, DAY)["stops"]

# --- a minority of sources is still counted when 2 agree, not when only 1 says it
two_of_five = {**{n: with_hour(sun, 17, code=95) for n in "ab"}, **{n: sun for n in "cde"}}
assert analyse(two_of_five, DAY)["stops"], "2 of 5 sources forecasting a storm must stop the day"
one_of_five = {**{"a": with_hour(sun, 17, code=95)}, **{n: sun for n in "bcde"}}
r1 = analyse(one_of_five, DAY)
assert not r1["stops"] and r1["score"] == 100        # lone outlier is outvoted ...
assert r1["day_scores"]["a"] < 100                    # ... but still shown in the per-source line
assert analyse({"a": with_hour(sun, 17, code=95)}, DAY)["stops"]   # sole source says storm: believe it

# --- disagreement lowers the score; one source is flagged low confidence
d = analyse({"a": sun, "b": {k: OK._replace(t=2, rh=95, mm=1.0, pop=90, code=61) for k in sun}}, DAY)
assert d["spread"] > 30 and d["score"] < 50 and "DISAGREE" in message("X", DAY, d)
assert "ONLY 1 source" in message("X", DAY, analyse({"a": sun}, DAY))
partial = {k: v for k, v in sun.items() if not k.endswith("T12:00")}
assert analyse({"a": sun, "b": partial}, DAY)["n"] == 1                    # incomplete source dropped
try:
    analyse({"b": partial}, DAY); raise SystemExit("expected failure")
except RuntimeError:
    pass

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
assert ride.send("hi", 50) == ["webhook"] and calls == ["webhook", "telegram"]   # webhook down, telegram still sent
def bad_get(url, **p): raise Boom("https://api.telegram.org/botSECRET/x")
ride.get = bad_get
import io, contextlib
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert ride.send("hi", 50) == ["webhook", "telegram"]
assert "SECRET" not in err.getvalue()

# --- CLI: unknown flags are errors (no accidental real sends); failures send a message and exit 1
for bad in (["Salamanca", "--dry-run"], ["Salamanca", "-dry"]):
    try:
        main(bad); raise SystemExit("expected argparse error")
    except SystemExit as e:
        assert e.code == 2, bad
for k in ("WEBHOOK_URL", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "RIDE_PLACE"):
    os.environ.pop(k, None)
try:
    main(["Salamanca"]); raise SystemExit("expected: no channel configured")
except SystemExit as e:
    assert e.code == 2
sent = []
ride.send = lambda text, score: sent.append(text) or []
ride.run = lambda a: (_ for _ in ()).throw(Boom("api down"))
os.environ["WEBHOOK_URL"] = "http://x"
with contextlib.redirect_stderr(io.StringIO()):
    assert main(["Salamanca"]) == 1
assert sent and sent[0].startswith("ride-forecast FAILED"), sent

# --- geocoding: country filter picks the right Leon
ride.get = lambda url, **p: {"results": [{"name": "León", "country_code": "NI"}, {"name": "León", "country_code": "ES"}]}
assert ride.geocode("Leon", "es")["country_code"] == "ES" and ride.geocode("Leon")["country_code"] == "NI"
assert hour_score(OK._replace(code=45)) == hour_score(OK) - 20            # fog costs points
# one provider down: the other still produces a report (ride.run is the real function again here)
import importlib; importlib.reload(ride)
ride.geocode = lambda p, c=None: {"name": "X", "country_code": "ES", "timezone": "Europe/Madrid", "latitude": 1, "longitude": 1}
def om_down(g): raise Boom("503")
ride.open_meteo = om_down
ride.met_norway = lambda g, tz: day_of()
with contextlib.redirect_stderr(io.StringIO()):
    text, score = ride.run(ride.argparse.Namespace(place="X", country=None, date=DAY))
assert "1 sources" in text.replace("ONLY ", "") and score == 100, text
print("ok")
