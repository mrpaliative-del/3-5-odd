# SolveEase v3.10.5 Verification

- `python -m py_compile main.py advanced_features.py test_main.py`: PASS
- `python -m unittest -v`: PASS — 63 tests
- Special-market guards: early-goals, 1UP/2UP and compound markets are rejected by standard full-match models.
- Team-name alias regression checks: PASS (PSG/Paris Saint-Germain, Man Utd/Manchester United, Wolves/Wolverhampton Wanderers, Lillestroem/Lillestrom, Bodoe/Glimt/Bodo Glimt, Galatasaray Istanbul/Galatasaray, Brighton/Brighton and Hove Albion)
- Service version metadata updated to 3.10.5.
- Existing 36-leg ticket arithmetic regression retained.

Not tested by these checks: live SportyBet endpoint access, current bookmaker prices, external historical-feed availability, predictive calibration, or market settlement for 1UP/2UP and early-goal specials. Those remain explicitly unverified.
