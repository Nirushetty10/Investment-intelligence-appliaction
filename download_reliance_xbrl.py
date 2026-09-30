from pathlib import Path

import requests


URL = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml"
)

OUTPUT = Path(
    "tests/fixtures/reliance_2026q1_consolidated.xbrl.xml"
)


def main():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,*/*",
        "Referer": "https://www.nseindia.com/",
    })

    homepage = session.get(
        "https://www.nseindia.com/",
        timeout=30,
    )

    print("NSE homepage status:", homepage.status_code)

    response = session.get(
        URL,
        timeout=30,
    )

    print("XBRL status:", response.status_code)
    print("Content-Type:", response.headers.get("Content-Type"))
    print("Content-Length:", response.headers.get("Content-Length"))
    print("Downloaded bytes:", len(response.content))

    response.raise_for_status()

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT.write_bytes(response.content)

    print("Saved:", OUTPUT)
    print("File size:", OUTPUT.stat().st_size)


if __name__ == "__main__":
    main()