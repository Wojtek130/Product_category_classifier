
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time

from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ==================================================
# CONFIGURATION
# ==================================================

BASE_URL = "https://erli.pl"
CATEGORY_MAP_URL = f"{BASE_URL}/mapa-strony/kategorie"

OUTPUT_DIR = Path("erli_dataset")
IMAGES_DIR = OUTPUT_DIR / "images"
JSON_PATH = OUTPUT_DIR / "products.json"

MAX_CATEGORY_PAGES = 3
MAX_PRODUCTS_PER_CATEGORY = 100

IMAGE_WORKERS = 4
REQUEST_DELAY = 1.0
REQUEST_TIMEOUT = 25

REQUEST_STATS = {
    "success": 0,
    "forbidden": 0,
    "errors": 0,
}


# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ==================================================
# HTTP SESSION
# ==================================================

session = requests.Session()

retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)

adapter = HTTPAdapter(max_retries=retry)

session.mount("https://", adapter)
session.mount("http://", adapter)

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
})


# ==================================================
# HTTP HELPERS
# ==================================================

def get_soup(url: str) -> BeautifulSoup | None:
    """Download a webpage and return parsed HTML."""

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)

        if response.status_code == 403:
            REQUEST_STATS["forbidden"] += 1

            logger.warning(
                "HTTP 403 - access forbidden: %s",
                url
            )

            return None

        response.raise_for_status()

        REQUEST_STATS["success"] += 1

        return BeautifulSoup(response.text, "html.parser")

    except requests.RequestException as exc:
        REQUEST_STATS["errors"] += 1

        logger.warning(
            "Failed to fetch %s: %s",
            url,
            exc
        )

        return None

    finally:
        time.sleep(REQUEST_DELAY)


def normalize_url(url: str) -> str:
    """Normalize URL by removing query parameters and fragments."""

    parsed = urlparse(url)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/"),
        "",
        "",
        ""
    ))


def is_product_url(url: str) -> bool:
    """Check whether a URL points to a product page."""

    return "/produkt/" in urlparse(url).path


# ==================================================
# CATEGORY DISCOVERY
# ==================================================

def get_category_urls() -> list[str]:
    """Discover category URLs from the ERLI category map."""

    soup = get_soup(CATEGORY_MAP_URL)

    if soup is None:
        raise RuntimeError("Failed to download the ERLI category map.")

    urls = set()

    for a in soup.select("a[href]"):
        href = urljoin(BASE_URL, a["href"])
        parsed = urlparse(href)

        if (
            parsed.scheme in ("http", "https")
            and parsed.netloc == "erli.pl"
            and parsed.path not in ("", "/")
            and not is_product_url(href)
        ):
            urls.add(normalize_url(href))

    result = sorted(urls)

    logger.info(
        "Discovered %d potential category URLs.",
        len(result)
    )

    return result


def extract_json_ld(soup: BeautifulSoup) -> list[dict]:
    """Extract JSON-LD objects from a webpage."""

    result = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())

            if isinstance(data, list):
                result.extend(data)
            elif isinstance(data, dict):
                result.append(data)

        except (json.JSONDecodeError, TypeError):
            continue

    return result


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    """Extract category hierarchy from page breadcrumbs."""

    for schema in extract_json_ld(soup):

        if schema.get("@type") != "BreadcrumbList":
            continue

        categories = []

        for item in schema.get("itemListElement", []):

            name = item.get("name")

            if not name and isinstance(item.get("item"), dict):
                name = item["item"].get("name")

            if name:
                categories.append(name.strip())

        if categories:
            return categories

    selectors = [
        'nav[aria-label*="breadcrumb" i] a',
        '[class*="breadcrumb"] a',
        'ol[class*="breadcrumb"] li',
        '[data-testid*="breadcrumb"]',
    ]

    for selector in selectors:

        elements = soup.select(selector)

        values = [
            element.get_text(" ", strip=True)
            for element in elements
        ]

        values = [
            value for value in values
            if value and value.lower() not in (
                "erli",
                "home",
                "strona główna",
            )
        ]

        if values:
            return values

    return []


def clean_breadcrumbs(breadcrumbs: list[str]) -> list[str]:
    """Remove homepage labels from breadcrumbs."""

    excluded = {
        "erli",
        "home",
        "strona główna",
    }

    return [
        category.strip()
        for category in breadcrumbs
        if category.strip().lower() not in excluded
    ]


# ==================================================
# PRODUCT DISCOVERY
# ==================================================

def extract_product_links(soup: BeautifulSoup) -> set[str]:
    """Extract product URLs from a category listing page."""

    products = set()

    for a in soup.select("a[href]"):

        href = normalize_url(
            urljoin(BASE_URL, a["href"])
        )

        if is_product_url(href):
            products.add(href)

    return products


def get_product_links(
    category_url: str,
    max_pages: int = MAX_CATEGORY_PAGES,
) -> list[str]:
    """Collect product URLs from category listing pages."""

    products = set()

    for page in range(1, max_pages + 1):

        url = (
            category_url
            if page == 1
            else f"{category_url}?page={page}"
        )

        soup = get_soup(url)

        if soup is None:
            break

        current_products = extract_product_links(soup)

        if not current_products:
            break

        new_products = current_products - products

        if not new_products:
            break

        products.update(current_products)

        logger.info(
            "%s | page %d | %d products",
            category_url,
            page,
            len(current_products),
        )

        if len(products) >= MAX_PRODUCTS_PER_CATEGORY:
            break

    return list(products)[:MAX_PRODUCTS_PER_CATEGORY]


# ==================================================
# POC MODE
# ==================================================

def run_poc(poc_pages: int = 1):
    """
    Analyze product counts across category levels.

    No individual product pages are visited.
    No images are downloaded.
    No product metadata is saved.

    Counts represent unique product URLs discovered
    on the inspected category listing pages.
    """

    logger.info("Starting POC mode.")

    category_urls = get_category_urls()

    category_products = defaultdict(set)

    for index, category_url in enumerate(
        category_urls,
        start=1,
    ):

        soup = get_soup(category_url)

        if soup is None:
            continue

        breadcrumbs = clean_breadcrumbs(
            extract_breadcrumbs(soup)
        )

        if not breadcrumbs:
            logger.warning(
                "Could not extract breadcrumbs: %s",
                category_url,
            )
            continue

        category_path = (
            breadcrumbs[0] if len(breadcrumbs) > 0 else None,
            breadcrumbs[1] if len(breadcrumbs) > 1 else None,
            breadcrumbs[2] if len(breadcrumbs) > 2 else None,
        )

        product_urls = extract_product_links(soup)

        for page in range(2, poc_pages + 1):

            page_url = f"{category_url}?page={page}"
            page_soup = get_soup(page_url)

            if page_soup is None:
                break

            page_products = extract_product_links(page_soup)

            if not page_products:
                break

            product_urls.update(page_products)

        category_products[category_path].update(product_urls)

        logger.info(
            "[%d/%d] %s | %d products",
            index,
            len(category_urls),
            " > ".join(filter(None, category_path)),
            len(product_urls),
        )

    counts = {
        "category_1": defaultdict(set),
        "category_2": defaultdict(set),
        "category_3": defaultdict(set),
    }

    for (cat1, cat2, cat3), products in category_products.items():

        if cat1:
            counts["category_1"][cat1].update(products)

        if cat1 and cat2:
            counts["category_2"][(cat1, cat2)].update(products)

        if cat1 and cat2 and cat3:
            counts["category_3"][(cat1, cat2, cat3)].update(products)

    print("\n" + "=" * 75)
    print("ERLI DATASET - POC REPORT")
    print("=" * 75)

    for level in (
        "category_1",
        "category_2",
        "category_3",
    ):

        print(f"\n{level.upper()}")
        print("-" * 75)

        categories = counts[level]

        if not categories:
            print("No categories found.")
            continue

        for category, products in sorted(categories.items()):

            category_name = (
                " > ".join(category)
                if isinstance(category, tuple)
                else category
            )

            print(
                f"{category_name:<60} {len(products):>10}"
            )

        total_products = (
            len(set().union(*categories.values()))
            if categories
            else 0
        )

        print("-" * 75)
        print(f"Total unique products: {total_products}")

    print_request_stats()

    print("\nPOC completed.")
    print("No product images or individual product details were downloaded.")


# ==================================================
# PRODUCT METADATA EXTRACTION
# ==================================================

def find_product_schema(data) -> dict | None:
    """Recursively find a Product schema in JSON-LD."""

    if isinstance(data, list):

        for item in data:
            found = find_product_schema(item)

            if found:
                return found

    elif isinstance(data, dict):

        schema_type = data.get("@type", [])

        if isinstance(schema_type, str):
            schema_type = [schema_type]

        if "Product" in schema_type:
            return data

        for value in data.values():

            found = find_product_schema(value)

            if found:
                return found

    return None


def extract_image_url(
    soup: BeautifulSoup,
    product: dict | None,
) -> str | None:
    """Extract the main product image URL."""

    if product:

        image = product.get("image")

        if isinstance(image, str):
            return urljoin(BASE_URL, image)

        if isinstance(image, list) and image:

            first = image[0]

            if isinstance(first, str):
                return urljoin(BASE_URL, first)

            if isinstance(first, dict):

                image_url = (
                    first.get("url")
                    or first.get("contentUrl")
                )

                if image_url:
                    return urljoin(BASE_URL, image_url)

    og_image = soup.select_one(
        'meta[property="og:image"]'
    )

    if og_image and og_image.get("content"):
        return urljoin(BASE_URL, og_image["content"])

    for img in soup.select("img"):

        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
        )

        if src and not src.startswith("data:"):
            return urljoin(BASE_URL, src)

    return None


def extract_product(product_url: str) -> dict | None:
    """Extract metadata for an individual product."""

    soup = get_soup(product_url)

    if soup is None:
        return None

    schemas = extract_json_ld(soup)
    product_schema = find_product_schema(schemas)

    item = (
        product_schema.get("name")
        if product_schema
        else None
    )

    if not item:

        h1 = soup.select_one("h1")
        item = h1.get_text(" ", strip=True) if h1 else None

    if not item:
        return None

    image_url = extract_image_url(
        soup,
        product_schema,
    )

    if not image_url:
        return None

    breadcrumbs = clean_breadcrumbs(
        extract_breadcrumbs(soup)
    )

    if not breadcrumbs:
        logger.warning(
            "Missing category breadcrumbs: %s",
            product_url,
        )

    return {
        "item": item.strip(),
        "image_url": image_url,
        "category_1": breadcrumbs[0] if len(breadcrumbs) > 0 else None,
        "category_2": breadcrumbs[1] if len(breadcrumbs) > 1 else None,
        "category_3": breadcrumbs[2] if len(breadcrumbs) > 2 else None,
        "product_url": product_url,
    }


# ==================================================
# IMAGE DOWNLOADING
# ==================================================

def download_image(product: dict) -> bool:
    """Download a product image."""

    image_url = product["image_url"]

    try:

        response = session.get(
            image_url,
            timeout=30,
        )

        if response.status_code == 403:
            REQUEST_STATS["forbidden"] += 1
            logger.warning(
                "Image access forbidden: %s",
                image_url,
            )
            return False

        response.raise_for_status()

        REQUEST_STATS["success"] += 1

        content_type = response.headers.get(
            "Content-Type",
            "",
        )

        if not content_type.startswith("image/"):
            return False

        image_id = hashlib.sha256(
            product["product_url"].encode("utf-8")
        ).hexdigest()[:16]

        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(
            content_type.split(";")[0],
            ".jpg",
        )

        image_path = IMAGES_DIR / f"{image_id}{extension}"

        if not image_path.exists():
            image_path.write_bytes(response.content)

        product["image_path"] = str(image_path)

        return True

    except requests.RequestException as exc:

        REQUEST_STATS["errors"] += 1

        logger.warning(
            "Image download failed: %s",
            exc,
        )

        return False

    finally:
        time.sleep(REQUEST_DELAY)


# ==================================================
# FULL SCRAPING PIPELINE
# ==================================================

def save_json(products: list[dict]):
    """Save product metadata to JSON."""

    JSON_PATH.write_text(
        json.dumps(
            products,
            ensure_ascii=False,
            indent=4,
        ),
        encoding="utf-8",
    )


def run_full_scraping():

    OUTPUT_DIR.mkdir(exist_ok=True)
    IMAGES_DIR.mkdir(exist_ok=True)

    category_urls = get_category_urls()

    all_product_urls = set()

    for category_url in category_urls:

        product_urls = get_product_links(category_url)

        all_product_urls.update(product_urls)

    logger.info(
        "Discovered %d unique products.",
        len(all_product_urls),
    )

    products = []

    for index, product_url in enumerate(
        sorted(all_product_urls),
        start=1,
    ):

        product = extract_product(product_url)

        if product is None:
            continue

        products.append(product)

        if index % 20 == 0:
            save_json(products)

        logger.info(
            "[%d/%d] %s",
            index,
            len(all_product_urls),
            product["item"],
        )

    save_json(products)

    logger.info(
        "Collected %d products.",
        len(products),
    )

    success = 0

    with ThreadPoolExecutor(
        max_workers=IMAGE_WORKERS
    ) as executor:

        futures = [
            executor.submit(download_image, product)
            for product in products
        ]

        for future in as_completed(futures):

            if future.result():
                success += 1

    save_json(products)

    logger.info(
        "Successfully downloaded %d/%d images.",
        success,
        len(products),
    )

    logger.info("JSON output: %s", JSON_PATH)
    logger.info("Images directory: %s", IMAGES_DIR)

    print_request_stats()


# ==================================================
# REQUEST STATISTICS
# ==================================================

def print_request_stats():

    print("\nREQUEST STATISTICS")
    print("-" * 40)

    for key, value in REQUEST_STATS.items():
        print(f"{key:<15}: {value}")

    total = sum(REQUEST_STATS.values())

    if total:
        success_rate = (
            REQUEST_STATS["success"] / total * 100
        )

        print(f"{'success rate':<15}: {success_rate:.2f}%")


# ==================================================
# CLI
# ==================================================

def parse_args():

    parser = argparse.ArgumentParser(
        description="ERLI product image and metadata scraper."
    )

    parser.add_argument(
        "--poc",
        action="store_true",
        help="Run lightweight category coverage analysis.",
    )

    parser.add_argument(
        "--poc-pages",
        type=int,
        default=1,
        help="Number of listing pages to inspect per category in POC mode.",
    )

    return parser.parse_args()


def main():

    args = parse_args()

    if args.poc:

        if args.poc_pages < 1:
            raise ValueError("--poc-pages must be at least 1.")

        run_poc(
            poc_pages=args.poc_pages,
        )

    else:
        run_full_scraping()


if __name__ == "__main__":
    main()
