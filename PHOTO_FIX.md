# 🖼️ Photo / File Upload Fix — पूरी रिपोर्ट

## 🐛 असली दिक्कत क्या थी

मैंने खुद photo भेजकर test किया। यही निकला 👇

```
=== upload_file(): /tmp/test.txt  ===  fid= file-da98...  ✅ 0.8s
=== upload_file(): /tmp/test.png  ===  fid= None          ❌ 61.5s
```

Photo 61 सेकंड तक अटकी रही, फिर बिना बताए fail हो गई।

### Status polling करके पकड़ा

```
poll 0  status=PARSING
poll 1  status=CONTENT_EMPTY   ← ये आया
poll 2  status=CONTENT_EMPTY
...
poll 11 status=CONTENT_EMPTY   ← 60s तक यही घूमता रहा
```

### Root cause — पुराना code

```python
while status not in ('SUCCESS', 'FAILED') and _time.time() < deadline:
```

`CONTENT_EMPTY` न `SUCCESS` है, न `FAILED` → **loop कभी रुका ही नहीं**।
पूरे 60 सेकंड घूमा, फिर `return None, None` — कोई reason नहीं बताया।

Bot में सिर्फ ये दिखता था:
> ❌ Upload failed or file couldn't be parsed by DeepSeek.

---

## 💡 दूसरी बड़ी बात — DeepSeek सिर्फ OCR करता है

ये बहुत ज़रूरी है समझना:

| Photo का प्रकार | चलेगा? | क्यों |
|---|---|---|
| Screenshot, notes, bill, document scan | ✅ | साफ़ text है, OCR पढ़ लेता है |
| Selfie, scenery, meme, logo, blank image | ❌ | कोई readable text नहीं → `CONTENT_EMPTY` |

**DeepSeek का web upload असल में vision model नहीं है — वो सिर्फ image से text
निकालता है (OCR)।** ये GitHub issue #415 में भी confirmed है, और आज भी open है।

मेरा test इसे साबित करता है:

| Test image | Status | Time |
|---|---|---|
| 8x8 blank red PNG | `CONTENT_EMPTY` ❌ | — |
| Real photo with handwritten text | `SUCCESS` ✅ | 4s |

---

## ✅ क्या fix किया

### 1. सारे terminal statuses handle किए (`deepseek_client.py`)

```python
_TERMINAL_OK   = {'SUCCESS'}
_TERMINAL_FAIL = {
    'FAILED', 'CONTENT_EMPTY', 'CONTENT_TOO_LONG', 'UNSUPPORTED',
    'AUDIT_FAILED', 'AUDIT_BLOCKED', 'PARSE_FAILED', 'EXPIRED',
}
```

अब loop तुरंत रुकता है — 60s का इंतज़ार ख़त्म।

### 2. नया `upload_file_ex()` — असली reason बताता है

```python
fid, fname, err = ds.upload_file_ex(path)
```

पुराना `upload_file()` भी काम करता रहेगा (backwards compatible)।

### 3. Smart polling

- Exponential backoff: 0.5s → 3s (server पर कम load)
- Timeout 60s → 90s (बड़ी PDF के लिए)
- Network error पर `break` नहीं, `continue` — hiccup से fail नहीं होगा

### 4. Bot में साफ़ message + guidance (`bot.py`)

अब photo fail होने पर ये दिखेगा:

> ❌ **Upload nahi ho paaya**
>
> DeepSeek is image se koi text nahi nikaal paaya.
> DeepSeek ka file upload sirf OCR karta hai — photo me saaf padhne
> layak likhaai honi chahiye.
>
> **💡 Photo bhejne ke liye:**
> • Screenshot, document scan, notes, bill — ye chalega ✅
> • Selfie, scenery, meme, logo — ye nahi chalega ❌
> • Photo ko **Document/File** ki tarah bhejo (compress mat hone do)

---

## 🧪 Fix के बाद के results

```
=== TEXT FILE ===                      fid: file-9a04...  err: None   1.0s  ✅
=== BLANK IMAGE (was hanging 60s) ===  fid: None          err: साफ़ msg  5.3s ✅
=== REAL PHOTO w/ text ===             fid: file-af67...  err: None   5.9s  ✅
```

**61.5s का hang → 5.3s में clear error.**

### End-to-end proof (photo → DeepSeek → answer)

```
1) uploading real photo…   fid: file-98fd0947-...  err: None
2) creating chat session…  sid: 61875578-...
3) asking DeepSeek to read the image…

=== DEEPSEEK ANSWER ===
Is image mein Exactly ye likha hai:

    Hello
    DeepSeek
    Test 2026

Yani teen alag-alag lines mein:
1. Hello  2. DeepSeek  3. Test 2026
```

पूरा pipeline चल रहा है — upload → parse → chat → सही जवाब। ✅

---

## 📌 इस्तेमाल करते वक़्त ध्यान रखो

1. **Mode सही रखो** — Expert mode में files **blocked** हैं।
   Instant 🚀 या Vision 👁️ इस्तेमाल करो।
2. **Photo को Document बनाकर भेजो** — Telegram photo compress कर देता है,
   उससे OCR की quality गिरती है। Attach → File चुनो।
3. **Text वाली photo ही भेजो** — ये DeepSeek की limitation है, bot की नहीं।

---

## 🚀 Push करने के लिए

Commit local हो चुका है (`445d52e`)। GitHub पर भेजने के लिए:

```bash
cd ~/deepseek-telegram-bot
git push https://<NEW_GITHUB_TOKEN>@github.com/Saurabh-gzp/deepseek-telegram-bot.git main
```

⚠️ पुराना token chat में leak हो चुका है — पहले उसे revoke करके नया बनाओ।
