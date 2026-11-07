# ride-forecast

A Telegram bot that tells you whether to ride your motorbike. It checks six weather sources, judges each of your trips (the commute there, the commute back, an evening out) and answers in the chat. It sends tomorrow's forecast every night, and you can ask for any day in the next week or a chart of the hours. It needs no API keys apart from the Telegram bot token.

```
Mon 09 Nov · Salamanca, Castille and León, ES
🔴 work 07:30-09:00: STAY HOME, 13-13°C, rain 0.2 mm at 08:00, sources disagree
🟢 home 17:00-19:00: OK, 14-16°C, gusts 51 km/h at 17:00
🟢 out 20:00-23:00: OK, 13-13°C, gusts 60 km/h at 22:00
Day 08-20: Mixed: ride in the best window (59/100), best 17:00-20:00 (82)
Gear: rain gear, strong wind, 60 km/h gusts
6 sources, SOURCES DISAGREE, low confidence: MET.no 86 · ECMWF 68 · MeteoFr 67 · ICON 66 · UKMO 60 · GFS 50
```

## What the bot does

| Command | Result |
|---|---|
| `/today`, `/tomorrow` | The verdict for each of your trips on that day. |
| `/date <day>` | The same for any day up to 7 days ahead: `2026-11-14`, `14/11`, `sat`, `monday`, `lunes`. A weekday name means the next one, starting tomorrow. |
| `/chart [day]` | A graph of the hours: the score of each source and their median, temperature and rain, wind gusts, with your trips shaded. Without a day it uses tomorrow, or today if it is before noon. |
| `/place [name, CC]` | Show or change the place, for example `/place Leon, ES`. Saved. |
| `/trips [spec]` | Show or change your trips, for example `/trips work 07:30-09:00 mon-fri; out 20:00-23:00`. `/trips default` restores the defaults and `/trips none` removes them. Saved. |
| `/help` | The list above. |

The bot only answers the chat in `TELEGRAM_CHAT_ID` and ignores everyone else. Every night at `RIDE_PUSH_TIME` (default 21:00, in the place's time zone) it sends tomorrow's report and chart on its own. If the server was down at that time, it sends them as soon as it is back.

## How it decides

It looks up the place with the Open-Meteo geocoding API, then asks six sources for the hourly forecast:

- Five Open-Meteo models: ECMWF, GFS, ICON, Météo-France and the UK Met Office.
- MET Norway, a separate API.

Weather apps disagree, so each source is scored on its own and the scores are combined. The five Open-Meteo models are different forecast models behind one API, which makes MET Norway the only fully independent source.

### Hours

Each hour of the day gets a score from 0 to 100 for each source:

| Factor | Effect |
|---|---|
| Temperature | No penalty from 18 to 26 °C. Points are lost as it gets colder or hotter. |
| Rain | Both the chance and the amount count. |
| Wind | Gusts over 40 km/h lose points. |
| Humidity | Above 80% loses points. Fog costs 20. |
| Dangerous hour | Thunderstorm, snow, freezing rain, gusts of 70 km/h or more, and ice risk (2 °C or colder with rain or 90% humidity) score 0. |

The score of an hour is the median across sources. A dangerous hour counts when two or more sources report it. A single source reporting one is outvoted, but it still shows up in the chart.

### Trips

A trip is a name, a time window and optionally the days it applies to. A trip is judged on its worst hour, because you can't choose when in the window you ride. One bad hour at 08:30 spoils the morning, whatever the afternoon does.

| Trip result | Meaning |
|---|---|
| OK | The worst hour scores 70 or more. |
| CAUTION | 45 to 69. |
| STAY HOME | Below 45, or any dangerous hour. |

If the sources are more than 30 points apart over the trip, the score is lowered and the line says "sources disagree". Each line names the single factor that drives the verdict, with its hour. Trips only appear on their days, so with the defaults a Sunday shows only the evening outing.

The bot also adds a `Day 08-20` line: the average score of the day, the best 3-hour window, and a cap at "marginal" if any hour is dangerous. A line of gear hints appears when it is cold, wet, foggy or windy.

The weights are judgment calls, not tuned values. They are constants at the top of `ride.py`.

### Trip format

```
NAME HH:MM-HH:MM [days];NAME HH:MM-HH:MM [days]
```

Days are optional (every day by default). Use `mon-fri`, `sat,sun`, `fri-mon`, or the Spanish `lun-vie`. A trip must stay within one day. A window that touches part of an hour counts that whole hour, so `07:30-09:00` covers 07:00 and 08:00.

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

Send `/today` to the bot. Stop it with Ctrl+C.

### Settings

| Env variable | Meaning |
|---|---|
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | The bot and the one chat it answers. Required. |
| `RIDE_PLACE`, `RIDE_COUNTRY` | The default place. `/place` overrides it. |
| `RIDE_TRIPS` | The default trips. `/trips` overrides it. |
| `RIDE_PUSH_TIME` | When the nightly report goes out, `HH:MM` local. Default `21:00`. |
| `STATE_DIRECTORY` | Where `state.json` lives (the place, trips and last push). Default `~/.local/state/ride-forecast`; systemd sets it for you. |
| `WEBHOOK_URL` | Only for `ride.py` run by hand or from cron: also POST the report as JSON with `text` (Slack), `content` (Discord) and `score`. |

### One-shot script

`ride.py` still works on its own, without the bot. It prints the report for a place and can send it to a webhook or Telegram, which suits a cron job:

```
.venv/bin/python ride.py "Salamanca" --country ES [--date 2026-11-14] [--dry]
```

Without `--dry` it sends to every channel that is set and exits with code 1 if one fails, or if the forecast fails (it then sends a "ride-forecast FAILED" message). Unknown flags are errors, so a typo like `--dry-run` can't send a message by accident.

## Run it as a service with systemd

The bot is one always-on service. It sends the nightly report itself, so no timer is needed. systemd restarts it if it crashes and starts it at boot.

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

Then send `/today` to the bot. Telegram replies within a few seconds. The journal logs only the type of a network error, never the URL, because the URL contains the token.

### Update or remove

To update, copy the new `*.py` files to `/opt/ride-forecast/` and run `sudo systemctl restart ride-forecast-bot`. The place and trips you set in the chat are kept in `state.json`, so they survive an update and a reboot.

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

- If the server reboots, the service starts again at boot and sends the night's report late if it missed 21:00. A boot right at 21:00 can find the network not up yet; the push retries every 5 minutes. For a clean start, enable the wait-online service of your network manager (`systemd-networkd-wait-online` or `NetworkManager-wait-online`).
- While Telegram or the weather APIs are down, the bot logs it, waits and tries again. Commands that need weather answer "Something broke" in the meantime.
- Weather for a place is cached for 10 minutes, so repeated commands don't hit the APIs again.

## Limits

- Forecasts are forecasts. The verdict is a rule of thumb, not a safety guarantee. Check the road, your gear and your own judgment.
- It scores weather only. Daylight, traffic and road works are not in it, so it can suggest a window after sunset.
- Beyond 2 or 3 days only some sources have hourly data, so the report says how many sources it used. Treat the later days as a rough guide.
- Only MET Norway is independent of the Open-Meteo API. Adding more providers is a small change in `ride.py`.
- The bot serves one chat and one place at a time.

## Tests

```
python3 test_ride.py     # scoring, trips, stop rules, disagreement, dates, sending
python3 test_bot.py      # commands, dates, state, charts, the push, a stub Telegram API
```

The tests call no network service. `test_bot.py` starts a local stub of the Telegram API, so it also checks the photo upload and what happens when Telegram is down.

## License

MIT. See [LICENSE](LICENSE).
