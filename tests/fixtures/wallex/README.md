# Wallex response fixtures

Captured from `scripts/probe_api.py` on 2026-10-21 (api.wallex.ir), unless noted.

| File | Source | Notes |
|---|---|---|
| `markets_subset.json` | `GET /hector/web/v1/markets` (real) | Envelope and market objects are unmodified. Only a subset of the 469 markets is kept: the 11 allowlisted markets, 3 blocked TMN markets (ZECTMN, DASHTMN, GRAMTMN) and 1 non-blocked USDT market (BTCUSDT). |
| `udf_history_btctmn_60.json` | `GET /v1/udf/history` (real) | Full response: BTCTMN, resolution 60, 48 hourly bars. |
| `error_401_authorization_missing.json` | `GET /v1/account/openOrders` without key (real) | Error envelope `{code, message, result, success}`. |
| `depth_all_synthetic.json` | **Synthetic**, built from Swagger `resources.AllMarketDepthResponse` | Real `/depth/all` returned HTTP 503 during capture. Replace with a real capture once the probe succeeds. |
