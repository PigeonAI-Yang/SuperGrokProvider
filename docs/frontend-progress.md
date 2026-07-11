# Frontend Progress

## Current Phase

Delivered

## Completed Loops

- Official `GROK_HOME` isolation and device authorization probe.
- Account add, authorize, list, select, reset, enable and delete lifecycle.
- Transparent `/v1/*` forwarding with quota-aware rotation.
- 1280×720 App-mode UI with pagination and connection-details drawer.
- Windows system proxy enforcement without direct fallback while enabled.
- Account deletion repaired with an in-app confirmation and pending/error feedback.
- DELETE now consumes optional request bodies, preventing keep-alive refresh requests from failing with 501; refresh errors are no longer swallowed.
- Official billing usage polling runs at startup and every 30–35 minutes, with manual refresh, reset time display and automatic exhausted-state recovery.
- The active marker now moves atomically to the account that actually handled the latest Provider request; UI reflects it within 2 seconds.
- A secondary client-config drawer now generates copyable, client-native reasoning presets for Zcode Desktop, Hermes and Grok Build while preserving transparent upstream request bodies.
- Independent launcher and backend singleton locks prevent duplicate windows, tray hosts and listeners.
- The top-left brand and account workspace now share the same 1180px shell gutter.
- ZCODE, GROK BUILD and HERMES now have independent Provider keys and Agent-level route locks. Each Agent supports ordered account-group fallback, isolation toggles, reordering, create/rename/delete and cross-Agent account moves.
- Account rows now open a keyboard-accessible details drawer; account names can be validated and persisted without reauthorization.
- The account drawer now queries live xAI models and labels documented reasoning effort, fixed reasoning and non-applicable media models without guessing unknown levels. Transient transport failures retry twice; quota-blocked accounts show an explicit same-Agent reference result.

## Current Loop

- None.

## Next Loop

- None.

## Risks

- One account cannot belong to multiple groups by design; move it to the Agent that should own its traffic.

## Blockers

- None.
