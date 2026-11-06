"""Download ERLI product images and write one metadata sidecar per image."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

METADATA_FIELDS = (
    "item",
    "image_url",
    "product_url",
)
IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
CONTENT_TYPE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_IMAGE_BYTES = 50 * 1024 * 1024
ERLI_HOME_URL = "https://erli.pl"
TOP_CATEGORY_NAMES = {
    "dom-i-ogrod": "Dom i Ogród",
    "dziecko": "Dziecko",
    "elektronika": "Elektronika",
    "firma": "Firma",
    "kolekcje-i-sztuka": "Kolekcje i sztuka",
    "kultura-i-rozrywka": "Kultura i rozrywka",
    "moda": "Moda",
    "motoryzacja": "Motoryzacja",
    "sport-i-turystyka": "Sport i turystyka",
    "supermarket": "Supermarket",
    "uroda": "Uroda",
    "zdrowie": "Zdrowie",
}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def metadata_from_record(record: Any) -> dict[str, str]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    category_fields = sorted(
        (field for field in record if re.fullmatch(r"category_\d+", field)),
        key=lambda field: int(field.split("_")[1]),
    )
    required_fields = (*METADATA_FIELDS, *category_fields)
    missing = [field for field in required_fields if not isinstance(record.get(field), str)]
    if missing:
        raise ValueError(f"missing string fields: {', '.join(missing)}")
    return {field: record[field] for field in METADATA_FIELDS}


def absolute_url(url: str, base_url: str) -> str:
    return urljoin(base_url, url.strip())


def category_id(url: str) -> str | None:
    path = urlsplit(url).path.rstrip("/")
    match = re.search(r",(\d+)$", path)
    return match.group(1) if match else None


def category_slug(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].rsplit(",", 1)[0]


def discover_top_categories(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    categories: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = absolute_url(anchor["href"], base_url)
        slug = category_slug(url)
        if slug not in TOP_CATEGORY_NAMES or url in seen or not category_id(url):
            continue
        seen.add(url)
        categories.append((TOP_CATEGORY_NAMES[slug], url))
    return categories


def discover_leaf_categories(
    soup: BeautifulSoup,
    category_name: str,
    category_url: str,
) -> list[tuple[str, str, str, str]]:
    tree_id = category_id(category_url)
    if not tree_id:
        raise ValueError(f"category URL has no numeric ID: {category_url}")
    tree = soup.find("nav", id=f"main-category-tree:{tree_id}")
    if tree is None:
        raise ValueError(f"category tree not found for {category_url}")

    leaves: list[tuple[str, str, str, str]] = []
    current_category_2: tuple[str, str] | None = None
    list_element = tree.find("ol", recursive=False)
    if list_element is None:
        return leaves
    for item in list_element.find_all("li", recursive=False):
        anchor = item.find("a", href=True, recursive=False)
        if anchor is None:
            continue
        name = " ".join(anchor.get_text(" ", strip=True).split())
        url = absolute_url(anchor["href"], category_url)
        if item.get("data-type") == "2":
            current_category_2 = (name, url)
        elif current_category_2 and category_id(url):
            leaves.append((category_name, current_category_2[0], name, url))
    return leaves


PROMOTIONAL_IMAGE_TERMS = (
    "black",
    "weeks",
    "banner",
    "baner",
    "promoc",
    "gazet",
)


def image_url_from_element(image: Any, base_url: str) -> str | None:
    for attribute in ("data-src", "data-lazy-src", "src"):
        value = image.get(attribute)
        if value and not value.startswith("data:"):
            return absolute_url(value, base_url)
    srcset = image.get("srcset") or image.get("srcSet")
    if srcset:
        return absolute_url(srcset.split(",")[0].strip().split(" ")[0], base_url)
    return None


def image_candidates(anchor: Any, base_url: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    ancestors = [anchor, *list(anchor.parents)[:4]]
    for ancestor in ancestors:
        for image in ancestor.find_all("img"):
            image_url = image_url_from_element(image, base_url)
            if image_url:
                description = " ".join(
                    str(image.get(attribute, ""))
                    for attribute in ("alt", "title", "class")
                ).lower()
                candidates.append((image_url, description))
        if candidates and ancestor is not anchor:
            break
    return candidates


def choose_product_image(anchor: Any, base_url: str) -> tuple[str | None, str]:
    title = " ".join(anchor.get_text(" ", strip=True).split())
    title_words = {word.lower() for word in re.findall(r"\w{4,}", title)}
    scored: list[tuple[int, str, str]] = []
    for image_url, description in image_candidates(anchor, base_url):
        image_text = f"{image_url} {description}".lower()
        score = 0
        if any(term in image_text for term in PROMOTIONAL_IMAGE_TERMS):
            score -= 100
        if "i.erli.pl" in image_url:
            score += 10
        score += sum(word in image_text for word in title_words) * 5
        scored.append((score, image_url, description))
    if not scored:
        return None, title
    scored.sort(key=lambda candidate: candidate[0], reverse=True)
    score, image_url, description = scored[0]
    if score < 0:
        return None, title
    return image_url, title or description


def discover_product_records(
    soup: BeautifulSoup,
    categories: list[str],
    category_url: str,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for anchor in soup.select('a[href*="/produkt/"]'):
        product_url = absolute_url(anchor["href"], category_url)
        if product_url in seen_urls:
            continue
        image_url, title = choose_product_image(anchor, category_url)
        if not image_url:
            logging.warning("No image found for product %s", product_url)
            continue
        record = {
            "item": title or product_url.rsplit("/", 1)[-1],
            "image_url": image_url,
            "product_url": product_url,
        }
        record.update({f"category_{index}": name for index, name in enumerate(categories, 1)})
        records.append(record)
        seen_urls.add(product_url)
    return records


def category_names_from_url(category_url: str) -> list[str]:
    slug = category_slug(category_url)
    words = re.sub(r"[-_]+", " ", slug).split()
    label = " ".join(words).strip().title() or "Unknown"
    return [label]


def breadcrumb_categories(soup: BeautifulSoup, category_url: str) -> list[str]:
    target_id = category_id(category_url)
    candidates: list[list[str]] = []
    for breadcrumb in soup.select("ul"):
        values: list[str] = []
        found_target = False
        for anchor in breadcrumb.select("a[href]"):
            href = absolute_url(anchor["href"], category_url)
            if not category_id(href):
                continue
            values.append(" ".join(anchor.get_text(" ", strip=True).split()))
            found_target = found_target or category_id(href) == target_id
        if found_target and values:
            candidates.append(values)
    return max(candidates, key=len) if candidates else category_names_from_url(category_url)


def has_product_links(soup: BeautifulSoup) -> bool:
    return bool(soup.select('a[href*="/produkt/"]'))


def load_lazy_products(page: Any, passes: int = 8) -> None:
    unchanged = 0
    previous_height = 0
    for _ in range(passes):
        height = page.evaluate("document.body.scrollHeight")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.wait_for_load_state("networkidle", timeout=750)
        except PlaywrightError:
            pass
        page.wait_for_timeout(300)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height <= previous_height and new_height <= height:
            unchanged += 1
        else:
            unchanged = 0
        previous_height = new_height
        if unchanged >= 3:
            break


def total_pages(soup: BeautifulSoup) -> int:
    value = soup.select_one('[aria-label="Liczba stron"]')
    if value:
        try:
            return max(1, int(value.get_text(strip=True)))
        except ValueError:
            pass
    return 1


def next_page(page: Any, page_number: int, wait_seconds: float) -> bool:
    input_locator = page.locator('input[aria-label="Numer aktualnej strony"]')
    if input_locator.count() == 0:
        return False
    input_locator.first.fill(str(page_number))
    input_locator.first.press("Enter")
    page.wait_for_timeout(int(wait_seconds * 1000))
    return True


def limit_records_per_leaf(
    records: Iterable[dict[str, str]],
    max_images_per_leaf: int | None,
) -> list[dict[str, str]]:
    if not max_images_per_leaf:
        return list(records)
    counts: dict[tuple[str, ...], int] = {}
    limited: list[dict[str, str]] = []
    for record in records:
        leaf = tuple(
            record[field]
            for field in sorted(
                (key for key in record if re.fullmatch(r"category_\d+", key)),
                key=lambda key: int(key.split("_")[1]),
            )
        )
        if counts.get(leaf, 0) >= max_images_per_leaf:
            continue
        counts[leaf] = counts.get(leaf, 0) + 1
        limited.append(record)
    return limited


def load_retry_records(path: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    try:
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    records.append(metadata_from_record(value))
                except (json.JSONDecodeError, ValueError) as error:
                    logging.warning("Ignoring invalid retry-log line %d: %s", line_number, error)
    except OSError as error:
        raise ValueError(f"cannot read retry log {path}: {error}") from error
    return records


def image_stem(image_url: str) -> str:
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:20]


def safe_path_component(value: str) -> str:
    component = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not component:
        return "_"
    if component.upper() in {"CON", "PRN", "AUX", "NUL"}:
        return f"_{component}"
    if len(component) > 120:
        component = component[:120].rstrip(" .")
    return component


def record_output_dir(output_dir: Path, record: dict[str, str]) -> Path:
    category_fields = sorted(
        (field for field in record if re.fullmatch(r"category_\d+", field)),
        key=lambda field: int(field.split("_")[1]),
    )
    return output_dir.joinpath(
        *(safe_path_component(record[field]) for field in category_fields)
    )


def existing_image(output_dir: Path, stem: str) -> Path | None:
    for candidate in output_dir.glob(f"{stem}.*"):
        if candidate.suffix.lower() in IMAGE_EXTENSIONS:
            return candidate
    return None


def extension_for_response(image_url: str, content_type: str) -> str:
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type.lower())
    if extension:
        return extension
    suffix = Path(urlparse(image_url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".img"


def validate_image_url(image_url: str) -> None:
    parsed = urlparse(image_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "erli.pl" or hostname.endswith(".erli.pl")
    ):
        raise ValueError("image_url must use http(s) and belong to erli.pl")


def download_record(
    session: requests.Session,
    record: dict[str, str],
    output_dir: Path,
    timeout: float,
    max_image_bytes: int,
    overwrite: bool,
) -> None:
    image_url = record["image_url"]
    validate_image_url(image_url)
    stem = image_stem(image_url)
    metadata_path = output_dir / f"{stem}.json"
    image_path = existing_image(output_dir, stem)

    if not overwrite and image_path and metadata_path.exists():
        logging.info("Skipping existing image: %s", image_url)
        return

    with session.get(image_url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_image_bytes:
                raise ValueError(f"image exceeds {max_image_bytes} bytes")
            chunks.append(chunk)
    if not chunks:
        raise ValueError("response body is empty")
    if content_type and not content_type.startswith("image/"):
        raise ValueError(f"unexpected content type: {content_type}")

    image_path = output_dir / f"{stem}{extension_for_response(image_url, content_type)}"
    temporary_image = image_path.with_suffix(f"{image_path.suffix}.part")
    temporary_metadata = metadata_path.with_suffix(".json.part")
    try:
        temporary_image.write_bytes(b"".join(chunks))
        temporary_metadata.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_image, image_path)
        os.replace(temporary_metadata, metadata_path)
    finally:
        temporary_image.unlink(missing_ok=True)
        temporary_metadata.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download ERLI images and write JSON metadata sidecars."
    )
    parser.add_argument(
        "--category-url",
        action="append",
        default=[],
        dest="category_urls",
        help="category/list URL; repeat this option for multiple lists",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("erli_dataset/images"),
        help="directory for images and metadata (default: erli_dataset/images)",
    )
    parser.add_argument(
        "--failed-log",
        type=Path,
        default=Path("erli_dataset/failed_downloads.jsonl"),
        help="JSONL file for failed records (default: erli_dataset/failed_downloads.jsonl)",
    )
    parser.add_argument(
        "--retry-log",
        type=Path,
        help="retry records from a previous failed-download JSONL file",
    )
    parser.add_argument("--max-category-1", type=positive_int)
    parser.add_argument("--max-category-2", type=positive_int)
    parser.add_argument("--max-category-3", type=positive_int)
    parser.add_argument(
        "--max-images-per-leaf",
        "--max-images-per-category-3",
        type=positive_int,
        help="maximum number of successful downloads per deepest category path",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--page-wait",
        type=float,
        default=10.0,
        help="seconds to wait after opening or changing a category page",
    )
    parser.add_argument("--max-image-bytes", type=positive_int, default=DEFAULT_MAX_IMAGE_BYTES)
    parser.add_argument("--retries", type=positive_int, default=3)
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=Path(".erli-browser-profile"),
        help="persistent Chromium profile for ERLI cookies/session",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run Chromium without a visible window; omit this to complete challenges manually",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def write_discovery_failures(path: Path, failures: list[dict[str, str]]) -> None:
    if not failures:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as failed_file:
        for failure in failures:
            failed_file.write(json.dumps(failure, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; erli-image-dataset/1.0)"
    discovery_failures: list[dict[str, str]] = []
    try:
        if args.retry_log:
            records = limit_records_per_leaf(
                load_retry_records(args.retry_log),
                args.max_images_per_leaf,
            )
        else:
            records = []
            category_urls = args.category_urls or [ERLI_HOME_URL]
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(args.browser_profile),
                    headless=args.headless,
                )
                try:
                    page = context.new_page()
                    page.set_default_timeout(int(args.timeout * 1000))
                    for category_url in category_urls:
                        try:
                            page.goto(category_url, wait_until="domcontentloaded")
                            try:
                                page.wait_for_load_state("networkidle", timeout=10_000)
                            except PlaywrightError:
                                logging.warning(
                                    "Timed out waiting for network idle: %s",
                                    category_url,
                                )
                            page.wait_for_timeout(int(args.page_wait * 1000))
                            load_lazy_products(page)
                            category_soup = BeautifulSoup(page.content(), "html.parser")
                            categories = breadcrumb_categories(category_soup, category_url)
                            category_records: list[dict[str, str]] = []
                            seen_products: set[str] = set()
                            page_number = 1
                            while True:
                                for record in discover_product_records(
                                    category_soup,
                                    categories,
                                    category_url,
                                ):
                                    if record["product_url"] not in seen_products:
                                        category_records.append(record)
                                        seen_products.add(record["product_url"])
                                pages = total_pages(category_soup)
                                logging.info(
                                    "Page %d/%d: discovered %d unique products",
                                    page_number,
                                    pages,
                                    len(category_records),
                                )
                                if (
                                    args.max_images_per_leaf
                                    and len(category_records) >= args.max_images_per_leaf
                                ):
                                    logging.info(
                                        "Collected %d candidates; stopping pagination",
                                        len(category_records),
                                    )
                                    break
                                if page_number >= pages or not next_page(
                                    page, page_number + 1, args.page_wait
                                ):
                                    break
                                page_number += 1
                                load_lazy_products(page)
                                category_soup = BeautifulSoup(page.content(), "html.parser")
                            records.extend(category_records)
                            if not category_records:
                                title = page.title()
                                logging.error(
                                    "No products found at %s (page title: %s, final URL: %s)",
                                    category_url,
                                    title,
                                    page.url,
                                )
                                discovery_failures.append(
                                    {
                                        "error": "no product records found",
                                        "category_url": category_url,
                                        "page_title": title,
                                        "final_url": page.url,
                                    }
                                )
                        except (OSError, PlaywrightError, ValueError) as error:
                            logging.error("Failed to fetch category %s: %s", category_url, error)
                            discovery_failures.append(
                                {
                                    "error": str(error),
                                    "category_url": category_url,
                                }
                            )
                    for cookie in context.cookies():
                        session.cookies.set(
                            cookie["name"],
                            cookie["value"],
                            domain=cookie.get("domain"),
                            path=cookie.get("path", "/"),
                        )
                finally:
                    context.close()
        args.output_dir.mkdir(parents=True, exist_ok=True)
        args.failed_log.parent.mkdir(parents=True, exist_ok=True)
        logging.info("Discovery complete: %d product records queued for download", len(records))
    except (OSError, PlaywrightError, requests.RequestException, ValueError) as error:
        if isinstance(error, PlaywrightError) and "Executable doesn't exist" in str(error):
            logging.error(
                "Chromium is not installed for Playwright. Run: "
                "uv run playwright install chromium"
            )
        try:
            write_discovery_failures(args.failed_log, discovery_failures)
        except OSError as log_error:
            logging.error("Could not write failure log: %s", log_error)
        logging.error("%s", error)
        return 2

    failures = 0
    downloaded = 0
    skipped = 0
    successful_by_leaf: dict[tuple[str, ...], int] = {}
    with args.failed_log.open("w", encoding="utf-8") as failed_file:
        for failure in discovery_failures:
            failed_file.write(json.dumps(failure, ensure_ascii=False) + "\n")
        failed_file.flush()
        for record in records:
            try:
                category_fields = sorted(
                    (field for field in record if re.fullmatch(r"category_\d+", field)),
                    key=lambda field: int(field.split("_")[1]),
                )
                leaf = tuple(record[field] for field in category_fields)
                if (
                    args.max_images_per_leaf
                    and successful_by_leaf.get(leaf, 0) >= args.max_images_per_leaf
                ):
                    continue
                record_dir = record_output_dir(args.output_dir, record)
                record_dir.mkdir(parents=True, exist_ok=True)
                stem = image_stem(record["image_url"])
                was_complete = (
                    existing_image(record_dir, stem) is not None
                    and (record_dir / f"{stem}.json").exists()
                    and not args.overwrite
                )
                for attempt in range(1, args.retries + 1):
                    try:
                        download_record(
                            session,
                            record,
                            record_dir,
                            args.timeout,
                            args.max_image_bytes,
                            args.overwrite,
                        )
                        break
                    except (OSError, requests.RequestException, ValueError):
                        if attempt == args.retries:
                            raise
                        time.sleep(min(2** (attempt - 1), 8))
                if was_complete:
                    skipped += 1
                else:
                    downloaded += 1
                logging.info(
                    "Processed %d/%d: %s",
                    downloaded + skipped + failures,
                    len(records),
                    record["image_url"],
                )
                successful_by_leaf[leaf] = successful_by_leaf.get(leaf, 0) + 1
            except (OSError, requests.RequestException, ValueError) as error:
                failures += 1
                failed_file.write(
                    json.dumps(
                        {**record, "error": str(error)},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                failed_file.flush()
                logging.error("Failed %s: %s", record["image_url"], error)

    logging.info(
        "Finished: %d downloaded, %d skipped, %d failed",
        downloaded,
        skipped,
        failures,
    )
    return 1 if failures or discovery_failures else 0


if __name__ == "__main__":
    sys.exit(main())
