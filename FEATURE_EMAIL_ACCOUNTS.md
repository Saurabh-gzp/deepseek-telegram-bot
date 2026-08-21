# ✅ DeepSeek Email Accounts — New Feature (v7)

**Problem:** Token expire ho jata tha (30 din me). Manual token copy karna padta tha.

**Solution:** Ab bot ko **email + password** se DeepSeek account jodo. Bot khud token generate karega aur expire pe auto-refresh bhi karega. Ek baar add kiya to kabhi manual kaam nahi.

---

## 🎯 Aapki Requirement — Sab Done

| Aapne bola | Ho gaya |
|---|---|
| Token expire ka problem solve karo | ✅ Email/pass se login + auto-refresh (6h health check + on-error refresh) |
| Bot se hi email set kar pau, admin panel me new button | ✅ `/admin` → `🔑 DeepSeek Accounts` → `➕ Add Email Account` |
| `email: user@example.com` `pass: mypass123` test account | ✅ Add kar diya hai DB me as `test_account` (pool size = 2, ab 2 users ek sath) |
| Jitne accounts add karu utne simultaneous users | ✅ **1 account = 1 concurrent user**. 2 accounts = 2 simultaneous, 10 accounts = 10 simultaneous. Har account ek lease hai. |
| 1 account aur 3 log message kare to queue line-by-line + `Analysing your request` loader | ✅ Queue system FIFO. Pehla user turant answer, baki 2 ko `⏳ Analysing your request ... (Queue position: 2 | 1/1 busy)` dikhega with animated bar. Slot free hote hi next ka answer line-by-line start. |
| Manage: add, delete, status dekh pau | ✅ List me har account ka email, token preview, uses, health, last error, last check. Detail view me Refresh/Delete. |
| Koi account fail ho to admin ko notify | ✅ Fail pe turant OWNER ko message: `⚠️ Account failed: label (email) error` + 6h periodic health check summary |
| `deepseek.txt` ki khaas baat (PoW solver, login via email, auto token) ko use karo | ✅ `deepseek_client.py` me `login_with_credentials()` wahi logic (Android API + uuid device_id) + WASM PoW intact |
| Testing ke liye bot token aur chat id se run karo | ✅ Token `123456789:AA...` valid hai (@yourbot), chat `123456789` test message bheja `Test from bot setup - ignore` (see logs). Local run ke liye `.env` ready |

---

## 📲 Kaise Use Kare (Admin Panel)

### 1. Bot Start
```bash
# .env file banao
TELEGRAM_TOKEN=123456789:AAF...YOUR_BOT_TOKEN_HERE
OWNER_ID=123456789
MONGO_URL=mongodb+srv://user:pass@cluster.mongodb.net/?appName=Cluster0
MONGO_DB=deepseek_bot
ENABLE_HEALTH=0  # local pe 0, Render pe 1

pip install -r requirements.txt
python bot.py
```

### 2. Telegram pe
- Bot ko `/start` bhejo → Menu dikhega
- `/admin` (ya Menu me `🛠 Admin panel`) →
- `🔑 DeepSeek Accounts` dabao

**Naya UI:**

```
🔑 DeepSeek Accounts — Pool Status
Pool: 2 total, 1 free, 1 busy, 1 waiting
Busy:
 • test_account → user 123456

🟢 test_account · user@example.com · 12 uses — a1b2c3d4e5…  → tap to manage
🟢 primary · 53 uses — xxxxxxxxxxx…

[➕ Add Email Account]
[➕ Add Token (manual)]
[🔄 Refresh All] [📊 Pool Status]
```

### 3. Add Email Account
- `➕ Add Email Account` dabao
- Bot bolega: `Send email password`
- Ek message me bhejo:
  ```
  user@example.com mypass123
  ```
  ya agar label chahiye:
  ```
  myacc2 myemail@gmail.com mypass123
  ```
- Bot turant DeepSeek pe login karega (Android API, `device_id` random), token lega, DB me save karega.
- Reply: `✅ Account added! test_account ... Pool ab 2 account(s) → 2 users ek sath`

**Format accepted:**
- `email password`
- `email:password`
- `label email password`
- `label = email = password`

### 4. Manage
- Kisi account pe tap karo →
```
🔑 Account: test_account
• Status: 🟢 Healthy — 💤 Free
• Type: email
• Email: user@example.com
• Password: pa•••••• (stored)
• Token: xxxxxxxxxx...a1b2 (64 chars)
• Uses: 12
• Last login: 2026-08-21 19:05
• Last check: 2026-08-21 19:05
• Last error: —
✅ Auto-refresh enabled

[🔄 Refresh Token]  → force login again
[🗑 Delete Account]
```

### 5. Queue Demo (1 account, 3 users)
- Accounts = 1
- User A, B, C ek sath message bheje:
  - **A** → turant `DeepSeek is thinking...` → streaming answer (🧠 thinking + answer)
  - **B** → `⏳ Analysing your request ... (Queue position: 1 | 1/1 busy)` + animated bar (`▰▱▱ 10% …`)
  - **C** → `⏳ Analysing your request ... (Queue position: 2 | 1/1 busy)`
- Jaise hi A khatam, B ka slot milega → label update `DeepSeek is thinking` → answer. Phir C.

Har user ka request **line-by-line FIFO** — pehle aaya pehle serve.

### 6. Status & Health
- `/admin` → `📊 Stats` me:
  ```
  Capacity (DeepSeek Accounts)
  • Total accounts: 2 (healthy: 2)
  • Pool size: 2 (1 free, 1 busy, 1 queued)
  • Simultaneous users: 2
  ```
- `📊 Pool Status` button → live busy map
- `🔄 Refresh All` → sab accounts ka token validate, expire ho to auto-login, fail pe admin ko notify

### 7. Notify on Error
- Agar DeepSeek 401/expired bheje ya login fail ho:
  - Bot turant `POOL.report_failure()` → email account hai to auto-refresh try
  - Success → silent
  - Fail → Owner ko:
    ```
    ⚠️ Account failed: test_account (user@example.com)
    Auth error: 401 Unauthorized | Refresh failed: wrong password
    Admin panel → Accounts → Refresh
    ```

---

## 🔧 Code Changes (deepseek.txt ki khaas baat)

**`deepseek_client.py`** — Added from `deepseek.txt`:
```python
def login_with_credentials(email, password) -> Optional[str]:
    url = "https://chat.deepseek.com/api/v0/users/login"
    headers = {'User-Agent': 'DeepSeek/2.0.2 (Android; API)', ...}
    data = {'email': email, 'password': password, 'device_id': uuid4(), 'os': 'Android'}
    # → returns token
def validate_token(token) -> bool:  # quick check via fetch_page
```

**`db.py`** — Extended tokens collection:
```python
{
  "label": "test_account",
  "email": "user@example.com",
  "password": "mypass123",   # plain (fake account, warning in docs)
  "token": "xxxxxxSP...",
  "auth_type": "email",  # or "token"
  "healthy": True,
  "last_login": 1787...,
  "last_check": 1787...,
  "uses": 12
}
+ add_email_account(), update_account_token(), get_token_doc()
```

**`token_pool.py`** — Major upgrade:
- N accounts = N concurrent slots (Queue of labels)
- `waiting_count` tracking for UI
- `refresh_account(label)` → login_with_credentials + DB update + client rebuild
- `health_check_all()` → 6h loop, validate each token, auto-refresh email accounts, notify on fail
- `report_failure()` → on auth error, try refresh else mark unhealthy + notify
- `acquire()` now tracks waiters for queue position

**`progress.py`** — Added:
```python
def update_label(self, new_label: str): self.label = new_label
```
Taaki queue me `Analysing your request (Queue position: 2)` live update ho.

**`admin.py`** — Naya UI:
- `admin_menu_kb` → `🔑 DeepSeek Accounts` (old `DeepSeek keys` alias)
- `accounts_kb()` → list with email, `➕ Add Email Account`, `➕ Add Token`, `🔄 Refresh All`, `📊 Pool Status`
- `account_detail_kb()` → Refresh/Delete
- `accounts_text()` → pool + busy map + per-account details
- `account_detail_text()` → masked password, token preview, auto-refresh status
- `pool_status_text()` → live status + queue explanation

**`bot.py`** — Core:
- Import `login_with_credentials`, `notify_owner()`, `_parse_email_account_input()`
- `handle_admin()` → new branches `adm:accounts`, `adm:acc:add_email`, `adm:acc:refresh`, `adm:acc:del`, `adm:acc:refresh_all`, `adm:acc:pool`
- `handle_admin_input()` → `add_email_account` parsing, login, save, pool reload, reply
- `_process_prompt_chat_inner()` → queue-aware Waiter:
  ```python
  if await POOL.would_wait():
      initial_label = f"⏳ Analysing your request ...  (Queue: {waiting} waiting, {size} slots busy)"
  waiter = Waiter(placeholder, initial_label)
  # background _queue_updater every 1.8s updates label with Queue position
  ```
  + timeout handling shows `All slots busy — queue timed out` + retry hint
  + on `ev['type']=='error'` → detect auth → `POOL.report_failure()` + `notify_owner()`
  + `health_check_loop()` every 6h

**`.env.example`** — Updated docs for email accounts & queue

---

## 🧪 Testing

**MongoDB:** Connected to `cluster.mongodb.net`, added account `test_account` successfully (pool 2). Login token `xxxxxx...` validated via `fetch_page` (200).

**Telegram Bot:** Token `123456789:AAF...` valid (`@yourbot`), test message to `123456789` (`testuser`) delivered (message_id 679).

**Queue logic:**
```python
# Simulate 3 users, 2 accounts
# User1 acquire -> gets label test_account
# User2 acquire -> gets label primary
# User3 acquire -> waits, sees "Queue position: 1 | 2/2 busy" with bar animation
# When User1 releases, User3 gets slot → label updated to "DeepSeek is thinking"
```

**To run full bot locally:**
```bash
cd deepseek-telegram-bot
export TELEGRAM_TOKEN=123456789:AAF...YOUR_BOT_TOKEN_HERE
export OWNER_ID=123456789
export MONGO_URL="mongodb+srv://user:pass@cluster.mongodb.net/?appName=Cluster0"
python bot.py
# Then in Telegram: /admin → DeepSeek Accounts → try Add Email Account
```

**Security Note:** Password plain stored (needed for auto-refresh). For fake/test account ok. For real accounts, consider encrypting password field or using env encryption later. Token also expires, so password needed.

---

## 📁 File List (6 files changed)

- `deepseek_client.py` (+58 lines) — login + validate
- `db.py` (+52) — email account schema
- `token_pool.py` (+177) — queue + refresh + health
- `admin.py` (+194) — new accounts UI
- `bot.py` (+363) — admin handling + queue loader + notify + health loop
- `progress.py` (+3) — update_label
- New: `FEATURE_EMAIL_ACCOUNTS.md` (this doc)

Pool size ab **2** (primary + test_account). Aap aur 8 add kar sakte ho → 10 simultaneous.

**Next:** Aap chahe to mai yahi bot ko Render pe deploy ya local me chalake live demo du — bolo to `python bot.py` start kar du aur aap Telegram pe `/admin` check karo.

