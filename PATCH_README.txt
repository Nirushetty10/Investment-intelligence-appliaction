NSE PIPELINE BUG-FIX OVERLAY

This ZIP contains only changed/new files from the stabilization pass.
1. Back up your existing D:\Niranjan\Investment\nse_pipeline directory.
2. Extract this ZIP directly into D:\Niranjan\Investment\nse_pipeline and allow the listed changed files to merge/overwrite.
3. Activate your project's Python environment and run: python -m pytest tests/ -v --tb=short
4. The six PostgreSQL integration tests will still skip until NSE_DB_HOST, NSE_DB_PORT, NSE_DB_USER, and NSE_DB_PASSWORD point at a reachable dedicated test database. Never use production for migration tests.
5. Read BUGFIX_NOTES.md and CLEAN_DATA_PLAN.md before treating financial values as trusted. The supplied 2025 taxonomy catalog does not replace the exact 2026 taxonomy.

Validated in the working copy: 130 passed, 6 skipped. The six skipped tests are PostgreSQL integration tests; they have not been counted as passed.
