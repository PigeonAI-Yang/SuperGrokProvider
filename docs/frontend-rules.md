# Frontend Rules

- API calls live in `static/app.js`; markup does not contain business rules.
- Every mutation shows pending, success and actionable failure states.
- Never render or log OAuth access/refresh tokens.
- Device codes are temporary and disappear after authorization finishes.
- Destructive account deletion requires explicit confirmation.
- Destructive confirmation uses an in-app dialog with visible pending and inline failure states, not browser-native `confirm()`.
- Keyboard focus, visible labels, contrast and reduced motion are required.
- One dark theme, one green accent, 12px controls and 16px surfaces.
- The 1280×720 shell never scrolls; account overflow uses pagination and secondary connection details use a drawer.
- No external fonts, images, analytics or CDNs.
- Client identity is selected only by a per-group API Key; never infer it from User-Agent or request bodies.
- Accounts have exactly one group. Empty or exhausted groups never fall back across group boundaries.
