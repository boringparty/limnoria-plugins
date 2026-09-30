import os
import re
import supybot.utils as utils
from supybot.commands import wrap, optional
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.ircmsgs as ircmsgs
import requests
import json
import dateutil.parser
from datetime import datetime, timedelta, timezone
import pycountry
from icalendar import Calendar, Event
from collections import defaultdict

try:
    from supybot.i18n import PluginInternationalization
    _ = PluginInternationalization("F1")
except ImportError:
    _ = lambda x: x

class F1(callbacks.Plugin):
    """Uses API to retrieve F1 data."""
    threaded = True

    def _local_dt(self, date_str, time_str, offset):
        dt = dateutil.parser.parse(date_str + " " + time_str.replace("Z", ""))
        return dt + timedelta(hours=offset)

    def _format_dt(self, dt):
        return dt.strftime("%b %d @ %H:%M")

    def _deg_to_compass(self, deg):
        dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
                'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
        ix = int((deg + 11.25) / 22.5) % 16
        return dirs[ix]

    def champ(self, irc, msg, args, arg=None):
        """[year|all] Show driver standings."""
        show_all = arg and arg.lower() == "all"
        url = f"https://api.jolpi.ca/ergast/f1/{arg}/driverStandings.json" if arg and arg.isdigit() else \
              "https://api.jolpi.ca/ergast/f1/current/driverStandings.json"

        data = requests.get(url).json()
        drivers = data["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"]

        reply = []
        for d in drivers:
            pts = int(d["points"])
            if pts == 0 and not show_all:
                continue
            code = d["Driver"].get("code", d["Driver"]["familyName"][:3].upper())
            reply.append(f"{code}\x0304 {pts}\x0F")

        irc.reply(", ".join(reply))

    champ = wrap(champ, [optional("text")])

    def constructor(self, irc, msg, args, year):
        """<year> Shows constructor standings."""
        url = f"https://api.jolpi.ca/ergast/f1/{year}/constructorStandings.json" if year else \
              "https://api.jolpi.ca/ergast/f1/current/constructorStandings.json"
        standings = requests.get(url).json()["MRData"]["StandingsTable"]["StandingsLists"][0]["ConstructorStandings"]

        segments = [
            f"{d['Constructor']['name']}\x034 {d['points']}\x0F"
            for d in standings if int(d['points']) > 0
        ]

        irc.reply(", ".join(segments))

    constructor = wrap(constructor, [optional("int")])

    def gp(self, irc, msg, args, race):
        """<race> Show GP results."""
        if race in range(1, 22):
            r_url = f"https://api.jolpi.ca/ergast/f1/current/{race}/results.json"
            f_url = f"https://api.jolpi.ca/ergast/f1/current/{race}/fastest/1/results.json"
        else:
            r_url = "https://api.jolpi.ca/ergast/f1/current/last/results.json"
            f_url = "https://api.jolpi.ca/ergast/f1/current/last/fastest/1/results.json"

        race_info = requests.get(r_url).json()["MRData"]["RaceTable"]["Races"][0]
        fastest = requests.get(f_url).json()["MRData"]["RaceTable"]["Races"][0]["Results"][0]

        race_name = race_info["Circuit"]["circuitName"]
        positions = []
        for r in race_info["Results"]:
            pos = r["positionText"]
            pos = "\x035R\x0F" if pos == "R" else pos
            code = r["Driver"]["code"]
            positions.append(f"{pos}. {code}")

        suffix = f"  \x0304,15 {fastest['Driver']['code']} {fastest['FastestLap']['Time']['time']} \x0F"
        irc.reply(f"\x0301,15 {race_name} \x0F {', '.join(positions)}{suffix}")

    gp = wrap(gp, [optional("int")])


    def sprint(self, irc, msg, args, race):
        """[<race #>] Show Sprint results."""
        if race in range(1, 30):
            url = f"https://api.jolpi.ca/ergast/f1/current/{race}/sprint.json"
        else:
            url = "https://api.jolpi.ca/ergast/f1/current/last/sprint.json"

        try:
            race_data = requests.get(url).json()["MRData"]["RaceTable"]["Races"][0]
            name = race_data["raceName"]
            results = race_data.get("SprintResults", [])

            if not results:
                irc.reply("No sprint results found for that round.")
                return

            positions = []
            for r in results:
                pos = r["positionText"]
                pos = "\x035R\x0F" if pos == "R" else pos
                code = r["Driver"]["code"]
                positions.append(f"{pos}. {code}")

            fastest = min(results, key=lambda r: r.get("FastestLap", {}).get("rank", 999))
            fast_code = fastest["Driver"]["code"]
            fast_time = fastest["FastestLap"]["Time"]["time"]
            suffix = f"  \x0304,15 {fast_code} {fast_time} \x0F"

            irc.reply(f"\x0301,15 {name} Sprint \x0F {', '.join(positions)}{suffix}")
        except Exception as e:
            irc.reply(f"Error fetching sprint results: {e}")

    sprint = wrap(sprint, [optional("int")])


    def track(self, irc, msg, args):
        """Returns current F1 track location and country."""
        try:
            res = requests.get("https://livetiming.formula1.com/static/SessionInfo.json", timeout=5)
            res.raise_for_status()
            data = res.json()
            location = data["Meeting"]["Location"]
            country = data["Meeting"]["Country"]["Name"]
            irc.reply(f"{location}, {country}")
        except:
            irc.reply("Could not fetch current track info.")

    track = wrap(track)

    def weekly(self, irc, msg, args):
        """Fetches this week's F1 update from Google Sheets."""
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSLsH3aQvTP0wgYQjV3_dr-LEBad9seP2Vs15PCoZMtP_xD2KrLJhutQKh3hcrQ-lgQF_cCcEqFRB8j/pub?gid=0&single=true&output=tsv&range=I1"
        try:
            res = requests.get(url)
            text = res.text.strip()
            irc.reply(text if text else "No weekly update found.")
        except:
            irc.reply("Failed to fetch weekly update.")

    weekly = wrap(weekly)

    def calendar(self, irc, msg, args, rest):
        """[full] [<year>] F1 race calendar."""
        parts = rest.split() if rest else []
        full = "full" in parts
        year = next((int(p) for p in parts if p.isdigit()), datetime.utcnow().year)

        special = {
            "São Paulo": "SP", "Mexico City": "MEX", "Abu Dhabi": "AD",
            "Monte-Carlo": "MON", "Sakhir": "BAH", "Austin": "USA",
            "Imola": "EMI", "Budapest": "HUN", "Zandvoort": "NED",
            "Melbourne": "AUS", "Shanghai": "CHN", "Suzuka": "JPN",
            "Jeddah": "SAU", "Montreal": "CAN", "Monaco": "MON",
            "Barcelona-Catalunya": "ESP", "Spielberg": "AUT",
            "Silverstone": "GBR", "Spa-Francorchamps": "BEL",
            "Monza": "ITA", "Madrid": "ESP", "Baku": "AZE",
            "Singapore": "SGP", "Lusail": "QAT", "Yas Marina": "UAE"
        }

        url = f"https://api.jolpi.ca/ergast/f1/{year}.json"
        data = requests.get(url).json()
        today = datetime.utcnow()
        next_highlight = False
        races = []

        for r in data["MRData"]["RaceTable"]["Races"]:
            dt = dateutil.parser.parse(r["date"])
            loc = r["Circuit"]["Location"]
            label = r["raceName"].replace("Grand Prix", "").strip()
            try:
                code = special.get(loc["locality"], pycountry.countries.lookup(loc["country"]).alpha_3)
            except:
                code = loc["country"][:3].upper()
            text = f"\x02{label if full else code}\x0F {dt.strftime('%m/%d')}"
            if not next_highlight and dt > today:
                text = f"\x034,15{text}\x03"
                next_highlight = True
            races.append(text)

        irc.reply(", ".join(races).strip())

    calendar = wrap(calendar, [optional("text")])


    def f1weather(self, irc, msg, args):
        """Shows weather at current F1 track."""
        try:
            s = requests.get("https://livetiming.formula1.com/static/SessionInfo.json", timeout=5)
            s.raise_for_status()
            m = s.json()["Meeting"]
            place = f"{m['Location']}, {m['Country']['Name']}"

            geo = requests.get("https://nominatim.openstreetmap.org/search",
                params={"q": place, "format": "json", "limit": 1},
                headers={'User-Agent': 'F1WeatherBot/1.0 (+contact@example.com)'}, timeout=5).json()

            if not geo:
                irc.reply(f"Could not find coordinates for {place}.")
                return

            lat, lon = geo[0]["lat"], geo[0]["lon"]
            w = requests.get("https://api.openweathermap.org/data/2.5/weather",
                params={"lat": lat, "lon": lon, "appid": "fcde02fd74078c2aa35770bb13c65f9e", "units": "metric"}, timeout=5).json()

            desc = w['weather'][0]['description'].title()
            main = w['main']
            wind = w.get('wind', {})
            reply = (f"\x0301,15 {place} \x0F \x1D{desc}\x1D, \x0304{main['temp']}°C\x0F (feels like {main['feels_like']}°C), "
                     f"Pres: {main['pressure']} hPa, Hum: {main['humidity']}%, Vis: {w.get('visibility', 0)}m, "
                     f"Wind: {round(wind.get('speed', 0)*3.6,1)}km/h {self._deg_to_compass(wind.get('deg', 0))}, "
                     f"Cloud Coverage: {w['clouds']['all']}%")
            irc.reply(reply)
        except Exception as e:
            irc.reply(f"Error fetching F1 weather: {e}")

    f1weather = wrap(f1weather)

    def pool(self, irc, msg, args):
        """Fetches the rank for the sheet"""
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSLsH3aQvTP0wgYQjV3_dr-LEBad9seP2Vs15PCoZMtP_xD2KrLJhutQKh3hcrQ-lgQF_cCcEqFRB8j/pub?gid=475767348&single=true&output=tsv&range=B31"
        try:
            res = requests.get(url)
            text = res.content.decode("utf-8").strip()
            irc.reply(text if text else "Scoring not set.")
        except:
            irc.reply("Failed to fetch ranks.")

    pool = wrap(pool)

    def next(self, irc, msg, args, tz_offset="0"):
        """[tz_offset] Shows all sessions for the next F1 weekend using ICS with IRC colors."""
        try:
            offset = int(tz_offset)
        except:
            offset = 0

        ics_file = os.path.join(os.path.dirname(__file__), "f1_schedule.ics")
        if not os.path.exists(ics_file):
            irc.reply("ICS file not found.")
            return

        with open(ics_file, "rb") as f:
            cal = Calendar.from_ical(f.read())

        now = datetime.now(timezone.utc) + timedelta(hours=offset)
        events = []

        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            dt = comp.get('dtstart').dt
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
            dt = dt + timedelta(hours=offset)

            summary = comp.get("SUMMARY").replace("F1: ", "")
            if "(" in summary:
                session = summary.split("(")[0].strip()
                gp_name = summary.split("(")[1].replace(")", "").strip()
            else:
                session = summary
                gp_name = summary

            events.append((dt, gp_name, session))

        # Only sessions for the next GP weekend
        upcoming_events = [e for e in sorted(events) if e[0] >= now]
        if not upcoming_events:
            irc.reply("No upcoming events.")
            return

        next_gp_name = upcoming_events[0][1]
        weekend_events = [(sess, dt) for dt, gp, sess in upcoming_events if gp == next_gp_name]

        # Map labels for short names like old script
        labels = {
            "FirstPractice": "FP1", "SecondPractice": "FP2",
            "ThirdPractice": "FP3", "Qualifying": "Q", "SprintQualifying": "SQ",
            "Sprint": "S", "Grand Prix": "GP"
        }


        formatted = []
        next_found = False
        for sess, dt in weekend_events:
            short_sess = labels.get(sess, sess)
            if not next_found and dt > now:
                delta = dt - now
                days = delta.days
                hours, remainder = divmod(delta.seconds, 3600)
                minutes = remainder // 60
                formatted.append(
                    f"\x0304,15 {short_sess} \x0F {dt.strftime('%m/%d @ %H:%M')} "
                    f"({days}d{hours}h{minutes}m)"
                )
                next_found = True
            else:
                formatted.append(f"\x0301,15 {short_sess} \x0F {dt.strftime('%m/%d @ %H:%M')}")




        irc.reply(f"\x02\x0301,15 {next_gp_name} \x0F " + ", ".join(formatted))

    next = wrap(next, [optional("something")])



Class = F1

