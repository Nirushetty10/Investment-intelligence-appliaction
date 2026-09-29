"""
Central configuration for the NSE financial data ingestion pipeline.

Nothing in here invents data. It only configures *how* we talk to sources
and where we store things. All values are overridable via environment
variables so the same code runs in dev / CI / prod without edits.
"""

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

RAW_STORE_DIR = Path(
    os.environ.get("NSE_RAW_STORE_DIR", BASE_DIR / "raw_store")
)

LOG_DIR = Path(
    os.environ.get("NSE_LOG_DIR", BASE_DIR / "logs")
)

RAW_STORE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class DBConfig:
    host: str = os.environ.get("NSE_DB_HOST", "localhost")
    port: int = int(os.environ.get("NSE_DB_PORT", "5432"))

    # Change this through NSE_DB_NAME if your database has another name.
    name: str = os.environ.get("NSE_DB_NAME", "nse_financials")

    user: str = os.environ.get("NSE_DB_USER", "postgres")

    password: str = os.environ.get(
        "NSE_DB_PASSWORD",
        "10051998",
    )

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://"
            f"{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


@dataclass(frozen=True)
class NSEEndpoints:
    """
    NSE endpoint configuration.

    NSE periodically changes its API endpoints and request parameters.
    Discovery therefore keeps the endpoint configuration here rather than
    embedding URLs inside parsing/normalization logic.
    """

    base: str = "https://www.nseindia.com"

    archives_base: str = "https://nsearchives.nseindia.com"

    # ------------------------------------------------------------------
    # Company master / search
    # ------------------------------------------------------------------

    company_master: str = (
        "https://www.nseindia.com/api/equity-master"
    )

    company_search: str = (
        "https://www.nseindia.com/api/search/autocomplete"
    )

    # ------------------------------------------------------------------
    # Corporate filings / Integrated Filing
    # ------------------------------------------------------------------

    # Current NSE Integrated Filing - Financials endpoint.
    integrated_filings: str = (
        "https://www.nseindia.com/api/integrated-filing-results"
    )

    corp_filings_announcements: str = (
        "https://www.nseindia.com/api/corporate-announcements"
    )

    # Legacy / other financial-result endpoint.
    financial_results: str = (
        "https://www.nseindia.com/api/corporate-financial-results"
    )

    # ------------------------------------------------------------------
    # Corporate actions
    # ------------------------------------------------------------------

    corporate_actions: str = (
        "https://www.nseindia.com/api/corporates-corporateActions"
    )

    # ------------------------------------------------------------------
    # Shareholding pattern
    # ------------------------------------------------------------------

    shareholding_pattern: str = (
        "https://www.nseindia.com/api/corporate-share-holdings-master"
    )

    # ------------------------------------------------------------------
    # Historical price / bhavcopy
    # ------------------------------------------------------------------

    bhavcopy_archive: str = (
        "https://nsearchives.nseindia.com/"
        "products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
    )


@dataclass(frozen=True)
class HttpConfig:
    """
    HTTP behavior.

    NSE can rate-limit or block aggressive clients, so requests are
    deliberately conservative and configurable.
    """

    default_delay_seconds: float = float(
        os.environ.get("NSE_DELAY", "2.0")
    )

    max_retries: int = int(
        os.environ.get("NSE_MAX_RETRIES", "5")
    )

    backoff_base_seconds: float = 2.0

    backoff_max_seconds: float = 120.0

    timeout_seconds: float = 30.0

    max_workers: int = int(
        os.environ.get("NSE_MAX_WORKERS", "1")
    )

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )


@dataclass(frozen=True)
class HistoricalCoverage:
    start_date: str = "2015-01-01"

    # This is only a provisional routing hint.
    # The actual transition between legacy filings and Integrated Filing
    # should eventually be established empirically per company.
    provisional_integrated_filing_start: str = "2023-04-01"


DB = DBConfig()

NSE = NSEEndpoints()

HTTP = HttpConfig()

COVERAGE = HistoricalCoverage()


TEST_SYMBOLS = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "ITC",
    "SBIN",
]