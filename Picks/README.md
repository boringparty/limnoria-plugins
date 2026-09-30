# Picks Plugin for Limnoria

A localized, non-monetary prediction market plugin for IRC. Allow channel users to wager virtual points on custom prediction questions using clean text slugs or numeric IDs.

---

## Commands

### User Commands

*   **`@picks add <date> [time] <slug/question> <outcome1>|<outcome2>|...`**
    Creates a new prediction market (Admin/Owner only).
    *   **Date format:** `YYYY-MM-DD`
    *   **Time format:** Optional `HH:MM` (defaults to `23:59` if omitted).
    *   **Slug extraction:** If you put a short keyword right after the date/time (e.g., `Coinflip`), it becomes the short nickname. If omitted, a slug is automatically generated from the question.
    *   *Example (with time & custom slug):*
        `@picks add 2026-10-01 09:00 Coinflip Will it land on heads? Heads|Tails`
    *   *Example (default time & auto-slug):*
        `@picks add 2026-10-01 Will it rain tomorrow? Yes|No`

*   **`@picks pick <market_id|slug> <outcome> <points>`**
    Wagers virtual points on an outcome for an open market. You can modify your wager anytime before the market closes.
    *   *Example:* `@picks pick coinflip Heads 100`
    *   *Example:* `@picks pick 1 Tails 50`

*   **`@picks markets [OPEN|CLOSED|RESOLVED|CANCELLED|ALL]`**
    Lists prediction markets in the channel. Defaults to showing open markets.
    *   *Example:* `@picks markets OPEN`
    *   *Example:* `@picks markets ALL`

*   **`@picks market <market_id|slug>`**
    Displays detailed status, options, pool sizes, total wagers, and your active wager for a specific market.
    *   *Example:* `@picks market coinflip`

*   **`@picks balance`**
    Displays your current point balance, active wagers locked in open markets, and available points.
    *   *Example:* `@picks balance`

*   **`@picks leaderboard`**
    Displays the top 10 participants ranked by total net worth (balance + active wagers) in the channel.
    *   *Example:* `@picks leaderboard`

*   **`@picks history`**
    Shows your recent wager history, chosen outcomes, and whether they won or lost.
    *   *Example:* `@picks history`

---

### Admin Commands

*   **`@picks resolve <market_id|slug> <winning_outcome>`**
    Resolves a market and distributes payouts using pari-mutuel rules (Admin/Owner only). Losing points are split proportionally among winners.
    *   *Example:* `@picks resolve coinflip Heads`

*   **`@picks cancel <market_id|slug>`**
    Cancels an active or closed market and fully refunds all wagered points to participants (Admin/Owner only).
    *   *Example:* `@picks cancel coinflip`

*   **`@picks adjust <nick|hostmask> <amount> <reason>`**
    Manually adjusts a user's point balance up or down and records an audit log entry (Admin/Owner only).
    *   *Example:* `@picks adjust Alice 500 Bonus points for bug hunting`
