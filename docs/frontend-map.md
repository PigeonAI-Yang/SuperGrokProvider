# Frontend Map

## Backend modules

| Module | Responsibility | Route |
| --- | --- | --- |
| Accounts | Add, authorize, select, reset and delete isolated Grok accounts | `/api/accounts` |
| Authorization | Run official `grok login --device-auth` and expose temporary progress | `/api/accounts/{id}/authorize` |
| Provider | Forward OpenAI-compatible traffic to the official xAI API | `/v1/*` |
| Settings | Show local provider URL, API key, routing policy and client-native configuration presets | `/api/config` |

## Core entities

- Account: `id`, `name`, `email`, `state`, `enabled`, timestamps and last error.
- Router config: active account, local API key, upstream base URL.

## Session and authentication

- Each account has an isolated `GROK_HOME` and is authorized by the official Grok Build CLI.
- The local provider requires its generated bearer key.
- Management routes bind to localhost and reject foreign browser origins.

## Main flows

1. Add label -> receive official device URL/code -> authorize -> account becomes ready.
2. Provider request -> active healthy account -> official xAI API -> transparent response.
3. Quota/auth failure -> quarantine account -> retry with the next healthy account.
4. Delete -> stop pending authorization -> official logout -> remove isolated account home.
