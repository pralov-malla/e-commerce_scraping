from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    import requests
    from bs4 import BeautifulSoup
    from bs4.element import Tag
except ImportError as exc:
    print(
        "Missing dependencies. Install them with:\n\n"
        "  pip install requests beautifulsoup4 lxml\n",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


DEFAULT_MARKETPLACE = "https://www.amazon.com"
DEFAULT_SEED_NAME = "Toys & Games"
DEFAULT_SEED_URL = "https://www.amazon.com/toys-games/b?node=165793011"


CONSUMABLE_OR_SHORT_LIVED_TERMS = {
    "baby food",
    "body wash",
    "candy",
    "cereal",
    "diaper",
    "diapers",
    "drink mix",
    "formula",
    "fruit pouch",
    "grocery",
    "gummies",
    "juice",
    "lotion",
    "milk",
    "multivitamin",
    "puree",
    "shampoo",
    "snack",
    "snacks",
    "soap",
    "supplement",
    "toothpaste",
    "vitamin",
    "wipes",
    "yogurt",
}

DURABLE_CONTEXT_TERMS = {
    "activity book",
    "blocks",
    "board game",
    "book",
    "cards",
    "craft",
    "educational",
    "game",
    "kit",
    "learning",
    "play",
    "pretend",
    "puzzle",
    "set",
    "stem",
    "toy",
    "toys",
}

CURRENCY_SYMBOLS = {
    "US$": "USD",
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "₹": "INR",
    "¥": "JPY",
    "C$": "CAD",
    "A$": "AUD",
}

TRACKING_QUERY_PREFIXES = ("pd_rd_", "pf_rd_", "tag")
TRACKING_QUERY_KEYS = {
    "content-id",
    "crid",
    "dib",
    "dib_tag",
    "qid",
    "ref",
    "rnid",
    "sprefix",
    "sr",
    "th",
}

BAD_CATEGORY_TEXT_EXACT = {
    "",
    "all",
    "back to top",
    "customer reviews",
    "departments",
    "featured categories",
    "new releases",
    "see all",
    "see more",
    "shop all",
    "show more",
    "sponsored",
    "today's deals",
}

BAD_CATEGORY_TEXT_CONTAINS = (
    " & up",
    " stars",
    " star",
    "avg. customer review",
    "availability",
    "brand",
    "condition",
    "customer review",
    "delivery",
    "discount",
    "eligible for free shipping",
    "free shipping",
    "from our brands",
    "international shipping",
    "packaging option",
    "price",
    "ratings",
    "seller",
    "sort by",
)

CATEGORY_LINK_SELECTORS = [
    "#departments a",
    "#s-refinements a",
    "#leftNav a",
    "#wayfinding-breadcrumbs_container a",
    "a[href*='/b?node=']",
    "a[href*='rh=n%3A']",
    "a[href*='rh=n:']",
]


@dataclass(frozen=True)
class ScraperConfig:
    marketplace: str
    output_path: Path
    max_category_depth: int
    max_categories_per_node: int
    max_pages: int
    max_products_per_category: int
    delay_min: float
    delay_max: float
    timeout: float
    fetch_details: bool
    skip_sponsored: bool
    scrape_intermediate_products: bool
    user_agent: str


@dataclass
class ScrapeState:
    root: dict[str, Any]
    output_path: Path
    seen_asins: set[str]
    seen_category_urls: set[str]
    request_count: int = 0
    products_saved: int = 0


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").split())


def normalize_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)

    clean_query = []

    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.startswith(TRACKING_QUERY_PREFIXES):
            continue

        if key in TRACKING_QUERY_KEYS:
            continue

        clean_query.append((key, value))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(clean_query, doseq=True),
            "",
        )
    )


def same_marketplace(marketplace: str, url: str) -> bool:
    marketplace_host = urlsplit(marketplace).netloc.lower()
    url_host = urlsplit(url).netloc.lower()

    return url_host == marketplace_host or url_host.endswith("." + marketplace_host)


def canonical_product_url(marketplace: str, asin: str) -> str:
    return urljoin(marketplace.rstrip("/") + "/", f"dp/{asin}")


def parse_price(
    value: str | None,
    default_currency: str = "USD",
) -> tuple[float | None, str]:
    text = normalize_text(value)

    if not text:
        return None, default_currency

    currency = default_currency

    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            currency = code
            break

    match = re.search(r"([0-9][0-9,]*\.?[0-9]*)", text)

    if not match:
        return None, currency

    try:
        return float(match.group(1).replace(",", "")), currency
    except ValueError:
        return None, currency


def parse_rating(value: str | None) -> float | None:
    text = normalize_text(value)

    if not text:
        return None

    match = re.search(r"([0-5](?:\.[0-9])?)\s*out of\s*5", text, flags=re.I)

    if not match:
        match = re.search(r"\b([0-5](?:\.[0-9])?)\b", text)

    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    text = normalize_text(value)

    if not text:
        return None

    match = re.search(r"([0-9][0-9,]*)", text)

    if not match:
        return None

    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def clean_brand(value: str | None) -> str | None:
    text = normalize_text(value)

    if not text:
        return None

    text = re.sub(r"^Brand:\s*", "", text, flags=re.I)
    text = re.sub(r"^Brand\s+", "", text, flags=re.I)
    text = re.sub(r"^Visit the\s+", "", text, flags=re.I)
    text = re.sub(r"\s+Store$", "", text, flags=re.I)

    return normalize_text(text).strip(": -") or None


def contains_term(text: str, terms: set[str]) -> bool:
    lower = text.lower()

    for term in terms:
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"s?\b"

        if re.search(pattern, lower):
            return True

    return False


def is_probably_durable_kids_product(title: str, category_path: list[str]) -> bool:
    title_text = title.lower()
    combined = f"{title} {' '.join(category_path)}".lower()

    if not contains_term(combined, CONSUMABLE_OR_SHORT_LIVED_TERMS):
        return True

    return contains_term(title_text, DURABLE_CONTEXT_TERMS)


def looks_like_block_page(soup: BeautifulSoup) -> bool:
    title = normalize_text(
        soup.title.get_text(" ", strip=True) if soup.title else ""
    ).lower()

    text = soup.get_text(" ", strip=True).lower()

    return (
        "robot check" in title
        or "enter the characters you see below" in text
        or "sorry, we just need to make sure you're not a robot" in text
    )


def save_json_snapshot(state: ScrapeState) -> None:
    state.output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = state.output_path.with_suffix(state.output_path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(state.root, file, ensure_ascii=False, indent=2)
        file.write("\n")

    temp_path.replace(state.output_path)


def append_product_jsonl(state: ScrapeState, product: dict[str, Any]) -> None:
    jsonl_path = state.output_path.with_suffix(".jsonl")
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    with jsonl_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(product, ensure_ascii=False))
        file.write("\n")
        file.flush()


def is_probably_category_url(marketplace: str, url: str) -> bool:
    if not same_marketplace(marketplace, url):
        return False

    parts = urlsplit(url)
    path = parts.path.lower()
    query_items = dict(parse_qsl(parts.query, keep_blank_values=True))

    if "/dp/" in path or "/gp/product/" in path:
        return False

    if path.endswith("/b") or path.endswith("/b/") or "/b/" in path:
        return "node" in query_items

    if path.startswith("/s") or path == "/s":
        return any(key in query_items for key in {"bbn", "rh", "node", "k", "i"})

    if "node=" in parts.query:
        return True

    return False


def is_good_category_label(label: str, current_path: list[str]) -> bool:
    text = normalize_text(label)
    lower = text.lower()

    if lower in BAD_CATEGORY_TEXT_EXACT:
        return False

    if any(piece in lower for piece in BAD_CATEGORY_TEXT_CONTAINS):
        return False

    if len(text) < 3 or len(text) > 80:
        return False

    if text.startswith("$") or "$" in text:
        return False

    if re.fullmatch(r"[\d,\s]+", text):
        return False

    if re.search(r"\b\d+\s*-\s*\d+\b", lower):
        return False

    if lower in {normalize_text(x).lower() for x in current_path}:
        return False

    return True


def extract_category_links(
    soup: BeautifulSoup,
    current_url: str,
    marketplace: str,
    current_path: list[str],
    limit: int,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()

    for selector in CATEGORY_LINK_SELECTORS:
        for element in soup.select(selector):
            if not isinstance(element, Tag):
                continue

            href = element.get("href")

            if not isinstance(href, str) or not href:
                continue

            name = normalize_text(element.get_text(" ", strip=True))

            if not is_good_category_label(name, current_path):
                continue

            url = normalize_url(current_url, href)

            if not is_probably_category_url(marketplace, url):
                continue

            name_key = name.lower()

            if url in seen_urls or name_key in seen_names:
                continue

            seen_urls.add(url)
            seen_names.add(name_key)

            links.append(
                {
                    "name": name,
                    "url": url,
                }
            )

            if len(links) >= limit:
                return links

    return links


class AmazonNaturalFlowScraper:
    def __init__(self, config: ScraperConfig, state: ScrapeState) -> None:
        self.config = config
        self.state = state
        self.session = requests.Session()

        self.headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "User-Agent": config.user_agent,
        }

    def delay(self) -> None:
        if self.state.request_count == 0:
            return

        seconds = random.uniform(self.config.delay_min, self.config.delay_max)
        log(f"Sleeping {seconds:.1f}s")
        time.sleep(seconds)

    def fetch_soup(self, url: str, reason: str) -> BeautifulSoup | None:
        self.delay()

        self.state.request_count += 1
        log(f"Request #{self.state.request_count}: {reason}: {url}")

        try:
            response = self.session.get(
                url,
                headers=self.headers,
                timeout=self.config.timeout,
            )
        except requests.RequestException as exc:
            log(f"Request failed: {exc}")
            return None

        log(f"Response {response.status_code}: {response.url}")

        if response.status_code in {429, 503}:
            log("Throttling or robot-check likely. This scraper will not bypass it.")
            return None

        if not response.ok:
            return None

        soup = BeautifulSoup(response.text, "lxml")

        if looks_like_block_page(soup):
            log("Robot-check page detected. Stopping this page.")
            return None

        return soup

    def discover_and_scrape(
        self,
        node: dict[str, Any],
        url: str,
        category_path: list[str],
        depth: int,
    ) -> None:
        clean_url = normalize_url(self.config.marketplace, url)
        node["url"] = clean_url

        if clean_url in self.state.seen_category_urls:
            log(f"Skipping repeated category URL: {clean_url}")
            node.setdefault("products", [])
            return

        self.state.seen_category_urls.add(clean_url)

        soup = self.fetch_soup(clean_url, f"discover {' > '.join(category_path)}")

        if soup is None:
            node.setdefault("products", [])
            save_json_snapshot(self.state)
            return

        product_cards = soup.select("[data-component-type='s-search-result']")

        child_links = extract_category_links(
            soup=soup,
            current_url=clean_url,
            marketplace=self.config.marketplace,
            current_path=category_path,
            limit=self.config.max_categories_per_node,
        )

        log(
            f"Discovered {len(child_links)} child categories and "
            f"{len(product_cards)} product cards in {' > '.join(category_path)}"
        )

        should_recurse = bool(child_links) and depth < self.config.max_category_depth

        if self.config.scrape_intermediate_products and product_cards:
            node.setdefault("products", [])

            self.scrape_products_for_leaf(
                url=clean_url,
                category_path=category_path,
                products=node["products"],
                first_soup=soup,
            )

            save_json_snapshot(self.state)

        if should_recurse:
            node.setdefault("subcategories", [])

            for child in child_links:
                child_node: dict[str, Any] = {
                    "name": child["name"],
                    "url": child["url"],
                }

                node["subcategories"].append(child_node)
                save_json_snapshot(self.state)

                self.discover_and_scrape(
                    node=child_node,
                    url=child["url"],
                    category_path=[*category_path, child["name"]],
                    depth=depth + 1,
                )

                save_json_snapshot(self.state)

            if not node.get("subcategories") and not node.get("products"):
                node.setdefault("products", [])

                self.scrape_products_for_leaf(
                    url=clean_url,
                    category_path=category_path,
                    products=node["products"],
                    first_soup=soup,
                )

                save_json_snapshot(self.state)

            return

        node.setdefault("products", [])

        self.scrape_products_for_leaf(
            url=clean_url,
            category_path=category_path,
            products=node["products"],
            first_soup=soup,
        )

        save_json_snapshot(self.state)

    def scrape_products_for_leaf(
        self,
        url: str,
        category_path: list[str],
        products: list[dict[str, Any]],
        first_soup: BeautifulSoup | None = None,
    ) -> None:
        current_url: str | None = url
        page = 1

        while current_url and page <= self.config.max_pages:
            if page == 1 and first_soup is not None:
                soup = first_soup
            else:
                soup = self.fetch_soup(
                    current_url,
                    f"{' > '.join(category_path)} products page {page}",
                )

            if soup is None:
                break

            candidates = self.extract_products_from_search_page(
                soup,
                category_path,
                current_url,
            )

            for product in candidates:
                if product["asin"] in self.state.seen_asins:
                    continue

                if not is_probably_durable_kids_product(
                    product["title"],
                    category_path,
                ):
                    log(f"Filtered consumable/short-lived item: {product['title']}")
                    continue

                self.state.seen_asins.add(product["asin"])

                if self.config.fetch_details:
                    self.enrich_from_detail_page(product)

                products.append(product)
                self.state.products_saved += 1

                append_product_jsonl(self.state, product)

                log(
                    f"Saved product #{self.state.products_saved}: "
                    f"{product['title']} ({product['asin']})"
                )

                save_json_snapshot(self.state)

                if len(products) >= self.config.max_products_per_category:
                    log(f"Reached category product limit: {len(products)}")
                    return

            current_url = self.extract_next_page_url(soup, current_url)
            page += 1

    def extract_products_from_search_page(
        self,
        soup: BeautifulSoup,
        category_path: list[str],
        category_url: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cards = soup.select("[data-component-type='s-search-result']")

        for card in cards:
            if not isinstance(card, Tag):
                continue

            if self.config.skip_sponsored and self.is_sponsored(card):
                continue

            asin = normalize_text(str(card.get("data-asin") or ""))

            if not asin:
                asin = self.extract_asin_from_card_link(card)

            if not asin:
                continue

            title = self.extract_title(card)

            if not title:
                continue

            price_text = self.text_from_first(
                card,
                [
                    ".a-price .a-offscreen",
                    ".a-color-price",
                ],
            )

            price, currency = parse_price(price_text)

            rating = parse_rating(
                self.text_from_first(
                    card,
                    [
                        "i.a-icon-star-small span.a-icon-alt",
                        "i.a-icon-star span.a-icon-alt",
                        "span.a-icon-alt",
                        "[aria-label*='out of 5 stars']",
                    ],
                )
            )

            reviews_count = parse_int(
                self.text_from_first(
                    card,
                    [
                        "a[href*='customerReviews'] span",
                        "a[href*='#customerReviews'] span",
                        "span[aria-label*='ratings']",
                        "span[aria-label*='rating']",
                    ],
                )
            )

            image_url = self.attr_from_first(card, ["img.s-image", "img"], "src")
            brand = self.extract_brand_from_search_card(card, title)

            results.append(
                {
                    "title": title,
                    "url": canonical_product_url(self.config.marketplace, asin),
                    "asin": asin,
                    "brand": brand,
                    "price": price,
                    "currency": currency,
                    "rating": rating,
                    "reviews_count": reviews_count,
                    "image_url": image_url,
                    "availability": None,
                    "category_path": category_path,
                    "category_url": category_url,
                    "scraped_at": now_iso(),
                }
            )

        log(f"Found {len(results)} product candidates in {' > '.join(category_path)}")

        return results

    def enrich_from_detail_page(self, product: dict[str, Any]) -> None:
        soup = self.fetch_soup(product["url"], f"detail page {product['asin']}")

        if soup is None:
            return

        detail_title = self.text_from_first(soup, ["#productTitle"])

        if detail_title:
            product["title"] = detail_title

        brand = self.extract_brand_from_detail_page(soup)

        if brand:
            product["brand"] = brand

        price_text = self.text_from_first(
            soup,
            [
                "#corePrice_feature_div .a-offscreen",
                ".apexPriceToPay .a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                "#priceblock_saleprice",
            ],
        )

        price, currency = parse_price(price_text, product.get("currency") or "USD")

        if price is not None:
            product["price"] = price
            product["currency"] = currency

        rating = parse_rating(
            self.text_from_first(
                soup,
                [
                    "#acrPopover",
                    "i[data-hook='average-star-rating'] span",
                    "span[data-hook='rating-out-of-text']",
                ],
            )
        )

        if rating is not None:
            product["rating"] = rating

        reviews_count = parse_int(
            self.text_from_first(
                soup,
                [
                    "#acrCustomerReviewText",
                    "span[data-hook='total-review-count']",
                ],
            )
        )

        if reviews_count is not None:
            product["reviews_count"] = reviews_count

        image_url = self.attr_from_first(
            soup,
            [
                "#landingImage",
                "#imgTagWrapperId img",
            ],
            "src",
        )

        if image_url:
            product["image_url"] = image_url

        availability = self.text_from_first(
            soup,
            [
                "#availability span",
                "#availability",
                "#outOfStock",
            ],
        )

        if availability:
            product["availability"] = availability

    def extract_next_page_url(
        self,
        soup: BeautifulSoup,
        current_url: str,
    ) -> str | None:
        selectors = [
            "a.s-pagination-next:not(.s-pagination-disabled)",
            "li.a-last a",
        ]

        for selector in selectors:
            link = soup.select_one(selector)

            if not isinstance(link, Tag):
                continue

            href = link.get("href")

            if isinstance(href, str) and href:
                return normalize_url(current_url, href)

        return None

    def extract_title(self, card: Tag) -> str | None:
        return self.text_from_first(
            card,
            [
                "h2 a span",
                "h2 span",
                "[data-cy='title-recipe'] span",
                ".a-size-medium.a-color-base.a-text-normal",
                ".a-size-base-plus.a-color-base.a-text-normal",
            ],
        )

    def extract_asin_from_card_link(self, card: Tag) -> str | None:
        link = card.select_one("a[href*='/dp/'], a[href*='/gp/product/']")

        if not isinstance(link, Tag):
            return None

        href = link.get("href")

        if not isinstance(href, str):
            return None

        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)

        return match.group(1) if match else None

    def extract_brand_from_search_card(self, card: Tag, title: str) -> str | None:
        explicit = self.text_from_first(
            card,
            [
                ".s-line-clamp-1 .a-size-base-plus",
                ".s-line-clamp-1 .a-size-base",
                "h5 .a-size-base",
            ],
        )

        if explicit and explicit.lower() not in title.lower():
            return clean_brand(explicit)

        first_word = title.split()[0].strip(":-,") if title.split() else ""

        if first_word.isupper() and 2 <= len(first_word) <= 12:
            return first_word

        return None

    def extract_brand_from_detail_page(self, soup: BeautifulSoup) -> str | None:
        byline = clean_brand(self.text_from_first(soup, ["#bylineInfo"]))

        if byline:
            return byline

        for row in soup.select("tr"):
            if not isinstance(row, Tag):
                continue

            heading_el = row.select_one("th")
            value_el = row.select_one("td")

            heading = (
                normalize_text(heading_el.get_text(" ", strip=True))
                if heading_el
                else ""
            )

            if heading.lower() == "brand" and value_el:
                return clean_brand(value_el.get_text(" ", strip=True))

        return None

    def is_sponsored(self, card: Tag) -> bool:
        text = normalize_text(card.get_text(" ", strip=True)).lower()

        return "sponsored" in text[:300]

    @staticmethod
    def text_from_first(root: Tag | BeautifulSoup, selectors: list[str]) -> str | None:
        for selector in selectors:
            element = root.select_one(selector)

            if element:
                text = normalize_text(element.get_text(" ", strip=True))

                if text:
                    return text

        return None

    @staticmethod
    def attr_from_first(
        root: Tag | BeautifulSoup,
        selectors: list[str],
        attr: str,
    ) -> str | None:
        for selector in selectors:
            element = root.select_one(selector)

            if not element:
                continue

            value = element.get(attr)

            if isinstance(value, str) and value:
                return value

        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Discover Amazon category schema from site navigation, then scrape "
            "durable kids products from discovered leaf categories."
        )
    )

    parser.add_argument("--marketplace", default=DEFAULT_MARKETPLACE)
    parser.add_argument("--seed-name", default=DEFAULT_SEED_NAME)
    parser.add_argument("--seed-url", default=DEFAULT_SEED_URL)

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/amazon_natural_kids_products.json"),
    )

    parser.add_argument(
        "--max-category-depth",
        type=int,
        default=3,
        help="How many category levels to discover below the seed page.",
    )

    parser.add_argument(
        "--max-categories-per-node",
        type=int,
        default=20,
        help="Maximum child category links to follow per category page.",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        help="Product-result pages per leaf category.",
    )

    parser.add_argument("--max-products-per-category", type=int, default=30)
    parser.add_argument("--delay-min", type=float, default=4.0)
    parser.add_argument("--delay-max", type=float, default=9.0)
    parser.add_argument("--timeout", type=float, default=25.0)

    parser.add_argument(
        "--details",
        action="store_true",
        help="Also visit product detail pages. Slower and more likely to be blocked.",
    )

    parser.add_argument(
        "--include-sponsored",
        action="store_true",
        help="Include sponsored search results.",
    )

    parser.add_argument(
        "--scrape-intermediate-products",
        action="store_true",
        help=(
            "Also scrape product cards from category pages that have child categories. "
            "Default behavior only scrapes leaf categories."
        ),
    )

    parser.add_argument(
        "--append-jsonl",
        action="store_true",
        help="Append to existing JSONL sidecar instead of deleting it at start.",
    )

    parser.add_argument(
        "--user-agent",
        default=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    marketplace = args.marketplace.rstrip("/")
    seed_url = normalize_url(marketplace, args.seed_url)
    output_path = args.output

    jsonl_path = output_path.with_suffix(".jsonl")

    if jsonl_path.exists() and not args.append_jsonl:
        jsonl_path.unlink()

    root = {
        "category": args.seed_name,
        "name": args.seed_name,
        "marketplace": marketplace,
        "seed_url": seed_url,
        "url": seed_url,
        "schema_source": "discovered_from_site_navigation",
        "scraped_at": now_iso(),
    }

    state = ScrapeState(
        root=root,
        output_path=output_path,
        seen_asins=set(),
        seen_category_urls=set(),
    )

    config = ScraperConfig(
        marketplace=marketplace,
        output_path=output_path,
        max_category_depth=max(args.max_category_depth, 0),
        max_categories_per_node=max(args.max_categories_per_node, 1),
        max_pages=max(args.max_pages, 1),
        max_products_per_category=max(args.max_products_per_category, 1),
        delay_min=max(args.delay_min, 0.0),
        delay_max=max(args.delay_max, args.delay_min, 0.0),
        timeout=max(args.timeout, 1.0),
        fetch_details=args.details,
        skip_sponsored=not args.include_sponsored,
        scrape_intermediate_products=args.scrape_intermediate_products,
        user_agent=args.user_agent,
    )

    log("Starting natural-flow scrape")
    log(f"Marketplace: {config.marketplace}")
    log(f"Seed: {args.seed_name} -> {seed_url}")
    log(f"Output nested JSON: {config.output_path}")
    log(f"Output live JSONL: {config.output_path.with_suffix('.jsonl')}")
    log(f"Max category depth: {config.max_category_depth}")
    log(f"Max categories per node: {config.max_categories_per_node}")
    log(f"Fetch details: {config.fetch_details}")

    save_json_snapshot(state)

    scraper = AmazonNaturalFlowScraper(config, state)

    scraper.discover_and_scrape(
        node=state.root,
        url=seed_url,
        category_path=[args.seed_name],
        depth=0,
    )

    state.root["finished_at"] = now_iso()
    state.root["products_saved"] = state.products_saved
    state.root["requests"] = state.request_count

    save_json_snapshot(state)

    log(
        f"Done. Products saved: {state.products_saved}. "
        f"Requests: {state.request_count}."
    )


if __name__ == "__main__":
    main()