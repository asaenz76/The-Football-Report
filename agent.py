from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(".env.local")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
API_FOOTBALL_KEY = os.environ["API_FOOTBALL_KEY"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
WP_URL = os.environ["WP_URL"]
WP_USERNAME = os.environ["WP_USERNAME"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]

API_FOOTBALL_BASE = "https://apiv3.apifootball.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

DATA_DIR = Path(__file__).parent / "data"
ODDS_HISTORY_PATH = DATA_DIR / "odds_history.json"
RUN_LOG_PATH = DATA_DIR / "run_log.json"
PENDING_DIR = DATA_DIR / "pending"
MAX_LOG_ENTRIES = 90

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

# Tier 1-3 sources from the editorial standard's SOURCE HIERARCHY. Hard restriction on
# the web search tool — Claude can only cite from these domains.
#
# NOTE: apnews.com, bbc.com, bbc.co.uk, nytimes.com, reuters.com, theathletic.com, and
# transfermarkt.com are rejected by the Anthropic web search tool with "not accessible
# to our user agent" (their robots.txt/user-agent rules block the crawler) — they are
# excluded here. See the report back to the user about this; it significantly thins out
# Tier 2 coverage and removes the Tier 3 reference site.
ALLOWED_DOMAINS = [
    # Tier 1: official governing bodies
    "fifa.com", "uefa.com", "conmebol.com", "concacaf.com", "cafonline.com", "the-afc.com",
    # Tier 1: official league sites
    "premierleague.com", "laliga.com", "legaseriea.it", "bundesliga.com", "ligue1.com",
    "eredivisie.nl", "ligaportugal.pt", "mlssoccer.com", "cbf.com.br", "spl.com.sa",
    # Tier 2: trusted journalism (reachable subset)
    "skysports.com", "espn.com",
    # Tier 3: statistical/data reference (reachable subset)
    "fbref.com", "understat.com", "opta.com",
]

OUTPUT_FORMAT_INSTRUCTIONS = """
Once you have completed the edition according to your editorial standard, output your
final answer in exactly this format and nothing else — no preamble, no commentary before
or after, no markdown code fences:

<meta>
{"title": "...", "slug": "...", "meta_description": "...", "og_title": "...", "og_description": "...", "publication_date": "YYYY-MM-DD"}
</meta>
<content>
...full HTML body of the edition. Each of the five sections under its own <h2> heading,
in order: Story of the Day, What Happened Overnight, Matches to Watch, Market Watch,
Talking Point of the Day...
</content>
""".strip()


def check_anthropic() -> None:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with the word: ready"}],
    )
    text = response.content[0].text.strip()
    print(f"  Anthropic: OK — model replied \"{text}\"")


def check_api_football() -> None:
    resp = requests.get(
        f"{API_FOOTBALL_BASE}/",
        params={"action": "get_countries", "APIkey": API_FOOTBALL_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload.get("message", payload))
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"unexpected response shape: {payload}")
    print(f"  API-Football (apifootball.com): OK — {len(payload)} countries available")


def check_odds_api() -> None:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports",
        params={"apiKey": ODDS_API_KEY},
        timeout=15,
    )
    resp.raise_for_status()
    sports = resp.json()
    remaining = resp.headers.get("x-requests-remaining")
    print(f"  The Odds API: OK — {len(sports)} sports available, {remaining} requests remaining")


def check_wordpress() -> None:
    resp = requests.get(
        f"{WP_URL}/wp-json/wp/v2/users/me",
        auth=(WP_USERNAME, WP_APP_PASSWORD),
        timeout=15,
    )
    resp.raise_for_status()
    user = resp.json()
    print(f"  WordPress: OK — authenticated as \"{user['name']}\" (user id {user['id']})")


def _apifootball_call(action: str, retries: int = 4, **params) -> list:
    # apifootball.com returns frequent 500/502s under normal operation — observed in
    # practice, not just theoretical — so retry with backoff meaningfully improves
    # reliability without masking a genuinely broken key/plan.
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                f"{API_FOOTBALL_BASE}/",
                params={"action": action, "APIkey": API_FOOTBALL_KEY, **params},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(f"{action}: {payload.get('message', payload)}")
            if not isinstance(payload, list):
                return []
            return payload
        except (requests.exceptions.RequestException, RuntimeError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(3 * (2**attempt))  # 3s, 6s, 12s, 24s
    raise last_exc


def get_events(date_str: str) -> list:
    """Fixtures and results for a single day (yyyy-mm-dd), across all leagues."""
    return _apifootball_call("get_events", **{"from": date_str, "to": date_str})


def get_standings(league_id: str) -> list:
    try:
        return _apifootball_call("get_standings", league_id=league_id)
    except (RuntimeError, requests.exceptions.RequestException):
        # Not every league/plan tier exposes standings, and apifootball.com returns
        # intermittent 502s on this endpoint — don't fail the whole run over one league.
        return []


def _trim_event(event: dict) -> dict:
    """Keep only the fields useful for editorial judgment. Drops the heavy nested
    arrays (full match statistics, lineups, substitutions) that apifootball.com
    includes on every event — on the free plan these are mostly empty anyway, and
    including them was the single biggest driver of generation cost (every internal
    tool-use round trip in the Claude call resends the full context)."""
    return {
        "match_id": event.get("match_id"),
        "country_name": event.get("country_name"),
        "league_name": event.get("league_name"),
        "match_date": event.get("match_date"),
        "match_time": event.get("match_time"),
        "match_status": event.get("match_status"),
        "match_live": event.get("match_live"),
        "home_team": event.get("match_hometeam_name"),
        "home_score": event.get("match_hometeam_score"),
        "away_team": event.get("match_awayteam_name"),
        "away_score": event.get("match_awayteam_score"),
        "round": event.get("match_round"),
        "goalscorer": [
            {"time": g.get("time"), "score": g.get("score"), "side": g.get("info")}
            for g in event.get("goalscorer", [])
        ],
        "cards": [
            {"time": c.get("time"), "card": c.get("card"), "side": c.get("info")}
            for c in event.get("cards", [])
        ],
    }


def _trim_standings_row(row: dict) -> dict:
    """Standings rows are already fairly flat — drop only the badge/logo URLs, which
    add bytes with no editorial value."""
    return {k: v for k, v in row.items() if not k.endswith(("_logo", "_badge"))}


def collect_football_data(date_str: str) -> dict:
    events = get_events(date_str)
    league_ids = sorted({e["league_id"] for e in events if e.get("league_id")})
    standings = {lid: get_standings(lid) for lid in league_ids}
    return {
        "events": [_trim_event(e) for e in events],
        "standings": {lid: [_trim_standings_row(r) for r in rows] for lid, rows in standings.items()},
    }


def get_active_soccer_sports() -> list:
    """Free — no odds/markets requested, doesn't count against quota."""
    resp = requests.get(f"{ODDS_API_BASE}/sports", params={"apiKey": ODDS_API_KEY}, timeout=15)
    resp.raise_for_status()
    return [s["key"] for s in resp.json() if s["group"] == "Soccer" and s["active"]]


def get_events_for_sport(sport_key: str) -> list:
    """Free — event list with no odds/markets, doesn't count against quota."""
    resp = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/events", params={"apiKey": ODDS_API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def sports_with_upcoming_matches(within_days: int = 3) -> list:
    """Which soccer sport keys have at least one match kicking off within the next
    `within_days` days. Uses only the free /events endpoint so we don't spend odds
    quota on leagues with nothing coming up soon.

    A window (not just "today") matters here: to detect market movement we need to
    observe the same match's odds on multiple days before kickoff. Restricting to
    matches happening exactly today would mean every match is seen exactly once —
    on the day it plays — and opening/current would always be identical."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=within_days)
    matching = []
    for sport_key in get_active_soccer_sports():
        for event in get_events_for_sport(sport_key):
            commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            if now <= commence <= cutoff:
                matching.append(sport_key)
                break
    return matching


def get_odds_for_sport(sport_key: str) -> list:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds",
        params={"apiKey": ODDS_API_KEY, "regions": "uk,us,eu", "markets": "h2h", "oddsFormat": "decimal"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _consensus_h2h(event: dict) -> dict | None:
    """Average decimal odds per outcome across all bookmakers offering h2h — a simple market consensus."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                totals[outcome["name"]] = totals.get(outcome["name"], 0.0) + outcome["price"]
                counts[outcome["name"]] = counts.get(outcome["name"], 0) + 1
    if not totals:
        return None
    return {name: round(totals[name] / counts[name], 3) for name in totals}


def collect_odds_snapshot(within_days: int = 3) -> dict:
    """Consensus h2h odds for soccer matches kicking off within `within_days` days,
    keyed by match id. Only spends odds-quota requests on leagues with something
    coming up soon."""
    snapshot = {}
    for sport_key in sports_with_upcoming_matches(within_days):
        try:
            events = get_odds_for_sport(sport_key)
        except requests.exceptions.HTTPError:
            # Some sport keys are outright/futures markets (e.g. "_winner") with no
            # per-match h2h market — skip rather than fail the whole run.
            continue
        for event in events:
            consensus = _consensus_h2h(event)
            if consensus is None:
                continue
            snapshot[event["id"]] = {
                "sport_key": sport_key,
                "sport_title": event["sport_title"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "commence_time": event["commence_time"],
                "h2h": consensus,
            }
    return snapshot


def load_odds_history() -> dict:
    if ODDS_HISTORY_PATH.exists():
        return json.loads(ODDS_HISTORY_PATH.read_text())
    return {}


def save_odds_history(history: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ODDS_HISTORY_PATH.write_text(json.dumps(history, indent=2, sort_keys=True))


def update_odds_history(history: dict, snapshot: dict, today: str) -> list:
    """Merge today's snapshot into history in place. Returns matches with an opening
    line from a previous day, i.e. candidates for describing market movement."""
    movements = []

    for match_id, today_data in snapshot.items():
        entry = history.get(match_id)
        if entry is None:
            history[match_id] = {
                "sport_key": today_data["sport_key"],
                "sport_title": today_data["sport_title"],
                "home_team": today_data["home_team"],
                "away_team": today_data["away_team"],
                "commence_time": today_data["commence_time"],
                "opening": {"date": today, "h2h": today_data["h2h"]},
                "latest": {"date": today, "h2h": today_data["h2h"]},
            }
            continue

        if entry["opening"]["date"] != today:
            movements.append({
                "home_team": entry["home_team"],
                "away_team": entry["away_team"],
                "commence_time": entry["commence_time"],
                "opening": entry["opening"],
                "current": {"date": today, "h2h": today_data["h2h"]},
            })
        entry["latest"] = {"date": today, "h2h": today_data["h2h"]}

    # Prune matches that have already kicked off — keeps the file bounded.
    now = datetime.now(timezone.utc)
    for match_id in list(history.keys()):
        commence = history[match_id].get("commence_time")
        try:
            kickoff = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if kickoff < now:
            del history[match_id]

    return movements


def generate_edition(date_str: str, football_data: dict, odds_movements: list) -> dict:
    """Calls Claude with the editorial system prompt, the collected structured data,
    and the web search tool (restricted to allowed_domains). Returns the parsed
    metadata + HTML content — does not publish anything."""
    user_message = (
        f"Today's date is {date_str}.\n\n"
        "Structured data collected for this date from API-Football and The Odds API — "
        "use this for all results, fixtures, standings, and market data. Never rely on "
        "memory for these; use web search only for stories, transfers, quotes, and context "
        "(including injury/suspension news, since no structured injuries feed is available).\n\n"
        f"FOOTBALL DATA (fixtures, results, standings):\n{json.dumps(football_data, ensure_ascii=False)}\n\n"
        "ODDS MARKET MOVEMENT CANDIDATES (matches with an opening line from a previous day; "
        "empty if none — in that case Market Watch should say plainly that no market moved "
        f"meaningfully):\n{json.dumps(odds_movements, ensure_ascii=False)}\n\n"
        f"{OUTPUT_FORMAT_INSTRUCTIONS}"
    )

    # A generous but bounded timeout — plain httpx/requests read timeouts are per-chunk,
    # not wall-clock, so a stalled streaming connection can otherwise hang indefinitely.
    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=480.0)
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 10,
            "allowed_domains": ALLOWED_DOMAINS,
        }
    ]

    with client.messages.stream(
        model="claude-sonnet-5",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=tools,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to generate this edition (safety refusal)")

    full_text = "\n".join(block.text for block in message.content if block.type == "text")

    meta_match = re.search(r"<meta>(.*?)</meta>", full_text, re.DOTALL)
    content_match = re.search(r"<content>(.*?)</content>", full_text, re.DOTALL)
    if not meta_match or not content_match:
        raise RuntimeError(
            "Claude's response did not match the expected <meta>/<content> format:\n" + full_text
        )

    metadata = json.loads(meta_match.group(1).strip())
    content_html = content_match.group(1).strip()

    searches_used = sum(
        1 for block in message.content if block.type == "server_tool_use" and block.name == "web_search"
    )

    return {
        "metadata": metadata,
        "content_html": content_html,
        "searches_used": searches_used,
        "usage": message.usage,
    }


def publish_to_wordpress(metadata: dict, content_html: str, status: str = "draft", post_id: int | None = None) -> dict:
    """Create (or update, if post_id is given) a WordPress post for this edition.

    SEO/OG fields go through Yoast's REST-registered meta keys — this WordPress
    install only exposes _yoast_wpseo_title and _yoast_wpseo_metadesc via the REST
    API (verified empirically; Yoast free doesn't register dedicated Open Graph
    override fields for REST write access). Yoast falls back to these same two
    fields for og:title/og:description when no override exists, so this covers
    both the meta description and the Open Graph tags with the fields available.
    """
    payload = {
        "title": metadata["title"],
        "slug": metadata["slug"],
        "content": content_html,
        "status": status,
        "meta": {
            "_yoast_wpseo_title": metadata.get("og_title", metadata["title"]),
            "_yoast_wpseo_metadesc": metadata.get("meta_description", ""),
        },
    }

    url = f"{WP_URL}/wp-json/wp/v2/posts"
    if post_id:
        url += f"/{post_id}"

    resp = requests.post(url, auth=(WP_USERNAME, WP_APP_PASSWORD), json=payload, timeout=30)
    resp.raise_for_status()
    post = resp.json()
    return {"id": post["id"], "link": post["link"], "status": post["status"]}


def load_run_log() -> list:
    if RUN_LOG_PATH.exists():
        return json.loads(RUN_LOG_PATH.read_text())
    return []


def save_run_log(log: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    RUN_LOG_PATH.write_text(json.dumps(log[-MAX_LOG_ENTRIES:], indent=2))


def _log_run(status: str, date_str: str, **details) -> None:
    log = load_run_log()
    log.append({
        "date": date_str,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **details,
    })
    save_run_log(log)


def _already_ran_today(date_str: str) -> dict | None:
    """A prior successful run today (draft or published) — retrying would create a
    second WordPress post for the same date. Only a "failed" status allows a rerun."""
    for entry in reversed(load_run_log()):
        if entry["date"] == date_str and entry["status"] in ("draft", "publish"):
            return entry
    return None


def _pending_path(date_str: str) -> Path:
    return PENDING_DIR / f"{date_str}.json"


def _load_pending(date_str: str) -> dict | None:
    path = _pending_path(date_str)
    return json.loads(path.read_text()) if path.exists() else None


def _save_pending(date_str: str, metadata: dict, content_html: str, post_id: int | None) -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _pending_path(date_str).write_text(json.dumps(
        {"metadata": metadata, "content_html": content_html, "post_id": post_id}, indent=2
    ))


def _clear_pending(date_str: str) -> None:
    path = _pending_path(date_str)
    if path.exists():
        path.unlink()


def _publish_with_retry(metadata: dict, content_html: str, status: str, post_id: int | None, retries: int = 3) -> dict:
    """Retries transient failures (network blips, WP hiccups) using the SAME content
    and, once known, the SAME post_id — so a retry updates rather than duplicates."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return publish_to_wordpress(metadata, content_html, status=status, post_id=post_id)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                wait = 10 * (2**attempt)
                print(f"  Publish attempt {attempt + 1} failed ({exc}); retrying in {wait}s...")
                time.sleep(wait)
    raise last_exc


def run_daily_briefing(publish_status: str = "draft") -> None:
    """The daily job: collect data, generate the edition, publish it. Safe to rerun —
    skips if today's edition already exists, and reuses already-generated content
    (instead of re-paying for Claude) if a previous attempt got that far but failed
    to publish."""
    today = datetime.now(timezone.utc).date().isoformat()
    print(f"=== The Football Report — daily run for {today} ===")

    already = _already_ran_today(today)
    if already:
        print(f"Already ran today ({already['status']}, {already.get('link')}) — skipping to avoid a duplicate post.")
        return

    metadata: dict | None = None
    content_html: str | None = None
    post_id: int | None = None

    try:
        pending = _load_pending(today)
        if pending:
            print("Found unpublished content from an earlier attempt today — reusing it instead of regenerating.")
            metadata, content_html, post_id = pending["metadata"], pending["content_html"], pending.get("post_id")
        else:
            print("Collecting football data...")
            football = collect_football_data(today)
            print(f"  {len(football['events'])} events, {len(football['standings'])} leagues with standings")

            print("Collecting odds data...")
            snapshot = collect_odds_snapshot(within_days=3)
            history = load_odds_history()
            movements = update_odds_history(history, snapshot, today)
            save_odds_history(history)
            print(f"  {len(snapshot)} odds snapshots, {len(movements)} movement candidates")

            print("Generating edition...")
            result = generate_edition(today, football, movements)
            usage = result["usage"]
            print(
                f"  {result['searches_used']} searches used, "
                f"{usage.input_tokens} input tokens, {usage.output_tokens} output tokens"
            )
            metadata, content_html = result["metadata"], result["content_html"]
            _save_pending(today, metadata, content_html, post_id=None)  # preserve before publish is attempted

        print(f"Publishing to WordPress (status={publish_status})...")
        post = _publish_with_retry(metadata, content_html, publish_status, post_id)
        print(f"  {post['link']} (id={post['id']}, status={post['status']})")

        _log_run(post["status"], today, post_id=post["id"], link=post["link"])
        _clear_pending(today)
        print("Done.")

    except Exception as exc:
        print(f"FAILED: {exc}")
        if metadata is not None and content_html is not None:
            # Preserve content (and the post_id, if publish had already created one on
            # an earlier attempt) so the next run retries the publish, not the generation.
            _save_pending(today, metadata, content_html, post_id)
        _log_run("failed", today, error=str(exc))
        raise


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run_daily_briefing(publish_status=os.environ.get("PUBLISH_STATUS", "draft"))
        return

    checks = [
        ("Anthropic", check_anthropic),
        ("API-Football", check_api_football),
        ("The Odds API", check_odds_api),
        ("WordPress", check_wordpress),
    ]

    failures = []
    print("Checking connections...\n")
    for name, check in checks:
        try:
            check()
        except Exception as exc:
            print(f"  {name}: FAILED — {exc}")
            failures.append(name)

    print()
    if failures:
        print(f"{len(failures)}/{len(checks)} connection(s) failed: {', '.join(failures)}")
        sys.exit(1)

    print("All connections OK.")


if __name__ == "__main__":
    main()
