from __future__ import annotations

"""
Flipkart kids/baby category discovery scraper
=============================================

Purpose:
  Discover the real Flipkart kids/baby category tree from Flipkart listing
  navigation, recurse through available subcategories, then scrape products.

Outputs:
  1) data/flipkart/flipkart_kids_categories.json
     - discovered category tree and scrape counters
     - saved incrementally

  2) data/flipkart/flipkart_kids_products.jsonl
     - one accepted product per line
     - appended immediately

Run:
  uv run src/flipkart_scraper.py

Ubuntu 26.04:
  Playwright may not provide a bundled Chromium. This script will try to use a
  system Chrome/Chromium automatically, or you can pass:

  uv run src/flipkart_scraper.py --browser-executable /usr/bin/chromium-browser

If Flipkart shows bot protection, run headed once:
  uv run src/flipkart_scraper.py --headed
"""

import argparse
import asyncio
import json
import random
import re
import shutil
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


BASE_DIR = Path("data/flipkart")
CATEGORIES_JSON = BASE_DIR / "flipkart_kids_categories.json"
PRODUCTS_JSONL = BASE_DIR / "flipkart_kids_products.jsonl"
BROWSER_STATE = BASE_DIR / "flipkart_browser_state.json"
DEBUG_HTML_DIR = BASE_DIR / "debug_html"

DEFAULT_MARKETPLACE = "https://www.flipkart.com"
SYSTEM_BROWSER_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
)

# These are entry points, not a category schema. The scraper discovers children
# from Flipkart's own category sidebar after landing on each page.
FALLBACK_ROOT_SEEDS: list[dict[str, str]] = [
    {"name": "Baby Care", "url": "https://www.flipkart.com/baby-care/pr?sid=kyh"},
    {"name": "Toys and Games", "url": "https://www.flipkart.com/toys/pr?sid=tng"},
    {"name": "Kids Clothing", "url": "https://www.flipkart.com/search?q=kids+clothing"},
    {"name": "Kids Footwear", "url": "https://www.flipkart.com/search?q=kids+footwear"},
]

BOOTSTRAP_QUERIES = (
    "kids",
    "baby care",
    "toys for kids",
    "kids clothing",
    "kids footwear",
)

KIDS_TERMS = {
    "baby",
    "babies",
    "boy",
    "boys",
    "child",
    "children",
    "girl",
    "girls",
    "infant",
    "kid",
    "kids",
    "newborn",
    "nursery",
    "school",
    "toddler",
    "toddlers",
    "toy",
    "toys",
    "youth",
}

ROOT_SAFE_TERMS = {
    "baby care",
    "toys",
    "toys and games",
    "kids clothing",
    "kids footwear",
    "kids accessories",
    "school supplies",
}

SAFE_CHILD_TERMS = {
    "accessories",
    "activity",
    "art",
    "arts",
    "backpack",
    "bag",
    "bags",
    "bath",
    "bathing",
    "bedding",
    "blocks",
    "bottle",
    "car seat",
    "car seats",
    "clothing",
    "crib",
    "cribs",
    "diaper",
    "diapers",
    "dress",
    "dresses",
    "education",
    "educational",
    "feeding",
    "footwear",
    "games",
    "gear",
    "learning",
    "musical",
    "play",
    "playmat",
    "pram",
    "pretend",
    "puzzle",
    "school",
    "shoes",
    "soft toys",
    "stroller",
    "strollers",
    "t-shirt",
    "tshirts",
    "walker",
    "walkers",
}

FORBIDDEN_CATEGORY_TERMS = {
    "adult",
    "automotive",
    "bike",
    "car parts",
    "cars",
    "condom",
    "for men",
    "for women",
    "grocery",
    "jewellery",
    "jewelry",
    "laptop",
    "lingerie",
    "men's",
    "mens",
    "mobile",
    "motorcycle",
    "perfume",
    "pet",
    "pets",
    "saree",
    "sex",
    "smartphone",
    "watch",
    "watches",
    "women's",
    "womens",
}

SKIP_LABEL_EXACT = {
    "",
    "all",
    "availability",
    "brand",
    "cart",
    "categories",
    "customer ratings",
    "discount",
    "explore plus",
    "filters",
    "home",
    "login",
    "new arrivals",
    "offers",
    "popularity",
    "price",
    "price -- high to low",
    "price -- low to high",
    "show more",
    "sort by",
}

SKIP_LABEL_CONTAINS = (
    "% off",
    "& above",
    "become a seller",
    "buy more",
    "cash on delivery",
    "customer",
    "delivery",
    "gst invoice",
    "min",
    "more",
    "only few left",
    "page ",
    "rating",
    "review",
    "special price",
)

PRODUCT_TITLE_KIDS_TERMS = KIDS_TERMS | {
    "bodysuit",
    "bouncer",
    "crib",
    "diaper",
    "doll",
    "educational",
    "feeding bottle",
    "onesie",
    "playmat",
    "pram",
    "puzzle",
    "rattle",
    "romper",
    "school bag",
    "stroller",
    "teether",
    "toddler",
    "walker",
}

FORBIDDEN_TITLE_TERMS = FORBIDDEN_CATEGORY_TERMS | {
    "adult toy",
    "beer",
    "cigarette",
    "wine",
}

KEEP_QUERY_KEYS = {"q", "sid", "page", "pid", "lid", "marketplace"}

CATEGORY_LINK_SELECTORS = [
    "a[href*='/pr?sid=']",
    "a[href*='/pr?'][href*='sid=']",
    "a[href*='/search?'][href*='q=']",
]

PRODUCT_LINK_SELECTORS = [
    "a[href*='/p/']",
    "a[href*='pid=']",
]


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
    scrape_intermediate: bool
    discover_from_search: bool
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


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\u200b", "").replace("\u2063", "").split()).strip()


def clean_label(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"^shop\s+", "", text, flags=re.I)
    text = re.sub(r"\s+\(\d[\d,]*\)$", "", text)
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


def same_host(base: str, url: str) -> bool:
    base_host = urlsplit(base).netloc.lower()
    url_host = urlsplit(url).netloc.lower()
    return url_host == base_host or url_host.endswith("." + base_host)


def clean_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None

    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None

    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in KEEP_QUERY_KEYS:
            query.append((key, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def is_product_url(marketplace: str, url: str | None) -> bool:
    if not url or not same_host(marketplace, url):
        return False
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    return "/p/" in parts.path.lower() or bool(query.get("pid"))


def is_category_url(marketplace: str, url: str | None) -> bool:
    if not url or not same_host(marketplace, url) or is_product_url(marketplace, url):
        return False

    parts = urlsplit(url)
    path = parts.path.lower()
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if any(piece in path for piece in ("/account", "/help", "/pages", "/plus", "/viewcart")):
        return False
    if path.endswith("/pr") and query.get("sid"):
        return True
    if path == "/search" and query.get("q"):
        return True
    return False


def category_key(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("sid"):
        return f"sid:{query['sid']}"
    if query.get("q"):
        return f"search:{normalize_text(query['q']).lower()}"
    return f"url:{strip_query(url).lower()}"


def product_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("pid"):
        return query["pid"]
    match = re.search(r"/p/(?:itm)?([a-z0-9]{8,})", parts.path, re.I)
    return match.group(1) if match else None


def paged_url(url: str, page_num: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page_num)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def search_url(marketplace: str, query: str) -> str:
    return f"{marketplace.rstrip('/')}/search?{urlencode({'q': query})}"


def find_system_browser() -> str | None:
    for candidate in SYSTEM_BROWSER_CANDIDATES:
        if "/" in candidate:
            path = Path(candidate)
            if path.exists() and path.is_file():
                return str(path)
            continue

        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    return None


def looks_blocked(soup: BeautifulSoup, final_url: str = "") -> bool:
    title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    body = soup.get_text(" ", strip=True).lower()
    final = final_url.lower()
    flags = (
        "captcha",
        "robot",
        "unusual traffic",
        "verify",
        "access denied",
        "login to flipkart",
    )
    return "challenge" in final or any(flag in title or flag in body for flag in flags)


def parse_price(value: str | None) -> tuple[float | None, str | None, str | None]:
    text = normalize_text(value)
    if not text:
        return None, None, None

    currency = "INR" if "₹" in text or "rs" in text.lower() else None
    match = re.search(r"₹\s*([0-9][0-9,]*\.?[0-9]*)", text)
    if not match:
        match = re.search(r"([0-9][0-9,]*\.?[0-9]*)", text)
    if not match:
        return None, currency, text

    try:
        return float(match.group(1).replace(",", "")), currency or "INR", text
    except ValueError:
        return None, currency, text


def parse_rating(value: str | None) -> float | None:
    text = normalize_text(value)
    match = re.search(r"\b([0-5](?:\.[0-9])?)\b", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_count(value: str | None) -> int | None:
    text = normalize_text(value)
    match = re.search(r"\(([0-9][0-9,]*)\)", text) or re.search(r"([0-9][0-9,]*)\s+ratings?", text, re.I)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def nearest_product_container(link: Tag) -> Tag:
    current: Tag = link
    best = link
    for _ in range(6):
        parent = current.parent
        if not isinstance(parent, Tag):
            break
        text = normalize_text(parent.get_text(" ", strip=True))
        if "₹" in text:
            best = parent
            if len(text) > 80:
                break
        current = parent
    return best


def text_from_link(link: Tag) -> str:
    text = normalize_text(link.get_text(" ", strip=True))
    if text and not text.startswith("₹"):
        return text

    title = link.get("title")
    if isinstance(title, str) and normalize_text(title):
        return normalize_text(title)

    image = link.select_one("img")
    if isinstance(image, Tag):
        alt = image.get("alt")
        if isinstance(alt, str) and normalize_text(alt):
            return normalize_text(alt)
    return ""


def image_from_container(container: Tag) -> str | None:
    image = container.select_one("img[src], img[data-src]")
    if not isinstance(image, Tag):
        return None
    value = image.get("src") or image.get("data-src")
    return value if isinstance(value, str) and value.startswith("http") else None


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
            try:
                product = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(product, dict):
                continue
            key = str(product.get("product_id") or product.get("url") or "")
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
    return has_substring(joined, FORBIDDEN_CATEGORY_TERMS)


def good_category_label(label: str, current_path: list[str]) -> bool:
    text = clean_label(label)
    lower = text.lower()

    if lower in SKIP_LABEL_EXACT:
        return False
    if any(piece in lower for piece in SKIP_LABEL_CONTAINS):
        return False
    if len(text) < 3 or len(text) > 90:
        return False
    if text.startswith("₹") or re.fullmatch(r"[\d,\s]+", text):
        return False
    if lower in {clean_label(part).lower() for part in current_path}:
        return False
    if has_substring(lower, FORBIDDEN_CATEGORY_TERMS):
        return False
    return True


def category_is_allowed(label: str, url: str, parent_path: list[str]) -> bool:
    label = clean_label(label)
    if not good_category_label(label, parent_path):
        return False
    if path_has_forbidden([*parent_path, label]):
        return False

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    label_and_url = f"{label} {parts.path} {query.get('q', '')}".lower()

    if has_term(label_and_url, KIDS_TERMS | ROOT_SAFE_TERMS):
        return True

    parent_text = " ".join(parent_path).lower()
    if has_term(parent_text, KIDS_TERMS | ROOT_SAFE_TERMS):
        return has_term(label_and_url, SAFE_CHILD_TERMS) or "/pr" in parts.path.lower()

    return False


def product_is_allowed(title: str, category_path: list[str]) -> bool:
    title_clean = normalize_text(title)
    if len(title_clean) < 4:
        return False
    if path_has_forbidden(category_path) or has_substring(title_clean.lower(), FORBIDDEN_TITLE_TERMS):
        return False

    leaf_context = " ".join(category_path[-3:]).lower()
    if has_term(leaf_context, KIDS_TERMS | ROOT_SAFE_TERMS | SAFE_CHILD_TERMS):
        return True
    return has_term(title_clean.lower(), PRODUCT_TITLE_KIDS_TERMS)


def make_tree(config: Config) -> dict[str, Any]:
    return {
        "marketplace": config.marketplace,
        "scraper": "flipkart_kids_discovery",
        "schema_source": "discovered_from_flipkart_listing_category_links",
        "generated_at": now_iso(),
        "last_saved_at": None,
        "status": "starting",
        "config": {
            "max_depth": config.max_depth,
            "max_children": config.max_children,
            "max_pages": config.max_pages,
            "max_per_category": config.max_per_category,
            "max_total": config.max_total,
            "scrape_intermediate": config.scrape_intermediate,
            "discover_from_search": config.discover_from_search,
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
        "roots": [],
    }


class FlipkartKidsScraper:
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
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(700)
            final_url = page.url
            html = await page.content()
        except Exception as exc:
            self.state.pages_failed += 1
            record["status"] = f"request_failed: {exc}"
            return None, record

        record["final_url"] = final_url
        soup = BeautifulSoup(html, "lxml")

        if record["status_code"] in {403, 429, 503} or looks_blocked(soup, final_url):
            self.state.pages_blocked += 1
            record["status"] = "blocked_or_challenge"
            return None, record

        self.state.pages_ok += 1
        record["status"] = "ok"

        if self.config.save_debug_html and self.state.debug_saved < 100:
            DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
            debug_file = DEBUG_HTML_DIR / f"{self.state.requests:04d}.html"
            debug_file.write_text(html, encoding="utf-8")
            self.state.debug_saved += 1

        return soup, record

    async def discover_roots(self, page: Page) -> list[dict[str, Any]]:
        roots: list[dict[str, Any]] = []

        if self.config.discover_from_search:
            for query in BOOTSTRAP_QUERIES:
                soup, _record = await self.fetch(page, search_url(self.config.marketplace, query), f"bootstrap search {query}")
                if not soup:
                    continue
                roots.extend(self.discover_children(soup, search_url(self.config.marketplace, query), ["Kids"]))
                if len(roots) >= self.config.max_children:
                    break

        if not roots:
            roots = [
                {
                    "name": seed["name"],
                    "url": seed["url"],
                    "key": category_key(seed["url"]),
                    "path": ["Kids", seed["name"]],
                    "depth": 1,
                    "product_count": 0,
                    "product_ids": [],
                    "subcategories": [],
                    "discovery_log": [],
                }
                for seed in FALLBACK_ROOT_SEEDS
                if category_is_allowed(seed["name"], seed["url"], ["Kids"])
            ]

        unique: dict[str, dict[str, Any]] = {}
        for root in roots:
            key = str(root.get("key") or category_key(str(root.get("url"))))
            unique.setdefault(key, root)
        return list(unique.values())[: self.config.max_children]

    def discover_children(self, soup: BeautifulSoup, current_url: str, parent_path: list[str]) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        seen: set[str] = set()

        for selector in CATEGORY_LINK_SELECTORS:
            for link in soup.select(selector):
                if not isinstance(link, Tag):
                    continue
                label = clean_label(link.get_text(" ", strip=True))
                href = link.get("href")
                url = clean_url(current_url, href if isinstance(href, str) else None)
                if not url or not is_category_url(self.config.marketplace, url):
                    continue
                if not category_is_allowed(label, url, parent_path):
                    continue

                key = category_key(url)
                if key in seen:
                    continue
                seen.add(key)
                children.append(
                    {
                        "name": label,
                        "url": url,
                        "key": key,
                        "path": [*parent_path, label],
                        "depth": len(parent_path),
                        "product_count": 0,
                        "product_ids": [],
                        "subcategories": [],
                        "discovery_log": [],
                    }
                )
                if len(children) >= self.config.max_children:
                    return children

        return children

    async def crawl_category(self, page: Page, node: dict[str, Any], depth: int) -> None:
        if self.state.products_saved >= self.config.max_total:
            return

        key = str(node.get("key") or category_key(str(node.get("url"))))
        if key in self.state.seen_categories:
            node["skipped_reason"] = "already_seen"
            return
        self.state.seen_categories.add(key)
        self.state.categories_found += 1

        url = str(node["url"])
        path = node_path(node)
        soup, record = await self.fetch(page, url, f"category depth {depth} | {path_text(path)}")
        node.setdefault("discovery_log", []).append(record)
        self.flush("discovering_categories")
        if not soup:
            return

        children: list[dict[str, Any]] = []
        if depth < self.config.max_depth:
            children = self.discover_children(soup, url, path)
            # Remove self/ancestor loops that Flipkart sometimes repeats in the sidebar.
            ancestor_keys = {category_key(str(node.get("url", "")))}
            children = [child for child in children if str(child.get("key")) not in ancestor_keys]
            node["subcategories"] = children
            node["discovered_children"] = len(children)
            self.flush("discovering_categories")

        should_scrape = self.config.scrape_intermediate or not children or depth >= self.config.max_depth
        if should_scrape:
            await self.scrape_products(page, node, first_soup=soup)

        for child in children:
            if self.state.products_saved >= self.config.max_total:
                break
            await self.crawl_category(page, child, depth + 1)

    async def scrape_products(self, page: Page, node: dict[str, Any], first_soup: BeautifulSoup | None = None) -> None:
        if int(node.get("product_count", 0)) >= self.config.max_per_category:
            return

        path = node_path(node)
        base_url = str(node["url"])

        for page_num in range(1, self.config.max_pages + 1):
            if self.state.products_saved >= self.config.max_total:
                break
            if int(node.get("product_count", 0)) >= self.config.max_per_category:
                break

            if page_num == 1 and first_soup is not None:
                soup = first_soup
                record = {
                    "url": base_url,
                    "label": f"products reused category page | {path_text(path)} | p1",
                    "status": "ok",
                    "candidates_found": 0,
                    "saved": 0,
                    "rejected": 0,
                }
            else:
                url = paged_url(base_url, page_num)
                soup, record = await self.fetch(page, url, f"products | {path_text(path)} | p{page_num}")
                if not soup:
                    node.setdefault("product_logs", []).append(record)
                    self.flush("scraping_products")
                    break

            candidates = self.extract_products(soup, node, base_url)
            record["candidates_found"] = len(candidates)
            saved_this_page = 0
            rejected_this_page = 0

            for product in candidates:
                if self.state.products_saved >= self.config.max_total:
                    break
                if int(node.get("product_count", 0)) >= self.config.max_per_category:
                    break

                key = str(product.get("_key") or product.get("product_id") or product.get("url") or "")
                if not key or key in self.state.seen_products:
                    continue

                title = str(product.get("title") or "")
                if not product_is_allowed(title, path):
                    self.state.products_rejected += 1
                    rejected_this_page += 1
                    continue

                self.state.seen_products.add(key)
                self.state.products_saved += 1
                saved_this_page += 1
                node["product_count"] = int(node.get("product_count", 0)) + 1
                if product.get("product_id"):
                    node.setdefault("product_ids", []).append(product["product_id"])

                output = {k: v for k, v in product.items() if k != "_key"}
                append_jsonl(PRODUCTS_JSONL, output)
                log(f"Saved product #{self.state.products_saved}: {title[:120]}")

            record["saved"] = saved_this_page
            record["rejected"] = rejected_this_page
            node.setdefault("product_logs", []).append(record)
            self.flush("scraping_products")

            if not candidates or (page_num > 1 and saved_this_page == 0 and rejected_this_page == 0):
                break

    def extract_products(self, soup: BeautifulSoup, node: dict[str, Any], source_url: str) -> list[dict[str, Any]]:
        products_by_key: dict[str, dict[str, Any]] = {}
        seen_hrefs: set[str] = set()

        for selector in PRODUCT_LINK_SELECTORS:
            for link in soup.select(selector):
                if not isinstance(link, Tag):
                    continue
                href = link.get("href")
                url = clean_url(source_url, href if isinstance(href, str) else None)
                if not url or not is_product_url(self.config.marketplace, url):
                    continue
                if url in seen_hrefs:
                    continue
                seen_hrefs.add(url)

                product = self.extract_from_product_link(link, node, source_url, url)
                if product:
                    products_by_key.setdefault(str(product["_key"]), product)

        log(f"Found {len(products_by_key)} product candidates in {path_text(node_path(node))}")
        return list(products_by_key.values())

    def extract_from_product_link(
        self,
        link: Tag,
        node: dict[str, Any],
        source_url: str,
        url: str,
    ) -> dict[str, Any] | None:
        title = text_from_link(link)
        container = nearest_product_container(link)
        container_text = normalize_text(container.get_text(" ", strip=True))

        if not title:
            # Product-name links can be empty image anchors; use nearby image alt.
            image = container.select_one("img[alt]")
            if isinstance(image, Tag):
                alt = image.get("alt")
                title = normalize_text(alt if isinstance(alt, str) else None)

        title = normalize_text(title)
        if len(title) < 4 or title.startswith("₹"):
            return None

        product_id = product_id_from_url(url)
        price, currency, price_text = parse_price(container_text)
        rating = parse_rating(container_text)
        rating_count = parse_count(container_text)

        key = product_id or strip_query(url)
        return {
            "_key": key,
            "marketplace": "Flipkart",
            "product_id": product_id,
            "title": title,
            "url": url,
            "source_url": source_url,
            "category_path": node_path(node),
            "price": price,
            "currency": currency,
            "price_text": price_text,
            "rating": rating,
            "rating_count": rating_count,
            "image_url": image_from_container(container),
            "scraped_at": now_iso(),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and scrape Flipkart kids/baby categories.")
    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-children", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-per-category", type=int, default=80)
    parser.add_argument("--max-total", type=int, default=500)
    parser.add_argument("--delay-min", type=float, default=1.2)
    parser.add_argument("--delay-max", type=float, default=3.2)
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--leaf-only", action="store_true", help="Only scrape categories that have no discovered children.")
    parser.add_argument("--no-search-bootstrap", action="store_true", help="Skip root discovery searches and use known entry URLs.")
    parser.add_argument("--append", action="store_true", help="Append to existing JSONL and skip known product IDs.")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--browser-executable", default=None)
    parser.add_argument("--save-debug-html", action="store_true")
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    config = Config(
        marketplace=args.marketplace.rstrip("/"),
        max_depth=max(args.max_depth, 1),
        max_children=max(args.max_children, 1),
        max_pages=max(args.max_pages, 1),
        max_per_category=max(args.max_per_category, 1),
        max_total=max(args.max_total, 1),
        delay_min=max(args.delay_min, 0),
        delay_max=max(args.delay_max, args.delay_min),
        timeout_ms=max(args.timeout_ms, 5000),
        scrape_intermediate=not args.leaf_only,
        discover_from_search=not args.no_search_bootstrap,
        headed=args.headed,
        slow_mo=max(args.slow_mo, 0),
        save_debug_html=args.save_debug_html,
        browser_executable=args.browser_executable,
        user_agent=args.user_agent,
        append=args.append,
    )

    tree = make_tree(config)
    existing_product_keys = load_existing_product_keys(PRODUCTS_JSONL) if config.append else set()
    state = State(tree=tree, seen_products=existing_product_keys)
    scraper = FlipkartKidsScraper(config, state)
    atomic_write_json(CATEGORIES_JSON, tree)

    if config.append:
        log(f"Append mode: skipping {len(existing_product_keys)} existing product keys")

    async with async_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {
            "headless": not config.headed,
            "slow_mo": config.slow_mo,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        browser_executable = config.browser_executable or find_system_browser()
        if browser_executable:
            launch_kwargs["executable_path"] = browser_executable
            if not config.browser_executable:
                log(f"Using system browser: {browser_executable}")

        try:
            browser: Browser = await playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if browser_executable:
                raise
            raise SystemExit(
                "Could not launch Playwright Chromium and no system Chrome/Chromium was found.\n\n"
                "On Ubuntu 26.04, install Chromium/Chrome or pass the executable path, for example:\n\n"
                "  uv run src/flipkart_scraper.py --browser-executable /usr/bin/chromium-browser\n\n"
                f"Original error: {exc}"
            ) from exc
        context_kwargs: dict[str, Any] = {
            "user_agent": config.user_agent,
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
            "viewport": {"width": 1366, "height": 900},
        }
        if BROWSER_STATE.exists():
            context_kwargs["storage_state"] = str(BROWSER_STATE)
            log(f"Loaded saved browser state: {BROWSER_STATE}")

        context: BrowserContext = await browser.new_context(**context_kwargs)
        page: Page = await context.new_page()

        try:
            roots = await scraper.discover_roots(page)
            tree["roots"] = roots
            scraper.flush("roots_discovered")
            log(f"Root categories: {len(roots)}")

            for root in roots:
                if state.products_saved >= config.max_total:
                    break
                await scraper.crawl_category(page, root, depth=1)
        finally:
            BASE_DIR.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(BROWSER_STATE))
            await context.close()
            await browser.close()

    final_status = "finished"
    if state.products_saved == 0 and state.pages_blocked > 0:
        final_status = "finished_blocked_zero_products"
    elif state.products_saved == 0:
        final_status = "finished_zero_products"

    tree["finished_at"] = now_iso()
    scraper.flush(final_status)
    log(f"Done. Categories: {state.categories_found}, Saved: {state.products_saved}, Rejected: {state.products_rejected}")

    if final_status == "finished_blocked_zero_products" and not config.headed:
        log("Flipkart likely showed bot protection. Try again with --headed and solve any challenge once.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
