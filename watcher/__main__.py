from __future__ import annotations
import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watcher import apple, notify, state as state_mod
from watcher.config import load_config, ConfigError

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.toml"
INCOMPLETE_TTL_MINUTES = 30
log = logging.getLogger("watcher")


def read_dotenv(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def watch_fingerprint(cfg) -> str:
    """Identifies the watch configuration, so that editing config.toml takes
    effect on the next run instead of waiting out the discovery TTL."""
    parts = [f"{w.model}|{w.capacity}|{','.join(sorted(w.colors))}" for w in cfg.watch]
    return ";".join(sorted(parts)) + f"#canary={cfg.canary_part}"


def refresh_products(cfg, st, session):
    """Re-discover parts if the cache is stale or the watch config changed,
    else reuse it."""
    fingerprint = watch_fingerprint(cfg)
    fetched = st.get("products_fetched_at")
    fresh = False
    if fetched:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched)
        # A discovery that missed some targets is retried sooner, so a model
        # that was not on sale yet gets picked up within the hour.
        ttl = (timedelta(minutes=INCOMPLETE_TTL_MINUTES) if st.get("missing_targets")
               else timedelta(hours=cfg.discovery_ttl_hours))
        fresh = age < ttl
    if st.get("watch_fingerprint") != fingerprint:
        log.info("watch configuration changed; rediscovering parts")
        fresh = False
    if fresh and st.get("products"):
        return {p: apple.Product(p, d["name"], d["price"])
                for p, d in st["products"].items()}

    log.info("refreshing product list from the buy page")
    products = apple.discover_products(apple.fetch_buy_page(session))
    watched: list[apple.Product] = []
    missing: list[str] = []
    for target in cfg.watch:
        # One target that Apple does not list (yet) must not take the others
        # down with it; it is reported in the heartbeat instead.
        try:
            found = apple.select_watched(
                products, target.model, target.capacity, target.colors
            )
        except apple.DiscoveryError as exc:
            log.warning("skipping target %s: %s", target, exc)
            missing.append(str(target))
            continue
        log.info("target %s -> %d parts", target, len(found))
        watched.extend(found)
    if not watched:
        raise apple.DiscoveryError(
            "none of the watch targets matched any product; watching nothing is a bug"
        )
    # Dedupe: overlapping targets must not queue a part twice.
    watched = list({p.part: p for p in watched}.values())
    canary = next((p for p in products if p.part == cfg.canary_part), None)
    if canary is None:
        raise apple.DiscoveryError(
            f"canary part {cfg.canary_part} not found on the buy page"
        )
    chosen = {p.part: p for p in [*watched, canary]}
    st["products"] = {p.part: {"name": p.name, "price": p.price} for p in chosen.values()}
    st["products_fetched_at"] = datetime.now(timezone.utc).isoformat()
    st["watch_fingerprint"] = fingerprint
    st["missing_targets"] = missing

    # Parts that left the watch list must not linger in state as stale entries.
    for gone in set(st.get("parts", {})) - set(chosen):
        log.info("no longer watching %s; dropping from state", gone)
        st["parts"].pop(gone, None)
    log.info("watching %d parts: %s", len(watched),
             ", ".join(sorted(p.name for p in watched)))
    return chosen


def update_heartbeat(tg, st, results, canary, cfg, alerted: bool, dry_run: bool) -> None:
    """Keep a single message in the chat showing the last check time, editing
    it in place so being alive costs no notifications."""
    text = notify.format_heartbeat(
        results, cfg.zip, datetime.now().astimezone(), canary,
        missing=st.get("missing_targets") or [],
    )
    if dry_run:
        print(text + "\n" + "-" * 50)
        return

    hb_id = st.get("heartbeat_message_id")

    # After a real alert, move the heartbeat below it so it stays the newest
    # message rather than being buried above the thing you care about.
    if alerted and hb_id:
        tg.delete(hb_id)
        hb_id = None

    if hb_id and tg.edit(hb_id, text):
        log.info("heartbeat %s updated", hb_id)
        return

    if hb_id:
        log.info("heartbeat message %s is gone; sending a new one", hb_id)
    new_id = tg.send_id(text)
    log.info("heartbeat posted as %s", new_id)
    st["heartbeat_message_id"] = new_id or None


def run_once(cfg, dry_run: bool) -> int:
    st = state_mod.State.load(STATE_PATH)
    tg = notify.Telegram(cfg.bot_token, cfg.chat_id)
    session = apple._session()

    try:
        products = refresh_products(cfg, st, session)
        payload = apple.fetch_pickup(cfg.zip, list(products), session)
        results = apple.parse_pickup(payload, products)
        try:
            apple.check_canary(results, cfg.canary_part)
        except apple.ImplausibleResponse:
            log.warning("canary failed; retrying once after backoff")
            time.sleep(5)
            payload = apple.fetch_pickup(cfg.zip, list(products), session)
            results = apple.parse_pickup(payload, products)
            apple.check_canary(results, cfg.canary_part)
    except (apple.TransientError, apple.ImplausibleResponse, apple.DiscoveryError) as exc:
        n = state_mod.record_failure(st)
        log.error("check failed (%d consecutive): %s", n, exc)
        if n >= cfg.failure_alert_threshold and not st.get("alerted_unhealthy"):
            if not dry_run:
                tg.send(notify.format_unhealthy(str(exc), n))
            st["alerted_unhealthy"] = True
        if not dry_run:
            st.save(STATE_PATH)  # failure bookkeeping only; part states untouched
        return 1

    if state_mod.record_success(st) and not dry_run:
        tg.send(notify.format_recovered())

    watched = {p: r for p, r in results.items() if p != cfg.canary_part}
    decision = state_mod.decide(st, watched, datetime.now(timezone.utc), cfg.repeat_minutes)

    for part in decision.newly_available + decision.repeats:
        msg = notify.format_available(watched[part], cfg.zip, repeat=part in decision.repeats)
        if dry_run:
            print(msg + "\n" + "-" * 50)
        else:
            tg.send(msg)
        log.info("notified: %s", watched[part].name)

    for part in decision.went_away:
        log.info("no longer available: %s", watched[part].name)

    if not decision.newly_available and not decision.repeats:
        canary = results[cfg.canary_part]
        summary = ", ".join(
            f"{r.name.replace('iPhone 18 Pro Max ', '')}={r.store_count}"
            for r in sorted(watched.values(), key=lambda x: x.name)
        )
        log.info("no change (canary %s at %d stores); %s",
                 canary.part, canary.store_count, summary)

    update_heartbeat(
        tg, st, list(watched.values()), results.get(cfg.canary_part), cfg,
        alerted=bool(decision.newly_available or decision.repeats),
        dry_run=dry_run,
    )

    if not dry_run:
        st.save(STATE_PATH)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="watcher")
    ap.add_argument("--once", action="store_true", help="single check (default)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print messages instead of sending; do not persist state")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--check-telegram", action="store_true",
                    help="verify bot token and chat access")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-jitter", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if args.status:
        st = state_mod.State.load(STATE_PATH)
        print(f"consecutive failures: {st.get('consecutive_failures', 0)}")
        print(f"products refreshed:   {st.get('products_fetched_at') or 'never'}")
        print(f"heartbeat message:    {st.get('heartbeat_message_id') or 'none'}")
        for part, d in sorted(st.get("parts", {}).items()):
            mark = "AVAILABLE" if d.get("available") else "-"
            print(f"  {part:<12} {mark:<10} {d.get('store_count', 0):>3} stores  "
                  f"{d.get('name', '')}")
        return 0

    try:
        cfg = load_config(CONFIG_PATH, read_dotenv(ROOT / ".env"))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.check_telegram:
        try:
            name = notify.Telegram(cfg.bot_token, cfg.chat_id).check()
        except RuntimeError as exc:
            print(f"telegram check failed: {exc}", file=sys.stderr)
            return 2
        print(f"telegram OK: bot @{name} can post to {cfg.chat_id}")
        return 0

    if not args.no_jitter and not args.dry_run and cfg.jitter_seconds:
        time.sleep(random.uniform(0, cfg.jitter_seconds))

    return run_once(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
