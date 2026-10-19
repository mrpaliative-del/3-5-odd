# v3.9.3 verification record

- Python compilation: passed (`main.py`, `advanced_features.py`, `test_main.py`).
- Automated unit tests: 44 passed.
- ZIP integrity: passed after packaging.
- Missing model estimates remain unavailable in the lookup display; JavaScript no longer converts `null` into a false `0.0%` estimate.
- Team matching normalizes Unicode accents (including `æ`/`ae`), punctuation, common club prefixes/suffixes, and selected established alternate names.
- Every source selection is allocated as KEEP or REVIEW; the 70% KEEP threshold is unchanged.
- The booking-code lookup does not expose a complete verified alternative-market odds list. The dashboard states this limitation instead of implying that replacements were searched. It does not invent odds, candidates, or probabilities. REVIEW selections are excluded from reconstruction.
- Local tests do not prove live bookmaker lookup, public CSV availability, model calibration, or alternative-market replacement availability. No bets are placed.
