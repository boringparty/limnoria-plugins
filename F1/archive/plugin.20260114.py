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
from icalendar import Calendar
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

    # --- Driver standings ---
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

    # --- Constructor standings ---
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

    # --- GP results ---
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

    # --- Sprint results ---
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

    # --- Track location ---

    def track(self, irc, msg, args):
        """Fetches this week's F1 update from Google Sheets."""
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vS_AlD2wUjpHCoHgP90IQR_dM7qD5pyXMIpkODgBucqJhlmItvNiil_OaP5LN21KZFunuvr9qEckVVw/pub?gid=1723154320&single=true&output=tsv&range=B26"
        try:
            res = requests.get(url)
            text = res.text.strip()
            irc.reply(text if text else "No weekly update found.")
        except:
            irc.reply("Failed to fetch weekly update.")

    track = wrap(track)


    # --- Weekly Google Sheet ---
    def weekly(self, irc, msg, args):
        """Fetches this week's F1 update from Google Sheets."""
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vS_AlD2wUjpHCoHgP90IQR_dM7qD5pyXMIpkODgBucqJhlmItvNiil_OaP5LN21KZFunuvr9qEckVVw/pub?gid=0&single=true&output=tsv&range=I1"
        try:
            res = requests.get(url)
            text = res.text.strip()
            irc.reply(text if text else "No weekly update found.")
        except:
            irc.reply("Failed to fetch weekly update.")

    weekly = wrap(weekly)


    def events(self, irc, msg, args):
        """Fetches this week's F1 update from Google Sheets."""
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vS_AlD2wUjpHCoHgP90IQR_dM7qD5pyXMIpkODgBucqJhlmItvNiil_OaP5LN21KZFunuvr9qEckVVw/pub?gid=1357483059&single=true&range=J1&output=tsv"
        try:
            res = requests.get(url)
            text = res.text.strip()
            irc.reply(text if text else "No races")
        except:
            irc.reply("Failed to fetch calendar")

    weekly = wrap(weekly)

    # --- NEXT sessions ---
    def next(self, irc, msg, args, arg=None):
        """[flags] Shows next GP weekend. Flags: 'nc' disables color, 'info' appends Google Sheets info."""
        use_color = True
        append_info = False
        offset = 0

        if arg:
            flags = arg.lower().split()
            if "nc" in flags:
                use_color = False
            if "info" in flags:
                append_info = True
            for f in flags:
                try:
                    offset = int(f)
                except:
                    continue

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

        upcoming_events = [e for e in sorted(events) if e[0] >= now]
        if not upcoming_events:
            irc.reply("No upcoming events.")
            return

        next_gp_name = upcoming_events[0][1]
        weekend_events = [(sess, dt) for dt, gp, sess in upcoming_events if gp == next_gp_name]

        labels = {
            "FirstPractice": "FP1", "SecondPractice": "FP2",
            "ThirdPractice": "FP3", "Qualifying": "Q", "SprintQualifying": "SQ",
            "Sprint": "S", "Grand Prix": "GP"
        }

        formatted = []
        next_found = False
        for sess, dt in weekend_events:
            short_sess = labels.get(sess, sess)
# add this near the top after flags parsing
            use_ceeks = "ceeks" in (arg.lower().split() if arg else [])

# then replace your delta formatting part
            if not next_found and dt > now:
                delta = dt - now
                if use_ceeks:
                    total_seconds = delta.total_seconds()
                    ceeks = total_seconds / (7*24*3600)
                    formatted_delta = f"{ceeks:.2f} ceeks"
                else:
                    days = delta.days
                    hours, remainder = divmod(delta.seconds, 3600)
                    minutes = remainder // 60
                    formatted_delta = f"{days}d{hours}h{minutes}m"
            
                if use_color:
                    formatted.append(f"\x0304,15 {short_sess} \x0F {dt.strftime('%m/%d @ %H:%M')} ({formatted_delta})")
                else:
                    formatted.append(f"{short_sess} {dt.strftime('%m/%d @ %H:%M')} ({formatted_delta})")
                next_found = True

            else:
                if use_color:
                    formatted.append(f"\x0301,15 {short_sess} \x0F {dt.strftime('%m/%d @ %H:%M')}")
                else:
                    formatted.append(f"{short_sess} {dt.strftime('%m/%d @ %H:%M')}")

        output = f"\x0301,15 {next_gp_name} \x0F " + ", ".join(formatted)

        if append_info:
            try:
                url = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vS_AlD2wUjpHCoHgP90IQR_dM7qD5pyXMIpkODgBucqJhlmItvNiil_Oa"
                       "P5LN21KZFunuvr9qEckVVw/pub?gid=1723154320&single=true&output=tsv&range=B27")
                res = requests.get(url, timeout=5)
                sheet_text = res.text.strip()
                if sheet_text:
                    output += f" | {sheet_text}"
            except Exception as e:
                output += f" | Failed to fetch sheet info: {e}"

        irc.reply(output)

    next = wrap(next, [optional("something")])

Class = F1
