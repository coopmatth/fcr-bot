# Polygon

- `custom.polygon` connects Muse to the Polygon.io REST API (the company now also brands itself as Massive) for US stock market data, including consolidated minute aggregates.

- It authenticates with the registered `api_key` credential, which the API reads as the `apiKey` query parameter, and it calls only `api.polygon.io`.

- The helper is `~/workspace/custom-connectors/custom.polygon/bin/poly.py`. It offers a harmless auth check (`ping`) and a paginated aggregates fetch (`aggs`) that writes bars to a JSONL file.

## Connect

- The connection was registered with `credentials.request_api_access` and these details. Reuse them unchanged to reconnect:

```json
{
  "provider": "polygon",
  "auth_scheme": "api_key",
  "placement": "query_param:apiKey",
  "api_hosts": ["api.polygon.io"]
}
```

- Before the first change in a conversation, prove access with a harmless read:

```text
python3 ~/workspace/custom-connectors/custom.polygon/bin/poly.py ping
```

- A good `ping` prints status OK with the SPY reference record (ticker SPY, name State Street SPDR S&P 500 ETF Trust, market stocks).

## Credentials

- The registered connection holds the credential. Helpers import `/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`, which returns an `hsurr:` surrogate and places it where the provider reads the key. Muse swaps in the real credential only on an approved request to a registered host.

- Never put a key, token, password or cookie in this doc, a helper, a command argument, an environment variable or a log. Never ask the user to paste one in chat.

- Keep redirects disabled on authenticated requests.

- On a 401 or 403, check the request first. Confirm that the helper attached the credential, that the host is registered and that the placement and scopes match.

- Only when a request that carried the credential is still rejected, call `credentials.request_api_access` with the details above and `"reconnect": true`.

## Check access with ping

- Run `ping` to verify the credential works before any real work. It reads one reference record and changes nothing.

```text
python3 ~/workspace/custom-connectors/custom.polygon/bin/poly.py ping
```

- The output is a small JSON object with the response status and the ticker, name and market fields of the first result.

## Fetch aggregates with aggs

- Run `aggs` to download range aggregates for one ticker into a JSONL file, one bar per line. Use it for historical backtests that need a consolidated tape.

```text
python3 ~/workspace/custom-connectors/custom.polygon/bin/poly.py aggs --ticker SPY --from 2024-10-21 --to 2026-10-20 --out ~/workspace/fcr-bot/data/spy_1m_polygon.jsonl
```

- The flags are `--ticker` (required), `--multiplier` (default 1), `--timespan` (default minute), `--from` and `--to` as YYYY-MM-DD (required) and `--out` (required). The from and to dates behave as inclusive Eastern calendar days, as observed on the verified pulls.

- Each output line is one bar exactly as the API returned it, with fields `t` (bar start, milliseconds since the Unix epoch in UTC), `o`, `h`, `l`, `c`, `v`, `vw` and `n`.

- The request fixes `adjusted=false`, so prices are raw and unadjusted for splits and dividends. Keep that convention when comparing against other raw feeds, and do not mix adjusted and unadjusted series in one study.

- Pages hold up to 50,000 bars. The helper follows the response `next_url` until it is absent and reattaches the credential to every page request.

- The helper paces itself to at most one call every 13 seconds, which keeps it under the Stocks Basic plan limit of 5 calls per minute. A 24 month pull of SPY minute bars took 9 pages and about four minutes.

- Prices can carry sub-penny precision and the `v` volume field can be fractional on rare bars. Both are properties of the SIP source data, not corruption.

- Verified 2026-10-21: the SPY pull above returned 431,824 bars, and an independent re-pull of one week returned bars identical to the main pull. Daily SPY volume in this data sums to tens of millions of shares, matching the real consolidated tape.

## Recover from errors

- A 401 or 403 means the credential was missing or rejected. Confirm the helper attached it and that the placement above still matches, then reconnect only if a request that carried the credential is still rejected.

- A 429 means the plan rate limit was hit. The helper already paces its calls, so wait a minute and rerun rather than tightening the loop.

- After a failed or timed-out fetch, check whether the output file was partially written before rerunning, because earlier pages may have completed. The `aggs` command rewrites its output file from scratch on each run.

## Limits

- The connected plan is Stocks Basic. It allows 5 API calls per minute and 2 years of historical data, and its data is end of day rather than real time, so the current day's bars may not be available until the day closes.

- The helper covers reference lookup and aggregates only. Trades, quotes, snapshots and WebSocket streaming are not implemented in the helper, even where the plan might allow them.

- End of day availability was verified for completed days through 2026-10-20. Intraday freshness on Basic is untested and should not be assumed.

## Related skills

- Use `muse.skill_search` to find other skills and `muse.connector_search` to find other connectors.
