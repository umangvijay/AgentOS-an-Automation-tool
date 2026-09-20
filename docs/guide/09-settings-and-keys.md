# Settings & Model API Keys

- **One key field for any provider** — Gemini, Claude, Grok, Z.ai, OpenAI. Every key is validated live against the provider *before* being stored (AES-256-GCM in Vault). Gemini powers the agent; Grok is the automatic quota fallback.
- **Autonomy level** 0–3 controls how much the agent may do without asking.
- **Theme** switches instantly (true dark glass theme).
- Security: *"Protected by industry-standard AES-256 and end-to-end client-side encryption"* — and it's true: secrets are encrypted in-browser (Web Crypto) and again at rest.
