# Wallex response fixtures

All files are real captures from api.wallex.ir. Public endpoints only, so they hold
no account identifiers or balances. Re-capture with `python scripts/capture_fixtures.py`
(markets subset, depth, trades) or `python scripts/probe_api.py --save-dir ...`.

| File | Source | Captured | Notes |
|---|---|---|---|
| `markets_subset.json` | `GET /hector/web/v1/markets` | 2026-10-21 | Envelope and market objects unmodified. Subset of the 469 markets: the 11 allowlisted, 3 blocked TMN markets (ZECTMN, DASHTMN, GRAMTMN) and BTCUSDT. |
| `markets_v1_subset.json` | `GET /v1/markets` (probe-verified, not in Swagger) | 2026-10-22 | Envelope and objects unmodified. Subset of 373 symbols: allowlist, their USDT pairs, ZECTMN, DASHTMN, GRAMTMN, BTCUSDT. Holds `minNotional`, `minQty`, `stepSize`/`tickSize` (decimal places) and `isMarketTypeEnable`. |
| `depth_v1_allowlist.json` | `GET /v1/depth?symbol=` (probe-verified) | 2026-10-22 08:36 UTC | `{symbol: full response}` for the 11 allowlisted markets, requested one after another (~1 s apart). 50 levels per side. |
| `trades_v1_btctmn.json` | `GET /v1/trades?symbol=BTCTMN` (probe-verified) | 2026-10-22 08:36 UTC | Last 50 public trades, newest first. |
| `udf_history_btctmn_60.json` | `GET /v1/udf/history` | 2026-10-21 | BTCTMN, resolution 60, 48 hourly bars. |
| `error_401_authorization_missing.json` | `GET /v1/account/openOrders` without key | 2026-10-21 | Error envelope `{code, message, result, success}`. |

`GET /depth/all` (in Swagger) answered HTTP 503 `text/html` on every probe
(2026-10-21 and 2026-10-22), so there is no capture of it. The code uses `/v1/depth`.
