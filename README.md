# TDS P1 — Data Analyst Telegram Bot

An LLM agent that receives a data-analysis question over Telegram, works out
the answer (fetching public data and running pandas where needed), and
replies with **exactly one bare JSON value** - the answer itself, nothing
wrapped around it.

## Answer format: a deliberate choice, documented here for the record

The actual grading harness is public:
[Jivraj-18/tds-p1-t2-2026-telegram-bot](https://github.com/Jivraj-18/tds-p1-t2-2026-telegram-bot).
Its code (`collect.py` + `grade.py`) exact-matches the entire reply against
a value computed *before* the bot runs, with no way to know the dynamic
`log_url` in advance - which means a reply containing `log_url` structurally
cannot pass that specific exact-match check, regardless of the value.

Despite that, this bot sends the PDF's literal two-key format -
`{"answer": ..., "log_url": "..."}` - as a single reply, per an explicit
decision to follow the assignment page's stated contract over the public
harness's apparent mechanics. The reasoning: the question text itself asks
for this exact shape, and the PDF's *"we download it for review"* language
suggests a second, LLM/human-based review layer beyond the simple
`grade.py` exact-match - so literal format compliance may matter more there
than to the auto-grader.

**If you want to verify this before the real deadline**, the local grader
setup (clone the harness repo, add a test question to
`evals/questions.json`, run `generate.py` → `collect.py` → `grade.py`) will
show you directly whether this format passes its exact-match check or not -
that's a stronger signal than either of our guesses.

## How it works

```
Telegram (long polling, no webhook needed)
      │
      ▼
bot/main.py  ── per-chat conversation history (in-memory, TTL'd)
      │
      ▼
bot/agent.py ── Gemini (primary) → AIPipe (fallback) chat completion
      │            with OpenAI-style tool calling
      ├── fetch_url(url)   (bot/tools.py) — pull MOSPI pages/CSVs etc.
      └── run_python(code) (bot/tools.py) — pandas/numpy, sandboxed subprocess
      │
      ▼
bot/gcs_logger.py ── uploads the run's JSONL trace to the same public GCS
                      bucket used for the Q3/Q4 tasks, returns its public URL
      │
      ▼
Telegram reply: {"answer": <shape the question asked for>, "log_url": "https://storage.googleapis.com/.../logs/<id>.jsonl"}
```

Long polling (`getUpdates`) is used instead of a webhook, so the bot needs
**no public HTTP endpoint of its own** — just a process that stays running
with outbound internet access. That's why deployment below is "a small
always-on VM", not a web service.

## 1. Create the Telegram bot

1. Message **@BotFather** on Telegram → `/newbot` → follow the prompts.
2. Pick a username ending in `bot` (this is what you register in the
   project submission).
3. Save the token it gives you — that's `TELEGRAM_BOT_TOKEN`.

## 2. Get the keys

- **Gemini**: an API key from Google AI Studio (same one you've used
  elsewhere in the course). Used via Gemini's OpenAI-compatible endpoint,
  so no extra SDK plumbing is needed.
- **AIPipe** (fallback only, optional but recommended): the AIPipe token,
  used only if the Gemini call errors out (quota/rate limit).
- **GCS bucket**: reuse the bucket you already created and made public in
  the Q3/Q4 tasks. If you want a dedicated one for logs, create it the same
  way (`gsutil mb`, then make it public-read) — see `deploy/setup_gce.sh`
  for the general pattern.

## 3. Local test

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then fill in real values with notepad/VS Code
python -m bot.main
```

(`.env` is loaded automatically via `python-dotenv` — no separate export step needed.)

Message the bot on Telegram directly to sanity-check it before wiring up
the grading harness. Then clone the public grading repo
(`tds-p1-t2-2026-telegram-bot`), point it at the bot username, and add a
few of the own questions to `evals/questions.json` to test against
realistic MOSPI-style prompts, including a multi-turn one, before you rely
on the shape of the official questions.

## 4. Set up a GCS service account key (needed once, off-GCP)

Since the bot isn't running on a GCE VM (no built-in service account to lean
on), create a dedicated key so it can write logs to the bucket:

```bash
gcloud iam service-accounts create tds-bot-uploader \
  --display-name="TDS P1 bot log uploader"

gcloud storage buckets add-iam-policy-binding gs://<the-bucket> \
  --member="serviceAccount:tds-bot-uploader@<the-project>.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud iam service-accounts keys create key.json \
  --iam-account=tds-bot-uploader@<the-project>.iam.gserviceaccount.com

base64 -w0 key.json   # copy this whole output — it's GOOGLE_APPLICATION_CREDENTIALS_JSON
```

Delete `key.json` locally once it's copied — you don't need the file, just
the base64 string, and it shouldn't sit around unencrypted.


```bash
curl -L https://fly.io/install.sh | sh   # installs flyctl
fly auth login

# from inside this repo directory:
fly launch --no-deploy   # detects the Dockerfile, uses fly.toml as-is;
                          # say "no" if it asks to create a Postgres/Redis db

fly secrets set \
  TELEGRAM_BOT_TOKEN="..." \
  GEMINI_API_KEY="..." \
  AIPIPE_API_KEY="..." \
  GCS_LOG_BUCKET="..." \
  GOOGLE_APPLICATION_CREDENTIALS_JSON="$(cat key.json | base64 -w0)"

fly deploy
fly logs -f   # watch it come up and confirm it's polling
```

`fly.toml` deliberately has no `[http_service]` block — Fly is fine running
an app that only makes outbound connections, it just keeps the machine
alive and restarts it if it crashes.

**Alternative**: Render's background worker tier or another small always-on
VM work the same way — same `Dockerfile`/`requirements.txt`, same env vars.

## 6. Register for grading

Submit, comma-separated:
- this repo's public GitHub URL
- the bot's `@username`

Keep the Fly.io machine running until grading completes — it restarts
automatically on crashes, but won't survive being explicitly stopped
(`fly scale count 0`) or the app being destroyed.

## Notes / limitations

- `deploy/tds-p1-bot.service` and `deploy/setup_gce.sh` are kept in the repo
  for the alternative path (a dedicated GCE VM) — ignore them if you're on
  Fly.io.

- `run_python` sandboxing blocks the dangerous builtins (`open`, `__import__`,
  etc.) and runs in a separate process with a timeout, but it is scoped to
  "don't let a wayward LLM step do something silly", not "safe against a
  hostile user" — the only caller is the own agent reacting to the
  official grading account.
- Conversation memory is in-process and resets if the bot restarts, and
  expires after `CONVERSATION_TTL_SECONDS` of chat inactivity — fine for the
  short multi-turn sequences described in the spec, not meant as durable
  storage.
- If both Gemini and AIPipe error out for a question, the bot replies with
  bare JSON `null` rather than hanging or sending malformed text — it won't
  score correctly (there's no real answer to give), but it won't compound
  that with a `format_error` either. Worth checking the journal logs if you
  see this during a test run.
