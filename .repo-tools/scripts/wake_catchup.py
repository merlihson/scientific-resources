#!/usr/bin/env python3
"""
Wake Catch-Up Script — runs on login/wake via launchd (RunAtLoad).

Checks if today's review pipeline steps completed. For any that didn't,
calls the appropriate script. The scripts' own 5-layer dedup is the safety net.

Steps checked (in dependency order):
  1. daily_review_processor — DOCX in ReviewsInbox → repo
  2. telegram_uploader — upload to Telegram channels
  3. twitter_thread_auto_poster — generate Twitter threads (needs telegram done)
  4. discord_poster — post to Discord (needs telegram done)
"""

import sys
import re
import json
import subprocess
import logging
import time
from pathlib import Path
from datetime import datetime, date, timedelta

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".repo-tools" / "scripts"
LOG_DIR = REPO_ROOT / ".repo-tools" / "logs"
DOWNLOADS_DIR = Path.home() / "ReviewsInbox"
DOCX_DIR = REPO_ROOT / "mike-paper-reviews-all" / "split-reviews-docx"

# Ledger paths
TELEGRAM_LEDGER = LOG_DIR / "telegram_upload_ledger.json"
TWITTER_LEDGER = LOG_DIR / "twitter_upload_ledger.json"
DISCORD_LEDGER = LOG_DIR / "discord_upload_ledger.json"

# Cooldown
COOLDOWN_FILE = LOG_DIR / "wake_catchup_last_run"
COOLDOWN_SECONDS = 600  # 10 minutes

# Python interpreters (must match launchd plists)
SYSTEM_PYTHON = "/usr/bin/python3"
VENV_PYTHON = str(REPO_ROOT / ".repo-tools" / ".venv" / "bin" / "python3")

# Setup logging
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_file = LOG_DIR / "wake_catchup.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def check_cooldown() -> bool:
    """Return True if we should skip (last run too recent)."""
    if not COOLDOWN_FILE.exists():
        return False
    try:
        last_run = float(COOLDOWN_FILE.read_text().strip())
        elapsed = time.time() - last_run
        if elapsed < COOLDOWN_SECONDS:
            logger.info(f"Cooldown active — last run {elapsed:.0f}s ago (limit {COOLDOWN_SECONDS}s). Skipping.")
            return True
    except (ValueError, OSError):
        pass
    return False


def update_cooldown():
    """Record current time as last run."""
    COOLDOWN_FILE.write_text(str(time.time()))


def wait_for_network(timeout: int = 60) -> bool:
    """Wait for real connectivity after wake from sleep.

    Checks that a hostname RESOLVES — pinging a raw IP (8.8.8.8) can succeed while
    DNS is still down right after wake, which caused false "available" reports.
    """
    import socket
    logger.info("Checking network connectivity...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            socket.setdefaulttimeout(3)
            socket.gethostbyname("github.com")
            logger.info("✓ Network is available")
            return True
        except Exception:
            pass
        logger.info("Waiting for network...")
        time.sleep(5)
    logger.warning(f"⚠️  No network (DNS) after {timeout}s, proceeding anyway")
    return False


def _auto_resolve_conflicts():
    """Auto-resolve merge conflicts in auto-generated files (readmes, metadata).

    After a failed rebase, does a merge pull, resolves conflicts by accepting
    remote version then re-running update_metadata.py, and completes the merge.
    Returns True on success.
    """
    logger.info("Auto-resolving merge conflicts...")
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "pull", "--no-rebase", "--autostash"],
        capture_output=True, text=True, timeout=60
    )
    status = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True
    )
    unmerged = [f.strip() for f in status.stdout.strip().split('\n') if f.strip()]
    if not unmerged:
        logger.info("No unmerged files found")
        return True

    logger.info(f"Resolving {len(unmerged)} conflicted file(s): {unmerged}")
    for f in unmerged:
        subprocess.run(["git", "-C", str(REPO_ROOT), "checkout", "--theirs", "--", f],
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(REPO_ROOT), "add", f],
                       capture_output=True, text=True)

    # Re-run update_metadata.py to regenerate correct stats
    try:
        subprocess.run(
            ["python3", str(REPO_ROOT / ".repo-tools" / "scripts" / "update_metadata.py")],
            capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT)
        )
        # Stage any regenerated files
        for f in unmerged:
            subprocess.run(["git", "-C", str(REPO_ROOT), "add", f],
                           capture_output=True, text=True)
    except Exception as e:
        logger.warning(f"update_metadata.py error during conflict resolution: {e}")

    commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "commit", "--no-edit"],
        capture_output=True, text=True, timeout=30
    )
    if commit.returncode == 0:
        logger.info("✓ Merge conflicts auto-resolved")
        return True
    logger.error(f"✗ Merge commit failed: {commit.stderr.strip()}")
    return False


def git_pull():
    """Pull latest to sync ledgers from other machines."""
    logger.info("Pulling latest from remote...")
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "pull", "--rebase", "--autostash"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            logger.info(f"Git pull OK: {result.stdout.strip()}")
        else:
            combined = result.stdout + result.stderr
            if 'CONFLICT' in combined:
                logger.warning("Rebase conflict during pull, auto-resolving...")
                subprocess.run(["git", "-C", str(REPO_ROOT), "rebase", "--abort"],
                               capture_output=True, text=True)
                _auto_resolve_conflicts()
            else:
                logger.warning(f"Git pull issue (rc={result.returncode}): {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        logger.warning("Git pull timed out after 60s")
    except Exception as e:
        logger.error(f"Git pull failed: {e}")


def push_unpushed_commits():
    """Push any locally committed but unpushed changes from a previous failed run."""
    try:
        ahead = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-list", "--count", "@{u}..HEAD"],
            capture_output=True, text=True, timeout=10
        )
        if ahead.returncode != 0 or int(ahead.stdout.strip()) == 0:
            return
        n = ahead.stdout.strip()
        logger.info(f"Found {n} unpushed commit(s) from previous run. Pushing...")
        for attempt in range(1, 4):
            pull_r = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "pull", "--rebase", "--autostash"],
                capture_output=True, text=True, timeout=60
            )
            if pull_r.returncode != 0 and 'CONFLICT' in (pull_r.stdout + pull_r.stderr):
                subprocess.run(["git", "-C", str(REPO_ROOT), "rebase", "--abort"],
                               capture_output=True, text=True)
                _auto_resolve_conflicts()
            push = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "push"],
                capture_output=True, text=True, timeout=60
            )
            if push.returncode == 0:
                logger.info("✓ Pushed previously unpushed commits")
                return
            logger.warning(f"Push retry attempt {attempt}/3 failed: {push.stderr.strip()}")
            if attempt < 3:
                time.sleep(attempt * 5)
        logger.error("✗ Failed to push unpushed commits after 3 attempts")
    except Exception as e:
        logger.warning(f"Unpushed commit check failed: {e}")


def find_inbox_review_numbers() -> set:
    """Find all Review_NNN.docx numbers in ReviewsInbox."""
    if not DOWNLOADS_DIR.exists():
        logger.warning(f"ReviewsInbox not found: {DOWNLOADS_DIR}")
        return set()
    numbers = set()
    for f in DOWNLOADS_DIR.glob("Review_*.docx"):
        # Skip English files
        if "_english" in f.name.lower() or "_English" in f.name:
            continue
        match = re.search(r'Review_(\d+)\.docx', f.name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def find_repo_review_numbers() -> set:
    """Find all Review_NNN.docx numbers already in repo."""
    if not DOCX_DIR.exists():
        return set()
    numbers = set()
    for f in DOCX_DIR.glob("Review_*.docx"):
        match = re.search(r'Review_(\d+)\.docx', f.name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def load_ledger(path: Path) -> dict:
    """Load a JSON ledger file."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read ledger {path.name}: {e}")
        return {}


def is_in_telegram_ledger(review_num: int) -> bool:
    """Check if review is in telegram ledger (both Hebrew and English)."""
    ledger = load_ledger(TELEGRAM_LEDGER)
    hebrew = set(ledger.get("hebrew", []))
    english = set(ledger.get("english", []))
    return review_num in hebrew and review_num in english


def is_in_ledger(review_num: int, ledger_path: Path) -> bool:
    """Check if review is in a simple {posted: [...]} ledger."""
    ledger = load_ledger(ledger_path)
    return review_num in set(ledger.get("posted", []))


def run_script(python: str, script: Path, label: str) -> bool:
    """Run a script via subprocess. Returns True on success."""
    logger.info(f"Running {label}...")
    try:
        result = subprocess.run(
            [python, str(script)],
            capture_output=True, text=True, timeout=300,
            cwd=str(REPO_ROOT)
        )
        if result.returncode == 0:
            logger.info(f"{label} completed successfully (rc=0)")
            return True
        else:
            logger.warning(f"{label} exited with rc={result.returncode}")
            if result.stderr:
                logger.warning(f"{label} stderr: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        logger.error(f"{label} timed out after 300s")
        return False
    except Exception as e:
        logger.error(f"{label} failed: {e}")
        return False


def _missed_scheduled_run(last_run_file: Path, scheduled_weekdays) -> bool:
    """True if the agent hasn't run since its most recent scheduled slot.

    Finds the most recent date on-or-before today whose weekday is scheduled,
    and returns True if last_run.txt is missing or predates it. This recovers
    runs missed while the machine was asleep/off on the scheduled day — the
    old check only fired when today itself was a scheduled day, so a Thursday
    miss noticed on Friday (or any later non-scheduled day) was never caught up.
    """
    today = date.today()
    most_recent_slot = None
    for delta in range(7):  # walk back to the last scheduled weekday
        d = today - timedelta(days=delta)
        if d.weekday() in scheduled_weekdays:
            most_recent_slot = d
            break
    if most_recent_slot is None:
        return False  # no scheduled weekday configured (shouldn't happen)
    try:
        last_date = date.fromisoformat(last_run_file.read_text().strip())
    except Exception:
        return True  # never ran / unreadable → catch up
    return last_date < most_recent_slot


def run_module(module: str, label: str, extra_args=None, timeout: int = 600) -> bool:
    """Run an agent as `python -m module` from the scripts dir (packages live there)."""
    logger.info(f"Running {label}...")
    try:
        result = subprocess.run(
            [VENV_PYTHON, "-m", module, *(extra_args or [])],
            capture_output=True, text=True, timeout=timeout, cwd=str(SCRIPTS_DIR),
        )
        if result.returncode == 0:
            logger.info(f"{label} completed successfully (rc=0)")
            return True
        logger.warning(f"{label} exited rc={result.returncode}: {(result.stderr or '')[:400]}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"{label} timed out after {timeout}s")
        return False
    except Exception as e:
        logger.error(f"{label} failed: {e}")
        return False


def catch_up_agents():
    """Catch up news_scout / paper_recommender if launchd missed them (asleep or
    offline at their scheduled slots). Each script self-skips if it already ran
    today; here we run one whenever it hasn't run since its most recent scheduled
    slot — so a run missed on a scheduled day is recovered on the next login,
    even if that login lands on a non-scheduled day.
    """
    ns_last = SCRIPTS_DIR / "news_scout" / "last_run.txt"
    pr_last = SCRIPTS_DIR / "paper_recommender" / "last_run.txt"

    # paper_recommender: Mon-Fri
    if _missed_scheduled_run(pr_last, {0, 1, 2, 3, 4}):
        logger.info("Catch-up: paper_recommender missed its last scheduled run — running.")
        run_module("paper_recommender.recommender", "paper_recommender", timeout=600)
    else:
        logger.info("Catch-up: paper_recommender is up to date.")

    # news_scout: NO auto catch-up, by explicit instruction (2026-08-11).
    # A news_scout run costs ~$4-5, and auto-recovery of missed runs is what
    # turned a broken git sync into repeated full runs. A missed slot now waits
    # for the next scheduled one; re-running is a human decision:
    #   cd .repo-tools/scripts && python3 -m news_scout.news_scout --force
    if not ns_last.exists() or _missed_scheduled_run(ns_last, {0, 3}):
        logger.info("news_scout looks overdue — NOT auto-running (cost guard); "
                    "run it manually with --force if you want a digest now.")


def main():
    logger.info("=" * 60)
    logger.info("Wake catch-up script starting")
    logger.info(f"Date: {date.today()}")

    # Cooldown check
    if check_cooldown():
        return

    update_cooldown()

    # Wait for network (important after wake from sleep)
    wait_for_network(timeout=60)

    # Sync ledgers
    git_pull()

    # Push any commits that failed to push in a previous run
    push_unpushed_commits()

    # Catch up the standalone agents (news_scout / paper_recommender) that launchd
    # may have missed while asleep/offline. Independent of the review inbox, so it
    # must run before the "no reviews -> return" early exit below.
    catch_up_agents()

    # Find what's in the inbox vs what's already processed
    inbox_nums = find_inbox_review_numbers()
    repo_nums = find_repo_review_numbers()

    if not inbox_nums:
        logger.info("No reviews found in ReviewsInbox. Nothing to do.")
        return

    # Reviews in inbox but not yet in repo = need processing
    unprocessed = inbox_nums - repo_nums
    # Reviews already in repo = check publishing steps
    processed = inbox_nums & repo_nums

    # Only care about recent reviews (highest numbers likely today's)
    # We check all unprocessed + the latest few processed ones
    all_to_check = unprocessed | {max(processed)} if processed else unprocessed
    if not all_to_check:
        logger.info("No reviews to check. Nothing to do.")
        return

    logger.info(f"Inbox reviews: {sorted(inbox_nums)}")
    logger.info(f"Unprocessed (not in repo): {sorted(unprocessed)}")
    logger.info(f"Checking publishing for: {sorted(all_to_check)}")

    ran_something = False

    # Step 1: Daily review processor (if any DOCX not yet in repo)
    if unprocessed:
        logger.info(f"Step 1: {len(unprocessed)} review(s) need processing: {sorted(unprocessed)}")
        run_script(SYSTEM_PYTHON, SCRIPTS_DIR / "daily_review_processor.py", "daily_review_processor")
        ran_something = True
        # Re-pull after processor commits
        git_pull()
    else:
        logger.info("Step 1: All inbox reviews already processed. Skipping processor.")

    # Step 2: Telegram uploader
    # Check the latest review number (most likely today's)
    latest_review = max(all_to_check)
    if not is_in_telegram_ledger(latest_review):
        logger.info(f"Step 2: Review {latest_review} not in telegram ledger. Running uploader.")
        run_script(SYSTEM_PYTHON, SCRIPTS_DIR / "telegram_uploader.py", "telegram_uploader")
        ran_something = True
        # Re-pull after uploader commits ledger
        git_pull()
    else:
        logger.info(f"Step 2: Review {latest_review} already in telegram ledger. Skipping.")

    # Re-check telegram status after potential upload
    telegram_done = is_in_telegram_ledger(latest_review)

    # Step 3 & 4: Twitter + Discord (both depend on telegram being done)
    if not telegram_done:
        logger.info("Steps 3-4: Telegram not done yet — skipping twitter and discord.")
    else:
        # Twitter
        if not is_in_ledger(latest_review, TWITTER_LEDGER):
            logger.info(f"Step 3: Review {latest_review} not in twitter ledger. Running poster.")
            run_script(VENV_PYTHON, SCRIPTS_DIR / "twitter_thread_auto_poster.py", "twitter_thread_auto_poster")
            ran_something = True
        else:
            logger.info(f"Step 3: Review {latest_review} already in twitter ledger. Skipping.")

        # Discord
        if not is_in_ledger(latest_review, DISCORD_LEDGER):
            logger.info(f"Step 4: Review {latest_review} not in discord ledger. Running poster.")
            run_script(VENV_PYTHON, SCRIPTS_DIR / "discord_poster.py", "discord_poster")
            ran_something = True
        else:
            logger.info(f"Step 4: Review {latest_review} already in discord ledger. Skipping.")

    if ran_something:
        logger.info("Wake catch-up completed — ran one or more scripts.")
    else:
        logger.info("Wake catch-up completed — everything already done, nothing to run.")

    logger.info("=" * 60)


if __name__ == "__main__":
    main()
