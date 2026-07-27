# 🔍 Project Audit — क्या fix हुआ + क्या कमियाँ बची हैं

**Date:** 2026-07-27 · **Commits:** `445d52e`, `b70c9e7`, `007ae97`

---

# ✅ PART 1 — इस session में जो fix हुआ

## 1. Progress bars — हर लंबे काम पे 🎯

नया **`progress.py`** module बनाया। दो classes:

### `Progress` — async context manager

```python
async with Progress(bot, chat_id, "📤 File upload",
                    steps=["Download", "Upload", "Parse"]) as p:
    await p.step(0, "2.3 MB")
    ...
# ← यहाँ bar अपने आप DELETE हो जाता है
```

Chat में ऐसा दिखता है:

```
📤 Photo: bill.jpg

▰▰▰▰▰▰▱▱▱▱  67%

✅ Telegram se download
✅ DeepSeek pe upload
⏳ Parse / OCR...

OCR chal raha hai

⏱ 8s
```

### तीन ज़रूरी behaviours (तीनों test किए)

| Case | क्या होता है | Test result |
|---|---|---|
| काम 0.6s से तेज़ | **कुछ भी नहीं भेजा जाता** (कोई flicker नहीं) | ✅ silent |
| काम पूरा हुआ | **Bar delete** → सिर्फ result बचता है | ✅ `DELETED? True` |
| Error आया | Bar लाल error बन जाता है और **रुकता है** | ✅ `DELETED? False` |

### कहाँ-कहाँ लगाया

| जगह | Steps |
|---|---|
| 📎 File / Photo upload | Download → Upload → Parse/OCR |
| 🎤 Voice input | Audio download → Whisper → भेजना |
| 🔊 **Voice output (Speak)** | Text साफ़ → आवाज़ generate → भेजना |
| 🔗 URL / YouTube | Fetch → Extract → भेजना |
| 📤 Chat export | Markdown → भेजना |
| 📄 Response→File | बनाना → भेजना |
| 📁 Chats list | "Loading…" |
| 💬 DeepSeek reply | `Waiter` — placeholder में ही animate |

**`Waiter`** अलग है: वो placeholder message को *जगह पर* animate करता है, फिर पहला token आते ही रुक जाता है और answer उसी message को overwrite कर देता है — कोई extra message नहीं।

**Live test** — तुम्हारे Telegram पे भेजकर verify किया:
```
--- LIVE TEST: progress bar in your Telegram ---
bar auto-deleted ✅
result message sent ✅
✅ TTS + progress bar: bar deleted, voice note sent
```

---

## 2. 🐛 `av` module missing (नई कमी पकड़ी)

TTS test करते वक़्त crash हुआ:
```
ModuleNotFoundError: No module named 'av'
```

`requirements.txt` में `av>=11.0.0` लिखा तो है, पर **अगर install fail हो जाए तो
Voice feature चुपचाप मर जाता है** और error सिर्फ log में जाता है। अब progress bar
साफ़ error दिखाता है। Install करके verify किया — `av 18.0.0` OK, voice note चला गया।

---

## 3. 🚨 Global Error Handler (सबसे बड़ी कमी थी)

**पहले:** `app.add_error_handler()` था ही नहीं। कोई भी unhandled exception →
update चुपचाप मर जाता, तुम dead chat देखते रहते।

**अब:**
- Exception → तुम्हें Telegram पे साफ़ message
- `Conflict` (दो bot instances चल रहे) → log में clear warning
- Network hiccups → ignore (library खुद retry करती है)

---

## 4. 💾 Atomic state save

**पहले:** `open(STATE_FILE,"w")` — लिखते वक़्त crash हुआ तो `state.json`
**corrupt**, सारी settings + session ID गायब।

**अब:** tmpfile → `fsync` → `os.replace()` (atomic). Crash से भी data safe।

---

## 5. ⚡ Concurrency 1 → 8 + per-user lock

**पहले:** `max_concurrent_updates = 1`. लंबा जवाब चल रहा हो तो सारे buttons dead।

**अब:** `.concurrent_updates(8)` — पर race condition से बचने के लिए हर user का
अपना `asyncio.Lock`. दूसरा message आए तो बताता है:

> ⏳ *Pichla jawab abhi chal raha hai — ye uske baad process hoga.*

`CONCURRENCY` env var से control होता है (`.env.example` + `render.yaml` में जोड़ा)।

---

## 6. पहले वाला photo fix (recap)

`CONTENT_EMPTY` status handle नहीं था → 61.5s hang। अब 5.3s में साफ़ error।
Detail `PHOTO_FIX.md` में।

---

# ⚠️ PART 2 — जो कमियाँ अभी बची हैं

## 🔴 गंभीर

### 1. DeepSeek token हमेशा के लिए नहीं चलेगा
Browser session token है — कुछ हफ़्तों में expire होगा। तब हर message पे
"No response" आएगा। **कोई auto-refresh नहीं है।** Manually DevTools से नया
token लेना पड़ेगा। *(अब कम-से-कम error साफ़ दिखता है।)*

### 2. `LAST` और `HISTORY` सिर्फ RAM में हैं
```python
LAST: Dict[int, dict] = {}       # disk पे save नहीं
HISTORY: Dict[int, List] = {}    # disk पे save नहीं
```
Bot restart → **पूरी history गायब**, Export/Regen/Quick-actions खाली।
Render free tier तो 15 min बाद sleep भी हो जाता है।

### 3. Render free tier पे Whisper crash करेगा
512MB RAM बनाम Whisper `small` = ~500MB + Python + PyAV.
**Voice input वहाँ काम नहीं करेगा।** `WHISPER_SIZE=tiny` करो या voice भूल जाओ।

### 4. कोई rate limiting नहीं
तेज़ी से 20 messages भेजे → 20 PoW subprocess + 20 API calls →
DeepSeek से **temporary ban**।

## 🟡 मध्यम

### 5. `_LOCKS` dict कभी साफ़ नहीं होता
Single-user bot में कोई दिक्कत नहीं, पर multi-user किया तो memory leak।

### 6. PoW solver हर request पे `node` process spawn करता है
~1-15s लगता है। Node process pool होता तो तेज़ होता।

### 7. Test files असल में tests नहीं हैं
`test_bot.py`, `test_e2e.py` — pytest नहीं, सिर्फ manual scripts।
कोई CI नहीं, कोई assertion coverage नहीं।

### 8. `md2tg.py` edge cases
Nested lists, tables, mixed RTL text ठीक से render नहीं होते।

### 9. File size check नहीं
Telegram 20MB तक देता है, DeepSeek की limit अलग है — पहले से check नहीं होता,
सीधा upload try करके fail होता है।

### 10. History disk पे नहीं, तो `MAX_HISTORY=100` का मतलब कम
RAM में 100 messages रखता है पर restart पे सब जाता है।

## 🟢 छोटी

11. Startup पे "🚀 Bot v4 LIVE!" हर बार भेजता है — Render restart पे spam
12. Vision mode और Instant mode में practically फ़र्क़ नहीं (दोनों OCR ही करते हैं)
13. `/help` text थोड़ा outdated है
14. Persona switch करने पर पुरानी chat उसी session में चलती रहती है
15. कोई `/cancel` command नहीं — चलता हुआ काम रोक नहीं सकते

---

# 📋 अगला कदम — priority के हिसाब से

| # | काम | फ़ायदा | मेहनत |
|---|---|---|---|
| 1 | HISTORY/LAST disk पे save करो | restart पे data बचेगा | कम |
| 2 | Rate limiting (per-minute cap) | token ban से बचाव | कम |
| 3 | Token expiry detect + alert | पता चलेगा कब बदलना है | कम |
| 4 | `/cancel` command | अटका काम रोक सको | कम |
| 5 | File size pre-check | fail होने से पहले पता चले | कम |
| 6 | Startup message सिर्फ पहली बार | spam बंद | बहुत कम |
| 7 | असली pytest tests + CI | भरोसा | ज़्यादा |

---

# 🚀 Push करने के लिए

3 commits तैयार हैं:
```
007ae97  Add error handler, atomic state writes, concurrency + per-user locks
b70c9e7  Add animated self-deleting progress bars for all long operations
445d52e  Fix file/photo upload: handle CONTENT_EMPTY and other statuses
```

```bash
cd ~/deepseek-telegram-bot
git push https://<NEW_TOKEN>@github.com/Saurabh-gzp/deepseek-telegram-bot.git main
```

⚠️ पुराना GitHub token chat में leak हो चुका है — revoke करके नया बनाओ।
