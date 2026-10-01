-- NSE Financial Data Pipeline — PostgreSQL schema
-- Design rules encoded here (do not violate when adding migrations):
--   * Standalone and Consolidated are NEVER merged (see unique constraints).
--   * Quarterly and Annual are NEVER merged.
--   * Original and Revised filings are NEVER merged — revisions are tracked,
--     raw history is retained; normalized tables point at the *selected*
--     (latest valid) revision via financial_periods.source_filing_id.
--   * Every normalized numeric column is NULLable. NULL means "not provided
--     by source". Never default to 0.

-- ============================================================
-- COMPANY MASTER
-- ============================================================
CREATE TABLE IF NOT EXISTS companies (
    company_id          BIGSERIAL PRIMARY KEY,
    isin                VARCHAR(12) UNIQUE,          -- stable identifier, preferred
    nse_symbol          VARCHAR(32) NOT NULL,
    company_name        TEXT NOT NULL,
    series              VARCHAR(8),
    exchange            VARCHAR(16) DEFAULT 'NSE',
    industry            TEXT,
    sector              TEXT,
    listing_date        DATE,
    face_value          NUMERIC(12,4),
    status              VARCHAR(32),                 -- ACTIVE / DELISTED / SUSPENDED
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_symbol ON companies (nse_symbol);

-- symbol/name change history, mergers, demergers
CREATE TABLE IF NOT EXISTS company_identity_events (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    event_type          VARCHAR(32) NOT NULL,        -- SYMBOL_CHANGE / NAME_CHANGE / MERGER / DEMERGER / DELISTING
    effective_date      DATE,
    old_value           TEXT,
    new_value           TEXT,
    related_company_id  BIGINT REFERENCES companies(company_id),
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- FILING REGISTRY  (every discovered filing, regardless of parse outcome)
-- ============================================================
CREATE TABLE IF NOT EXISTS nse_filing_registry (
    filing_id               BIGSERIAL PRIMARY KEY,
    company_id              BIGINT REFERENCES companies(company_id),
    symbol                  VARCHAR(32) NOT NULL,
    company_name            TEXT,

    period_start_date       DATE,
    period_end_date         DATE,

    period_type             VARCHAR(16) NOT NULL,    -- quarterly / annual
    reporting_quarter       VARCHAR(4),               -- Q1..Q4, NULL for annual
    financial_year          VARCHAR(9),               -- e.g. 2026-2027

    statement_type          VARCHAR(16) NOT NULL,    -- standalone / consolidated
    submission_type         VARCHAR(16),              -- audited / unaudited
    cumulative              BOOLEAN DEFAULT FALSE,

    accounting_standard     VARCHAR(16),              -- IND_AS / IGAAP etc.

    broadcast_date          TIMESTAMPTZ,
    revision_date           TIMESTAMPTZ,
    revision_remarks        TEXT,
    is_latest_revision      BOOLEAN NOT NULL DEFAULT TRUE,
    supersedes_filing_id    BIGINT REFERENCES nse_filing_registry(filing_id),

    xbrl_url                TEXT,             -- machine-readable XBRL XML URL (authoritative for parsing)
    ixbrl_url               TEXT,             -- rendered iXBRL/IndAS-details HTML companion document (ISSUE 7)
    detail_url              TEXT,
    source_url              TEXT,
    source_kind             VARCHAR(24) NOT NULL,     -- INTEGRATED_FILING_IXBRL / XBRL / HTML / PDF / ZIP

    filing_hash             VARCHAR(64),              -- sha256 of raw content
    catalog_json            JSONB,                    -- raw catalog row from NSE, verbatim

    discovery_status        VARCHAR(16) NOT NULL DEFAULT 'DISCOVERED',
    download_status         VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    parse_status            VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    validation_status       VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    normalization_status    VARCHAR(16) NOT NULL DEFAULT 'PENDING',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (symbol, period_end_date, period_type, statement_type, broadcast_date, source_kind)
);
CREATE INDEX IF NOT EXISTS idx_registry_symbol_period ON nse_filing_registry (symbol, period_end_date);
CREATE INDEX IF NOT EXISTS idx_registry_statuses ON nse_filing_registry (discovery_status, download_status, parse_status);

-- ISSUE 7: idempotent migration for databases created before ixbrl_url
-- existed. Safe to re-run: no-op if the column is already present (either
-- from this ALTER having run before, or because the CREATE TABLE above
-- already included it on a fresh install).
ALTER TABLE nse_filing_registry ADD COLUMN IF NOT EXISTS ixbrl_url TEXT;

-- ISSUE 6: idempotent migration for databases created before
-- ratios.needs_validation existed. Safe to re-run.
ALTER TABLE ratios ADD COLUMN IF NOT EXISTS needs_validation BOOLEAN NOT NULL DEFAULT FALSE;

-- ============================================================
-- RAW STORAGE (never overwritten, never discarded)
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_filings (
    raw_id              BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    source_url          TEXT NOT NULL,
    request_params      JSONB,
    response_timestamp  TIMESTAMPTZ NOT NULL,
    http_status         INT,
    content_type        VARCHAR(64),     -- xml / html / pdf / zip
    content_hash        VARCHAR(64) NOT NULL,
    storage_path        TEXT NOT NULL,   -- filesystem/object-store path to raw bytes
    parser_version       VARCHAR(32),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, content_hash)
);

CREATE TABLE IF NOT EXISTS raw_xbrl_documents (
    raw_xbrl_id         BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    raw_id              BIGINT REFERENCES raw_filings(raw_id),
    xbrl_format         VARCHAR(16) NOT NULL,  -- XBRL_XML / IXBRL_HTML
    sha256              VARCHAR(64) NOT NULL,
    storage_path        TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- XBRL LAYER — every fact and context, tag-alone lookups forbidden
-- ============================================================
CREATE TABLE IF NOT EXISTS xbrl_contexts (
    context_id          BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    context_ref         VARCHAR(128) NOT NULL,   -- as it appears in the source (e.g. "Duration2026Q1")
    entity_identifier   VARCHAR(64),
    period_start        DATE,
    period_end          DATE,
    instant_date        DATE,
    dimensions_json      JSONB,                   -- segment/scenario dimensions, verbatim
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, context_ref)
);

CREATE TABLE IF NOT EXISTS xbrl_units (
    unit_id             BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    unit_ref            VARCHAR(64) NOT NULL,
    measure             VARCHAR(64) NOT NULL,    -- e.g. INR, pure, shares, INR/shares
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, unit_ref)
);

CREATE TABLE IF NOT EXISTS xbrl_facts (
    fact_id             BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    context_id          BIGINT NOT NULL REFERENCES xbrl_contexts(context_id),

    namespace           VARCHAR(255),
    concept             VARCHAR(255) NOT NULL,   -- local name, e.g. RevenueFromOperations
    raw_tag             TEXT NOT NULL,           -- fully qualified as seen, e.g. in-capmkt:RevenueFromOperations

    raw_value           TEXT,                    -- exactly as extracted, pre-scale
    numeric_value        NUMERIC(30,4),           -- after applying scale/sign; NULL if non-numeric or unparseable

    unit_ref            VARCHAR(64),
    decimals            VARCHAR(16),
    scale               INT,
    sign                VARCHAR(4),

    dimensions_json      JSONB,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_facts_filing ON xbrl_facts (filing_id);
CREATE INDEX IF NOT EXISTS idx_facts_concept ON xbrl_facts (concept);
CREATE INDEX IF NOT EXISTS idx_facts_context ON xbrl_facts (context_id);

-- ============================================================
-- FINANCIAL PERIOD (the normalization anchor)
-- ============================================================
CREATE TABLE IF NOT EXISTS financial_periods (
    period_id           BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),

    period_type         VARCHAR(16) NOT NULL,   -- quarterly / annual
    statement_type      VARCHAR(16) NOT NULL,   -- standalone / consolidated

    period_start_date   DATE NOT NULL,
    period_end_date     DATE NOT NULL,

    financial_year      VARCHAR(9) NOT NULL,
    financial_quarter   VARCHAR(4),

    source_filing_id    BIGINT NOT NULL REFERENCES nse_filing_registry(filing_id),
    source              VARCHAR(24) NOT NULL,   -- INTEGRATED_FILING_IXBRL / XBRL / HTML / PDF

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (company_id, period_type, statement_type, period_end_date)
);

-- ============================================================
-- NORMALIZED STATEMENTS (all numeric fields NULLable; NULL = not reported)
-- ============================================================
CREATE TABLE IF NOT EXISTS income_statement (
    id                          BIGSERIAL PRIMARY KEY,
    period_id                   BIGINT NOT NULL UNIQUE REFERENCES financial_periods(period_id),

    revenue_from_operations      NUMERIC(24,2),
    other_operating_income       NUMERIC(24,2),
    other_income                 NUMERIC(24,2),
    total_income                 NUMERIC(24,2),

    cost_of_materials            NUMERIC(24,2),
    purchases                    NUMERIC(24,2),
    changes_in_inventory         NUMERIC(24,2),
    employee_benefit_expense     NUMERIC(24,2),
    finance_cost                 NUMERIC(24,2),
    depreciation                 NUMERIC(24,2),
    amortization                 NUMERIC(24,2),
    other_expenses               NUMERIC(24,2),
    total_expenses               NUMERIC(24,2),

    operating_profit             NUMERIC(24,2),
    profit_before_tax            NUMERIC(24,2),
    current_tax                  NUMERIC(24,2),
    deferred_tax                 NUMERIC(24,2),
    total_tax                    NUMERIC(24,2),

    profit_after_tax             NUMERIC(24,2),
    profit_for_period            NUMERIC(24,2),

    other_comprehensive_income   NUMERIC(24,2),
    total_comprehensive_income   NUMERIC(24,2),

    basic_eps                    NUMERIC(12,4),
    diluted_eps                  NUMERIC(12,4),
    eps_face_value               NUMERIC(12,4),

    unit                         VARCHAR(16) NOT NULL,   -- as reported in raw XBRL unit (e.g. INR); never assumed
    amount_multiplier_note       TEXT,                    -- e.g. "reported in Lakhs per filing header" — informational only

    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS balance_sheet (
    id                              BIGSERIAL PRIMARY KEY,
    period_id                       BIGINT NOT NULL UNIQUE REFERENCES financial_periods(period_id),

    equity_share_capital            NUMERIC(24,2),
    other_equity                    NUMERIC(24,2),

    long_term_borrowings            NUMERIC(24,2),
    other_long_term_liabilities     NUMERIC(24,2),
    deferred_tax_liabilities        NUMERIC(24,2),
    long_term_provisions            NUMERIC(24,2),

    short_term_borrowings           NUMERIC(24,2),
    trade_payables                  NUMERIC(24,2),
    other_current_liabilities       NUMERIC(24,2),
    short_term_provisions           NUMERIC(24,2),

    total_liabilities               NUMERIC(24,2),

    property_plant_equipment        NUMERIC(24,2),
    capital_work_in_progress        NUMERIC(24,2),
    goodwill                        NUMERIC(24,2),
    intangible_assets               NUMERIC(24,2),
    non_current_investments         NUMERIC(24,2),
    deferred_tax_assets             NUMERIC(24,2),
    other_non_current_assets        NUMERIC(24,2),

    current_investments              NUMERIC(24,2),
    inventories                      NUMERIC(24,2),
    trade_receivables                NUMERIC(24,2),
    cash_and_cash_equivalents        NUMERIC(24,2),
    bank_balances                    NUMERIC(24,2),
    other_current_assets             NUMERIC(24,2),

    total_assets                     NUMERIC(24,2),

    unit                             VARCHAR(16) NOT NULL,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cashflow_statement (
    id                          BIGSERIAL PRIMARY KEY,
    period_id                   BIGINT NOT NULL UNIQUE REFERENCES financial_periods(period_id),

    cfo                          NUMERIC(24,2),
    cfi                          NUMERIC(24,2),
    cff                          NUMERIC(24,2),
    net_change_in_cash           NUMERIC(24,2),
    opening_cash_balance         NUMERIC(24,2),
    closing_cash_balance         NUMERIC(24,2),

    depreciation_addback         NUMERIC(24,2),
    working_capital_changes      NUMERIC(24,2),
    interest_paid                NUMERIC(24,2),
    tax_paid                     NUMERIC(24,2),
    purchase_of_ppe              NUMERIC(24,2),
    sale_of_ppe                  NUMERIC(24,2),
    investments_net              NUMERIC(24,2),
    borrowings                   NUMERIC(24,2),
    repayments                   NUMERIC(24,2),
    dividends_paid                NUMERIC(24,2),

    unit                         VARCHAR(16) NOT NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ratios (
    id                  BIGSERIAL PRIMARY KEY,
    period_id           BIGINT NOT NULL REFERENCES financial_periods(period_id),
    ratio_name          TEXT NOT NULL,
    value               NUMERIC(20,6),
    unit                VARCHAR(16),
    source_concept      TEXT,
    -- ISSUE 6: raw XBRL ratio value is ALWAYS preserved unchanged (never
    -- rescaled) even when it looks scale-suspicious (e.g. a value ~100x
    -- smaller than NSE's own presentation HTML shows for the same filing
    -- — see validators.financial_validator.validate_ratio_plausibility).
    -- This flag is how "suspicious, don't use yet" is recorded without
    -- ever touching the value itself; any downstream fundamental/ML
    -- feature pipeline MUST filter on this (see
    -- repositories.financial_repository.get_validated_ratios) rather
    -- than querying this table directly.
    needs_validation    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (period_id, ratio_name)
);

CREATE TABLE IF NOT EXISTS segment_financials (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    period_id           BIGINT NOT NULL REFERENCES financial_periods(period_id),
    statement_type      VARCHAR(16) NOT NULL,
    segment_name        TEXT NOT NULL,
    concept             VARCHAR(64) NOT NULL,   -- revenue / result / assets / liabilities
    value               NUMERIC(24,2),
    unit                VARCHAR(16),
    source_concept      TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (period_id, statement_type, segment_name, concept)
);

-- ============================================================
-- SHAREHOLDING (separate periodic dataset, never mixed into financials)
-- ============================================================
CREATE TABLE IF NOT EXISTS shareholding (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    as_of_date          DATE NOT NULL,
    category            TEXT NOT NULL,          -- Promoters / Public / FPI / DII / ...
    shares_held         BIGINT,
    percentage          NUMERIC(8,4),
    pledged_shares      BIGINT,
    encumbered_shares   BIGINT,
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, as_of_date, category)
);

-- ============================================================
-- CORPORATE ACTIONS
-- ============================================================
CREATE TABLE IF NOT EXISTS corporate_actions (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    symbol              VARCHAR(32) NOT NULL,
    announcement_date   DATE,
    record_date         DATE,
    ex_date             DATE,
    action_type         VARCHAR(32) NOT NULL,  -- DIVIDEND / BONUS / SPLIT / RIGHTS / BUYBACK / FACE_VALUE_CHANGE / MERGER / DEMERGER / OTHER
    ratio               TEXT,
    amount              NUMERIC(14,4),
    description         TEXT,
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- PRICE HISTORY (separate pipeline, separate table)
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    trade_date          DATE NOT NULL,
    open                NUMERIC(14,4),
    high                NUMERIC(14,4),
    low                 NUMERIC(14,4),
    close               NUMERIC(14,4),
    adjusted_close       NUMERIC(14,4),
    volume              BIGINT,
    traded_value         NUMERIC(20,2),
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, trade_date)
);

-- ============================================================
-- DERIVED METRICS — clearly separated from source data (spec §44)
-- ============================================================
CREATE TABLE IF NOT EXISTS derived_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          BIGINT NOT NULL REFERENCES companies(company_id),
    period_id           BIGINT REFERENCES financial_periods(period_id),
    metric_name         TEXT NOT NULL,          -- revenue_growth_yoy / roce / cfo_to_pat / ...
    value               NUMERIC(20,6),
    computed_from       JSONB NOT NULL,         -- which source period_ids / fields fed this
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    pipeline_version    VARCHAR(32),
    UNIQUE (company_id, period_id, metric_name)
);

-- ============================================================
-- QUALITY / OPS LOGS
-- ============================================================
CREATE TABLE IF NOT EXISTS data_quality_log (
    id                  BIGSERIAL PRIMARY KEY,
    filing_id           BIGINT REFERENCES nse_filing_registry(filing_id),
    period_id           BIGINT REFERENCES financial_periods(period_id),
    check_name          TEXT NOT NULL,          -- e.g. "income_reconciliation"
    severity            VARCHAR(16) NOT NULL,   -- ERROR / WARNING / INFO
    message             TEXT NOT NULL,
    details_json        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              UUID NOT NULL,
    company_id          BIGINT REFERENCES companies(company_id),
    symbol              VARCHAR(32),
    stage               VARCHAR(32) NOT NULL,   -- DISCOVERY / DOWNLOAD / PARSE / NORMALIZE / VALIDATE / STORE
    status              VARCHAR(16) NOT NULL,   -- STARTED / SUCCESS / FAILED / PARTIAL
    period_end_date     DATE,
    statement_type      VARCHAR(16),
    source              TEXT,
    url                 TEXT,
    error_message       TEXT,
    exception_type      TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_run ON ingestion_log (run_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_log_company ON ingestion_log (company_id, stage, status);
