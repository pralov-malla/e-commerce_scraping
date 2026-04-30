from __future__ import annotations

"""
eBay STRICT Kids/Baby scraper
=============================

Purpose:
  Scrape ONLY kids/baby products from eBay.

Outputs:
  1) data/eBay/ebay_kids_categories.json
     - normal JSON category tree + logs
     - saved incrementally

  2) data/eBay/ebay_kids_products.jsonl
     - one product per line
     - appended immediately when each product is accepted
     - shape similar to your Amazon JSONL, but eBay uses item_id instead of asin

Why this version is stricter:
  Your previous run escaped from:
    Kids Clothing, Shoes & Accessories
  into broad eBay categories like:
    Clothing, Shoes & Accessories > Motorcycles > Women

  This version blocks that in two places:
    1. category discovery filter
    2. product acceptance filter

  A product is only saved if the leaf category or the title is truly kids/baby
  related. Merely having an old ancestor like "Kids Clothing" is NOT enough.

Install:
  uv add playwright beautifulsoup4 lxml

Ubuntu 22/24:
  uv run python -m playwright install chromium

Ubuntu 26.04:
  Playwright may not download bundled Chromium yet. Install Google Chrome and
  pass --browser-executable:

  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt install ./google-chrome-stable_current_amd64.deb

Run first time:
  uv run src/ebay_scraper.py --headed --browser-executable /usr/bin/google-chrome

Run normally:
  uv run src/ebay_scraper.py --browser-executable /usr/bin/google-chrome
"""

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    from bs4 import BeautifulSoup, Tag
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as exc:
    raise SystemExit(
        "Missing dependencies. Run:\n\n"
        "  uv add playwright beautifulsoup4 lxml\n"
        "  uv run python -m playwright install chromium\n"
    ) from exc


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

BASE_DIR = Path("data/eBay")
CATEGORIES_JSON = BASE_DIR / "ebay_kids_categories.json"
PRODUCTS_JSONL = BASE_DIR / "ebay_kids_products.jsonl"
BROWSER_STATE = BASE_DIR / "ebay_browser_state.json"
DEBUG_HTML_DIR = BASE_DIR / "debug_html"


# -----------------------------------------------------------------------------
# Safe root entry points
# -----------------------------------------------------------------------------

# Only start from narrow kids/baby roots. Do NOT start from broad Fashion,
# Clothing, Toys & Hobbies, Motors, Collectibles, etc.
ROOT_SEEDS: list[dict[str, Any]] = [
    {
        "name": "Baby",
        "category_id": "2984",
        "url": "https://www.ebay.com/b/Baby/2984/bn_1865497",
    },
    {
        "name": "Kids Clothing, Shoes & Accessories",
        "category_id": "171146",
        "url": "https://www.ebay.com/b/Kids-Clothing-Shoes-Accessories/171146/bn_1642843",
    },
    {
        "name": "Baby Clothing",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=baby+clothing&LH_BIN=1",
    },
    {
        "name": "Boys Clothing",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=boys+clothing&LH_BIN=1",
    },
    {
        "name": "Girls Clothing",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=girls+clothing&LH_BIN=1",
    },
    {
        "name": "Kids Shoes",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=kids+shoes&LH_BIN=1",
    },
    {
        "name": "Baby Gear",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=baby+stroller+car+seat+crib&LH_BIN=1",
    },
    {
        "name": "Baby Toys",
        "category_id": None,
        "url": "https://www.ebay.com/sch/i.html?_nkw=baby+toys&LH_BIN=1",
    },
]


# -----------------------------------------------------------------------------
# Strict filters
# -----------------------------------------------------------------------------

# These terms are strong enough to identify kids/baby context.
STRICT_KIDS_TERMS = {
    "baby", "babies", "boy", "boys", "girl", "girls", "child", "children",
    "infant", "kid", "kids", "newborn", "nursery", "preschool", "toddler",
    "toddlers", "youth", "junior", "juniors",
}

# Durable kids/baby product terms. These are used for product/title context,
# not as permission to walk into a broad category like Women or Motors.
PRODUCT_KIDS_TERMS = STRICT_KIDS_TERMS | {
    "onesie", "romper", "bodysuit", "crib", "bassinet", "stroller", "pram",
    "car seat", "booster seat", "high chair", "playpen", "playmat", "playard",
    "baby carrier", "bouncer", "sleepsack", "sleep sack", "swaddle", "pacifier",
    "teether", "teething", "bib", "diaper bag", "changing table", "baby monitor",
    "school bag", "kids shoes", "kids clothes", "kids clothing", "baby shoes",
    "baby clothes", "baby clothing", "boys shoes", "girls shoes", "boys clothes",
    "girls clothes", "boys clothing", "girls clothing", "toddler shoes",
    "toddler clothing", "infant shoes", "infant clothing", "newborn clothes",
    "newborn clothing", "youth shoes", "youth clothing",
}

# Category labels that are allowed when discovered under the Baby root even if
# the label itself is neutral. Example: "Strollers", "Nursery Bedding".
BABY_SAFE_NEUTRAL_CATEGORY_TERMS = {
    "accessories", "bedding", "blanket", "bottle", "bottles", "carrier",
    "carriers", "car seat", "car seats", "booster", "booster seats", "crib",
    "cribs", "decor", "feeding", "gear", "health", "safety", "high chair",
    "high chairs", "monitor", "monitors", "nursery", "playard", "playards",
    "playpen", "playpens", "stroller", "strollers", "travel system",
}

# Category labels that are allowed when discovered under the Kids Clothing root.
# Notice this DOES NOT include broad "Clothing, Shoes & Accessories" or "Women".
KIDS_CLOTHING_SAFE_CATEGORY_TERMS = {
    "baby", "babies", "boys", "girls", "kids", "children", "child", "infant",
    "newborn", "toddler", "toddlers", "youth", "junior", "juniors",
    "boys clothing", "girls clothing", "kids clothing", "baby clothing",
    "boys shoes", "girls shoes", "kids shoes", "baby shoes",
    "boys accessories", "girls accessories", "kids accessories", "baby accessories",
}

# Hard category stops. If a discovered category label, path, or URL suggests any
# of these, it will not be visited.
FORBIDDEN_CATEGORY_SUBSTRINGS = {
    "adult", "antiques", "art", "business", "cologne", "collectibles",
    "dogs", "dog", "cats", "cat", "for men", "for women", "handbag",
    "handbags", "industrial", "jewelry", "luggage", "makeup", "maternity",
    "men's", "mens", "men", "motorcycle", "motorcycles", "motor", "motors",
    "perfume", "pet", "pets", "purse", "purses", "ray-ban", "suitcase",
    "suitcases", "travel bag", "wallet", "wallets", "watch", "watches",
    "women's", "womens", "women", "automotive", "parts", "tires",
}

# Known broad/non-kids eBay category IDs that must never be followed.
# 11450 = Clothing, Shoes & Accessories, 260010 = Women's Clothing etc.
FORBIDDEN_CATEGORY_IDS = {
    "11450", "260010", "1059", "11116", "2984x", "6028", "220", "1",
    "281", "12576", "888", "6000", "131090", "26395", "625", "14339",
}

# Category labels that are never useful as category nodes.
SKIP_LABEL_EXACT = {
    "", "all", "back to top", "buy it now", "customer reviews", "departments",
    "featured categories", "free shipping", "new releases", "open box", "pre-owned",
    "price", "returns accepted", "see all", "see more", "shop all", "show more",
    "sponsored", "today's deals", "shop by category", "shop by age", "shop by brand",
    "black", "blue", "brown", "gray", "grey", "green", "orange", "pink", "purple",
    "red", "white", "yellow", "beige", "multi-color", "multicolor",
}

SKIP_LABEL_CONTAINS = (
    " & up", " stars", " star", "auction", "avg. customer", "availability",
    "brand", "condition", "customer review", "delivery", "discount", "eligible",
    "filter", "free shipping", "from our brands", "international shipping",
    "item location", "less than", "more than", "packaging option", "price",
    "ratings", "seller", "shipping", "sort by", "under $", "over $",
    "color", "colour", "size type", "material", "pattern",
)

CONSUMABLE_TERMS = {
    "baby food", "formula", "fruit pouch", "puree", "snack", "snacks", "milk",
    "cereal", "gummies", "vitamin", "supplement", "shampoo", "lotion",
    "body wash", "soap", "toothpaste", "wipes", "diaper", "diapers", "nappy",
    "nappies",
}

FORBIDDEN_TITLE_SUBSTRINGS = FORBIDDEN_CATEGORY_SUBSTRINGS | {
    "adult", "for adult", "pet food", "dog food", "cat food", "charity t-shirt",
    "stephen colbert", "birkenstock arizona", "hold ups stockings", "women sandals",
}

CURRENCY_MAP = {
    "US$": "USD", "$": "USD", "£": "GBP", "€": "EUR", "₹": "INR",
    "¥": "JPY", "C$": "CAD", "A$": "AUD",
}

TRACKING_PREFIXES = (
    "_trk", "_trksid", "campid", "customid", "hash", "mkcid", "mkevt",
    "mkrid", "ssspo", "toolid",
)

KEEP_QUERY_KEYS = {"_nkw", "_pgn", "_sacat", "LH_BIN", "rt"}

CATEGORY_SELECTORS = [
    ".b-visualnav__tile a[href]",
    ".b-guidancecard__link[href]",
    ".b-carousel__seeall a[href]",
    "section a[href*='/b/']",
    "section a[href*='/sch/'][href*='_sacat=']",
    "nav a[href*='/b/']",
    "a[href*='/b/']",
    "a[href*='/sch/'][href*='_sacat=']",
]

PRODUCT_CARD_SELECTORS = [
    "li.s-item",
    "div.s-item",
    "li[data-view*='mi:']",
    "div[data-view*='mi:']",
    "li.brwrvr__item-card",
    "div.brwrvr__item-card",
]


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\u200b", "").replace("\u2063", "").split()).strip()


def clean_title(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"^(New Listing|SPONSORED|Details about)\s*", "", text, flags=re.I)
    text = re.sub(r"\s*Opens in a new window.*$", "", text, flags=re.I)
    return normalize_text(text)


def has_term(text: str, terms: set[str]) -> bool:
    lower = text.lower()
    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"s?\b"
        if re.search(pattern, lower):
            return True
    return False


def has_substring(text: str, terms: set[str]) -> bool:
    lower = text.lower()
    return any(term in lower for term in terms)


def parse_price(value: str | None) -> tuple[float | None, str | None, str | None]:
    text = normalize_text(value)
    if not text:
        return None, None, None

    currency = next((code for symbol, code in CURRENCY_MAP.items() if symbol in text), None)
    match = re.search(r"([0-9][0-9,]*\.?[0-9]*)", text)
    if not match:
        return None, currency, text

    try:
        return float(match.group(1).replace(",", "")), currency, text
    except ValueError:
        return None, currency, text


def parse_rating(value: str | None) -> float | None:
    text = normalize_text(value)
    match = re.search(r"([0-5](?:\.[0-9])?)\s*out of\s*5", text, re.I) or re.search(r"\b([0-5](?:\.[0-9])?)\b", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    match = re.search(r"([0-9][0-9,]*)", normalize_text(value))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def clean_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None

    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None

    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.startswith(TRACKING_PREFIXES):
            continue
        if key not in KEEP_QUERY_KEYS:
            continue
        query.append((key, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def same_host(base: str, url: str) -> bool:
    base_host = urlsplit(base).netloc.lower()
    url_host = urlsplit(url).netloc.lower()
    return url_host == base_host or url_host.endswith("." + base_host)


def category_id_from_url(url: str | None) -> str | None:
    if not url:
        return None

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sacat = query.get("_sacat")
    if sacat and sacat.isdigit():
        return sacat

    for segment in parts.path.split("/"):
        if segment.isdigit() and len(segment) >= 2:
            return segment

    return None


def is_category_url(marketplace: str, url: str | None) -> bool:
    if not url or not same_host(marketplace, url):
        return False

    parts = urlsplit(url)
    path = parts.path.lower()
    if any(piece in path for piece in ("/itm/", "/usr/", "/help/", "/str/")):
        return False

    category_id = category_id_from_url(url)
    if category_id in FORBIDDEN_CATEGORY_IDS:
        return False

    if "/b/" in path and category_id:
        return True

    if path.startswith("/sch/") and category_id:
        return True

    return False


def is_product_url(marketplace: str, url: str | None) -> bool:
    if not url or not same_host(marketplace, url):
        return False
    return "/itm/" in urlsplit(url).path.lower()


def item_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/itm/(?:[^/?#]+/)?(\d{9,})", url)
    return match.group(1) if match else None


def category_key(url: str, category_id: str | None) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    keyword = normalize_text(query.get("_nkw", "")).lower()
    if keyword:
        return f"search:{keyword}"
    if category_id:
        return f"id:{category_id}:{strip_query(url)}"
    return f"url:{strip_query(url)}"


def search_url(marketplace: str, category_id: str | None, page_num: int) -> str:
    query: dict[str, str] = {"_pgn": str(page_num), "LH_BIN": "1"}
    if category_id:
        query["_sacat"] = category_id
    return f"{marketplace.rstrip('/')}/sch/i.html?{urlencode(query)}"


def paged_url(url: str, page_num: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_pgn"] = str(page_num)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def looks_blocked(soup: BeautifulSoup, final_url: str = "") -> bool:
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    body = soup.get_text(" ", strip=True).lower()
    final = final_url.lower()
    flags = (
        "captcha", "robot", "verify", "challenge", "unusual traffic",
        "access denied", "pardon our interruption", "security measure",
    )
    return "splashui/challenge" in final or any(flag in title or flag in body for flag in flags)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        file.write("\n")
        file.flush()


def load_existing_product_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                product = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(product, dict):
                continue
            key = str(product.get("item_id") or product.get("url") or "")
            if key:
                keys.add(key)
    return keys


def path_text(path: list[str]) -> str:
    return " > ".join(path)


def node_path(node: dict[str, Any]) -> list[str]:
    path = node.get("path")
    if isinstance(path, list):
        return [str(part) for part in path]
    return [str(node.get("name") or "")]


def path_has_forbidden(path: list[str]) -> bool:
    joined = " > ".join(path).lower()
    # Do not reject the literal root phrase "Kids Clothing, Shoes & Accessories".
    joined_without_safe_root = joined.replace("kids clothing, shoes & accessories", "")
    return has_substring(joined_without_safe_root, FORBIDDEN_CATEGORY_SUBSTRINGS)


def leaf_has_strict_kids_context(path: list[str]) -> bool:
    # Important: use leaf/near-leaf only. Do NOT use old root ancestor like
    # "Kids Clothing" because escaped paths can become Kids > ... > Women.
    leaf_parts = path[-2:] if len(path) >= 2 else path
    leaf_text = " ".join(leaf_parts).lower()
    if path_has_forbidden(path):
        return False
    return has_term(leaf_text, STRICT_KIDS_TERMS | BABY_SAFE_NEUTRAL_CATEGORY_TERMS | KIDS_CLOTHING_SAFE_CATEGORY_TERMS)


# -----------------------------------------------------------------------------
# Category/product relevance
# -----------------------------------------------------------------------------

def clean_category_label(label: str) -> str:
    text = normalize_text(label)
    text = re.sub(r"^Shop\s+", "", text, flags=re.I)
    text = re.sub(r"\s+See all$", "", text, flags=re.I)
    text = re.sub(r"\s+\(\d[\d,]*\)$", "", text)
    return normalize_text(text)


def good_category_label(label: str, current_path: list[str]) -> bool:
    text = clean_category_label(label)
    lower = text.lower()

    if lower in SKIP_LABEL_EXACT:
        return False
    if any(piece in lower for piece in SKIP_LABEL_CONTAINS):
        return False
    if has_substring(lower, FORBIDDEN_CATEGORY_SUBSTRINGS):
        return False
    if len(text) < 3 or len(text) > 90:
        return False
    if text.startswith("$") or "$" in text:
        return False
    if re.fullmatch(r"[\d,\s]+", text):
        return False
    if lower in {normalize_text(part).lower() for part in current_path}:
        return False

    # Explicitly block the broad category that caused the bad path.
    if lower in {
        "clothing, shoes & accessories",
        "clothing & accessories",
        "clothing shoes accessories",
        "fashion",
        "women",
        "woman",
        "womens",
        "women's",
        "men",
        "man",
        "mens",
        "men's",
        "unisex adult",
        "adult clothing",
        "motorcycles",
        "motorcycle parts",
        "hunting equipment",
        "sporting goods",
        "collectibles",
        "jewelry & watches",
    }:
        return False

    return True


def category_is_allowed(label: str, url: str, parent_path: list[str]) -> bool:
    label = clean_category_label(label)
    category_id = category_id_from_url(url)

    if category_id in FORBIDDEN_CATEGORY_IDS:
        return False
    if not good_category_label(label, parent_path):
        return False
    if path_has_forbidden([*parent_path, label]):
        return False

    parent_text = " ".join(parent_path).lower()
    label_text = label.lower()
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    keyword_text = normalize_text(query.get("_nkw", "")).lower()
    url_context = f"{parts.path} {keyword_text}".lower()

    # Under Baby root, allow baby-specific labels plus safe baby neutral labels.
    if "baby" in parent_text:
        return has_term(label_text, STRICT_KIDS_TERMS | BABY_SAFE_NEUTRAL_CATEGORY_TERMS) or has_term(
            keyword_text,
            STRICT_KIDS_TERMS | BABY_SAFE_NEUTRAL_CATEGORY_TERMS,
        )

    # Under Kids Clothing root, only allow labels that themselves mention kids,
    # babies, boys, girls, youth, etc. Do NOT allow neutral "Clothing" alone.
    if "kids clothing" in parent_text:
        return has_term(label_text, KIDS_CLOTHING_SAFE_CATEGORY_TERMS | STRICT_KIDS_TERMS) or has_term(
            keyword_text,
            KIDS_CLOTHING_SAFE_CATEGORY_TERMS | STRICT_KIDS_TERMS,
        )

    # For deeper paths, allow only if the new label/url is explicitly kids/baby.
    return has_term(
        f"{label_text} {url_context}",
        STRICT_KIDS_TERMS | KIDS_CLOTHING_SAFE_CATEGORY_TERMS | BABY_SAFE_NEUTRAL_CATEGORY_TERMS,
    )


def product_is_allowed(title: str, category_path: list[str], include_consumables: bool) -> bool:
    title_clean = clean_title(title)
    title_lower = title_clean.lower()

    if len(title_clean) < 5:
        return False
    if path_has_forbidden(category_path):
        return False
    if has_substring(title_lower, FORBIDDEN_TITLE_SUBSTRINGS):
        return False

    if not include_consumables and has_term(title_lower, CONSUMABLE_TERMS):
        # Keep durable diaper bags, changing tables, etc. but drop diapers/wipes/food.
        durable_exceptions = {"diaper bag", "changing table", "wipe warmer", "bottle warmer"}
        if not has_term(title_lower, durable_exceptions):
            return False

    # VERY STRICT JSONL rule:
    # The product title itself must prove it is a kids/baby product.
    # Category path alone is NOT enough, because eBay can leak from a kids root
    # into broad pages like Women, Men, Hunting Equipment, etc.
    # This may reject some valid items, but it prevents non-kids products in JSONL.
    title_has_kids = has_term(title_lower, PRODUCT_KIDS_TERMS)
    leaf_has_safe_context = leaf_has_strict_kids_context(category_path)

    return title_has_kids and leaf_has_safe_context


# -----------------------------------------------------------------------------
# Config/state
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    marketplace: str
    max_depth: int
    max_children: int
    max_pages: int
    max_per_category: int
    max_total: int
    delay_min: float
    delay_max: float
    timeout_ms: int
    include_sponsored: bool
    include_consumables: bool
    scrape_intermediate: bool
    search_fallback: bool
    headed: bool
    slow_mo: int
    save_debug_html: bool
    browser_executable: str | None
    user_agent: str
    append: bool


@dataclass
class State:
    tree: dict[str, Any]
    seen_categories: set[str] = field(default_factory=set)
    seen_products: set[str] = field(default_factory=set)
    requests: int = 0
    categories_found: int = 0
    products_saved: int = 0
    products_rejected: int = 0
    pages_ok: int = 0
    pages_failed: int = 0
    pages_blocked: int = 0
    debug_saved: int = 0


# -----------------------------------------------------------------------------
# Scraper
# -----------------------------------------------------------------------------

class EbayKidsScraper:
    def __init__(self, config: Config, state: State) -> None:
        self.config = config
        self.state = state

    def flush(self, status: str = "running") -> None:
        self.state.tree.update(
            status=status,
            last_saved_at=now_iso(),
            requests=self.state.requests,
            categories_found=self.state.categories_found,
            products_saved=self.state.products_saved,
            products_rejected=self.state.products_rejected,
            pages_ok=self.state.pages_ok,
            pages_failed=self.state.pages_failed,
            pages_blocked=self.state.pages_blocked,
        )
        atomic_write_json(CATEGORIES_JSON, self.state.tree)

    async def delay(self) -> None:
        if self.state.requests == 0:
            return
        seconds = random.uniform(self.config.delay_min, self.config.delay_max)
        log(f"Sleeping {seconds:.1f}s")
        await asyncio.sleep(seconds)

    async def fetch(self, page: Page, url: str, label: str) -> tuple[BeautifulSoup | None, dict[str, Any]]:
        await self.delay()
        self.state.requests += 1
        log(f"Request #{self.state.requests}: {label}: {url}")

        record: dict[str, Any] = {
            "url": url,
            "label": label,
            "status_code": None,
            "final_url": None,
            "status": None,
            "candidates_found": 0,
            "saved": 0,
            "rejected": 0,
        }

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
            record["status_code"] = response.status if response else None
            await page.wait_for_timeout(1500)
            await page.mouse.wheel(0, 1400)
            await page.wait_for_timeout(800)
            final_url = page.url
            html = await page.content()
        except Exception as exc:
            self.state.pages_failed += 1
            record["status"] = f"request_failed: {exc}"
            return None, record

        record["final_url"] = final_url
        soup = BeautifulSoup(html, "lxml")

        if record["status_code"] in {403, 429, 503}:
            self.state.pages_failed += 1
            record["status"] = "blocked_or_throttled"
            return None, record

        if looks_blocked(soup, final_url):
            self.state.pages_blocked += 1
            record["status"] = "captcha_or_challenge"
            log("eBay challenge page detected.")

            if self.config.headed:
                log("Solve it in browser, then press Enter here.")
                await asyncio.to_thread(input)
                html = await page.content()
                final_url = page.url
                soup = BeautifulSoup(html, "lxml")
                if looks_blocked(soup, final_url):
                    self.state.pages_failed += 1
                    record["status"] = "captcha_unsolved"
                    return None, record
                record["status"] = "ok_after_manual_check"
                record["final_url_after_manual_check"] = final_url
                self.state.pages_ok += 1
            else:
                self.state.pages_failed += 1
                return None, record
        else:
            record["status"] = "ok"
            self.state.pages_ok += 1

        if self.config.save_debug_html and self.state.debug_saved < 100:
            DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
            debug_file = DEBUG_HTML_DIR / f"page_{self.state.requests:04d}.html"
            debug_file.write_text(html, encoding="utf-8")
            record["debug_html"] = str(debug_file)
            self.state.debug_saved += 1

        return soup, record

    def discover_children(self, soup: BeautifulSoup, current_url: str, parent_path: list[str]) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        current_bare = strip_query(current_url)

        for selector in CATEGORY_SELECTORS:
            for element in soup.select(selector):
                if not isinstance(element, Tag):
                    continue

                href = element.get("href")
                if not isinstance(href, str) or not href.strip():
                    continue

                url = clean_url(current_url, href)
                if not is_category_url(self.config.marketplace, url):
                    continue

                assert url is not None
                if strip_query(url) == current_bare:
                    continue

                label = normalize_text(element.get_text(" ", strip=True))
                if not label:
                    img = element.select_one("img")
                    if isinstance(img, Tag):
                        label = normalize_text(str(img.get("alt") or ""))

                label = clean_category_label(label)
                if not category_is_allowed(label, url, parent_path):
                    continue

                category_id = category_id_from_url(url)
                key = category_key(url, category_id)
                if key in found:
                    continue

                found[key] = {"name": label, "url": url, "category_id": category_id}

                if len(found) >= self.config.max_children:
                    return list(found.values())

        return list(found.values())

    async def walk_category(self, page: Page, node: dict[str, Any]) -> None:
        if self.state.products_saved >= self.config.max_total:
            return

        url = str(node["url"])
        category_id = str(node.get("category_id") or category_id_from_url(url) or "") or None
        node["category_id"] = category_id
        key = category_key(url, category_id)
        path = node_path(node)
        depth = int(node.get("depth", 1))

        if category_id in FORBIDDEN_CATEGORY_IDS or path_has_forbidden(path):
            node["status"] = "blocked_by_strict_category_filter"
            self.flush("strict_filtering")
            return

        if key in self.state.seen_categories:
            node["status"] = "skipped_duplicate"
            self.flush("running")
            return

        self.state.seen_categories.add(key)
        if depth > 1:
            self.state.categories_found += 1

        node["status"] = "visiting"
        self.flush("discovering_categories")

        soup, record = await self.fetch(page, url, f"category | {path_text(path)}")
        node.setdefault("discovery_log", []).append(record)
        self.flush("discovering_categories")

        if soup is None:
            node["status"] = "fetch_failed"
            self.flush("discovering_categories")
            return

        children: list[dict[str, Any]] = []
        if depth < self.config.max_depth:
            children = self.discover_children(soup, url, path)

        node["discovered_children"] = len(children)

        new_children: list[dict[str, Any]] = []
        existing_keys = {
            category_key(str(child["url"]), str(child.get("category_id") or "") or None)
            for child in node.get("subcategories", [])
        }

        for child in children:
            child_key = category_key(str(child["url"]), str(child.get("category_id") or "") or None)
            if child_key in existing_keys:
                continue

            child_node = {
                "name": child["name"],
                "url": child["url"],
                "category_id": child.get("category_id"),
                "path": [*path, child["name"]],
                "depth": depth + 1,
                "product_count": 0,
                "product_ids": [],
                "subcategories": [],
                "discovery_log": [],
                "scrape_log": [],
            }

            node.setdefault("subcategories", []).append(child_node)
            new_children.append(child_node)
            existing_keys.add(child_key)
            log(f"Discovered category: {path_text(child_node['path'])}")
            self.flush("discovering_categories")

        should_scrape_here = self.config.scrape_intermediate or not new_children
        if should_scrape_here:
            await self.scrape_products(page, node, first_soup=soup if not new_children else None)

        for child_node in new_children:
            if self.state.products_saved >= self.config.max_total:
                break
            await self.walk_category(page, child_node)

        node["status"] = "done_with_children" if new_children else "done_leaf"
        self.flush("running")

    def product_page_urls(self, node: dict[str, Any], page_num: int) -> list[str]:
        urls = [paged_url(str(node["url"]), page_num)]
        category_id = str(node.get("category_id") or "") or None
        if self.config.search_fallback and category_id:
            urls.append(search_url(self.config.marketplace, category_id, page_num))
        return list(dict.fromkeys(urls))

    async def scrape_products(self, page: Page, node: dict[str, Any], first_soup: BeautifulSoup | None = None) -> None:
        if int(node.get("product_count", 0)) >= self.config.max_per_category:
            return

        path = node_path(node)
        if path_has_forbidden(path) or not leaf_has_strict_kids_context(path):
            node["product_scrape_skipped_reason"] = "leaf_category_not_strictly_kids_or_baby"
            self.flush("strict_filtering")
            return

        for page_num in range(1, self.config.max_pages + 1):
            if self.state.products_saved >= self.config.max_total:
                return
            if int(node.get("product_count", 0)) >= self.config.max_per_category:
                return

            saved_this_page = 0
            rejected_this_page = 0
            urls = self.product_page_urls(node, page_num)

            for index, url in enumerate(urls):
                if page_num == 1 and index == 0 and first_soup is not None:
                    soup = first_soup
                    first_soup = None
                    record = {
                        "url": url,
                        "label": f"products reused category page | {path_text(path)} | p{page_num}",
                        "status": "ok_reused_category_page",
                        "candidates_found": 0,
                        "saved": 0,
                        "rejected": 0,
                    }
                else:
                    soup, record = await self.fetch(page, url, f"products | {path_text(path)} | p{page_num}")
                    if soup is None:
                        node.setdefault("scrape_log", []).append(record)
                        self.flush("scraping_products")
                        continue

                candidates = self.extract_products(soup, node, url)
                record["candidates_found"] = len(candidates)

                for product in candidates:
                    if self.state.products_saved >= self.config.max_total:
                        break
                    if int(node.get("product_count", 0)) >= self.config.max_per_category:
                        break

                    key = str(product.get("_key") or product.get("item_id") or product.get("url") or "")
                    if not key or key in self.state.seen_products:
                        continue

                    title = str(product.get("title") or "")
                    if not product_is_allowed(title, path, self.config.include_consumables):
                        self.state.products_rejected += 1
                        rejected_this_page += 1
                        continue

                    self.state.seen_products.add(key)
                    self.state.products_saved += 1
                    saved_this_page += 1
                    node["product_count"] = int(node.get("product_count", 0)) + 1
                    if product.get("item_id"):
                        node.setdefault("product_ids", []).append(product["item_id"])

                    output = {k: v for k, v in product.items() if k != "_key"}
                    append_jsonl(PRODUCTS_JSONL, output)
                    log(f"Saved product #{self.state.products_saved}: {title[:120]}")
                    self.flush("scraping_products")

                record["saved"] = saved_this_page
                record["rejected"] = rejected_this_page
                node.setdefault("scrape_log", []).append(record)
                self.flush("scraping_products")

            if page_num == 1 and saved_this_page == 0:
                node["product_scrape_note"] = "no_strict_kids_products_saved_on_first_page_continuing"
                node["products_rejected_on_first_page"] = rejected_this_page
                self.flush("scraping_products")

    def extract_products(self, soup: BeautifulSoup, node: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
        products_by_key: dict[str, dict[str, Any]] = {}

        for selector in PRODUCT_CARD_SELECTORS:
            cards = [card for card in soup.select(selector) if isinstance(card, Tag)]
            if not cards:
                continue

            for card in cards:
                product = self.extract_from_card(card, node, source_url)
                if product:
                    products_by_key.setdefault(str(product["_key"]), product)

            if products_by_key:
                break

        for link in soup.select("a[href*='/itm/']"):
            if not isinstance(link, Tag):
                continue
            product = self.extract_from_link(link, node, source_url)
            if product:
                products_by_key.setdefault(str(product["_key"]), product)

        for product in self.extract_from_jsonld(soup, node, source_url):
            products_by_key.setdefault(str(product["_key"]), product)

        log(f"Found {len(products_by_key)} product candidates in {path_text(node_path(node))}")
        return list(products_by_key.values())

    def make_product(self, node: dict[str, Any], source_url: str, url: str, title: str, **fields: Any) -> dict[str, Any] | None:
        url = strip_query(url)
        item_id = item_id_from_url(url)
        key = item_id or url
        title = clean_title(title)
        if not key or len(title) < 5:
            return None

        product: dict[str, Any] = {
            "_key": key,
            "title": title,
            "url": url,
            "item_id": item_id,
            "category_path": node_path(node),
            "category_url": node["url"],
            "source_url": source_url,
            "scraped_at": now_iso(),
        }

        for field_key, value in fields.items():
            if value is not None and value != "":
                product[field_key] = value

        return product

    def extract_from_card(self, card: Tag, node: dict[str, Any], source_url: str) -> dict[str, Any] | None:
        card_text = normalize_text(card.get_text(" ", strip=True))
        if not self.config.include_sponsored and "sponsored" in card_text.lower():
            return None

        href = self.attr_from_first(card, ["a.s-item__link", "a[href*='/itm/']"], "href")
        url = clean_url(source_url, href)
        if not is_product_url(self.config.marketplace, url):
            return None

        assert url is not None
        title = self.extract_title(card)
        if not title:
            return None

        price_text = self.text_from_first(card, [".s-item__price", ".s-card__price", "[class*='price']"])
        price, currency, raw_price = parse_price(price_text)

        shipping = self.text_from_first(card, [".s-item__shipping", ".s-item__logisticsCost", "[class*='ship']"])
        condition = self.text_from_first(card, [".SECONDARY_INFO", ".s-item__subtitle", "[class*='condition']"])
        seller = self.text_from_first(card, [".s-item__seller-info-text", "[class*='seller']"])
        rating = parse_rating(self.text_from_first(card, [".x-star-rating span.clipped", "[aria-label*='out of 5']", ".s-item__reviews span.clipped"]))
        reviews_count = parse_int(self.text_from_first(card, [".s-item__reviews-count span", ".s-item__reviews-count", "[aria-label*='reviews']"]))
        image_url = self.attr_from_first(card, ["img.s-item__image-img", "img"], "src")
        if not image_url or image_url.startswith("data:"):
            image_url = self.attr_from_first(card, ["img.s-item__image-img", "img"], "data-src")
        watchers = self.text_from_first(card, [".s-item__hotness", "[class*='watch']"])

        return self.make_product(
            node=node,
            source_url=source_url,
            url=url,
            title=title,
            price=price,
            currency=currency,
            raw_price=raw_price,
            image_url=image_url,
            condition=normalize_text(condition) or None,
            shipping=normalize_text(shipping) or None,
            seller=normalize_text(seller) or None,
            rating=rating,
            reviews_count=reviews_count,
            watchers=normalize_text(watchers) or None,
        )

    def extract_from_link(self, link: Tag, node: dict[str, Any], source_url: str) -> dict[str, Any] | None:
        href = link.get("href")
        url = clean_url(source_url, href if isinstance(href, str) else None)
        if not is_product_url(self.config.marketplace, url):
            return None

        assert url is not None
        title = clean_title(link.get_text(" ", strip=True))
        if not title:
            img = link.select_one("img")
            if isinstance(img, Tag):
                title = clean_title(str(img.get("alt") or ""))

        return self.make_product(node=node, source_url=source_url, url=url, title=title)

    def extract_from_jsonld(self, soup: BeautifulSoup, node: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []

        for script in soup.select("script[type='application/ld+json']"):
            raw = script.string or script.get_text(strip=True)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            for item in self.iter_jsonld(payload):
                if not isinstance(item, dict):
                    continue

                item_type = item.get("@type", "")
                item_types = {str(x).lower() for x in item_type} if isinstance(item_type, list) else {str(item_type).lower()}
                if not ({"product", "listitem"} & item_types):
                    continue

                obj = item.get("item", item)
                if not isinstance(obj, dict):
                    continue

                title = clean_title(str(obj.get("name") or item.get("name") or ""))
                url = clean_url(source_url, str(obj.get("url") or item.get("url") or ""))
                if not title or not is_product_url(self.config.marketplace, url):
                    continue

                assert url is not None
                offers = obj.get("offers", {})
                raw_price = None
                currency = None
                price = None
                if isinstance(offers, dict):
                    raw_price = normalize_text(str(offers.get("price") or "")) or None
                    price, parsed_currency, _ = parse_price(raw_price)
                    currency = normalize_text(str(offers.get("priceCurrency") or "")) or parsed_currency

                image = obj.get("image")
                image_url = image[0] if isinstance(image, list) and image else image if isinstance(image, str) else None

                product = self.make_product(
                    node=node,
                    source_url=source_url,
                    url=url,
                    title=title,
                    price=price,
                    currency=currency,
                    raw_price=raw_price,
                    image_url=image_url,
                )
                if product:
                    products.append(product)

        return products

    def iter_jsonld(self, value: Any) -> list[Any]:
        out: list[Any] = []
        if isinstance(value, list):
            for item in value:
                out.extend(self.iter_jsonld(item))
            return out
        if not isinstance(value, dict):
            return out

        out.append(value)
        for key in ("@graph", "itemListElement"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    out.extend(self.iter_jsonld(item))
        return out

    def extract_title(self, card: Tag) -> str:
        selectors = [
            ".s-item__title span[role='heading']",
            ".s-item__title",
            ".s-card__title",
            "h3",
            "a[href*='/itm/']",
        ]
        for selector in selectors:
            element = card.select_one(selector)
            if isinstance(element, Tag):
                title = clean_title(element.get_text(" ", strip=True))
                if title and title.lower() not in {"shop on ebay", "new listing"}:
                    return title

        img = card.select_one("img")
        if isinstance(img, Tag):
            return clean_title(str(img.get("alt") or ""))
        return ""

    def text_from_first(self, root: Tag | BeautifulSoup, selectors: list[str]) -> str | None:
        for selector in selectors:
            element = root.select_one(selector)
            if isinstance(element, Tag):
                text = normalize_text(element.get_text(" ", strip=True))
                if text:
                    return text
        return None

    def attr_from_first(self, root: Tag | BeautifulSoup, selectors: list[str], attr: str) -> str | None:
        for selector in selectors:
            element = root.select_one(selector)
            if isinstance(element, Tag):
                value = element.get(attr)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def build_tree(config: Config) -> dict[str, Any]:
    roots: list[dict[str, Any]] = []
    for seed in ROOT_SEEDS:
        category_id = str(seed.get("category_id") or category_id_from_url(seed["url"]) or "") or None
        roots.append(
            {
                "name": seed["name"],
                "url": seed["url"],
                "category_id": category_id,
                "path": ["eBay", seed["name"]],
                "depth": 1,
                "product_count": 0,
                "product_ids": [],
                "subcategories": [],
                "discovery_log": [],
                "scrape_log": [],
            }
        )

    return {
        "name": "eBay Strict Kids/Baby",
        "marketplace": config.marketplace,
        "schema_source": "discovered_from_site_navigation_with_strict_kids_filter",
        "started_at": now_iso(),
        "last_saved_at": None,
        "status": "starting",
        "settings": {
            "max_depth": config.max_depth,
            "max_children": config.max_children,
            "max_pages": config.max_pages,
            "max_per_category": config.max_per_category,
            "max_total": config.max_total,
            "include_sponsored": config.include_sponsored,
            "include_consumables": config.include_consumables,
            "scrape_intermediate": config.scrape_intermediate,
            "search_fallback": config.search_fallback,
            "strict_note": "Products require title or leaf category to be kids/baby. Ancestor root alone is not enough.",
        },
        "outputs": {
            "categories_json": str(CATEGORIES_JSON),
            "products_jsonl": str(PRODUCTS_JSONL),
        },
        "requests": 0,
        "categories_found": 0,
        "products_saved": 0,
        "products_rejected": 0,
        "pages_ok": 0,
        "pages_failed": 0,
        "pages_blocked": 0,
        "roots": roots,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Strict eBay kids/baby scraper. Saves JSON + JSONL incrementally.")
    parser.add_argument("--marketplace", default="https://www.ebay.com")
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--max-children", type=int, default=60)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-per-category", type=int, default=250)
    parser.add_argument("--max-total", type=int, default=10000)
    parser.add_argument("--delay-min", type=float, default=3.0)
    parser.add_argument("--delay-max", type=float, default=7.0)
    parser.add_argument("--timeout", type=int, default=45_000)
    parser.add_argument("--include-sponsored", action="store_true")
    parser.add_argument("--include-consumables", action="store_true")
    parser.add_argument(
        "--scrape-intermediate",
        action="store_true",
        default=True,
        help="Scrape products from parent categories as well as leaf categories. Enabled by default.",
    )
    parser.add_argument(
        "--leaf-only",
        dest="scrape_intermediate",
        action="store_false",
        help="Only scrape categories that have no discovered children.",
    )
    parser.add_argument("--no-search-fallback", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--save-debug-html", action="store_true")
    parser.add_argument("--browser-executable", default=None, help="Example: /usr/bin/google-chrome")
    parser.add_argument("--append", action="store_true", help="Append to existing JSONL instead of resetting it.")
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    config = Config(
        marketplace=args.marketplace.rstrip("/"),
        max_depth=max(args.max_depth, 1),
        max_children=max(args.max_children, 1),
        max_pages=max(args.max_pages, 1),
        max_per_category=max(args.max_per_category, 1),
        max_total=max(args.max_total, 1),
        delay_min=max(args.delay_min, 0.0),
        delay_max=max(args.delay_max, args.delay_min, 0.0),
        timeout_ms=max(args.timeout, 5_000),
        include_sponsored=args.include_sponsored,
        include_consumables=args.include_consumables,
        scrape_intermediate=args.scrape_intermediate,
        search_fallback=not args.no_search_fallback,
        headed=args.headed,
        slow_mo=max(args.slow_mo, 0),
        save_debug_html=args.save_debug_html,
        browser_executable=args.browser_executable,
        user_agent=args.user_agent,
        append=args.append,
    )

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    existing_product_keys = load_existing_product_keys(PRODUCTS_JSONL) if config.append else set()
    if not config.append and PRODUCTS_JSONL.exists():
        PRODUCTS_JSONL.unlink()
    PRODUCTS_JSONL.touch()

    tree = build_tree(config)
    state = State(tree=tree, seen_products=existing_product_keys)
    atomic_write_json(CATEGORIES_JSON, tree)

    log("=" * 70)
    log("Starting STRICT eBay kids/baby scraper")
    log(f"Categories JSON: {CATEGORIES_JSON}")
    log(f"Products JSONL : {PRODUCTS_JSONL}")
    log(f"Headed         : {config.headed}")
    log(f"Browser exe    : {config.browser_executable}")
    if config.append:
        log(f"Append mode    : skipping {len(existing_product_keys)} existing product keys")
    log("=" * 70)

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": not config.headed,
            "slow_mo": config.slow_mo,
        }
        if config.browser_executable:
            launch_options["executable_path"] = config.browser_executable

        browser: Browser = await playwright.chromium.launch(**launch_options)

        context_options: dict[str, Any] = {
            "user_agent": config.user_agent,
            "viewport": {"width": 1366, "height": 768},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        if BROWSER_STATE.exists():
            context_options["storage_state"] = str(BROWSER_STATE)
            log(f"Loaded saved browser state: {BROWSER_STATE}")

        context: BrowserContext = await browser.new_context(**context_options)
        page = await context.new_page()
        scraper = EbayKidsScraper(config, state)
        scraper.flush("starting")

        try:
            for root_node in tree["roots"]:
                if state.products_saved >= config.max_total:
                    break
                log(f"Starting root: {root_node['name']}")
                await scraper.walk_category(page, root_node)
                scraper.flush("running")
        finally:
            BROWSER_STATE.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(BROWSER_STATE))
            await context.close()
            await browser.close()

    tree["finished_at"] = now_iso()
    final_status = "finished"
    if state.products_saved == 0 and state.pages_blocked > 0:
        final_status = "finished_zero_blocked"
    elif state.products_saved == 0:
        final_status = "finished_zero_products"

    tree["summary"] = {
        "categories_found": state.categories_found,
        "products_saved": state.products_saved,
        "products_rejected": state.products_rejected,
        "requests": state.requests,
        "pages_ok": state.pages_ok,
        "pages_failed": state.pages_failed,
        "pages_blocked": state.pages_blocked,
    }
    tree["status"] = final_status
    tree["last_saved_at"] = now_iso()
    tree["requests"] = state.requests
    tree["categories_found"] = state.categories_found
    tree["products_saved"] = state.products_saved
    tree["products_rejected"] = state.products_rejected
    tree["pages_ok"] = state.pages_ok
    tree["pages_failed"] = state.pages_failed
    tree["pages_blocked"] = state.pages_blocked
    atomic_write_json(CATEGORIES_JSON, tree)

    log("=" * 70)
    log(f"Done. Categories: {state.categories_found}, Saved: {state.products_saved}, Rejected: {state.products_rejected}")
    log(f"Categories JSON: {CATEGORIES_JSON}")
    log(f"Products JSONL : {PRODUCTS_JSONL}")
    if state.products_saved == 0 and state.pages_blocked > 0 and not config.headed:
        log("Tip: eBay showed challenge pages. Run with --headed and solve verification once.")
    log("=" * 70)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        log("Interrupted.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
