"""
SBA 2.0 CLI entry point.

Commands:
  bot      — Start Telegram bot (long polling)
  inbox    — Run inbox processor once
  legacy   — Run legacy processor once
  digest   — Run digest agent once
  check    — Check all integrations
  status   — Show DB stats
  auth     — Authentication commands (auth google)
  service  — Manage launchd daemons (install / uninstall / logs)
  backup   — Backup the database
"""

import asyncio
import logging
import sys
from pathlib import Path

import click
import yaml


def _load_config() -> dict:
    config_path = Path.home() / ".sba" / "config.yaml"
    if not config_path.exists():
        click.echo(f"❌ Config not found: {config_path}", err=True)
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    # Run DB migrations on every startup
    from sba.db import init_db_sync, get_db_path
    init_db_sync(get_db_path(config))
    return config


def _setup_logging(config: dict) -> None:
    log_dir = Path(config.get("paths", {}).get("logs", "~/.sba/logs/")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sba.log"

    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))


@click.group()
def cli() -> None:
    """Second Brain Agent 2.0"""


# ── bot ──────────────────────────────────────────────────────────────────────

@cli.command()
def bot() -> None:
    """Start Telegram bot (long polling)."""
    config = _load_config()
    _setup_logging(config)
    from sba.bot.bot import run_bot
    asyncio.run(run_bot(config))


# ── inbox ─────────────────────────────────────────────────────────────────────

@cli.command()
def inbox() -> None:
    """Run inbox processor once."""
    config = _load_config()
    _setup_logging(config)
    from sba import inbox_processor
    asyncio.run(inbox_processor.run(config))


# ── legacy ────────────────────────────────────────────────────────────────────

@cli.command()
def legacy() -> None:
    """Run legacy processor once."""
    config = _load_config()
    _setup_logging(config)
    from sba import legacy_processor
    asyncio.run(legacy_processor.run(config))


# ── digest ────────────────────────────────────────────────────────────────────

@cli.command()
def digest() -> None:
    """Run digest agent once (morning briefing)."""
    config = _load_config()
    _setup_logging(config)
    from sba.notifier import Notifier
    from sba import digest_agent
    notifier = Notifier(config)
    asyncio.run(digest_agent.run_digest(notifier=notifier, config=config))


# ── check ─────────────────────────────────────────────────────────────────────

@cli.command()
def check() -> None:
    """Check all integrations."""
    config = _load_config()
    _setup_logging(config)
    from sba.integrations.checker import check_all, print_report
    results = asyncio.run(check_all(config))
    print_report(results)


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
def status() -> None:
    """Show database statistics."""
    config = _load_config()

    async def _show() -> None:
        from sba.db import Database, get_db_path
        async with Database(get_db_path(config)) as db:
            stats = await db.get_stats()
        files = stats.get("files", {})
        total = sum(files.values())
        processed = files.get("processed", 0)
        pending_files = files.get("pending", 0)
        pending_del = stats.get("pending_deletions", 0)
        click.echo(
            f"📊 SBA 2.0 Status\n"
            f"  Total items   : {total}\n"
            f"  Processed     : {processed}\n"
            f"  Pending files : {pending_files}\n"
            f"  Pending delete: {pending_del}"
        )

    asyncio.run(_show())


# ── backup ────────────────────────────────────────────────────────────────────

@cli.command()
def backup() -> None:
    """Backup the SBA database."""
    import shutil
    from datetime import datetime
    config = _load_config()
    from sba.db import get_db_path
    db_path = get_db_path(config)
    backup_dir = Path.home() / ".sba" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"sba_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(db_path, dst)
    # Keep last 7
    backups = sorted(backup_dir.glob("sba_*.db"))
    for old in backups[:-7]:
        old.unlink()
    click.echo(f"✅ Backup saved: {dst}")


# ── auth ──────────────────────────────────────────────────────────────────────

@cli.group()
def auth() -> None:
    """Authentication commands."""


@auth.command("google")
def auth_google() -> None:
    """Re-authorize Google OAuth2 (Drive + Tasks). Opens browser."""
    config = _load_config()
    token_file = Path(
        config.get("google_drive", {}).get("token_file", "~/.sba/google_token.json")
    ).expanduser()
    if token_file.exists():
        token_file.unlink()
        click.echo(f"Removed old token: {token_file}")
    click.echo("Opening browser for Google authorization...")
    try:
        from sba.integrations.google_tasks import build_service
        build_service(config)
        click.echo("✅ Google authorized successfully (Drive + Tasks)")
    except Exception as e:
        click.echo(f"❌ Authorization failed: {e}", err=True)
        sys.exit(1)


# ── service ───────────────────────────────────────────────────────────────────

@cli.group()
def service() -> None:
    """Manage launchd daemons."""


@service.command("install")
@click.argument("daemon", type=click.Choice(["bot", "inbox", "legacy", "digest", "all"]))
def service_install(daemon: str) -> None:
    """Install launchd plist(s)."""
    config = _load_config()
    from sba.service_manager import install_daemon, DAEMONS
    targets = list(DAEMONS.keys()) if daemon == "all" else [daemon]
    for name in targets:
        install_daemon(name, config)
        click.echo(f"✅ {name} installed and loaded")


@service.command("uninstall")
@click.argument("daemon", type=click.Choice(["bot", "inbox", "legacy", "digest", "all"]))
def service_uninstall(daemon: str) -> None:
    """Uninstall launchd plist(s)."""
    from sba.service_manager import uninstall_daemon, DAEMONS
    targets = list(DAEMONS.keys()) if daemon == "all" else [daemon]
    for name in targets:
        uninstall_daemon(name)
        click.echo(f"✅ {name} uninstalled")


@service.command("status")
def service_status() -> None:
    """Show status of all daemons."""
    from sba.service_manager import daemon_status, DAEMONS
    for name in DAEMONS:
        s = daemon_status(name)
        click.echo(f"  {name:8s}  {s}")


@service.command("logs")
@click.argument("daemon", type=click.Choice(["bot", "inbox", "legacy", "digest"]))
@click.option("-n", default=40, help="Last N lines")
def service_logs(daemon: str, n: int) -> None:
    """Tail daemon log."""
    from sba.service_manager import get_log_path
    log = get_log_path(daemon)
    if not Path(log).exists():
        click.echo("(no log file)")
        return
    lines = Path(log).read_text().splitlines()
    click.echo("\n".join(lines[-n:]))
