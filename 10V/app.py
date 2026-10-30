"""WISE VIRGINS backend. Run: uvicorn app:app --reload
Env: PAYMENT_MODE=mock|daraja, REPO_URL, PAY_NUMBER, CALLBACK_SECRET, PUBLIC_URL,
DARAJA_KEY, DARAJA_SECRET, DARAJA_SHORTCODE, DARAJA_PASSKEY, DARAJA_BASE (sandbox default)
"""
import os, sqlite3, secrets, base64, random, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import closing
import httpx
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import RedirectResponse, FileResponse

E = os.environ.get
MODE = E("PAYMENT_MODE", "mock")
REPO_URL = E("REPO_URL", "https://github.com/your-name/your-repo")
PAY_NUMBER = E("PAY_NUMBER", "0105636227")
CB_SECRET = E("CALLBACK_SECRET", "change-me")
DARAJA = E("DARAJA_BASE", "https://sandbox.safaricom.co.ke")
EAT = ZoneInfo("Africa/Nairobi")
DB = E("DB_PATH", "wise.db")

# Package terms are assumptions: edit freely. uses = max link opens, days = link lifetime.
PACKAGES = {
    "01": dict(name="ONE-TIME ACCESS", price=1400, uses=1, days=7, note="Single-use repository link, valid 7 days."),
    "02": dict(name="FULL-TIME ACCESS", price=5000, uses=10**6, days=3650, note="Permanent repository link."),
    "03": dict(name="LITE ACCESS", price=700, uses=3, days=30, note="Repository link, 3 opens, valid 30 days."),
}

app = FastAPI()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with closing(db()) as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(token TEXT PRIMARY KEY, created REAL);
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, token TEXT, package TEXT, amount INT,
        phone TEXT, checkout_id TEXT UNIQUE, receipt TEXT, status TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS snippets(token TEXT PRIMARY KEY, payment_id INT, message TEXT, created REAL);
    CREATE TABLE IF NOT EXISTS links(link TEXT PRIMARY KEY, payment_id INT, uses INT, max_uses INT, expires REAL);
    CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY, name TEXT, text TEXT, bot INT, slot TEXT UNIQUE, ts REAL);
    CREATE INDEX IF NOT EXISTS i_pay ON payments(token, status);
    """)
    c.commit()


def user(req: Request, res: Response = None):
    t = req.cookies.get("wv")
    with closing(db()) as c:
        if t and c.execute("SELECT 1 FROM users WHERE token=?", (t,)).fetchone():
            return t
        t = secrets.token_urlsafe(24)
        c.execute("INSERT INTO users VALUES(?,?)", (t, time.time()))
        c.commit()
    if res is not None:
        res.set_cookie("wv", t, httponly=True, samesite="lax", max_age=31536000)
    return t


def paid(c, t):
    return c.execute("SELECT * FROM payments WHERE token=? AND status='paid' ORDER BY id DESC", (t,)).fetchone()


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/config")
def config():
    return dict(mode=MODE, pay_number=PAY_NUMBER, packages=PACKAGES)


@app.get("/api/status")
def status(req: Request, res: Response):
    t = user(req, res)
    with closing(db()) as c:
        p = c.execute("SELECT * FROM payments WHERE token=? ORDER BY id DESC", (t,)).fetchone()
        s = c.execute("SELECT message FROM snippets WHERE token=?", (t,)).fetchone()
        pp = paid(c, t)
        l = c.execute("SELECT link FROM links WHERE payment_id=?", (pp["id"],)).fetchone() if pp else None
    return dict(payment=dict(id=p["id"], package=p["package"], status=p["status"], receipt=p["receipt"]) if p else None,
                snippet=s["message"] if s else None, link=f"/r/{l['link']}" if l else None)


@app.post("/api/pay")
async def pay(req: Request, res: Response):
    t = user(req, res)
    b = await req.json()
    pk = PACKAGES.get(b.get("package"))
    phone = "".join(ch for ch in str(b.get("phone", "")) if ch.isdigit())
    if not pk:
        raise HTTPException(400, "Unknown package")
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if not (phone.startswith("254") and len(phone) == 12):
        raise HTTPException(400, "Enter a valid Safaricom number, e.g. 07XXXXXXXX")
    cid = "mock-" + secrets.token_hex(6)
    if MODE == "daraja":
        cid = await stk_push(phone, pk["price"])
    with closing(db()) as c:
        cur = c.execute("INSERT INTO payments(token,package,amount,phone,checkout_id,status,created) VALUES(?,?,?,?,?,?,?)",
                        (t, b["package"], pk["price"], phone, cid, "pending", time.time()))
        c.commit()
    return dict(payment_id=cur.lastrowid, mode=MODE)


async def stk_push(phone, amount):
    key, sec = E("DARAJA_KEY"), E("DARAJA_SECRET")
    sc, pf = E("DARAJA_SHORTCODE"), E("DARAJA_PASSKEY")
    async with httpx.AsyncClient(timeout=20) as h:
        r = await h.get(f"{DARAJA}/oauth/v1/generate?grant_type=client_credentials", auth=(key, sec))
        r.raise_for_status()
        ts = datetime.now(EAT).strftime("%Y%m%d%H%M%S")
        body = dict(BusinessShortCode=sc, Password=base64.b64encode(f"{sc}{pf}{ts}".encode()).decode(),
                    Timestamp=ts, TransactionType="CustomerPayBillOnline", Amount=amount, PartyA=phone,
                    PartyB=sc, PhoneNumber=phone, CallBackURL=f"{E('PUBLIC_URL')}/api/daraja/callback/{CB_SECRET}",
                    AccountReference="WISEVIRGINS", TransactionDesc="WISE VIRGINS")
        r = await h.post(f"{DARAJA}/mpesa/stkpush/v1/processrequest", json=body,
                         headers={"Authorization": "Bearer " + r.json()["access_token"]})
        r.raise_for_status()
        return r.json()["CheckoutRequestID"]


@app.post("/api/daraja/callback/{secret}")
async def callback(secret: str, req: Request):
    # Payment is marked paid ONLY here (or via mock confirm in dev). Amount must match.
    if not secrets.compare_digest(secret, CB_SECRET):
        raise HTTPException(403)
    cb = (await req.json()).get("Body", {}).get("stkCallback", {})
    with closing(db()) as c:
        p = c.execute("SELECT * FROM payments WHERE checkout_id=?", (cb.get("CheckoutRequestID"),)).fetchone()
        if p and p["status"] == "pending":
            if cb.get("ResultCode") == 0:
                items = {i["Name"]: i.get("Value") for i in cb["CallbackMetadata"]["Item"]}
                ok = int(items.get("Amount", 0)) == p["amount"]
                c.execute("UPDATE payments SET status=?, receipt=? WHERE id=?",
                          ("paid" if ok else "mismatch", items.get("MpesaReceiptNumber"), p["id"]))
            else:
                c.execute("UPDATE payments SET status='failed' WHERE id=?", (p["id"],))
            c.commit()
    return dict(ResultCode=0, ResultDesc="Accepted")


@app.post("/api/mock/confirm")
def mock_confirm(req: Request):
    if MODE != "mock":
        raise HTTPException(404)
    t = user(req)
    with closing(db()) as c:
        c.execute("UPDATE payments SET status='paid', receipt=? WHERE token=? AND status='pending'",
                  ("MOCK" + secrets.token_hex(4).upper(), t))
        c.commit()
    return dict(ok=True)


@app.post("/api/snippet")
async def snippet(req: Request):
    t = user(req)
    msg = str((await req.json()).get("message", "")).strip()
    if not 1 <= len(msg) <= 500:
        raise HTTPException(400, "Message must be 1-500 characters")
    with closing(db()) as c:
        p = paid(c, t)
        if not p:
            raise HTTPException(402, "Payment not verified")
        c.execute("INSERT OR REPLACE INTO snippets VALUES(?,?,?,?)", (t, p["id"], msg, time.time()))
        if not c.execute("SELECT 1 FROM links WHERE payment_id=?", (p["id"],)).fetchone():
            pk = PACKAGES[p["package"]]
            c.execute("INSERT INTO links VALUES(?,?,?,?,?)", (secrets.token_urlsafe(20), p["id"], 0, pk["uses"],
                      time.time() + pk["days"] * 86400))
        c.commit()
    return dict(ok=True)


@app.get("/r/{link}")
def open_link(link: str):
    with closing(db()) as c:
        l = c.execute("SELECT * FROM links WHERE link=?", (link,)).fetchone()
        if not l or l["expires"] < time.time() or l["uses"] >= l["max_uses"]:
            raise HTTPException(410, "Link expired or already used")
        c.execute("UPDATE links SET uses=uses+1 WHERE link=?", (link,))
        c.commit()
    return RedirectResponse(REPO_URL)


POOLS = {
    "morning": ["Good morning ☀️", "Another day, another opportunity 💻"],
    "afternoon": ["Still coding? 💻", "Keep your system running ⚡"],
    "evening": ["The night shift begins 🌙", "Stay connected 👨‍💻"],
    "midnight": ["The system is still awake 🌌", "Waiting for the next release ⏳"],
    "release": ["PACKAGE RELEASE INITIATED 🚀", "CHECK YOUR SNIPPET 📂"],
}


def pool(h):
    return ("release" if h == 1 else "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
            else "evening" if 17 <= h < 23 else "midnight")


@app.get("/api/chat")
def chat_get(after: int = 0):
    now = datetime.now(EAT)
    slot = now.strftime("%Y%m%d%H")  # one bot post per EAT hour, created lazily on read
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM chat WHERE slot=?", (slot,)).fetchone():
            try:
                c.execute("INSERT INTO chat(name,text,bot,slot,ts) VALUES('SYSTEM',?,1,?,?)",
                          (random.choice(POOLS[pool(now.hour)]), slot, time.time()))
                c.commit()
            except sqlite3.IntegrityError:
                pass
        rows = c.execute("SELECT id,name,text,bot,ts FROM chat WHERE id>? ORDER BY id DESC LIMIT 50", (after,)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.post("/api/chat")
async def chat_post(req: Request):
    t = user(req)
    b = await req.json()
    name = "".join(ch for ch in str(b.get("name", "anon")) if ch.isalnum() or ch in "_-")[:16] or "anon"
    text = str(b.get("text", "")).strip()[:300]
    if not text:
        raise HTTPException(400)
    with closing(db()) as c:
        last = c.execute("SELECT MAX(ts) m FROM chat WHERE name=? AND bot=0", (name,)).fetchone()["m"] or 0
        if time.time() - last < 2:
            raise HTTPException(429, "Slow down")
        c.execute("INSERT INTO chat(name,text,bot,ts) VALUES(?,?,0,?)", (name, text, time.time()))
        c.commit()
    return dict(ok=True)
