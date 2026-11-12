# ride-forecast

A Telegram bot that tells you whether to ride your motorbike. It checks six weather sources, judges each of your trips (the commute there, the commute back, an evening out) and answers in the chat. It sends tomorrow's forecast every night. You can ask for any day in the next week, or for a chart of the hours. It needs no API keys apart from the Telegram bot token.

The trips are yours. Nothing is built in: you tell the bot when you ride, and it judges those hours.

```
Fri 13 Nov · Salamanca, Castille and León, ES
🟢 Work 07:20-08:00: GO, 10°C, 88% humidity, gusts 46 km/h, light rain 0.1 mm/h at 07:00
🟡 out 15:30-16:30: CAUTION, 13 to 14°C, 82% humidity, gusts 50 km/h, light rain 0.3 mm/h at 15:00, sources disagree
Whole day 08:00-20:00: 🟢 GO, 80/100
Best 3 hours: 09:00-12:00, 🟢 GO, 86/100
Worst hour: 15:00, 🟡 CAUTION, gusts 50 km/h, light rain 0.3 mm/h
Gear: rain gear, strong wind, 52 km/h gusts
Ride: wet roads, brake early, 3 s gap, first minutes slippery; grip firmly, give trucks room on open roads
6 sources, sources agree: ECMWF 84 · MeteoFr 84 · GFS 83 · ICON 76 · MET.no 72 · UKMO 71
```

Every verdict is one of four levels: 🟢 GO, 🟡 CAUTION, 🟠 RISKY, 🔴 DON'T RIDE. The thresholds come from motorcycle safety sources (DGT, MSF, Met Office, AEMET, Michelin) and are documented in [docs/research](docs/research/motorcycle-weather-thresholds.md).

## What the bot does

| Command | Result |
|---|---|
| `/today`, `/tomorrow` | The verdict for each of your trips on that day. |
| `/date <day>` | The same for any day up to 7 days ahead. |
| `/week` | One line per day for the next 7 days: level and score, best window, temperature, and rain, gusts or fog when they matter. |
| `/chart week` | The same week as one chart: day score bars coloured and labelled by level, temperature range, rain. |
| `/chart [day]` | A graph of the hours: the score of each source and their median on the four level bands, temperature, rain, wind gusts and visibility, each with the lines where the level changes, and your trips shaded. Without a day it uses tomorrow, or today if it is before noon. |
| `/place [name, CC]` | Show or change the place, for example `/place Leon, ES`. Saved. |
| `/trips [spec]` | Show or change your trips. Saved. `/trips reset` goes back to the server's `RIDE_TRIPS` and `/trips none` removes them. |
| `/help` | The list above. |

`<day>` is `today`, `tomorrow`, a weekday (`sat`, `monday`, `lunes`), a date (`2026-11-14`) or `14/11`. A weekday name means the next one, starting tomorrow. Spanish `hoy` and `mañana` work too.

The bot only answers the chat in `TELEGRAM_CHAT_ID` and ignores everyone else. Every night at `RIDE_PUSH_TIME` (default 21:00, in the place's time zone) it sends tomorrow's report and chart on its own. If the server was down at that time, it sends them as soon as it is back, as long as it is still the same day. If the push fails three times in a row, the bot tells you in the chat.

## How it decides

It looks up the place with the Open-Meteo geocoding API, then asks six sources for the hourly forecast:

- Five Open-Meteo models: ECMWF, GFS, ICON, Météo-France and the UK Met Office.
- MET Norway, a separate API.

Weather apps disagree, so each source is scored on its own and the scores are combined. The five Open-Meteo models are different forecast models behind one API, which makes MET Norway the only fully independent source. Every hour is converted to the place's local time from the real instant, so the sources line up across daylight saving changes.

### Levels

Every hour, for each source, gets one of four levels. The worst real hazard decides, and two CAUTION factors together make RISKY. Hazards are not added up as points: a drizzle at 13°C is not a reason to stay home.

| Factor | 🟢 GO (tip) | 🟡 CAUTION | 🟠 RISKY | 🔴 DON'T RIDE |
|---|---|---|---|---|
| Cold, dry | under 8°C: layers and gloves | 3°C or less | | 0°C or less |
| Ice, damp (humidity 85% or more) or wet | | | 3°C or less | 1°C or less |
| Rain | 0.1 to under 1 mm/h: wet roads | 1 mm/h | 4 mm/h | 15 mm/h |
| Gusts | 40 km/h | 50 km/h | 60 km/h | 70 km/h |
| Visibility | under 2000 m: mist | under 1000 m (fog) | under 200 m | under 100 m |
| Heat | up to 32°C | above 32°C | 36°C and up | 40°C and up |
| Thunderstorm, snow, freezing rain | | | | always |

Where each number comes from, and which are judgment calls, is in [docs/research/motorcycle-weather-thresholds.md](docs/research/motorcycle-weather-thresholds.md). The thresholds are constants at the top of `ride.py`.

Visibility comes from the Open-Meteo models that provide it (the lowest figure any source reports counts). MET Norway has none, so it only contributes a fog symbol.

### Scores

The 0-100 score is derived from the level, so a score always sits inside its level's band and the charts, texts and rankings agree:

| Level | Score |
|---|---|
| 🟢 GO | 70 to 100 |
| 🟡 CAUTION | 45 to 69 |
| 🟠 RISKY | 20 to 44 |
| 🔴 DON'T RIDE | 0 |

Inside a level, smaller costs (colder, damper, gustier, rain chance, poor visibility) order the hours. That is what picks the best 3-hour window.

The score of an hour is the median across sources. A DON'T RIDE hour counts when two or more sources report it. A single source reporting one is outvoted, but it still shows up in the chart. Without such a vote the hour is never worse than RISKY. The reason shown for an hour is that of the source nearest the median.

### Trips

A trip is a name, a time window and optionally the days it applies to. A trip is judged on its worst hour, because you can't choose when in the window you ride. One bad hour at 08:30 spoils the morning, whatever the afternoon does.

The trip line shows the level, the temperature and humidity range of the trip, and the main factors of the worst hour with its time. If the sources are more than 30 points apart over the trip, the line says "sources disagree". That is a warning only: it does not lower the verdict. Trips only appear on their days.

With no trips set, the bot shows the day line and tells you how to add trips. The `Whole day` line is always there:

- The day's level and score: the average of the hours 08:00-20:00, lifted toward the best window but never above CAUTION, and capped at RISKY when any hour is DON'T RIDE.
- The best 3-hour window with its own level.
- The worst hour, when it is CAUTION or worse.
- A `Gear` line when it is cold, icy, wet, foggy, windy or hot, and a `Ride` line with riding tips (wet roads, gusts, fog, cold tyres).
- The sources line, which counts only sources that cover the whole day.

The weights are judgment calls, not tuned values. They are constants at the top of `ride.py`.

### Trip format

```
NAME HH:MM-HH:MM [days];NAME HH:MM-HH:MM [days]
```

For example `/trips work 08:00-09:00 mon-fri; home 17:30-18:30 mon-fri; out 20:00-22:00`. Days are optional (every day by default). Use `mon-fri`, `sat,sun`, `fri-mon`, or the Spanish `lun-vie`. A trip must stay within one day. A window that touches part of an hour counts that whole hour, so `07:30-09:00` covers 07:00 and 08:00.

## Quickstart

You need Python 3.9 or newer. Charts need matplotlib; without it the bot answers `/chart` with a text sparkline.

```
git clone https://github.com/n0nuser/ride-forecast
cd ride-forecast
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_ride.py && .venv/bin/python test_bot.py     # both print "ok"
.venv/bin/python ride.py "Salamanca" --country ES --dry           # the report, no Telegram needed
```

`--country` takes an ISO code. Use it, because "Leon" alone resolves to Nicaragua and "Cordoba" to Argentina. The first line of the report shows the place it picked.

To run the bot, create one with @BotFather and copy its token. Send the bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id` from the result.

```
export TELEGRAM_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
export RIDE_PLACE="Salamanca" RIDE_COUNTRY="ES"
.venv/bin/python bot.py
```

Send `/today` to the bot, then `/trips` to add your trips. Stop it with Ctrl+C.

### Settings

| Env variable | Meaning |
|---|---|
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | The bot and the one chat it answers. Required. A malformed value stops the bot at startup with a clear message. |
| `RIDE_PLACE`, `RIDE_COUNTRY` | The default place. `/place` overrides it. |
| `RIDE_TRIPS` | The default trips. Optional: there are none built in. `/trips` overrides it. |
| `RIDE_PUSH_TIME` | When the nightly report goes out, `HH:MM` local. Default `21:00`. |
| `STATE_DIRECTORY` | Where `state.json` lives (the place, trips and last push). Default `~/.local/state/ride-forecast`; systemd sets it for you. |
| `WEBHOOK_URL` | Only for `ride.py` run by hand or from cron: also POST the report as JSON with `text` (Slack), `content` (Discord) and `score`. |

### One-shot script

`ride.py` still works on its own, without the bot. It prints the report for a place, using the trips in `RIDE_TRIPS`, and can send it to a webhook or Telegram, which suits a cron job:

```
.venv/bin/python ride.py "Salamanca" --country ES [--date 2026-11-14] [--dry]
```

Without `--dry` it sends to every channel that is set and exits with code 1 if one fails, or if the forecast fails (it then sends a "ride-forecast FAILED" message). Unknown flags are errors, so a typo like `--dry-run` can't send a message by accident.

## Run it as a service with systemd

The bot is one always-on service. It sends the nightly report itself, so no timer is needed. systemd starts it at boot, restarts it if it crashes, and restarts it if it stops answering for 5 minutes (a watchdog). A configuration mistake exits with code 2, which systemd does not retry forever.

### Install

```
sudo mkdir -p /opt/ride-forecast
sudo cp ride.py bot.py charts.py requirements.txt /opt/ride-forecast/
sudo python3 -m venv /opt/ride-forecast/.venv
sudo /opt/ride-forecast/.venv/bin/pip install -r /opt/ride-forecast/requirements.txt

sudo cp systemd/ride-forecast.env.example /etc/ride-forecast.env
sudo chmod 600 /etc/ride-forecast.env
sudo $EDITOR /etc/ride-forecast.env           # set the token, the chat id and the defaults

sudo cp systemd/ride-forecast-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ride-forecast-bot.service
```

On Debian and Ubuntu the venv step needs the `python3-venv` package. The service runs as a throwaway user with a read-only view of the system. systemd creates `/var/lib/ride-forecast` for `state.json` and `/var/cache/ride-forecast` for matplotlib.

### Check it

```
systemctl status ride-forecast-bot            # active (running)
sudo journalctl -u ride-forecast-bot -n 30    # plain users need the systemd-journal group
```

Then send `/today` to the bot. Telegram replies within a few seconds. Log lines never contain the bot token: it is masked wherever it would appear.

### Update or remove

To update, keep a copy of the working version, install the new files, check them, then restart:

```
sudo cp -r /opt/ride-forecast /opt/ride-forecast.prev
sudo cp ride.py bot.py charts.py requirements.txt /opt/ride-forecast/
sudo /opt/ride-forecast/.venv/bin/pip install -r /opt/ride-forecast/requirements.txt
/opt/ride-forecast/.venv/bin/python /opt/ride-forecast/ride.py "Salamanca" --country ES --dry   # must print a report
sudo systemctl restart ride-forecast-bot
```

To roll back, copy `/opt/ride-forecast.prev` back over `/opt/ride-forecast` and restart. The place and trips you set in the chat are kept in `state.json`, so they survive an update and a reboot.

To remove it:

```
sudo systemctl disable --now ride-forecast-bot
sudo rm /etc/systemd/system/ride-forecast-bot.service /etc/ride-forecast.env
sudo rm -r /opt/ride-forecast
sudo systemctl daemon-reload
```

### Coming from the timer version

Earlier versions ran a one-shot service from a timer at 21:00. Disable it, or you will get two messages every night:

```
sudo systemctl disable --now ride-forecast.timer
sudo rm /etc/systemd/system/ride-forecast.{service,timer}
sudo systemctl daemon-reload
```

### Notes

- If the server reboots, the service starts again at boot and sends the night's report late if it missed 21:00, as long as it is still the same day. A boot right at 21:00 can find the network not up yet; the push retries every 5 minutes. For a clean start, enable the wait-online service of your network manager (`systemd-networkd-wait-online` or `NetworkManager-wait-online`).
- A bad `RIDE_PLACE` doesn't stop the bot from listening: `/place` still reaches it and fixes the problem.
- Each command and each push has a 90-second limit, so a slow weather API can't freeze the bot.
- While Telegram or the weather APIs are down, the bot logs it, waits and tries again. Commands that need weather answer "Something broke" in the meantime.
- Weather for a place is cached for 10 minutes, so repeated commands don't hit the APIs again.

## Limits

- Forecasts are forecasts. The verdict is a rule of thumb, not a safety guarantee. Check the road, your gear and your own judgment. In particular, air temperature is not road temperature: bridges, shade and clear nights can freeze while the forecast says 3 °C.
- It scores weather only. Daylight, traffic and road works are not in it, so it can suggest a window after sunset. Wind direction is not in it either, so gusts count the same on sheltered and exposed roads.
- Gust limits of 50 and 60 km/h and the heat limits are judgment calls: no authority publishes a motorcycle figure. A light bike deserves lower limits.
- Beyond 2 or 3 days only some sources have hourly data, so the report says how many sources it used. Treat the later days as a rough guide.
- Only MET Norway is independent of the Open-Meteo API. Adding more providers is a small change in `ride.py`.
- The bot serves one chat and one place at a time.
- A reply to a command can be lost if the network drops at the wrong moment. Ask again.

## Tests

```
python3 test_ride.py     # levels and thresholds, score bands, ice, visibility, trips, stop rules, DST, retries, deadlines, real API captures
python3 test_bot.py      # commands, dates, state, charts, the push, the watchdog, a stub Telegram API
```

The tests call no network service. They parse recorded real responses from `tests/fixtures/` for Open-Meteo, MET Norway and the geocoder, so a change in what those APIs return fails a test. `test_bot.py` starts a local stub of the Telegram API, so it also checks the photo upload and what happens when Telegram is down. GitHub Actions runs both on Python 3.9 and 3.13.

## License

MIT. See [LICENSE](LICENSE).
