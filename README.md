# second-brain-bot

Personal Telegram capture bot: send a voice note, text or photo, pick a type
(idea / task / note / todo / date), get an LLM-structured preview, approve, and it
lands in Notion — tasks in a database with due dates, ideas and notes as pages.

```
Telegram (voice / text / photo)
   │  owner allowlist
   ▼
Groq Whisper large-v3 ── voice → text
Groq Llama vision ────── photo → description + OCR
   ▼
type picker → Llama 3.3 70B (JSON mode, per-type schema) → preview
   ▼
[Save] [Redo] [Cancel] ── human approval before any write
   ▼
Notion API (tasks DB / projects page)
```

## Run

```bash
pip install -r requirements.txt
cp .env.example .env      # fill tokens + ALLOWED_USER_IDS
python bot.py
```

## Threat model

The bot holds write credentials to a personal knowledge base and spends paid API
quota, and it is reachable by anyone who learns its @username. That shapes the model.

### Assets
- Notion integration token (write access to the shared page and tasks database)
- Groq API key (billable quota)
- Personal context from the Obsidian vault, injected into the system prompt
- Content of the captured notes themselves

### Trust boundaries
1. Telegram users → bot (untrusted network input)
2. Bot → third-party LLM/ASR APIs (data leaves the machine)
3. LLM output → Notion writes (model output is untrusted)

### Threats and controls

| Threat | Control |
|---|---|
| Stranger finds the bot and writes to Notion / burns API quota | `ALLOWED_USER_IDS` allowlist on every message **and** callback handler; the bot refuses to start with an empty list (fail closed) |
| Forged inline-button callbacks from another chat | Callback handlers are wrapped in the same owner check, not only message handlers |
| Prompt injection in a forwarded message, voice note or image text tries to exfiltrate the personal context or write junk | The model has **no tools** and no network or file access; its only output is a JSON preview shown to the owner, and nothing is written until the owner presses *Save* |
| Malformed or oversized LLM output | JSON mode + fixed per-type schema; every Notion rich-text field is truncated to the API limit (2000 chars) |
| Secrets leaking via the repo or logs | Tokens only in `.env` (gitignored); `httpx` logging raised to WARNING because its INFO lines contain the token-bearing Telegram URL |
| Sensitive notes sent to third parties | Documented trade-off: audio, images and text are processed by Groq. Don't capture anything you wouldn't send to a cloud API |
| Temp files with voice/photo data left on disk | Downloads go to `tempfile` and are deleted in `finally` after processing |

### Out of scope / known gaps
- No rate limit per user: acceptable with a single-owner allowlist, required before any
  multi-user use.
- Telegram account takeover of the owner = bot takeover; mitigate with Telegram 2FA.
- No command execution or desktop control by design. If the bot is ever extended with
  tools that act on the local machine, it needs a separate model: an explicit command
  allowlist (no free-form shell), per-action confirmation, a sandboxed low-privilege
  runner, and audit logging.
