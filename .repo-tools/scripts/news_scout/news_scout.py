#!/usr/bin/env python3
"""News Scout — daily AI news digest entry point.

Pipeline:
  1. Fetch recent items from 10 English-language feeds (last ~36h).
  2. Pre-filter for AI relevance.
  3. Top-trim to a manageable set, then check Israeli Hebrew coverage via web_search.
  4. Rank remaining stories on general-public interestingness for an Israeli audience.
  5. Generate Hebrew teasers + Israeli-angle line per top item.
  6. Post to the Telegram channel review_testing_heb.

Multi-machine safety mirrors the existing publishers in this repo:
  - Machine-staggered delay (machine_id in config.yaml).
  - last_run.txt (git-tracked) — one digest per calendar day, max.
  - news_scout_ledger.json (git-tracked) — never repost the same URL.

CLI:
  python3 -m news_scout.news_scout              # normal scheduled run
  python3 -m news_scout.news_scout --dry-run    # print message, don't send
  python3 -m news_scout.news_scout --force      # ignore last_run / ledger
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Set

import yaml

from .coverage_checker import filter_uncovered
from .fetcher import NewsItem, fetch_recent
from .formatter import build_telegram_message, format_items
from .ranker import RankedItem, rank
from .telegram_sender import send as telegram_send

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
LAST_RUN_FILE = SCRIPT_DIR / "last_run.txt"
LEDGER_FILE = REPO_ROOT / ".repo-tools" / "logs" / "news_scout_ledger.json"
# Git-tracked log of every run start, one ISO date per line. Feeds the weekly
# cap below, so the ceiling holds across ALL machines and trigger paths.
RUN_LOG_FILE = SCRIPT_DIR / "run_log.txt"
MAX_RUNS_PER_WEEK = 6


# ---------- config ----------

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"Error: config file not found at {CONFIG_FILE}")
        print("Copy config.yaml.template to config.yaml and fill in your credentials.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def resolve_api_key(config: dict) -> str:
    key = config.get("anthropic_api_key", "")
    if not key or key == "YOUR_ANTHROPIC_API_KEY_HERE":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("Error: no Anthropic API key in config.yaml or ANTHROPIC_API_KEY env var.")
        sys.exit(1)
    return key


# ---------- network ----------

def wait_for_network(max_wait: int = 300) -> bool:
    """Wait for DNS to resolve the key hosts the script depends on.

    After laptop sleep/wake the network interface can take several minutes
    to fully come up. The previous 2 min budget was too short and the
    single-host probe sometimes returned True while RSS feeds were still
    DNS-failing. We now poll for up to 5 min and require BOTH the Anthropic
    API (coverage + ranker + formatter) AND a representative RSS host
    (Google News fronts most feeds) to resolve.
    """
    # Lazy import: _git_utils lives in the parent scripts/ directory and is
    # added to sys.path by Python's -m invocation, but importing at module
    # load would break callers that pick news_scout up from elsewhere.
    from _git_utils import wake_network
    start = time.time()
    while time.time() - start < max_wait:
        if wake_network("api.anthropic.com", 443) and wake_network("news.google.com", 443):
            return True
        time.sleep(15)
    return False


# ---------- run-once-per-day guard ----------

def already_ran_today() -> bool:
    if not LAST_RUN_FILE.exists():
        return False
    return LAST_RUN_FILE.read_text().strip() == datetime.now().strftime("%Y-%m-%d")


def mark_ran_today() -> None:
    LAST_RUN_FILE.write_text(datetime.now().strftime("%Y-%m-%d"))


def _recent_run_dates(days: int = 7) -> list[str]:
    """Run-start dates logged within the trailing `days` window."""
    if not RUN_LOG_FILE.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for line in RUN_LOG_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if datetime.strptime(line[:10], "%Y-%m-%d") >= cutoff:
                out.append(line)
        except ValueError:
            continue
    return out


def weekly_cap_reached() -> bool:
    """HARD ceiling: never more than MAX_RUNS_PER_WEEK runs in a trailing 7 days.

    Enforced in code rather than only in the launchd schedule, because the
    July/August overspend happened when the schedule-level guards failed and
    retry slots fired repeatedly. Applies to --force too; only the explicit
    --override-cap flag (a human typing it) bypasses this."""
    recent = _recent_run_dates(7)
    if len(recent) >= MAX_RUNS_PER_WEEK:
        print(f"Weekly cap reached: {len(recent)} runs in the last 7 days "
              f"(max {MAX_RUNS_PER_WEEK}). Not running. "
              f"Use --override-cap to bypass deliberately.")
        return True
    return False


def log_run_start() -> None:
    """Record this run BEFORE doing any paid work, so a crash mid-run still
    counts against the cap."""
    with open(RUN_LOG_FILE, "a") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M") + "\n")


def check_remote_last_run() -> bool:
    """Did another machine already mark today as done?"""
    today = datetime.now().strftime("%Y-%m-%d")
    rel = LAST_RUN_FILE.relative_to(REPO_ROOT)
    try:
        subprocess.run(
            ["git", "fetch", "--quiet"],
            cwd=REPO_ROOT, capture_output=True, timeout=60,
        )
        result = subprocess.run(
            ["git", "show", f"origin/main:{rel}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() == today
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[run-guard] git fetch/show failed ({exc}); proceeding on local state")
        return False


# ---------- ledger ----------

def load_ledger() -> Set[str]:
    if not LEDGER_FILE.exists():
        return set()
    try:
        data = json.loads(LEDGER_FILE.read_text())
        return set(data.get("posted_item_ids", []))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ledger] load failed ({exc}); starting empty")
        return set()


def save_ledger(ids: Set[str]) -> None:
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(
        {"posted_item_ids": sorted(ids), "updated_at": datetime.now().isoformat()},
        indent=2,
    ))


def git_pull_rebase() -> None:
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "--quiet"],
            cwd=REPO_ROOT, capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[git] pull --rebase failed ({exc}); continuing")


def git_commit_and_push(paths: list, message: str) -> None:
    try:
        for p in paths:
            subprocess.run(["git", "add", str(p)], cwd=REPO_ROOT, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=REPO_ROOT, capture_output=True, timeout=15,
        )
        subprocess.run(["git", "pull", "--rebase", "--autostash", "--quiet"],
                       cwd=REPO_ROOT, capture_output=True, timeout=60)
        subprocess.run(["git", "push", "--quiet"], cwd=REPO_ROOT, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[git] commit/push failed ({exc}); another machine may double-post")


# ---------- machine delay ----------

def machine_delay_slot(machine_id: int) -> int:
    """Deterministic stagger so two machines don't race on the same minute."""
    base = max(0, machine_id - 1) * 120
    jitter = random.randint(0, 20)
    return base + jitter


# ---------- main ----------

def main() -> None:
    parser = argparse.ArgumentParser(description="News Scout — daily AI news digest")
    parser.add_argument("--dry-run", action="store_true", help="Print message, don't send")
    parser.add_argument("--force", action="store_true", help="Ignore last_run and ledger")
    parser.add_argument("--skip-delay", action="store_true", help="Skip machine_id delay slot")
    parser.add_argument("--lookback", type=int, default=None, help="Hours to look back (overrides config)")
    parser.add_argument("--override-cap", action="store_true",
                        help="Bypass the weekly run cap (deliberate, human-typed only)")
    args = parser.parse_args()

    config = load_config()

    # HARD weekly ceiling — checked before anything else and before any paid
    # work. Applies even to --force; only --override-cap bypasses it.
    if not args.dry_run and not args.override_cap and weekly_cap_reached():
        return

    weekday = datetime.now().weekday()  # 0=Mon ... 6=Sun

    if not args.force and not args.dry_run and already_ran_today():
        print("Already ran today. Use --force to re-run.")
        return

    if not args.dry_run:
        print("Checking network...")
        if not wait_for_network():
            print("No network after 2 min. Will retry on next scheduled slot.")
            sys.exit(1)

    # Machine stagger
    if not args.skip_delay and not args.dry_run:
        delay = machine_delay_slot(config.get("machine_id", 1))
        if delay > 0:
            print(f"[stagger] sleeping {delay}s (machine_id={config.get('machine_id', 1)})...")
            time.sleep(delay)

    # After the stagger, refresh remote state and re-check
    if not args.force and not args.dry_run:
        git_pull_rebase()
        if check_remote_last_run() or already_ran_today():
            print("Today's digest already posted by another machine. Exiting.")
            mark_ran_today()
            return

    # Count this run against the weekly cap before spending anything.
    if not args.dry_run:
        log_run_start()

    api_key = resolve_api_key(config)
    # Cadence is every ~5 days, so one lookback window covers the whole gap.
    lookback = args.lookback or int(config.get("lookback_hours", 144))
    top_n = int(config.get("top_n", 7))
    ranker_model = config.get("ranker_model", "claude-haiku-4-5-20251001")
    coverage_model = config.get("coverage_model", "claude-sonnet-4-6")
    formatter_model = config.get("formatter_model", "claude-sonnet-4-6")
    hard_exclusions = config.get("hard_exclusions", []) or []

    # Step 1: fetch
    print(f"\n[1/5] Fetching English AI news (lookback {lookback}h)...")
    items = fetch_recent(lookback_hours=lookback)
    if not items:
        print("No candidates fetched. Exiting.")
        return

    # Step 2: drop already-posted (ledger)
    ledger = load_ledger()
    if not args.force:
        before = len(items)
        items = [i for i in items if i.item_id not in ledger]
        print(f"[2/5] Ledger removed {before - len(items)} previously posted items.")

    # Trim before coverage check (it's the most expensive step). Keep top 25 newest.
    items = items[:25]

    # Step 3: coverage check
    print(f"[3/5] Checking Hebrew Israeli coverage on {len(items)} items...")
    uncovered = filter_uncovered(items, api_key=api_key, model=coverage_model, max_check=len(items))
    if not uncovered:
        print("All items already covered in Hebrew. Nothing to post.")
        return

    # Step 4: rank
    print(f"[4/5] Ranking {len(uncovered)} items for general-public appeal...")
    ranked: list[RankedItem] = rank(
        uncovered,
        api_key=api_key,
        model=ranker_model,
        top_n=top_n,
        hard_exclusions=hard_exclusions,
    )
    if not ranked:
        print("Ranker dropped everything below threshold. Nothing to post.")
        return
    print(f"[4/5] Selected top {len(ranked)} items.")

    # Step 5: format + send
    print(f"[5/5] Formatting Hebrew teasers...")
    formatted = format_items(ranked, api_key=api_key, model=formatter_model)
    if not formatted:
        print("Formatter produced nothing. Aborting.")
        return

    message = build_telegram_message(formatted, today=datetime.now())

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — message preview")
        print("=" * 60)
        print(message)
        print("=" * 60)
        return

    bot_token = config.get("telegram_bot_token", "")
    channel_id = config.get("telegram_channel_id", "")
    if not bot_token or not channel_id:
        print("Error: telegram_bot_token / telegram_channel_id missing in config.yaml")
        sys.exit(1)

    print("Sending to Telegram...")
    if not telegram_send(message, bot_token=bot_token, channel_id=channel_id):
        print("Failed to send. Will retry on next scheduled slot.")
        sys.exit(1)

    # Update ledger + last_run, push so other machines see it
    for r in ranked:
        ledger.add(r.item.item_id)
    save_ledger(ledger)
    mark_ran_today()
    git_commit_and_push(
        paths=[LAST_RUN_FILE.relative_to(REPO_ROOT), LEDGER_FILE.relative_to(REPO_ROOT)],
        message=f"news_scout: daily digest {datetime.now().strftime('%Y-%m-%d')} ({len(ranked)} items)",
    )
    print("Done.")


if __name__ == "__main__":
    main()
