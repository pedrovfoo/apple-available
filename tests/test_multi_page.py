"""Watching several model families (18 Pro page + iPhone Duo page) at once."""
import json
from pathlib import Path

import pytest

from watcher import apple, notify
from watcher import __main__ as main_mod
from watcher.config import WatchTarget
from watcher.state import State

FIXTURES = Path(__file__).parent / "fixtures"


def pro_page() -> str:
    return "<script>var x = " + (FIXTURES / "buypage_product_array.json").read_text() + ";</script>"


def duo_page() -> str:
    items = []
    for i, (cap, price) in enumerate([("256GB", 1999), ("512GB", 2199), ("1TB", 2599), ("2TB", 3199)]):
        for j, color in enumerate(["Star White", "Night Sky"]):
            part = f"MD{i}{j}AA"
            items.append({"sku": part, "partNumber": f"{part}LL/A",
                          "price": {"fullPrice": float(price)}, "category": "iphone",
                          "name": f"iPhone Duo {cap} {color}"})
    return "<script>var y = " + json.dumps(items, separators=(",", ":"), ensure_ascii=False) + ";</script>"


class Cfg:
    canary_part = "MJQ34LL/A"
    discovery_ttl_hours = 24

    def __init__(self, watch):
        self.watch = watch


TARGETS = [
    WatchTarget("iPhone 18 Pro Max", "256GB", ["*"]),
    WatchTarget("iPhone 18 Pro Max", "1TB", ["Burgundy"]),
    WatchTarget("iPhone Duo", "256GB", ["*"]),
    WatchTarget("iPhone Duo", "512GB", ["*"]),
    WatchTarget("iPhone Duo", "1TB", ["*"]),
    WatchTarget("iPhone Duo", "2TB", ["*"]),
]


def test_all_targets_found_across_both_pages(monkeypatch):
    monkeypatch.setattr(apple, "fetch_buy_page", lambda s=None: pro_page() + duo_page())
    st = State.empty()
    chosen = main_mod.refresh_products(Cfg(TARGETS), st, None)
    names = sorted(p.name for p in chosen.values())
    assert len([n for n in names if n.startswith("iPhone Duo ")]) == 8
    assert len([n for n in names if n.startswith("iPhone 18 Pro Max 256GB ")]) == 4
    assert [n for n in names if n.startswith("iPhone 18 Pro Max 1TB ")] == ["iPhone 18 Pro Max 1TB Burgundy"]
    assert "MJQ34LL/A" in chosen          # canary still watched
    assert len(chosen) == 8 + 4 + 1 + 1
    assert st["missing_targets"] == []


def test_missing_duo_page_keeps_watching_pro_max(monkeypatch):
    monkeypatch.setattr(apple, "fetch_buy_page", lambda s=None: pro_page())
    st = State.empty()
    chosen = main_mod.refresh_products(Cfg(TARGETS), st, None)
    assert len(chosen) == 4 + 1 + 1
    assert len(st["missing_targets"]) == 4
    assert all("iPhone Duo" in t for t in st["missing_targets"])


def test_incomplete_discovery_is_retried_sooner(monkeypatch):
    calls = []

    def fake(s=None):
        calls.append(1)
        return pro_page()

    monkeypatch.setattr(apple, "fetch_buy_page", fake)
    st = State.empty()
    main_mod.refresh_products(Cfg(TARGETS), st, None)
    main_mod.refresh_products(Cfg(TARGETS), st, None)      # cached: just ran
    assert len(calls) == 1
    from datetime import datetime, timedelta, timezone
    st["products_fetched_at"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    main_mod.refresh_products(Cfg(TARGETS), st, None)      # incomplete -> 30 min TTL
    assert len(calls) == 2


def test_nothing_matched_at_all_still_fails(monkeypatch):
    monkeypatch.setattr(apple, "fetch_buy_page", lambda s=None: pro_page())
    with pytest.raises(apple.DiscoveryError):
        main_mod.refresh_products(Cfg([WatchTarget("iPhone Duo", "256GB", ["*"])]), State.empty(), None)


def test_one_broken_buy_page_is_skipped(monkeypatch):
    def fake_get(url, params, session):
        if "duo" in url:
            raise apple.TransientError("HTTP 404")
        class R:
            text = pro_page()
        return R()

    monkeypatch.setattr(apple, "_get", fake_get)
    assert "MJQ34LL/A" in apple.fetch_buy_page(object())


def test_rate_limit_on_any_buy_page_is_not_swallowed(monkeypatch):
    def fake_get(url, params, session):
        if "duo" in url:
            raise apple.RateLimited("HTTP 541")
        class R:
            text = pro_page()
        return R()

    monkeypatch.setattr(apple, "_get", fake_get)
    with pytest.raises(apple.RateLimited):
        apple.fetch_buy_page(object())


def test_all_buy_pages_broken_raises(monkeypatch):
    def fake_get(url, params, session):
        raise apple.TransientError("down")

    monkeypatch.setattr(apple, "_get", fake_get)
    with pytest.raises(apple.TransientError):
        apple.fetch_buy_page(object())


def _res(name):
    store = apple.Store("R1", "Christiana Mall", "125 Christiana Mall", "Newark", "DE", "6.55 mi", "Available Today")
    return apple.PartAvailability("X", name, [store])


def test_duo_alert_links_duo_page():
    assert notify.format_available(_res("iPhone Duo 1TB Night Sky"), "19702").endswith(notify.DUO_BUY_URL)


def test_pro_max_alert_links_pro_page():
    assert notify.format_available(_res("iPhone 18 Pro Max 1TB Burgundy"), "19702").endswith(notify.BUY_URL)


def test_heartbeat_lists_missing_targets():
    from datetime import datetime
    text = notify.format_heartbeat([], "19702", datetime.now(), missing=["iPhone Duo 2TB (all colors)"])
    assert "iPhone Duo 2TB (all colors) — not found" in text
