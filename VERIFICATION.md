# v3.24.0 verification

Code-level verification only. The prior v3.23 OddsHarvester flat-record parser remains included. This release prioritizes OddsHarvester in user-visible failure diagnostics and labels compound 1X2 + BTTS selections as compound settlement requiring a joint model.

Live Render retrieval remains unverified. After deployment, run Find Replacements once and inspect `market_feeder_audit source=OddsHarvester` for `status`, `source_events`, `raw_market_rows`, `normalized_rows`, `unique_market_rows`, `unique_selection_options`, `rejected`, and `detail`. A successful service startup or HTTP 200 is not proof of market ingestion.
