# 🤖 DeepSeek Telegram Bot

A powerful, button-driven personal Telegram bot backed by **DeepSeek** — with voice, files, URL/YouTube summarize, personas, and much more.

## ✨ Features

| Feature | Description |
|---|---|
| 🎭 **10 Personas** | Default · Tutor · Coder · Dost · Writer · Translator · Comedian · Scientist · Startup Coach · Health Info · 💕 Companion |
| 🎤 **Voice input** | Whisper `small` model (offline, Hinglish-aware) |
| 🔊 **Voice output** | edge-tts → OGG-Opus voice messages (Male/Female, Hindi/English auto) |
| 🔗 **URL summarize** | Paste any http(s) link → auto-fetch + summarize |
| ▶️ **YouTube summarize** | YT link → transcript → summary |
| ⚡ **Quick actions** | On every reply: Translate/Summarize/Rephrase/Explain/Continue |
| 🚀 **Modes** | Instant · Expert · Vision |
| 🧠 **Thinking chain** | Live rolling ticker while the model thinks |
| 🌐 **Web search** | Real-time (Instant mode only) |
| 📎 **Files** | Text, PDF, images — auto-polls DeepSeek until parsing succeeds |
| 📄 **Long-response handling** | Auto multi-bubble split, or `.md` file for 10 000+ char answers |
| 📤 **Export chat** | Download entire history as Markdown |
| 📊 **Usage stats** | Message count, char counts |
| 🩺 **Health-check server** | Built-in HTTP endpoint for Render/Fly.io |
| 🛡 **Owner-only** | Bot replies only to your Telegram user ID |
| 📝 **Markdown → Telegram HTML** | Bold, italic, code, links, blockquotes render properly |
| 🎨 **Button-only UI** | Just two slash commands (`/start`, `/help`) — everything else is inline buttons |

## 🚀 Deploy on Render (free, 5 minutes)

### 1. Push this repo to GitHub (already done for you)

### 2. Create a Render account

Go to [render.com](https://render.com) → sign in with GitHub.

### 3. New Web Service

- **New +** → **Web Service** → connect your GitHub repo
- Render auto-detects `render.yaml`; click **Apply**

### 4. Set your environment variables

In the service's **Environment** tab, add these **Secret** values (do not commit them):

| Key | Value |
|---|---|
| `TELEGRAM_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `DEEPSEEK_TOKEN` | Your DeepSeek session token (see below) |
| `OWNER_ID` | Your Telegram numeric user ID (from [@userinfobot](https://t.me/userinfobot)) |

`WHISPER_SIZE`, `ENABLE_HEALTH`, and `PYTHON_VERSION` are already set in `render.yaml`.

### 5. Deploy

Render builds and starts automatically. First run downloads the Whisper model (~500 MB) — takes a minute.

The bot sends you `🚀 Bot is LIVE!` in Telegram when it's ready. Send `/start` and enjoy.

### 6. Keep it awake (free tier)

Render free-tier Web Services sleep after 15 min of no HTTP traffic. Two easy fixes:

- **UptimeRobot** (free) → ping `https://<your-service>.onrender.com/health` every 5 min
- **Cron-job.org** → same idea

Both are free and take 30 seconds to set up.

---

## 🔑 Getting your DeepSeek token

1. Open [chat.deepseek.com](https://chat.deepseek.com) and log in
2. Open browser DevTools (F12) → **Application** tab → **Local Storage** → `https://chat.deepseek.com`
3. Find the `userToken` key — copy its value
4. Paste as `DEEPSEEK_TOKEN` in Render

This token expires after ~30 days. Refresh it whenever the bot starts saying "session expired".

---

## 💻 Run locally

```bash
git clone <this repo>
cd deepseek-telegram-bot
cp .env.example .env
# edit .env with your tokens

pip install -r requirements.txt
python bot.py
```

For local runs `ENABLE_HEALTH=0` is fine; no HTTP server needed.

---

## 🐳 Docker

```bash
docker build -t dsbot .
docker run -d --name dsbot \
  -e TELEGRAM_TOKEN=xxx \
  -e DEEPSEEK_TOKEN=xxx \
  -e OWNER_ID=123456789 \
  -p 10000:10000 \
  dsbot
```

---

## 🧪 Testing

```bash
python test_bot.py       # 60+ handler tests (offline, mocked)
python test_md.py        # Markdown → Telegram HTML (18 cases)
python test_client.py    # Real DeepSeek smoke test (needs DEEPSEEK_TOKEN)
python test_e2e.py       # Full pipeline: DeepSeek + TTS + Whisper + URL
```

---

## 📁 File layout

```
bot.py                 # Main entry point — Telegram bot logic
deepseek_client.py     # DeepSeek chat API + PoW WASM solver + file upload
md2tg.py               # Markdown → Telegram HTML converter
stt.py                 # faster-whisper wrapper (Speech-to-Text)
tts.py                 # edge-tts + OGG-Opus conversion (Text-to-Speech)
urlfetch.py            # trafilatura + youtube-transcript-api
personas.py            # 10 preset AI personalities
health.py              # aiohttp health server (for Render Web Service)
render.yaml            # Render deployment config
Dockerfile             # Container image build
requirements.txt       # Python dependencies
.env.example           # Environment variable template
```

---

## 🎛 Environment variables

| Name | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Bot token from @BotFather |
| `DEEPSEEK_TOKEN` | ✅ | — | DeepSeek session token |
| `OWNER_ID` | ✅ | — | Your Telegram numeric user ID |
| `WHISPER_SIZE` | ❌ | `small` | `tiny` / `base` / `small` / `medium` |
| `ENABLE_HEALTH` | ❌ | `0` | Set to `1` on Render / any HTTP host |
| `PORT` | ❌ | `10000` | Health server port |
| `STATE_FILE` | ❌ | `state.json` | Persistent per-user state file |

---

## 🐛 Known limits

- **DeepSeek Expert mode does not support file uploads or web search** — this is DeepSeek's own limitation
- The DeepSeek session token expires periodically — refresh from the browser
- Whisper `small` needs ~500 MB RAM; use `tiny` on very tight instances
- Telegram messages max out at 4096 chars per bubble — the bot auto-splits long answers or sends them as `.md` files
- **Companion persona** is warm/flirty but family-safe by design — NSFW content is refused

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Credits

- [DeepSeek](https://chat.deepseek.com) for the model
- [deepseek4free](https://github.com/xtekky/deepseek4free) and its fork by [@Doremii109](https://github.com/Doremii109/deepseek4free-fix) for reverse-engineering PoW + file upload
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [edge-tts](https://github.com/rany2/edge-tts), [trafilatura](https://github.com/adbar/trafilatura), [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
