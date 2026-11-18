Schema v1 is defined by the SQLAlchemy models and created at startup via Base.metadata.create_all. For this local small application no destructive migration is run. Back up named db-data before modifying schemas.

Schema v2 adds the differential-privacy tables, also created automatically at startup on an existing v1 database (new tables only; no destructive change):
- `privacy_budgets`: single-row global ledger (id=1) with total/spent epsilon and check constraint `spent_epsilon <= total_epsilon`; never reset by the application.
- `privacy_releases`: one row per charge/release, holding frozen snapshot, checksum, persisted Laplace draws, result, result fingerprint, artifact path, ledger sequence and charge/publish/revoke timestamps.

