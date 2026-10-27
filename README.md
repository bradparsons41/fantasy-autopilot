# ESPN Fantasy Basketball Autopilot

Runs every 15 minutes during NBA game windows and:

- **Benches starters ruled OUT** (or suspended) and starts your best healthy bench player who has a game today, reshuffling flex spots (G, F, UTIL) if needed.
- **Starts players who play over ones who don't** (or, in `injuries_only` mode, only touches lineups when someone is out).
- **Streams free agents** when a starting slot would otherwise sit empty, dropping only players you've listed as droppable.
- **Pushes a phone alert** for every move, for any OUT player whose game already locked, and when your ESPN cookies expire.

It's a dry run until you switch it to live.

## Setup (about 15 minutes)

### 1. Get your ESPN cookies
On a computer, log in at fantasy.espn.com, open your league, then open developer tools (Chrome: F12 → **Application** → **Cookies** → `https://fantasy.espn.com`). Copy the values of:
- `espn_s2` (a long string)
- `SWID` (looks like `{ABCD-1234-...}`, braces included)

These act like your password. Never put them in a file you commit. They last a few months; the tool alerts you when they expire.

### 2. Fill in `config.toml`
- `league_id` from your league URL (`...?leagueId=123456`). Your team is found by `team_name`, or you can set `team_id` instead.
- `year = 2027` for the 2026–27 season.
- Under `[waivers]`, list the players you're happy to drop in `droppable`. **The tool never drops anyone not on that list.** Leave it empty to only ever add into an open roster spot.

### 3. Get push alerts (optional but recommended)
Install the free **ntfy** app, subscribe to a hard-to-guess topic name (e.g. `hoops-autopilot-7f3k2`), and use that name as `NTFY_TOPIC` below.

### 4. Put it on GitHub
1. Create a **private** repository and upload these files, keeping the `.github/workflows` folder.
2. **Settings → Secrets and variables → Actions → Secrets**: add `ESPN_S2`, `ESPN_SWID`, and `NTFY_TOPIC`.
3. **Actions** tab → *Fantasy autopilot* → **Run workflow** to test. Open the run's log to see what it would have done, including the exact requests it would send.

### 5. Go live
Once a few dry runs look right, add a repository **variable** (Settings → Secrets and variables → Actions → **Variables**) named `AUTOPILOT_LIVE` with value `1`. Delete it to switch back to dry runs.

**Check the first live run in the ESPN app.** The write requests use ESPN's unofficial internal endpoint; if ESPN rejects them, you'll get an error alert with ESPN's response, and nothing on your roster changes.

## Running on your own machine instead
```
pip install -r requirements.txt
export ESPN_S2='...' ESPN_SWID='{...}' NTFY_TOPIC='...'
python autopilot.py            # dry run
python autopilot.py --live     # real moves
```
For cron, every 15 minutes: `*/15 * * * * cd /path/to/espn-autopilot && ESPN_S2=... ESPN_SWID=... python3 autopilot.py --live`

## Good to know
- **Locks:** ESPN locks each player at his own tip-off. A scratch announced after tip-off can't be fixed by anyone; you'll get an alert that it happened.
- **Timing:** GitHub's scheduler can run 5–15 minutes late at busy times. If that matters, run it from an always-on machine with cron.
- **GitHub minutes:** a private repo gets 2,000 free minutes a month; this schedule uses about 1,600. Change `*/15` to `*/20` in the workflow if you're close.
- **Rankings:** players are ranked by fantasy points per game (season average, ESPN projection, or a blend). Category leagues use a points-style formula on per-game stats, which is a rough proxy.
- **Not handled:** moving players to or from IR, and trades. Day-to-day players are left in your lineup unless you add `DAY_TO_DAY` to `bench_statuses`.
- **ESPN's rules:** automation isn't officially supported by ESPN. The tool makes a handful of requests per run, but use it at your own discretion.

## Tests
`python -m pytest -q` checks the lineup and pickup logic against made-up rosters without contacting ESPN.
