from __future__ import annotations
import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

BUY_PAGE = "https://www.apple.com/shop/buy-iphone/iphone-18-pro"
PICKUP_URL = "https://www.apple.com/shop/retail/pickup-message"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Product objects embedded in the buy page, e.g.
# {"sku":"MJW44","partNumber":"MJW44LL/A","price":{"fullPrice":1299.00},
#  "category":"iphone","name":"iPhone 18 Pro Max 256GB Black"}
_PRODUCT_RE = re.compile(
    r'\{"sku":"[A-Z0-9]+",'
    r'"partNumber":"([A-Z0-9]+LL/A)",'
    r'"price":\{"fullPrice":([0-9.]+)\},'
    r'"category":"[a-z]+",'
    r'"name":"([^"]+)"\}'
)


class DiscoveryError(Exception):
    """The buy page yielded no usable products, or none matched the watch."""


class TransientError(Exception):
    """Network failure or timeout. Retry later; do not write state."""


class RateLimited(TransientError):
    """Apple returned HTTP 541: too many requests from this client.

    Deliberately NOT retried in-process. Retrying a rate-limit within seconds
    is what keeps it tripped -- one blocked check becomes three requests in
    six seconds, which looks exactly like the abuse being throttled. Failing
    fast and waiting for the next scheduled run lets the limiter drain.
    """


class ImplausibleResponse(Exception):
    """A well-formed but entirely negative response that must not be believed."""


def normalize(s: str) -> str:
    """Apple embeds U+00A0 in product names. Matching raw names silently
    yields nothing, so every comparison goes through here first."""
    return unicodedata.normalize("NFKC", s).replace(" ", " ").strip()


@dataclass(frozen=True)
class Product:
    part: str
    name: str
    price: float


@dataclass(frozen=True)
class Store:
    store_id: str
    name: str
    street: str
    city: str
    state: str
    distance: str        # "0.63 mi", as Apple renders it
    quote: str
    distance_mi: float = 0.0   # numeric, for nearest-first ordering


@dataclass(frozen=True)
class PartAvailability:
    part: str
    name: str
    stores: list[Store]

    @property
    def available(self) -> bool:
        return bool(self.stores)

    @property
    def store_count(self) -> int:
        return len(self.stores)


def discover_products(html: str) -> list[Product]:
    seen: dict[str, Product] = {}
    for part, price, name in _PRODUCT_RE.findall(html):
        seen[part] = Product(part=part, name=normalize(name), price=float(price))
    if not seen:
        raise DiscoveryError(
            "no products found on the buy page; the page structure likely changed"
        )
    return sorted(seen.values(), key=lambda p: p.name)


def select_watched(
    products: list[Product], model: str, capacity: str, colors: list[str]
) -> list[Product]:
    model_n, capacity_n = normalize(model), normalize(capacity)
    wanted = {normalize(c).lower() for c in colors}
    take_all = "*" in wanted

    # Names are always "<model> <capacity> <color>". Matching model and
    # capacity as one prefix gives the word boundary that a bare
    # startswith(model) lacks -- without it "iPhone 18 Pro" also matches
    # "iPhone 18 Pro Max".
    prefix = f"{model_n} {capacity_n} "

    matched = []
    for p in products:
        if not p.name.startswith(prefix):
            continue
        color = p.name[len(prefix):].strip().lower()
        if take_all or color in wanted:
            matched.append(p)

    if not matched:
        raise DiscoveryError(
            f"no products matched model={model!r} capacity={capacity!r} "
            f"colors={colors!r}; watching nothing is always a bug"
        )
    return matched


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _get(url: str, params: dict | None, session: requests.Session) -> requests.Response:
    delay = 2.0
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=25)
            if r.status_code == 541:
                raise RateLimited(f"HTTP 541 (rate limited) from {url}")
            if r.status_code >= 500:
                raise TransientError(f"HTTP {r.status_code} from {url}")
            r.raise_for_status()
            return r
        except RateLimited as exc:
            # No backoff loop here: see RateLimited.
            log.warning("rate limited by Apple; backing off until the next run")
            raise
        except (requests.RequestException, TransientError) as exc:
            last = exc
            log.warning("request failed (attempt %d/3): %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
    raise TransientError(f"giving up after 3 attempts: {last}")


def fetch_buy_page(session: requests.Session | None = None) -> str:
    return _get(BUY_PAGE, None, session or _session()).text


def fetch_pickup(
    zip_code: str, parts: list[str], session: requests.Session | None = None
) -> dict:
    session = session or _session()
    params = {"pl": "true", "mts.0": "regular", "location": zip_code}
    for i, part in enumerate(parts):
        params[f"parts.{i}"] = part
    r = _get(PICKUP_URL, params, session)
    try:
        return r.json()
    except ValueError as exc:
        raise TransientError(f"response was not JSON: {exc}") from exc


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_pickup(payload: dict, products: dict[str, Product]) -> dict[str, PartAvailability]:
    stores = (payload.get("body") or {}).get("stores") or []
    found: dict[str, list[Store]] = {part: [] for part in products}

    for raw in stores:
        avail = raw.get("partsAvailability")
        if not isinstance(avail, dict):
            log.warning("store %r has no partsAvailability; skipping", raw.get("storeName"))
            continue
        addr = raw.get("address") or {}
        for part, info in avail.items():
            if part not in found or (part != "MJQ34LL/A" and "christiana" not in normalize(raw.get("storeName") or "").lower()):

                continue
            if (info or {}).get("pickupDisplay") != "available":
                continue
            found[part].append(
                Store(
                    store_id=raw.get("storeNumber") or raw.get("storeId") or "",
                    name=normalize(raw.get("storeName") or ""),
                    street=normalize(addr.get("address2") or addr.get("address") or ""),
                    city=normalize(raw.get("city") or ""),
                    state=raw.get("state") or "",
                    distance=raw.get("storeDistanceWithUnit") or "",
                    quote=normalize((info or {}).get("pickupSearchQuote") or ""),
                    distance_mi=_as_float(raw.get("storedistance")),
                )
            )

    # Nearest first: the message is used to decide where to drive.
    return {
        part: PartAvailability(
            part, products[part].name,
            sorted(s, key=lambda x: (x.distance_mi, x.name)),
        )
        for part, s in found.items()
    }


def check_canary(results: dict[str, PartAvailability], canary_part: str) -> None:
    """An all-negative response is indistinguishable from a broken one, so a
    control part known to be in stock decides whether to believe the zeros."""
    canary = results.get(canary_part)
    if canary is None:
        raise ImplausibleResponse(
            f"canary {canary_part} missing from response; not believing the result"
        )
    if not canary.available:
        raise ImplausibleResponse(
            f"canary {canary_part} ({canary.name}) reports 0 stores. Either the "
            f"endpoint stopped honouring the location, or the canary genuinely "
            f"sold out everywhere. Not believing the zeros. Pick a different "
            f"canary part if this persists."
        )
