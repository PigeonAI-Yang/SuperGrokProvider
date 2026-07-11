# Frontend Spec

## Account management

- Add any number of named accounts.
- Authorization uses `grok login --device-auth` under an isolated `GROK_HOME`.
- Show official URL and one-time code while pending.
- Select active account, enable/disable, reset exhausted/error state and delete.
- Show at most five accounts per page so the fixed-height window never needs an internal list scrollbar.

## Window layout

- Launch in browser App mode at 1280×720.
- Lock the main shell height with no page or panel scrolling.
- Keep common Provider fields in the main panel and place secondary connection information in a right-side drawer.
- Put client-specific configuration presets in a second fixed-height drawer; never infer a client from User-Agent or rewrite its request body.

## Provider

- Bind to `127.0.0.1` only.
- Proxy `/v1/models`, `/v1/responses` and `/v1/chat/completions` without rewriting request bodies.
- Preserve normal JSON and SSE streaming responses.
- Atomically move the active-account pointer to the account that actually accepted the latest Provider request.
- Refresh through the official CLI after a 401, then retry once.
- Rotate on 402/429 or an explicit quota marker such as xAI's 403 `spending-limit`; do not rotate on arbitrary 4xx validation errors.
- Return an OpenAI-shaped error when no healthy account remains.
- Read the current Windows Internet Settings proxy for every upstream request.
- When the system proxy is enabled, use that proxy exclusively and never fall back to direct traffic.
- When the system proxy is disabled, do not inherit stale `HTTP_PROXY` or `ALL_PROXY` process variables.

## Usage monitoring

- Query the Grok Build billing endpoint without sending a model prompt.
- Check immediately at startup and every 600 seconds afterward.
- Store used percentage, period end, product breakdown, last check and monitoring error per account.
- Mark 100% accounts exhausted and restore them automatically after the official period resets.
- Support a manual per-account refresh from the account row.
- Refresh local UI state every 2 seconds so the active marker follows routing without triggering billing calls.

## Data protection

- Store metadata and isolated Grok homes under `%LOCALAPPDATA%\SuperGrokRouter` by default.
- Never expose upstream OAuth tokens through the management API.
- Require a generated local bearer key for `/v1/*`.
