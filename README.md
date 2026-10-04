# telegram-notion-capture

[![ci](https://github.com/faceitall123qwe-hub/telegram-notion-capture/actions/workflows/ci.yml/badge.svg)](https://github.com/faceitall123qwe-hub/telegram-notion-capture/actions/workflows/ci.yml)

A Telegram bot I use to get ideas and tasks out of my head and into Notion. I send it a voice
note, a text or a photo, pick what it is (idea, task, note, todo, date), and it turns it into
a structured entry. Nothing is saved until I press Save.

```
voice / text / photo
  -> Whisper (voice) or Llama vision (photo), via Groq
  -> choose type
  -> Llama 3.3 70B in JSON mode, one schema per type
  -> preview with [Save] [Redo] [Cancel]
  -> Notion: tasks go to a database with due date and priority, ideas and notes become pages
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # Telegram token, Groq key, Notion token and page/database IDs, ALLOWED_USER_IDS
python bot.py
```

The bot won't start if `ALLOWED_USER_IDS` is empty.

## Threat model

The bot has write access to my Notion and spends paid API quota, and anyone who finds its
username can message it. That's what this section is about.

**What needs protecting:** the Notion token, the Groq key, the personal context that's put
into the system prompt (my "About me" note), and the notes themselves.

**Trust boundaries:** Telegram users to the bot; the bot to Groq (data leaves my machine);
model output to Notion writes (the model's output is untrusted).

| Threat | Mitigation |
|---|---|
| A stranger uses the bot to write to Notion or burn API credits | Allowlist of Telegram user IDs on every message handler and every button callback. Empty allowlist = the bot refuses to start. |
| Someone triggers inline buttons from another chat | Callback handlers go through the same owner check, not just message handlers |
| Prompt injection in a forwarded message, voice note or text inside an image | The model has no tools and no network or file access. Its only output is a preview I have to approve before anything is written. |
| Broken or huge model output | JSON mode with a fixed schema per type; every Notion text field is cut to the API limit |
| Tokens leaking through logs | `httpx` logging is set to WARNING because at INFO it logs the Telegram URL, which contains the bot token. Secrets live only in `.env`. |
| Private notes sent to a third party | Accepted: audio, images and text are processed by Groq. I don't send it anything I wouldn't put in a cloud service. |
| Voice and photo files left on disk | Downloaded to temp files and deleted right after processing |

**Not covered yet:**
- No per-user rate limit. Fine with one allowed user, needed before allowing more.
- If my Telegram account is taken over, so is the bot. Telegram 2FA is the mitigation.
- The bot can't run commands or control the computer, on purpose. If it ever gets tools that
  act on the machine, that needs its own design: a fixed list of allowed commands instead of a
  shell, confirmation for each action, a low-privilege sandbox and an audit log.

## License

MIT
