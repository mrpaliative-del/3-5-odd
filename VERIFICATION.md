# v3.9.4 Verification

Local checks performed for this package:
- Python source compilation.
- Unit tests against synthetic inputs.
- ZIP integrity check.

These checks do not establish live Render deployment, external CSV availability, full competition coverage, bookmaker alternative-odds availability, or probability calibration.

Key safety changes: exact/explicit team matching (no substring guessing), a minimum five-match team history and two venue-specific matches, and explicit unresolved status when data quality is inadequate. The 70% ticket threshold is unchanged.
