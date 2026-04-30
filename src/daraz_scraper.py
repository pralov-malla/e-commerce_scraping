from __future__ import annotations

"""
Daraz Nepal kids/baby scraper
=============================

Purpose:
  Discover kids/baby related Daraz categories and collect products into one
  readable JSON file.

Output:
  data/daraz/daraz_kids_products.json

Daraz listing pages usually embed product data in a JavaScript assignment named
window.pageData. This scraper reads that structured payload first, then falls
back to DOM product cards if the payload is missing.

Run:
  uv run src/daraz_scraper.py
"""

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    import requests
    from bs4 import BeautifulSoup, Tag
except ImportError as exc:
    print(
        "Missing dependencies. Run:\n\n"
        "  uv add requests beautifulsoup4 lxml\n",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
except ImportError:
    Browser = BrowserContext = Page = Any  # type: ignore[misc, assignment]
    sync_playwright = None  # type: ignore[assignment]


BASE_DIR = Path("data/daraz")
DEFAULT_OUTPUT = BASE_DIR / "daraz_kids_products.json"
DEFAULT_MARKETPLACE = "https://www.daraz.com.np"
DEFAULT_CATEGORY_INDEX = "https://pages.daraz.com.np/wow/i/np/landingpage/category?wh_weex=true"


# Used only as fallback breadth. The scraper still discovers Daraz category links
# from the category index and listing pages instead of relying on one fixed URL.
FALLBACK_CATEGORY_TREE: list[dict[str, Any]] = [
    {
        "name": "Mother & Baby",
        "query": "mother baby",
        "subcategories": [
            {
                "name": "Baby Gear",
                "query": "baby gear",
                "subcategories": [
                    {"name": "Strollers & Prams", "query": "baby stroller pram"},
                    {"name": "Baby Car Seats", "query": "baby car seat"},
                    {"name": "Baby Carriers", "query": "baby carrier"},
                    {"name": "Walkers", "query": "baby walker"},
                    {"name": "Bouncers & Jumpers", "query": "baby bouncer jumper"},
                ],
            },
            {
                "name": "Nursery",
                "query": "nursery baby",
                "subcategories": [
                    {"name": "Cribs & Cradles", "query": "baby crib cradle"},
                    {"name": "Baby Bedding", "query": "baby bedding blanket"},
                    {"name": "Baby Pillows", "query": "baby pillow"},
                ],
            },
            {
                "name": "Baby Clothing",
                "query": "baby clothes",
                "subcategories": [
                    {"name": "Newborn Clothing", "query": "newborn clothes"},
                    {"name": "Baby Shoes", "query": "baby shoes"},
                    {"name": "Baby Bibs", "query": "baby bib"},
                ],
            },
        ],
    },
    {
        "name": "Kids Toys",
        "query": "kids toys",
        "subcategories": [
            {"name": "Learning & Education", "query": "kids learning educational toys"},
            {"name": "Blocks & Building Toys", "query": "kids blocks building toys"},
            {"name": "Dolls & Accessories", "query": "kids doll accessories"},
            {"name": "Puzzle", "query": "kids puzzle"},
            {"name": "Soft Toys", "query": "kids soft toys"},
            {"name": "Pretend Play", "query": "kids pretend play toys"},
        ],
    },
    {
        "name": "Baby & Toddler Toys",
        "query": "baby toddler toys",
        "subcategories": [
            {"name": "Baby Rattles", "query": "baby rattle"},
            {"name": "Teethers", "query": "baby teether"},
            {"name": "Activity Gym & Playmats", "query": "baby playmat activity gym"},
            {"name": "Bath Toys", "query": "baby bath toys"},
            {"name": "Kids Tricycles", "query": "kids tricycle"},
        ],
    },
    {
        "name": "Kids Fashion",
        "query": "kids clothing shoes",
        "subcategories": [
            {"name": "Girls Clothing", "query": "girls clothing"},
            {"name": "Boys Clothing", "query": "boys clothing"},
            {"name": "Kids Shoes", "query": "kids shoes"},
            {"name": "Kids Bags", "query": "kids bag school bag"},
        ],
    },
]

KIDS_CATEGORY_TERMS = {
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
    "mother",
    "newborn",
    "nursery",
    "toddler",
    "toy",
    "toys",
}

KIDS_PRODUCT_TERMS = (KIDS_CATEGORY_TERMS - {"mother"}) | {
    "action figure",
    "activity",
    "blocks",
    "bouncer",
    "car seat",
    "crib",
    "cradle",
    "diaper bag",
    "doll",
    "feeding bottle",
    "high chair",
    "learning",
    "playmat",
    "pretend play",
    "pram",
    "puzzle",
    "rattle",
    "romper",
    "school bag",
    "stroller",
    "swaddle",
    "teether",
    "tricycle",
    "walker",
}

SAFE_CHILD_CATEGORY_TERMS = {
    "accessories",
    "activity",
    "arts",
    "backpack",
    "bag",
    "bags",
    "bathing",
    "bedding",
    "blanket",
    "blocks",
    "bottle",
    "bouncers",
    "car seat",
    "car seats",
    "carriers",
    "clothing",
    "cradles",
    "cribs",
    "doll",
    "education",
    "educational",
    "feeding",
    "gear",
    "high chairs",
    "learning",
    "play",
    "playards",
    "playmats",
    "playpens",
    "prams",
    "puzzle",
    "school bags",
    "shoes",
    "soft toys",
    "strollers",
    "tricycles",
    "walkers",
}

CONSUMABLE_TERMS = {
    "baby food",
    "biscuit",
    "body wash",
    "cereal",
    "formula",
    "grocery",
    "gummies",
    "lotion",
    "milk",
    "oil",
    "powder",
    "puree",
    "shampoo",
    "snack",
    "soap",
    "toothpaste",
    "vitamin",
    "wipes",
    "cream",
    "detergent",
    "laundry",
    "liquid",
}

DURABLE_CONSUMABLE_EXCEPTIONS = {
    "diaper bag",
    "wipe warmer",
    "bottle warmer",
}

FORBIDDEN_TITLE_TERMS = {
    "adult",
    "condom",
    "durex",
    "for men",
    "for women",
    "lingerie",
    "maternity belt",
    "feminine",
    "momwasher",
    "nail",
    "nails",
    "perineal",
    "postpartum",
    "mens",
    "men's",
    "perfume",
    "pet",
    "pets",
    "sexy",
    "toenail",
    "toenails",
    "women's",
    "womens",
}

BAD_CATEGORY_EXACT = {
    "",
    "all",
    "brand",
    "brands",
    "categories",
    "customer care",
    "daraz",
    "delivery",
    "discount",
    "filter",
    "free shipping",
    "login",
    "price",
    "ratings",
    "seller",
    "shipping",
    "shop all",
    "sort by",
}

BAD_CATEGORY_CONTAINS = (
    " & up",
    "% off",
    "app",
    "become a seller",
    "cash on delivery",
    "customer",
    "download",
    "help",
    "less than",
    "more than",
    "payment",
    "privacy",
    "rating",
    "review",
    "returns",
    "seller",
    "sign up",
    "terms",
)

KEEP_QUERY_KEYS = {
    "q",
    "page",
    "from",
    "sort",
    "price",
    "rating",
    "location",
    "categoryId",
    "spm",
}


@dataclass(frozen=True)
class ScraperConfig:
    marketplace: str
    category_index_url: str
    output_path: Path
    target_products: int
    max_depth: int
    max_categories_per_node: int
    max_pages_per_category: int
    delay_min: float
    delay_max: float
    timeout: float
    append: bool
    use_browser: bool
    browser_executable: str | None
    headed: bool
    slow_mo: int
    user_agent: str


@dataclass
class ScrapeState:
    categories_seen: set[str] = field(default_factory=set)
    products_seen: set[str] = field(default_factory=set)
    products: list[dict[str, Any]] = field(default_factory=list)
    request_count: int = 0
    pages_failed: int = 0


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").replace("\u200b", "").split()).strip()


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


def clean_label(label: str | None) -> str:
    text = normalize_text(label)
    text = re.sub(r"^shop\s+", "", text, flags=re.I)
    text = re.sub(r"\s+\(\d[\d,]*\)$", "", text)
    return normalize_text(text)


def clean_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None

    absolute = urljoin(base_url, href)
    if absolute.startswith("//"):
        absolute = "https:" + absolute

    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"}:
        return None

    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in KEEP_QUERY_KEYS:
            query.append((key, value))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def same_daraz_site(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return host == "daraz.com.np" or host.endswith(".daraz.com.np")


def search_url(marketplace: str, query: str, page: int = 1) -> str:
    params = {"q": query, "page": str(page)}
    return f"{marketplace.rstrip('/')}/catalog/?{urlencode(params)}"


def paged_url(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def category_key(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if query.get("q"):
        return f"search:{normalize_text(query['q']).lower()}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).lower()


def is_product_url(url: str | None) -> bool:
    if not url or not same_daraz_site(url):
        return False
    path = urlsplit(url).path.lower()
    return path.endswith(".html") and "-i" in path


def is_listing_url(url: str | None) -> bool:
    if not url or not same_daraz_site(url) or is_product_url(url):
        return False

    parts = urlsplit(url)
    path = parts.path.lower()
    query = dict(parse_qsl(parts.query, keep_blank_values=True))

    if any(piece in path for piece in ("/help", "/contact", "/cart", "/customer", "/privacy")):
        return False
    if path.startswith("/catalog"):
        return True
    if path.startswith("/tag/"):
        return True
    if path.startswith("/shop-"):
        return True
    if query.get("q") or query.get("categoryId"):
        return True
    return bool(path.strip("/") and not path.startswith("/wow/"))


def good_category_label(label: str, current_path: list[str]) -> bool:
    text = clean_label(label)
    lower = text.lower()

    if lower in BAD_CATEGORY_EXACT:
        return False
    if any(piece in lower for piece in BAD_CATEGORY_CONTAINS):
        return False
    if len(text) < 3 or len(text) > 90:
        return False
    if text.startswith("Rs.") or "$" in text:
        return False
    if re.fullmatch(r"[\d,\s]+", text):
        return False
    if lower in {part.lower() for part in current_path}:
        return False
    if has_substring(lower, FORBIDDEN_TITLE_TERMS):
        return False

    if has_term(lower, KIDS_CATEGORY_TERMS):
        return True

    parent_context = " ".join(current_path[1:]).lower()
    return bool(parent_context) and has_term(parent_context, KIDS_CATEGORY_TERMS) and has_term(
        lower,
        SAFE_CHILD_CATEGORY_TERMS,
    )


def product_is_allowed(title: str, category_path: list[str]) -> bool:
    title_clean = normalize_text(title)
    title_lower = title_clean.lower()
    if len(title_clean) < 5:
        return False
    if has_substring(title_lower, FORBIDDEN_TITLE_TERMS):
        return False
    if has_term(title_lower, CONSUMABLE_TERMS) and not has_term(title_lower, DURABLE_CONSUMABLE_EXCEPTIONS):
        return False

    return has_term(title_lower, KIDS_PRODUCT_TERMS)


def parse_price(value: Any) -> tuple[float | None, str | None]:
    if value is None:
        return None, None

    text = normalize_text(str(value))
    if not text:
        return None, None

    currency = "NPR" if "rs" in text.lower() or "रू" in text else None
    match = re.search(r"([0-9][0-9,]*\.?[0-9]*)", text)
    if not match:
        return None, currency

    try:
        return float(match.group(1).replace(",", "")), currency or "NPR"
    except ValueError:
        return None, currency


def parse_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    match = re.search(r"([0-9][0-9,]*)", str(value))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def text_from_first(root: Tag | BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        element = root.select_one(selector)
        if isinstance(element, Tag):
            text = normalize_text(element.get_text(" ", strip=True))
            if text:
                return text
    return None


def attr_from_first(root: Tag | BeautifulSoup, selectors: list[str], attr: str) -> str | None:
    for selector in selectors:
        element = root.select_one(selector)
        if isinstance(element, Tag):
            value = element.get(attr)
            if isinstance(value, str) and value:
                return value
    return None


def extract_balanced_json(html: str, marker: str) -> dict[str, Any] | None:
    marker_index = html.find(marker)
    if marker_index < 0:
        return None

    start = html.find("{", marker_index)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    quote = ""

    for index in range(start, len(html)):
        char = html[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw = html[start : index + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None

    return None


def find_list_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        list_items = value.get("listItems")
        if isinstance(list_items, list):
            return [item for item in list_items if isinstance(item, dict)]

        for nested in value.values():
            found = find_list_items(nested)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = find_list_items(item)
            if found:
                return found

    return []


def load_existing_products(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return []

    return [product for product in products if isinstance(product, dict)]


def detect_browser_executable() -> str | None:
    for candidate in (
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def seed_to_category(seed: dict[str, Any], marketplace: str, parent_path: list[str]) -> dict[str, Any]:
    name = clean_label(str(seed["name"]))
    query = normalize_text(str(seed.get("query") or name))
    path = [*parent_path, name]
    return {
        "name": name,
        "url": search_url(marketplace, query),
        "path": path,
        "subcategories": [
            seed_to_category(child, marketplace, path)
            for child in seed.get("subcategories", [])
            if isinstance(child, dict)
        ],
        "product_count": 0,
    }


class DarazKidsScraper:
    def __init__(self, config: ScraperConfig, state: ScrapeState) -> None:
        self.config = config
        self.state = state
        self.session = requests.Session()
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "User-Agent": config.user_agent,
        }
        self.playwright: Any | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.tree: dict[str, Any] = {
            "site": "Daraz Nepal",
            "marketplace": config.marketplace,
            "category_index_url": config.category_index_url,
            "started_at": now_iso(),
            "settings": {
                "target_products": config.target_products,
                "max_depth": config.max_depth,
                "max_categories_per_node": config.max_categories_per_node,
                "max_pages_per_category": config.max_pages_per_category,
                "use_browser": config.use_browser,
                "browser_executable": config.browser_executable,
            },
            "categories": [],
            "products": self.state.products,
            "summary": {},
        }

    def save(self, status: str) -> None:
        self.tree["status"] = status
        self.tree["last_saved_at"] = now_iso()
        self.tree["summary"] = {
            "products_saved": len(self.state.products),
            "categories_seen": len(self.state.categories_seen),
            "leaf_categories": len(self.tree.get("leaf_categories", [])),
            "requests": self.state.request_count,
            "pages_failed": self.state.pages_failed,
        }
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.config.output_path.with_suffix(self.config.output_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self.tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.config.output_path)

    def refresh_leaf_summary(self) -> list[dict[str, Any]]:
        leaves: list[dict[str, Any]] = []
        for root in self.tree["categories"]:
            leaves.extend(self.collect_leaf_categories(root))

        self.tree["leaf_categories"] = [
            {
                "name": str(leaf.get("name") or ""),
                "url": str(leaf.get("url") or ""),
                "path": [str(part) for part in leaf.get("path", [])],
                "product_count": int(leaf.get("product_count", 0) or 0),
            }
            for leaf in leaves
        ]
        self.tree["summary"]["leaf_categories"] = len(leaves)
        return leaves

    def delay(self) -> None:
        if self.state.request_count == 0:
            return
        seconds = random.uniform(self.config.delay_min, self.config.delay_max)
        log(f"Sleeping {seconds:.1f}s")
        time.sleep(seconds)

    def fetch_html(self, url: str, reason: str) -> str | None:
        self.delay()
        self.state.request_count += 1
        log(f"Request #{self.state.request_count}: {reason}: {url}")

        try:
            response = self.session.get(url, headers=self.headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            self.state.pages_failed += 1
            log(f"Request failed: {exc}")
            return None

        log(f"Response {response.status_code}: {response.url}")
        if not response.ok:
            self.state.pages_failed += 1
            return None

        text_lower = response.text[:5000].lower()
        if "captcha" in text_lower or "robot check" in text_lower or "unusual traffic" in text_lower:
            self.state.pages_failed += 1
            log("Possible block/captcha page detected.")
            return None

        return response.text

    def start_browser(self) -> None:
        if not self.config.use_browser:
            return
        if sync_playwright is None:
            log("Playwright is not installed; browser fallback is disabled.")
            return

        launch_options: dict[str, Any] = {
            "headless": not self.config.headed,
            "slow_mo": self.config.slow_mo,
        }
        executable = self.config.browser_executable or detect_browser_executable()
        if executable:
            launch_options["executable_path"] = executable
            log(f"Using browser executable: {executable}")

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(**launch_options)
        self.context = self.browser.new_context(
            user_agent=self.config.user_agent,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        self.page = self.context.new_page()

    def close_browser(self) -> None:
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def fetch_rendered_html(self, url: str, reason: str) -> str | None:
        if not self.page:
            return None

        self.delay()
        self.state.request_count += 1
        log(f"Browser request #{self.state.request_count}: {reason}: {url}")

        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=int(self.config.timeout * 1000))
            self.page.wait_for_timeout(2500)
            self.page.mouse.wheel(0, 1800)
            self.page.wait_for_timeout(1000)
            log(f"Browser response {response.status if response else 'unknown'}: {self.page.url}")
            return self.page.content()
        except Exception as exc:
            self.state.pages_failed += 1
            log(f"Browser request failed: {exc}")
            return None

    def discover_root_categories(self) -> list[dict[str, Any]]:
        roots: list[dict[str, Any]] = []

        html = self.fetch_html(self.config.category_index_url, "category index")
        if html:
            soup = BeautifulSoup(html, "lxml")
            roots.extend(
                self.extract_category_links(
                    soup=soup,
                    current_url=self.config.category_index_url,
                    current_path=["Daraz Kids"],
                    limit=self.config.max_categories_per_node,
                )
            )

        roots.extend(
            seed_to_category(seed, self.config.marketplace, ["Daraz Kids"])
            for seed in FALLBACK_CATEGORY_TREE
        )

        return self.unique_categories(roots)

    def unique_categories(self, categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for category in categories:
            name = clean_label(str(category.get("name") or ""))
            url = clean_url(self.config.marketplace, str(category.get("url") or ""))
            if not name or not url or not is_listing_url(url):
                continue
            key = category_key(url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(
                {
                    "name": name,
                    "url": url,
                    "subcategories": category.get("subcategories", []),
                    "product_count": int(category.get("product_count", 0) or 0),
                }
            )
        return unique

    def extract_category_links(
        self,
        soup: BeautifulSoup,
        current_url: str,
        current_path: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        for element in soup.select("a[href]"):
            if not isinstance(element, Tag):
                continue

            href = element.get("href")
            label = clean_label(element.get_text(" ", strip=True))
            if not label:
                img = element.select_one("img")
                if isinstance(img, Tag):
                    label = clean_label(str(img.get("alt") or img.get("title") or ""))

            if not good_category_label(label, current_path):
                continue

            url = clean_url(current_url, href if isinstance(href, str) else None)
            if not url or not is_listing_url(url):
                continue

            key = category_key(url)
            if key in seen:
                continue

            seen.add(key)
            found.append({"name": label, "url": url})
            if len(found) >= limit:
                break

        return found

    def discover_category_schema(self, node: dict[str, Any], depth: int) -> None:
        url = str(node["url"])
        key = category_key(url)
        path = [str(part) for part in node.get("path", [node["name"]])]

        if key in self.state.categories_seen:
            node["status"] = "skipped_duplicate"
            return

        self.state.categories_seen.add(key)
        node["status"] = "visiting"
        node.setdefault("subcategories", [])
        node.setdefault("product_count", 0)
        self.save("discovering_categories")

        html = self.fetch_html(url, f"category {' > '.join(path)}")
        if not html:
            node["status"] = "fetch_failed"
            self.save("running")
            return

        soup = BeautifulSoup(html, "lxml")
        child_links: list[dict[str, Any]] = []
        if depth < self.config.max_depth:
            child_links = self.extract_category_links(
                soup=soup,
                current_url=url,
                current_path=path,
                limit=self.config.max_categories_per_node,
            )

        existing_child_keys = {
            category_key(str(child["url"]))
            for child in node.get("subcategories", [])
            if child.get("url")
        }
        for child in child_links:
            child_key = category_key(str(child["url"]))
            if child_key in existing_child_keys:
                continue
            child_node = {
                "name": child["name"],
                "url": child["url"],
                "path": [*path, child["name"]],
                "subcategories": [],
                "product_count": 0,
            }
            node["subcategories"].append(child_node)
            existing_child_keys.add(child_key)

        node["discovered_subcategories"] = len(node["subcategories"])
        self.save("discovering_categories")

        for child_node in node["subcategories"]:
            self.discover_category_schema(child_node, depth + 1)

        node["is_leaf"] = len(node["subcategories"]) == 0
        node["status"] = "done"
        self.save("running")

    def collect_leaf_categories(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        subcategories = [
            child
            for child in node.get("subcategories", [])
            if isinstance(child, dict)
        ]
        if not subcategories:
            node["is_leaf"] = True
            return [node]

        node["is_leaf"] = False
        leaves: list[dict[str, Any]] = []
        for child in subcategories:
            leaves.extend(self.collect_leaf_categories(child))
        return leaves

    def scrape_products_for_category(self, node: dict[str, Any], first_html: str | None = None) -> None:
        path = [str(part) for part in node.get("path", [node["name"]])]
        base_url = str(node["url"])

        for page in range(1, self.config.max_pages_per_category + 1):
            if len(self.state.products) >= self.config.target_products:
                return

            url = paged_url(base_url, page)
            if self.page:
                html = self.fetch_rendered_html(url, f"products {' > '.join(path)} page {page}")
            else:
                html = first_html if page == 1 and first_html else self.fetch_html(
                    url,
                    f"products {' > '.join(path)} page {page}",
                )
            if not html:
                break

            products = self.extract_products(html, url, path)
            if not products:
                log(f"No product candidates in {' > '.join(path)} page {page}")
                if page == 1:
                    break
                continue

            saved_this_page = 0
            for product in products:
                if len(self.state.products) >= self.config.target_products:
                    return

                key = str(product.get("url") or product.get("name") or "")
                if not key or key in self.state.products_seen:
                    continue
                if not product_is_allowed(str(product.get("name") or ""), path):
                    continue

                self.state.products_seen.add(key)
                self.state.products.append(product)
                node["product_count"] = int(node.get("product_count", 0)) + 1
                saved_this_page += 1
                log(f"Saved product #{len(self.state.products)}: {product['name'][:100]}")

            self.save("scraping_products")

            if saved_this_page == 0 and page > 1:
                break

    def extract_products(self, html: str, source_url: str, category_path: list[str]) -> list[dict[str, Any]]:
        page_data = extract_balanced_json(html, "window.pageData")
        if page_data:
            items = find_list_items(page_data)
            products = [self.product_from_page_data_item(item, source_url, category_path) for item in items]
            products = [product for product in products if product]
            log(f"Found {len(products)} pageData product candidates in {' > '.join(category_path)}")
            return products

        soup = BeautifulSoup(html, "lxml")
        products = self.extract_products_from_dom(soup, source_url, category_path)
        log(f"Found {len(products)} DOM product candidates in {' > '.join(category_path)}")
        return products

    def product_from_page_data_item(
        self,
        item: dict[str, Any],
        source_url: str,
        category_path: list[str],
    ) -> dict[str, Any] | None:
        name = normalize_text(str(item.get("name") or item.get("title") or ""))
        if not name:
            return None

        raw_url = item.get("itemUrl") or item.get("productUrl") or item.get("url")
        url = clean_url(self.config.marketplace, str(raw_url or ""))
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url

        price, currency = parse_price(item.get("priceShow") or item.get("price"))
        original_price, _ = parse_price(item.get("originalPriceShow") or item.get("originalPrice"))
        rating = parse_float(item.get("ratingScore") or item.get("rating_score"))
        reviews = parse_int(item.get("review") or item.get("reviewCount"))
        sold = normalize_text(str(item.get("itemSoldCntShow") or item.get("sold") or "")) or None

        image = item.get("image") or item.get("imageUrl")
        image_url = str(image) if image else None
        if image_url and image_url.startswith("//"):
            image_url = "https:" + image_url

        return {
            "name": name,
            "url": url,
            "price": price,
            "original_price": original_price,
            "currency": currency or "NPR",
            "rating": rating,
            "reviews_count": reviews,
            "sold": sold,
            "image_url": image_url,
            "brand": normalize_text(str(item.get("brandName") or item.get("brand") or "")) or None,
            "seller": normalize_text(str(item.get("sellerName") or item.get("seller") or "")) or None,
            "location": normalize_text(str(item.get("location") or "")) or None,
            "category_path": category_path,
            "source_url": source_url,
            "scraped_at": now_iso(),
        }

    def extract_products_from_dom(
        self,
        soup: BeautifulSoup,
        source_url: str,
        category_path: list[str],
    ) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        cards = soup.select("[data-qa-locator='product-item'], div[data-item-id]")

        for card in cards:
            if not isinstance(card, Tag):
                continue

            link = card.select_one("a[href]")
            if not isinstance(link, Tag):
                continue

            url = clean_url(source_url, str(link.get("href") or ""))
            if not url:
                continue

            title = normalize_text(str(link.get("title") or link.get_text(" ", strip=True)))
            if not title:
                img = link.select_one("img")
                if isinstance(img, Tag):
                    title = normalize_text(str(img.get("alt") or ""))
            if not title:
                continue

            price_text = text_from_first(card, [".aBrP0 .ooOxS", ".aBrP0 span", "[class*='price']"])
            price, currency = parse_price(price_text)
            original_price_text = text_from_first(card, [".WNoq3 del", "del"])
            original_price, _ = parse_price(original_price_text)
            image = attr_from_first(card, ["img"], "src") or attr_from_first(card, ["img"], "data-src")
            location = text_from_first(card, [".oa6ri", "[title*='Province']"])

            products.append(
                {
                    "name": title,
                    "url": url,
                    "price": price,
                    "original_price": original_price,
                    "currency": currency or "NPR",
                    "rating": None,
                    "reviews_count": None,
                    "sold": None,
                    "image_url": str(image) if image else None,
                    "brand": None,
                    "seller": None,
                    "location": location,
                    "category_path": category_path,
                    "source_url": source_url,
                    "scraped_at": now_iso(),
                }
            )

        return products


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape kids/baby products from Daraz Nepal into one JSON file.")
    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--category-index-url", default=DEFAULT_CATEGORY_INDEX)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--target-products", type=int, default=150)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-categories-per-node", type=int, default=40)
    parser.add_argument("--max-pages-per-category", type=int, default=4)
    parser.add_argument("--delay-min", type=float, default=1.5)
    parser.add_argument("--delay-max", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--append", action="store_true", help="Keep existing products in the output JSON and add new ones.")
    parser.add_argument("--no-browser", action="store_true", help="Disable Playwright rendering fallback.")
    parser.add_argument("--browser-executable", default=None, help="Example: /snap/bin/chromium or /usr/bin/google-chrome")
    parser.add_argument("--headed", action="store_true", help="Open a visible browser for Daraz pages.")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config = ScraperConfig(
        marketplace=args.marketplace.rstrip("/"),
        category_index_url=args.category_index_url,
        output_path=args.output,
        target_products=max(args.target_products, 1),
        max_depth=max(args.max_depth, 0),
        max_categories_per_node=max(args.max_categories_per_node, 1),
        max_pages_per_category=max(args.max_pages_per_category, 1),
        delay_min=max(args.delay_min, 0.0),
        delay_max=max(args.delay_max, args.delay_min, 0.0),
        timeout=max(args.timeout, 1.0),
        append=args.append,
        use_browser=not args.no_browser,
        browser_executable=args.browser_executable,
        headed=args.headed,
        slow_mo=max(args.slow_mo, 0),
        user_agent=args.user_agent,
    )

    state = ScrapeState()
    if config.append:
        state.products = load_existing_products(config.output_path)
        state.products_seen = {str(product.get("url") or product.get("name") or "") for product in state.products}

    scraper = DarazKidsScraper(config, state)

    log("Starting Daraz kids/baby scraper")
    log(f"Output JSON: {config.output_path}")
    log(f"Target products: {config.target_products}")
    if config.append:
        log(f"Append mode: loaded {len(state.products)} existing products")

    try:
        scraper.start_browser()

        roots = scraper.discover_root_categories()
        scraper.tree["categories"] = []
        for root in roots:
            root.setdefault("path", ["Daraz Kids", root["name"]])
            root.setdefault("subcategories", [])
            root.setdefault("product_count", 0)
            scraper.tree["categories"].append(root)
        scraper.save("starting")

        for root_node in scraper.tree["categories"]:
            scraper.discover_category_schema(root_node, depth=1)

        leaf_categories = scraper.refresh_leaf_summary()
        scraper.save("schema_discovered")
        log(f"Schema discovered. Leaf categories: {len(leaf_categories)}")

        for leaf_node in leaf_categories:
            if len(state.products) >= config.target_products:
                break
            scraper.scrape_products_for_category(leaf_node)

        scraper.tree["finished_at"] = now_iso()
        scraper.refresh_leaf_summary()
        scraper.save("finished")
    finally:
        scraper.close_browser()

    log(
        f"Done. Products saved: {len(state.products)}. "
        f"Categories visited: {len(state.categories_seen)}. "
        f"Requests: {state.request_count}."
    )


if __name__ == "__main__":
    main()
