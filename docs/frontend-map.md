# Frontend Map

## Backend modules

| Module | Responsibility | Route |
| --- | --- | --- |
| Accounts | Add, authorize, select, reset and delete isolated Grok accounts | `/api/accounts` |
| Agents | Own Provider keys, route locks and ordered account-group queues | `/api/agents`, `/api/config?agent_id=...` |
| Groups | Create, rename, isolate, reorder and delete routing pools; move accounts between them | `/api/groups`, `/api/accounts/{id}/move` |
| Authorization | Run official `grok login --device-auth` and expose temporary progress | `/api/accounts/{id}/authorize` |
| Provider | Forward OpenAI-compatible traffic to the official xAI API | `/v1/*` |
| Settings | Show local provider URL, API key, routing policy and client-native configuration presets | `/api/config` |

## Core entities

- Account: `id`, `name`, `email`, `state`, `enabled`, timestamps and last error.
- Agent: `id`, `name`, kind, local API key, active group and aggregate counts.
- Router group: `id`, `agent_id`, `name`, enabled state, position, active account and counts.
- Router config: selected Agent/group, Agent API key and upstream base URL.

## Session and authentication

- Each account has an isolated `GROK_HOME` and is authorized by the official Grok Build CLI.
- The local provider requires its generated bearer key.
- Management routes bind to localhost and reject foreign browser origins.

## Main flows

1. Add label -> receive official device URL/code -> authorize -> account becomes ready.
2. Provider Key -> Agent -> enabled groups in order -> healthy account -> official xAI API -> transparent response.
3. Quota/auth failure -> quarantine account -> retry within the group, then advance to the next enabled group.
4. Delete -> stop pending authorization -> official logout -> remove isolated account home.
