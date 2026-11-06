import csv
import requests

from bs4 import BeautifulSoup
from urllib.parse import urljoin


BASE_URL = "https://erli.pl"
SITEMAP_URL = f"{BASE_URL}/mapa-strony/kategorie"

OUTPUT_FILE = "erli_categories.csv"

MAIN_CATEGORIES = [
    "Dziecko",
    "Elektronika",
    "Firma",
    "Kolekcje i sztuka",
    "Kultura i rozrywka",
    "Moda",
    "Motoryzacja",
    "Sport i turystyka",
    "Supermarket",
    "Uroda",
    "Zdrowie",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

# Pomijamy kategorie prowadzące do treści nieodpowiednich
# dla niepełnoletnich użytkowników.
EXCLUDED_CATEGORIES = {
    "erotyka",
    "militaria",
    "samoobrona",
    "wiatrówki",
    "noże",
}


def normalize(text):
    return " ".join(text.lower().split())


def get_soup(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()

    return BeautifulSoup(response.text, "html.parser")


def get_direct_subcategories(soup, main_category):
    """
    Zwraca wyłącznie bezpośrednie podkategorie kategorii głównej.
    """

    import re

    subcategories = []
    seen_urls = set()

    main_heading = None

    for heading in soup.find_all("h2"):
        if normalize(heading.get_text(" ", strip=True)) == normalize(
            main_category
        ):
            main_heading = heading
            break

    if main_heading is None:
        print(f"[WARNING] Nie znaleziono kategorii: {main_category}")
        return subcategories

    for heading in main_heading.find_all_next(["h2", "h3"]):

        if heading.name == "h2":
            break

        if heading.name != "h3":
            continue

        name = heading.get_text(" ", strip=True)

        if not name:
            continue

        if normalize(name) in EXCLUDED_CATEGORIES:
            continue

        # Szukamy linku w nagłówku lub jego najbliższym
        # elemencie nadrzędnym.
        link = heading.find("a", href=True)

        if link is None:
            for parent in heading.parents:
                if parent.name in ("h2", "h3"):
                    break

                link = parent.find("a", href=True)

                if link:
                    break

        if link is None:
            print(f"[WARNING] Nie znaleziono linku dla: {name}")
            continue

        href = link.get("href", "")

        if not re.search(r",\d+/?$", href):
            continue

        url = urljoin(BASE_URL, href)

        if url in seen_urls:
            continue

        seen_urls.add(url)

        subcategories.append({
            "main_category": main_category,
            "subcategory": name,
            "url": url
        })

    return subcategories


def main():
    print("Pobieranie mapy kategorii ERLI...")

    soup = get_soup(SITEMAP_URL)

    results = []

    for main_category in MAIN_CATEGORIES:

        print(f"\nPrzetwarzanie: {main_category}")

        subcategories = get_direct_subcategories(
            soup,
            main_category
        )

        for subcategory in subcategories:
            results.append(subcategory)

            print(
                f"  {subcategory['subcategory']}: "
                f"{subcategory['url']}"
            )

        print(
            f"  Znaleziono: {len(subcategories)} podkategorii"
        )

    # Zapis wyników do CSV.
    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "main_category",
                "subcategory",
                "url"
            ]
        )

        writer.writeheader()
        writer.writerows(results)

    print("\n" + "=" * 50)
    print(f"Zakończono. Łącznie znaleziono: {len(results)}")
    print(f"Wyniki zapisano w: {OUTPUT_FILE}")
    print("=" * 50)


if __name__ == "__main__":
    main()