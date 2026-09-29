from pathlib import Path

import requests


URL = (
    "https://nsearchives.nseindia.com/corporate/ixbrl/"
    "INTEGRATED_FILING_INDAS_175608_17072026195004_iXBRL_WEB.html"
)

OUTPUT = Path(
    "tests/fixtures/reliance_2026q1_consolidated_ixbrl.html"
)


def main():
    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }

    session.headers.update(headers)

    print("Opening NSE homepage...")

    homepage = session.get(
        "https://www.nseindia.com/",
        timeout=30,
    )

    print("NSE homepage status:", homepage.status_code)

    print("Downloading RELIANCE iXBRL filing...")

    response = session.get(
        URL,
        timeout=30,
    )

    print("Filing status:", response.status_code)
    print("Content-Type:", response.headers.get("Content-Type"))
    print("Content-Length:", response.headers.get("Content-Length"))
    print("Downloaded bytes:", len(response.content))

    response.raise_for_status()

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Write the exact HTTP response body as bytes.
    OUTPUT.write_bytes(response.content)

    print()
    print("Saved:", OUTPUT)
    print("File size:", OUTPUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()