import os
import sqlite3
import datetime
import re
import zoneinfo
from typing import Optional, Tuple, List, Dict, Any

from supybot import callbacks, plugins, commands, conf, world
from supybot.commands import *
import supybot.ircutils as ircutils
import supybot.ircmsgs as ircmsgs
import supybot.ircdb as ircdb


class DatabaseManager:
    """Handles thread-safe SQLite connection, schema migrations, and transactions."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        dirname = os.path.dirname(self.db_path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        self._init_db()

    def get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self):
        with self.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                hostmask TEXT NOT NULL,
                nickname TEXT NOT NULL,
                balance INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel, hostmask)
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT 'market',
                question TEXT NOT NULL,
                close_time TIMESTAMP NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED', 'RESOLVED', 'CANCELLED')),
                winning_outcome_id INTEGER,
                created_by TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                FOREIGN KEY(winning_outcome_id) REFERENCES outcomes(id),
                UNIQUE(channel, slug)
            );
            """)

            # Automatically add slug column if upgrading from an older database
            try:
                cursor.execute("ALTER TABLE markets ADD COLUMN slug TEXT NOT NULL DEFAULT 'market';")
            except sqlite3.OperationalError:
                pass  # Column already exists

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE CASCADE,
                UNIQUE(market_id, name)
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS wagers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                outcome_id INTEGER NOT NULL,
                points INTEGER NOT NULL CHECK(points > 0),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(outcome_id) REFERENCES outcomes(id) ON DELETE CASCADE,
                UNIQUE(market_id, user_id)
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                transaction_type TEXT NOT NULL CHECK(transaction_type IN (
                    'INITIAL_BALANCE', 'WAGER', 'WAGER_RETURN', 'WINNINGS', 'REFUND', 'ADMIN_ADJUSTMENT'
                )),
                market_id INTEGER,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE SET NULL
            );
            """)
            conn.commit()


class Picks(callbacks.Plugin):
    """
    Localized, non-monetary prediction market plugin for Limnoria IRC bot.
    Allows channel users to wager virtual points on custom prediction questions.
    """

    def __init__(self, irc):
        super().__init__(irc)
        # Force it to an absolute path so there's zero ambiguity
        db_dir = os.path.expanduser('~/.ircbot/data')
        os.makedirs(db_dir, exist_ok=True)
        db_file = os.path.join(db_dir, 'prediction_market.sqlite')
        
        print(f"[Picks Plugin] Initializing database at: {db_file}")
        self.db = DatabaseManager(db_file)

    # ----------------------------------------------------------------------
    # Helper & Utility Methods
    # ----------------------------------------------------------------------

    def _get_tz(self, channel: str) -> zoneinfo.ZoneInfo:
        tz_str = self.registryValue('timezone', channel)
        try:
            return zoneinfo.ZoneInfo(tz_str)
        except Exception:
            return zoneinfo.ZoneInfo('America/Vancouver')

    def _parse_datetime(self, date_str: str, time_str: Optional[str], tz: zoneinfo.ZoneInfo) -> datetime.datetime:
        if time_str:
            dt_raw = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        else:
            dt_raw = datetime.datetime.strptime(f"{date_str} 23:59", "%Y-%m-%d %H:%M")
        
        dt_local = dt_raw.replace(tzinfo=tz)
        return dt_local.astimezone(datetime.timezone.utc)

    def _format_dt(self, utc_iso: str, tz: zoneinfo.ZoneInfo) -> str:
        dt_utc = datetime.datetime.fromisoformat(utc_iso)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)
        dt_local = dt_utc.astimezone(tz)
        return dt_local.strftime("%b %d, %Y at %H:%M %Z")

    def _slugify(self, text: str) -> str:
        """Converts a short title or question fragment into a clean URL/IRC-friendly slug."""
        text = text.lower().strip()
        text = re.sub(r'[^a-z0-9\s_-]', '', text)
        text = re.sub(r'[\s_-]+', '-', text)
        return text[:30] or "market"

    def _resolve_market(self, cursor: sqlite3.Cursor, channel: str, identifier: str) -> Optional[sqlite3.Row]:
        """Finds a market by primary ID integer string or text slug."""
        if identifier.isdigit():
            cursor.execute("SELECT * FROM markets WHERE id = ? AND channel = ?", (int(identifier), channel))
        else:
            cursor.execute("SELECT * FROM markets WHERE slug = ? AND channel = ?", (identifier.lower(), channel))
        return cursor.fetchone()

    def _get_or_create_user(self, conn: sqlite3.Connection, channel: str, hostmask: str, nick: str) -> sqlite3.Row:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE channel = ? AND hostmask = ?", (channel, hostmask))
        user = cursor.fetchone()

        if user:
            if user['nickname'] != nick:
                cursor.execute("""
                    UPDATE users SET nickname = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """, (nick, user['id']))
            return user

        starting_bal = self.registryValue('startingPoints', channel)
        cursor.execute("""
            INSERT INTO users (channel, hostmask, nickname, balance)
            VALUES (?, ?, ?, ?)
        """, (channel, hostmask, nick, starting_bal))
        user_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO ledger (user_id, amount, transaction_type, description)
            VALUES (?, ?, 'INITIAL_BALANCE', 'Account created with starting points')
        """, (user_id, starting_bal))

        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cursor.fetchone()

    def _get_user_balances(self, conn: sqlite3.Connection, user_id: int) -> Tuple[int, int, int]:
        """Returns tuple of (total_balance, wagered_points, available_points)."""
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        available = row['balance'] if row else 0

        cursor.execute("""
            SELECT COALESCE(SUM(w.points), 0) AS wagered
            FROM wagers w
            JOIN markets m ON w.market_id = m.id
            WHERE w.user_id = ? AND m.status = 'OPEN' AND m.close_time > CURRENT_TIMESTAMP
        """, (user_id,))
        wagered = cursor.fetchone()['wagered']
        total_bal = available + wagered
        return total_bal, wagered, available

    # ----------------------------------------------------------------------
    # User Commands
    # ----------------------------------------------------------------------

    @wrap(['channel', 'something', 'text'])
    def add(self, irc, msg, args, channel, date_str, rest_of_args):
        """<date> [time] <slug> <question> <outcome1>|<outcome2>|...

        Creates a prediction market. Date format: YYYY-MM-DD. Optional time defaults to 23:59.
        Example: @picks add 2026-10-01 Coinflip Will it land on heads? Heads|Tails
        """
        if not (ircutils.isUserHostmask(msg.prefix) and 
                (ircdb.checkCapability(msg.prefix, 'admin') or ircdb.checkCapability(msg.prefix, 'owner'))):
            irc.error("Permission denied. You must have admin/owner capability to create markets.", Raise=True)

        tz = self._get_tz(channel)

        # Check if the first word after the date looks like a time (e.g., "09:00" or "9:30")
        parts = rest_of_args.split(maxsplit=1)
        potential_time = parts[0]
        
        if re.match(r'^\d{1,2}:\d{2}$', potential_time):
            time_str = potential_time
            raw_slug_q_and_outcomes = parts[1] if len(parts) > 1 else ""
        else:
            time_str = None
            raw_slug_q_and_outcomes = rest_of_args

        try:
            close_dt_utc = self._parse_datetime(date_str, time_str, tz)
        except ValueError:
            irc.error("Invalid date or time format. Use YYYY-MM-DD [HH:MM].", Raise=True)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if close_dt_utc <= now_utc:
            irc.error("Market close date/time must be set in the future.", Raise=True)

        if '?' in raw_slug_q_and_outcomes:
            q_parts = raw_slug_q_and_outcomes.rsplit('?', 1)
            first_part = q_parts[0].strip()
            question = first_part + '?'
            outcomes_raw = q_parts[1].strip()
            
            sub_parts = first_part.split(maxsplit=1)
            if len(sub_parts) == 2 and not sub_parts[0].endswith('?'):
                slug = self._slugify(sub_parts[0])
                question = sub_parts[1] + '?'
            else:
                slug = self._slugify(first_part)
        else:
            irc.error("Please supply a valid question ending with a '?' followed by outcomes separated by '|'.", Raise=True)

        outcomes = [o.strip() for o in outcomes_raw.split('|') if o.strip()]
        if len(outcomes) < 2:
            irc.error("A market must contain at least 2 distinct outcomes separated by '|'.", Raise=True)

        if len(set(o.lower() for o in outcomes)) < len(outcomes):
            irc.error("Outcome choices must be unique.", Raise=True)

        close_iso = close_dt_utc.strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM markets WHERE channel = ? AND slug = ?", (channel, slug))
            if cursor.fetchone():
                slug = f"{slug}-{int(datetime.datetime.now().timestamp()) % 1000}"

            cursor.execute("""
                INSERT INTO markets (channel, slug, question, close_time, status, created_by)
                VALUES (?, ?, ?, ?, 'OPEN', ?)
            """, (channel, slug, question, close_iso, msg.nick))
            market_id = cursor.lastrowid

            for outcome in outcomes:
                cursor.execute("""
                    INSERT INTO outcomes (market_id, name) VALUES (?, ?)
                """, (market_id, outcome))
            conn.commit()

        formatted_close = self._format_dt(close_iso, tz)
        irc.reply(f"Market #{market_id} [slug: {slug}] created: \"{question}\" Choices: {', '.join(outcomes)}. Closes: {formatted_close}.")



    @wrap(['channel', 'something', 'something', 'int'])
    def pick(self, irc, msg, args, channel, market_id_or_slug, choice, points):
        """<market_id|slug> <outcome> <points>

        Wagers virtual points on an outcome for an open prediction market.
        """
        if points <= 0:
            irc.error("Wager amount must be a positive integer.", Raise=True)

        min_w = self.registryValue('minWager', channel)
        max_w = self.registryValue('maxWager', channel)
        if points < min_w:
            irc.error(f"Minimum allowed wager is {min_w} points.", Raise=True)
        if max_w > 0 and points > max_w:
            irc.error(f"Maximum allowed wager is {max_w} points.", Raise=True)

        tz = self._get_tz(channel)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            user = self._get_or_create_user(conn, channel, msg.prefix, msg.nick)

            market = self._resolve_market(cursor, channel, market_id_or_slug)
            if not market:
                irc.error(f"Market '{market_id_or_slug}' not found in this channel.", Raise=True)
            market_id = market['id']

            close_dt = datetime.datetime.fromisoformat(market['close_time']).replace(tzinfo=datetime.timezone.utc)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if market['status'] != 'OPEN' or now_utc >= close_dt:
                formatted_close = self._format_dt(market['close_time'], tz)
                irc.error(f"Market #{market_id} ({market['slug']}) is closed. Predictions were accepted until {formatted_close}.", Raise=True)

            cursor.execute("SELECT * FROM outcomes WHERE market_id = ?", (market_id,))
            outcomes = {o['name'].lower(): o for o in cursor.fetchall()}
            if choice.lower() not in outcomes:
                valid_names = ", ".join([o['name'] for o in outcomes.values()])
                irc.error(f"Invalid outcome '{choice}'. Valid options: {valid_names}", Raise=True)
            
            selected_outcome = outcomes[choice.lower()]

            cursor.execute("SELECT points, outcome_id FROM wagers WHERE market_id = ? AND user_id = ?", (market_id, user['id']))
            existing_wager = cursor.fetchone()

            existing_points = existing_wager['points'] if existing_wager else 0
            total_bal, wagered_points, available_points = self._get_user_balances(conn, user['id'])
            
            effective_available = available_points + existing_points

            if points > effective_available:
                irc.error(f"Insufficient available balance. You have {effective_available} points available for this market.", Raise=True)

            if existing_wager:
                cursor.execute("""
                    INSERT INTO ledger (user_id, amount, transaction_type, market_id, description)
                    VALUES (?, ?, 'WAGER_RETURN', ?, ?)
                """, (user['id'], existing_points, market_id, f"Wager modification refund for Market #{market_id}"))

                cursor.execute("""
                    UPDATE wagers SET outcome_id = ?, points = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE market_id = ? AND user_id = ?
                """, (selected_outcome['id'], points, market_id, user['id']))
                
                cursor.execute("UPDATE users SET balance = balance + ? - ? WHERE id = ?", (existing_points, points, user['id']))
            else:
                cursor.execute("""
                    INSERT INTO wagers (market_id, user_id, outcome_id, points)
                    VALUES (?, ?, ?, ?)
                """, (market_id, user['id'], selected_outcome['id'], points))
                
                cursor.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (points, user['id']))

            cursor.execute("""
                INSERT INTO ledger (user_id, amount, transaction_type, market_id, description)
                VALUES (?, ?, 'WAGER', ?, ?)
            """, (user['id'], -points, market_id, f"Wagered on '{selected_outcome['name']}' in Market #{market_id}"))

            conn.commit()

        irc.reply(f"Wager confirmed! {msg.nick} wagered {points} points on '{selected_outcome['name']}' for Market #{market_id} ({market['slug']}).")

    @wrap(['channel', optional('something')])
    def markets(self, irc, msg, args, channel, filter_status=None):
        """[OPEN|CLOSED|RESOLVED|CANCELLED]

        Lists prediction markets in the channel. Defaults to showing open markets.
        """
        status_filter = filter_status.upper() if filter_status else 'OPEN'
        if status_filter not in ('OPEN', 'CLOSED', 'RESOLVED', 'CANCELLED', 'ALL'):
            status_filter = 'OPEN'

        tz = self._get_tz(channel)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            if status_filter == 'ALL':
                cursor.execute("SELECT * FROM markets WHERE channel = ? ORDER BY id DESC LIMIT 10", (channel,))
            else:
                cursor.execute("SELECT * FROM markets WHERE channel = ? AND status = ? ORDER BY id DESC LIMIT 10", (channel, status_filter))
            rows = cursor.fetchall()

        if not rows:
            irc.reply(f"No {status_filter.lower()} markets found for {channel}.")
            return

        lines = []
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for m in rows:
            close_dt = datetime.datetime.fromisoformat(m['close_time']).replace(tzinfo=datetime.timezone.utc)
            is_expired = now_utc >= close_dt and m['status'] == 'OPEN'
            disp_status = "CLOSED" if is_expired else m['status']
            close_str = self._format_dt(m['close_time'], tz)
            lines.append(f"#{m['id']} [{m['slug']}] \"{m['question']}\" [{disp_status}] — Closes: {close_str}")

        irc.reply(f"{status_filter.capitalize()} Markets: " + " | ".join(lines))

    @wrap(['channel', 'something'])
    def market(self, irc, msg, args, channel, market_id_or_slug):
        """<market_id|slug>

        Displays detailed status, options, pool size, and your active wager for a market.
        """
        tz = self._get_tz(channel)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            market = self._resolve_market(cursor, channel, market_id_or_slug)
            if not market:
                irc.error(f"Market '{market_id_or_slug}' not found.", Raise=True)
            market_id = market['id']

            cursor.execute("SELECT * FROM outcomes WHERE market_id = ?", (market_id,))
            outcomes = cursor.fetchall()

            cursor.execute("""
                SELECT outcome_id, COALESCE(SUM(points), 0) as total_pts, COUNT(*) as wagers_cnt
                FROM wagers WHERE market_id = ? GROUP BY outcome_id
            """, (market_id,))
            wager_stats = {row['outcome_id']: row for row in cursor.fetchall()}

            total_pool = sum(s['total_pts'] for s in wager_stats.values())

            user = self._get_or_create_user(conn, channel, msg.prefix, msg.nick)
            cursor.execute("""
                SELECT w.points, o.name as outcome_name
                FROM wagers w JOIN outcomes o ON w.outcome_id = o.id
                WHERE w.market_id = ? AND w.user_id = ?
            """, (market_id, user['id']))
            user_wager = cursor.fetchone()

        close_str = self._format_dt(market['close_time'], tz)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        close_dt = datetime.datetime.fromisoformat(market['close_time']).replace(tzinfo=datetime.timezone.utc)
        status = "CLOSED" if (now_utc >= close_dt and market['status'] == 'OPEN') else market['status']

        outcomes_fmt = []
        for o in outcomes:
            st = wager_stats.get(o['id'], {'total_pts': 0, 'wagers_cnt': 0})
            outcomes_fmt.append(f"{o['name']} ({st['total_pts']} pts / {st['wagers_cnt']} wagers)")

        user_wager_str = f"{user_wager['points']} points on '{user_wager['outcome_name']}'" if user_wager else "None"

        irc.reply(f"Market #{market['id']} [{market['slug']}] [{status}] — \"{market['question']}\" | Closes: {close_str} | "
                  f"Outcomes: [{', '.join(outcomes_fmt)}] | Total Pool: {total_pool} pts | Your Wager: {user_wager_str}")

    @wrap(['channel'])
    def balance(self, irc, msg, args, channel):
        """Displays your current point balance, active wagers, and available balance."""
        with self.db.get_conn() as conn:
            user = self._get_or_create_user(conn, channel, msg.prefix, msg.nick)
            total_bal, wagered, available = self._get_user_balances(conn, user['id'])

        irc.reply(f"{msg.nick}: Net Worth: {total_bal} pts | Available: {available} pts | Locked in Wagers: {wagered} pts")

    @wrap(['channel'])
    def leaderboard(self, irc, msg, args, channel):
        """Displays the top players ranked by net worth in the channel."""
        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.nickname, 
                       u.balance + COALESCE(active_wagers.total_wagered, 0) AS net_worth
                FROM users u
                LEFT JOIN (
                    SELECT w.user_id, SUM(w.points) AS total_wagered
                    FROM wagers w
                    JOIN markets m ON w.market_id = m.id
                    WHERE m.status = 'OPEN' AND m.close_time > CURRENT_TIMESTAMP
                    GROUP BY w.user_id
                ) active_wagers ON u.id = active_wagers.user_id
                WHERE u.channel = ?
                ORDER BY net_worth DESC LIMIT 10
            """, (channel,))
            rows = cursor.fetchall()

        if not rows:
            irc.reply("No registered participants found on the leaderboard yet.")
            return

        leaderboard_str = ", ".join([f"{idx+1}. {r['nickname']} ({r['net_worth']} pts)" for idx, r in enumerate(rows)])
        irc.reply(f"Prediction Leaderboard ({channel}): {leaderboard_str}")

    @wrap(['channel'])
    def history(self, irc, msg, args, channel):
        """Shows your recent wager history and outcome results."""
        with self.db.get_conn() as conn:
            user = self._get_or_create_user(conn, channel, msg.prefix, msg.nick)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT w.points, m.id as market_id, m.slug, m.status, m.winning_outcome_id,
                       o.id as outcome_id, o.name as outcome_name
                FROM wagers w
                JOIN markets m ON w.market_id = m.id
                JOIN outcomes o ON w.outcome_id = o.id
                WHERE w.user_id = ?
                ORDER BY w.updated_at DESC LIMIT 5
            """, (user['id'],))
            wagers = cursor.fetchall()

        if not wagers:
            irc.reply(f"{msg.nick}: You have no prediction history.")
            return

        history_items = []
        for w in wagers:
            if w['status'] == 'RESOLVED':
                if w['outcome_id'] == w['winning_outcome_id']:
                    res_str = "Won"
                else:
                    res_str = "Lost"
            else:
                res_str = w['status']
            history_items.append(f"#{w['market_id']} ({w['slug']}) on '{w['outcome_name']}' ({w['points']} pts) → {res_str}")

        irc.reply(f"History for {msg.nick}: " + " | ".join(history_items))

    # ----------------------------------------------------------------------
    # Admin / Resolution Commands
    # ----------------------------------------------------------------------

    @wrap(['channel', 'something', 'text'])
    def resolve(self, irc, msg, args, channel, market_id_or_slug, winning_choice):
        """<market_id|slug> <winning_outcome>

        Resolves a market and distributes payouts according to pari-mutuel rules. (Admin only)
        """
        if not (ircutils.isUserHostmask(msg.prefix) and 
                (ircdb.checkCapability(msg.prefix, 'admin') or ircdb.checkCapability(msg.prefix, 'owner'))):
            irc.error("Permission denied. You must have admin/owner capability.", Raise=True)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()

            market = self._resolve_market(cursor, channel, market_id_or_slug)
            if not market:
                irc.error(f"Market '{market_id_or_slug}' not found.", Raise=True)
            market_id = market['id']

            if market['status'] in ('RESOLVED', 'CANCELLED'):
                irc.error(f"Market #{market_id} ({market['slug']}) is already {market['status']}.", Raise=True)

            cursor.execute("SELECT * FROM outcomes WHERE market_id = ?", (market_id,))
            outcomes = cursor.fetchall()
            matching_outcome = next((o for o in outcomes if o['name'].lower() == winning_choice.lower()), None)

            if not matching_outcome:
                valid_names = ", ".join([o['name'] for o in outcomes])
                irc.error(f"Invalid outcome '{winning_choice}'. Choices were: {valid_names}", Raise=True)

            cursor.execute("SELECT * FROM wagers WHERE market_id = ?", (market_id,))
            all_wagers = cursor.fetchall()

            if not all_wagers:
                cursor.execute("""
                    UPDATE markets SET status = 'CANCELLED', resolved_at = CURRENT_TIMESTAMP WHERE id = ?
                """, (market_id,))
                conn.commit()
                irc.reply(f"Market #{market_id} ({market['slug']}) resolved with zero participants. Market marked CANCELLED.")
                return

            winning_wagers = [w for w in all_wagers if w['outcome_id'] == matching_outcome['id']]
            losing_wagers = [w for w in all_wagers if w['outcome_id'] != matching_outcome['id']]

            if not winning_wagers:
                for w in all_wagers:
                    cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (w['points'], w['user_id']))
                    cursor.execute("""
                        INSERT INTO ledger (user_id, amount, transaction_type, market_id, description)
                        VALUES (?, ?, 'REFUND', ?, ?)
                    """, (w['user_id'], w['points'], market_id, f"Market #{market_id} cancelled: No winning picks."))

                cursor.execute("""
                    UPDATE markets SET status = 'CANCELLED', resolved_at = CURRENT_TIMESTAMP WHERE id = ?
                """, (market_id,))
                conn.commit()
                irc.reply(f"Market #{market_id} ({market['slug']}) cancelled! Nobody picked '{matching_outcome['name']}'. All wagers returned.")
                return

            losing_pool = sum(w['points'] for w in losing_wagers)
            num_winners = len(winning_wagers)
            share_per_winner = losing_pool // num_winners if num_winners > 0 else 0

            for w in winning_wagers:
                payout = w['points'] + share_per_winner
                cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (payout, w['user_id']))
                
                cursor.execute("""
                    INSERT INTO ledger (user_id, amount, transaction_type, market_id, description)
                    VALUES (?, ?, 'WINNINGS', ?, ?)
                """, (w['user_id'], payout, market_id, 
                      f"Market #{market_id} payout: {w['points']} original + {share_per_winner} share of losing pool"))

            cursor.execute("""
                UPDATE markets
                SET status = 'RESOLVED', winning_outcome_id = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (matching_outcome['id'], market_id))

            conn.commit()

        irc.reply(f"Market #{market_id} ({market['slug']}) RESOLVED! Winning outcome: '{matching_outcome['name']}'. "
                  f"Total Winners: {num_winners} | Losing Pool Distributed: {losing_pool} pts ({share_per_winner} pts/winner).")

    @wrap(['channel', 'something'])
    def cancel(self, irc, msg, args, channel, market_id_or_slug):
        """<market_id|slug>

        Cancels an active/closed market and refunds all wagered points. (Admin only)
        """
        if not (ircutils.isUserHostmask(msg.prefix) and 
                (ircdb.checkCapability(msg.prefix, 'admin') or ircdb.checkCapability(msg.prefix, 'owner'))):
            irc.error("Permission denied. You must have admin/owner capability.", Raise=True)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            market = self._resolve_market(cursor, channel, market_id_or_slug)
            if not market:
                irc.error(f"Market '{market_id_or_slug}' not found.", Raise=True)
            market_id = market['id']

            if market['status'] in ('RESOLVED', 'CANCELLED'):
                irc.error(f"Market #{market_id} ({market['slug']}) is already {market['status']}.", Raise=True)

            cursor.execute("SELECT * FROM wagers WHERE market_id = ?", (market_id,))
            wagers = cursor.fetchall()

            for w in wagers:
                cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (w['points'], w['user_id']))
                cursor.execute("""
                    INSERT INTO ledger (user_id, amount, transaction_type, market_id, description)
                    VALUES (?, ?, 'REFUND', ?, ?)
                """, (w['user_id'], w['points'], market_id, f"Market #{market_id} cancelled by admin."))

            cursor.execute("""
                UPDATE markets SET status = 'CANCELLED', resolved_at = CURRENT_TIMESTAMP WHERE id = ?
            """, (market_id,))
            conn.commit()

        irc.reply(f"Market #{market_id} ({market['slug']}) CANCELLED. Refunded {len(wagers)} wager(s).")

    @wrap(['channel', 'something', 'int', 'text'])
    def adjust(self, irc, msg, args, channel, nick_or_hostmask, amount, reason):
        """<nick|hostmask> <amount> <reason>

        Manually adjust a user's point balance (can be positive or negative). Generates audit log. (Admin only)
        """
        if not (ircutils.isUserHostmask(msg.prefix) and 
                (ircdb.checkCapability(msg.prefix, 'admin') or ircdb.checkCapability(msg.prefix, 'owner'))):
            irc.error("Permission denied.", Raise=True)

        with self.db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM users WHERE channel = ? AND (nickname = ? OR hostmask = ?)
            """, (channel, nick_or_hostmask, nick_or_hostmask))
            user = cursor.fetchone()

            if not user:
                irc.error(f"User '{nick_or_hostmask}' not found in channel {channel}.", Raise=True)

            cursor.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user['id']))
            cursor.execute("""
                INSERT INTO ledger (user_id, amount, transaction_type, description)
                VALUES (?, ?, 'ADMIN_ADJUSTMENT', ?)
            """, (user['id'], amount, f"Admin adjustment by {msg.nick}: {reason}"))
            
            conn.commit()

        irc.reply(f"Adjusted balance for {user['nickname']} by {amount} points. New balance updated.")


Class = Picks
