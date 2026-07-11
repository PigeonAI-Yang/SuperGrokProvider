# Frontend Stack Decision

- Backend: Python 3.11 standard library.
- Frontend: semantic HTML, native CSS and vanilla JavaScript.
- State: server is the source of truth; UI polls only while authorization is pending.
- Tests: Python `unittest` plus browser-level smoke checks.
- Reason: this is one small localhost control surface. A framework adds installation and build failure modes without improving the account workflow.
