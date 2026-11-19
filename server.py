#!/usr/bin/env python3
"""Waypoint server: a private, single-person finance dashboard you host yourself.

Pages:  /            dashboard          /connect   add / reconnect / disconnect banks (Plaid Link)
        /settings    goals, budget, savings accounts, manual accounts
        /login /logout /healthz /status
API:    POST /api/plaid/link_token  /api/plaid/exchange  /api/plaid/event  /api/plaid/remove  /api/plaid/webhook
        GET/POST /api/settings      POST /api/sync      POST /api/override
Env:    DASHBOARD_PASSWORD, SESSION_SECRET (required); PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV,
        PLAID_REDIRECT_URI, PUBLIC_URL; optional APP_NAME, TZ_NAME, SYNC_HOUR, NTFY_TOPIC.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if (ROOT / ".env").exists():  # local development; on Railway the variables come from the service settings
    for line in (ROOT / ".env").read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#") and v and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")
DATA = Path(os.environ.get("FIN_DATA", "/data"))
os.environ["FIN_DATA"] = str(DATA)
DATA.mkdir(parents=True, exist_ok=True)

import plaid_sync  # noqa: E402  (reads FIN_DATA at import)

APP = os.environ.get("APP_NAME", "Waypoint")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SECRET = os.environ.get("SESSION_SECRET", "").encode()
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))
SYNC_HOUR = int(os.environ.get("SYNC_HOUR", "7"))
NTFY = os.environ.get("NTFY_TOPIC", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SESSION_DAYS = 30
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#121831"/>
<path d="M32 12 46 50 32 42 18 50z" fill="#8193ff"/><circle cx="32" cy="12" r="4" fill="#f2b544"/></svg>"""


def _png(size: int) -> bytes:
    """App icon (navy tile, periwinkle waypoint arrow, amber dot) as a PNG, no image libraries."""
    navy, arrow, sun = (18, 24, 49), (129, 147, 255), (242, 181, 68)
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            u, v = x / size, y / size
            c = navy
            if 0.22 <= v <= 0.78:
                half = (v - 0.22) / 0.56 * 0.22
                notch = v > 0.62 and abs(u - 0.5) < (v - 0.62) / 0.16 * 0.1
                if abs(u - 0.5) <= half and not notch:
                    c = arrow
            if (u - 0.5) ** 2 + (v - 0.19) ** 2 <= 0.06 ** 2:
                c = sun
            row += bytes(c)
        rows.append(bytes(row))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


PNGS: dict[int, bytes] = {}
SW = """const C='wp-v1';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{const r=e.request; if(r.method!=='GET'||new URL(r.url).origin!==location.origin) return;
  const u=new URL(r.url); if(u.pathname!=='/') return;
  e.respondWith(fetch(r).then(res=>{ if(res.ok&&res.type==='basic'&&!res.redirected){const cp=res.clone(); caches.open(C).then(c=>c.put(r,cp));} return res; })
    .catch(()=>caches.match(r).then(m=>m||new Response('Offline and nothing cached yet.',{status:503}))));});"""
MANIFEST = {"name": APP, "short_name": APP[:12], "start_url": "/", "display": "standalone",
            "background_color": "#0b0f1e", "theme_color": "#0b0f1e",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                      {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
HEAD_EXTRA = ('<link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/icon.svg">'
              '<link rel="apple-touch-icon" href="/icon-180.png"><meta name="apple-mobile-web-app-capable" content="yes">'
              '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#0b0f1e">')

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title>""" + HEAD_EXTRA + """<style>
:root{{color-scheme:light dark;--bg:#eef1f6;--fg:#121831;--card:#fff;--line:#dfe3ed;--accent:#3346d3;--muted:#5f6680;--soft:#f6f8fc;--bad:#c23a32;--good:#17803f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0f1e;--fg:#edf0f8;--card:#131a2f;--line:#242c47;--accent:#8193ff;--muted:#959cb8;--soft:#18203a;--bad:#f07068;--good:#45c27a}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,sans-serif;padding:max(24px,env(safe-area-inset-top)) 16px 40px}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;width:min(100%,{width}px);margin:0 auto}}
h1{{font-size:24px;margin:0 0 6px}} h2{{font-size:17px;margin:26px 0 8px}} p{{color:var(--muted);margin:0 0 16px}}
input,select{{width:100%;font:16px system-ui;padding:10px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}}
input[type=checkbox]{{width:auto}}
button,a.btn{{display:inline-block;font:600 16px system-ui;padding:12px 18px;border-radius:10px;border:0;background:var(--accent);color:#fff;cursor:pointer;text-decoration:none}}
button:disabled{{opacity:.5;cursor:default}}
button.ghost{{background:transparent;color:var(--accent);padding:6px 0}}
button.small{{padding:6px 12px;font-size:14px}} button.danger{{background:transparent;color:var(--bad);border:1px solid var(--line)}}
.err{{color:var(--bad)}} .ok{{color:var(--good)}} .muted{{color:var(--muted);font-size:14px}}
.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
label{{display:block;font-size:14px;color:var(--muted)}} label input,label select{{margin-top:4px}}
.bank{{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 0;border-top:1px solid var(--line)}}
.card{{border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0;background:var(--soft)}}
.trust{{font-size:14px;color:var(--muted);margin-top:12px}} .trust li{{margin:4px 0}}
a{{color:var(--accent)}}
</style></head><body><div class="box">{body}</div></body></html>"""


def page(title: str, body: str, width: int = 460) -> str:
    return PAGE.format(title=title, body=body, width=width)


# ---------------------------------------------------------------- auth
def sign(exp: int) -> str:
    return f"{exp}.{hmac.new(SECRET, str(exp).encode(), hashlib.sha256).hexdigest()}"


def valid_session(cookie_header: str | None) -> bool:
    if not cookie_header or not SECRET:
        return False
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "wp_session":
            exp, _, _ = v.partition(".")
            if exp.isdigit() and int(exp) > time.time():
                return hmac.compare_digest(sign(int(exp)), v)
    return False


def throttled(ip: str) -> bool:
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
    return len(_fails[ip]) >= 8


# ---------------------------------------------------------------- jobs
def run_build() -> str:
    r = subprocess.run([sys.executable, str(ROOT / "build.py")], capture_output=True, text=True, env=os.environ.copy())
    if r.returncode:
        print(r.stderr, file=sys.stderr, flush=True)
    return (r.stdout + r.stderr).strip()[-600:]


def run_sync(only_item: str | None = None) -> str:
    with _lock:
        try:
            out = plaid_sync.sync(only_item)
        except Exception as e:
            traceback.print_exc()
            out = f"sync failed: {type(e).__name__}"
        out += "\n" + run_build()
        push_new_alerts()
        push_checkin()
        (DATA / "last_sync.txt").write_text(datetime.now(TZ).isoformat(timespec="minutes") + "\n" + out)
        return out


def ntfy(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    if not NTFY:
        return
    h = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
    if PUBLIC_URL:
        h["Click"] = PUBLIC_URL
    if tags:
        h["Tags"] = tags
    urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{NTFY}", data=body.encode(), headers=h), timeout=10)


def push_new_alerts() -> None:
    f = DATA / "alerts.json"
    if not NTFY or not f.exists():
        return
    seen_f = DATA / "alerts_pushed.json"
    alerts = json.loads(f.read_text())
    if not seen_f.exists():  # first run: remember what already exists, say hello once
        seen_f.write_text(json.dumps(sorted(a["id"] for a in alerts)))
        try:
            ntfy("Alerts are on", "You'll get a push when money leaves savings, a category runs over, a big charge posts, or a bank needs attention.")
        except Exception:
            traceback.print_exc()
        return
    seen = set(json.loads(seen_f.read_text()))
    for a in alerts:
        if a["id"] in seen or (a["level"] == "info" and ":big:" not in a["id"]):
            continue
        try:
            ntfy(a["title"], a["detail"], "high" if a["level"] == "critical" else "default", "warning" if a["level"] != "info" else "moneybag")
            seen.add(a["id"])
        except Exception:
            traceback.print_exc()
    seen_f.write_text(json.dumps(sorted(seen)))


def push_checkin() -> None:
    f, sent_f = DATA / "checkin.json", DATA / "checkin_sent.txt"
    if not NTFY or datetime.now(TZ).day != 1 or not f.exists():
        return
    c = json.loads(f.read_text())
    if sent_f.exists() and sent_f.read_text().strip() == c["month"]:
        return
    mname = datetime.strptime(c["month"], "%Y-%m").strftime("%B")
    lines = [f"Kept ${c['kept']:,.0f} of ${c['income']:,.0f} take-home (spent ${c['spent']:,.0f}"
             + (f", ${c['one_time']:,.0f} one-time" if c["one_time"] else "") + ").",
             "Nothing pulled from savings." if not c["pulled_from_savings"] else f"${c['pulled_from_savings']:,.0f} pulled from savings."]
    try:
        ntfy(f"{mname} check-in", "\n".join(lines))
        sent_f.write_text(c["month"])
    except Exception:
        traceback.print_exc()


def scheduler() -> None:
    while True:
        now = datetime.now(TZ)
        nxt = now.replace(hour=SYNC_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        print("[scheduler]", run_sync().splitlines()[0], flush=True)


_pending: dict[str, float] = {}
_plock = threading.Lock()


def queue_sync(item_id: str) -> None:
    """Webhook-triggered sync, coalesced per Item so a burst of webhooks becomes one sync."""
    with _plock:
        if item_id in _pending:
            return
        _pending[item_id] = time.time()

    def work():
        time.sleep(20)
        with _plock:
            _pending.pop(item_id, None)
        print("[webhook sync]", run_sync(item_id).splitlines()[0], flush=True)
    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- settings
def settings() -> dict:
    f = DATA / "settings.json"
    return json.loads(f.read_text()) if f.exists() else {}


def clean_settings(s: dict) -> dict:
    """Keep only known fields with sane types."""
    num = lambda v, lo=0, hi=1e9: None if v in (None, "") else max(lo, min(hi, float(v)))
    out = {"name": str(s.get("name") or "")[:40],
           "monthly_take_home": num(s.get("monthly_take_home")), "monthly_savings": num(s.get("monthly_savings")),
           "invest_monthly": num(s.get("invest_monthly")), "essential_monthly": num(s.get("essential_monthly")),
           "emergency_months": num(s.get("emergency_months"), 0, 24) if s.get("emergency_months") not in (None, "") else 6,
           "big_charge": num(s.get("big_charge"), 0) or 250, "return_pct": num(s.get("return_pct"), 0, 20) or 7,
           "savings_apy": num(s.get("savings_apy"), 0, 20) if s.get("savings_apy") not in (None, "") else 3.5,
           "savings_account_ids": [str(x)[:80] for x in (s.get("savings_account_ids") or [])][:20],
           "debt_account_ids": [str(x)[:80] for x in s["debt_account_ids"]][:30] if isinstance(s.get("debt_account_ids"), list) else None,
           "hidden_accounts": [str(x)[:80] for x in (s.get("hidden_accounts") or [])][:50],
           "order": [str(x)[:40] for x in (s.get("order") or [])][:20],
           "budget": {str(k)[:60]: num(v) for k, v in (s.get("budget") or {}).items() if num(v) is not None} or None,
           "goals": [], "manual_accounts": []}
    for g in (s.get("goals") or [])[:10]:
        gid = re.sub(r"[^a-z0-9_-]", "", str(g.get("id") or "")) or "g" + secrets.token_hex(3)
        out["goals"].append({"id": gid, "kind": "home" if g.get("kind") == "home" else "custom", "name": str(g.get("name") or "Goal")[:50],
                             "amount": num(g.get("amount")), "price": num(g.get("price")), "down_pct": num(g.get("down_pct"), 0, 100),
                             "closing_pct": num(g.get("closing_pct"), 0, 20), "saved": num(g.get("saved")) or 0,
                             "by": str(g.get("by") or "")[:7] or None})
    for m in (s.get("manual_accounts") or [])[:30]:
        out["manual_accounts"].append({"id": "m-" + (re.sub(r"[^a-z0-9]", "", str(m.get("id") or ""))[-10:] or secrets.token_hex(4)),
                                       "name": str(m.get("name") or "Account")[:60], "liability": bool(m.get("liability")),
                                       "kind": str(m.get("kind") or ("loan" if m.get("liability") else "other"))[:20],
                                       "balance": num(m.get("balance")), "note": str(m.get("note") or "")[:120]})
    if out["budget"] is None:
        out.pop("budget")
    return out


SETTINGS_JS = r"""
const S = await (await fetch('/api/settings')).json();
const st = S.settings, acc = S.accounts, sug = S.suggestions;
const $ = s => document.querySelector(s), esc = v => String(v ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money = n => n == null ? '' : '$' + Math.round(n).toLocaleString();
const field = (id, label, val, ph, type = 'number') => `<label>${label}<input id="${id}" type="${type}" value="${val ?? ''}" placeholder="${ph ?? ''}" inputmode="decimal"></label>`;
const chk = (name, id, on, label) => `<label class="row" style="color:var(--fg)"><input type="checkbox" name="${name}" value="${esc(id)}"${on ? ' checked' : ''}> ${label}</label>`;
const savDefault = acc.filter(a => a.kind === 'savings').map(a => a.id);
const debtDefault = sug.debts;
let goals = (st.goals || []).slice(), manual = (st.manual_accounts || []).slice();
$('#f').innerHTML = `
  <h2>You</h2><div class="grid">${field('name', 'First name', st.name, '', 'text')}</div>
  <h2>Money in</h2><p>Detected take-home: <b>${money(sug.income)}</b> a month${sug.income ? '' : ' (connect your checking account to detect it)'}. Leave blank to use it.</p>
  <div class="grid">${field('take', 'Take-home per month', st.monthly_take_home, money(sug.income))}${field('save', 'Saved per month toward goals', st.monthly_savings, money(sug.savings))}${field('inv', 'Invested per month', st.invest_monthly, money(sug.invest))}</div>
  <h2>Savings accounts</h2><p>Where your emergency fund lives. You get an alert whenever money leaves these.</p>
  <div>${acc.filter(a => !a.liability && ['savings', 'cash'].includes(a.kind)).map(a => chk('sav', a.id, (st.savings_account_ids?.length ? st.savings_account_ids : savDefault).includes(a.id), `${esc(a.name)} <span class="muted">${esc(a.institution)} ${money(a.balance)}</span>`)).join('') || '<p class="muted">Connect a bank first.</p>'}</div>
  <h2>Emergency fund</h2><div class="grid">${field('em', 'Months of essentials', st.emergency_months ?? 6, '6')}${field('ess', 'Essentials per month', st.essential_monthly, money(sug.essentials))}</div>
  <h2>Debts to pay off</h2><p>Mortgages and car loans are left out by default. Highest interest first.</p>
  <div>${acc.filter(a => a.liability && a.balance > 0).map(a => chk('debt', a.id, (st.debt_account_ids ?? debtDefault).includes(a.id), `${esc(a.name)} <span class="muted">${money(a.balance)}</span>`)).join('') || '<p class="muted">No debts found yet.</p>'}</div>
  <h2>Savings goals</h2><p>A home, a car, a trip. Goals are funded in the order listed, after the emergency fund and debts.</p>
  <div id="goals"></div><button type="button" class="ghost" id="addGoal">+ Add a goal</button> <button type="button" class="ghost" id="addHome">+ Add a home down payment</button>
  <h2>Accounts Plaid can't see</h2><p>A car's value, a loan from family, a 401k at an unsupported provider.</p>
  <div id="manual"></div><button type="button" class="ghost" id="addManual">+ Add an account</button>
  <h2>Hide accounts</h2><div>${acc.filter(a => a.source === 'plaid').map(a => chk('hide', a.id, (st.hidden_accounts || []).includes(a.id), `${esc(a.name)} <span class="muted">${esc(a.institution)}</span>`)).join('') || '<p class="muted">None yet.</p>'}</div>
  <h2>Monthly budget</h2><p>${st.budget ? 'Your targets.' : 'Suggested from your last three months. Edit any number.'} Blank removes a line.</p>
  <div class="grid" id="budget"></div>
  <h2>Alerts and assumptions</h2><div class="grid">${field('big', 'Large-charge alert at', st.big_charge ?? 250)}${field('ret', 'Investment return %/yr', st.return_pct ?? 7)}${field('apy', 'Savings APY %', st.savings_apy ?? 3.5)}</div>
  <p style="margin-top:24px"><button type="submit" id="saveBtn">Save</button> <span id="msg" class="muted"></span></p>`;
const budget = st.budget || sug.budget;
const cats = [...new Set(Object.keys(budget).concat(Object.keys(sug.budget)))].sort();
$('#budget').innerHTML = cats.map(c => `<label>${esc(c)}<input data-cat="${esc(c)}" type="number" value="${budget[c] ?? ''}" placeholder="${sug.budget[c] ? money(sug.budget[c]) : ''}" inputmode="decimal"></label>`).join('');
function drawGoals() {
  $('#goals').innerHTML = goals.map((g, i) => `<div class="card"><div class="grid">
    ${field('gn' + i, 'Name', g.name, '', 'text')}
    ${g.kind === 'home' ? field('gp' + i, 'Home price', g.price, '400000') + field('gd' + i, 'Down payment %', g.down_pct ?? 20, '20') + field('gc' + i, 'Closing costs %', g.closing_pct ?? 3, '3') : field('ga' + i, 'Amount', g.amount, '10000')}
    ${field('gs' + i, 'Already saved elsewhere', g.saved, '0')}${field('gb' + i, 'Target month', g.by, '', 'month')}</div>
    <button type="button" class="ghost" data-del-goal="${i}" style="color:var(--bad)">Remove</button></div>`).join('');
}
function drawManual() {
  $('#manual').innerHTML = manual.map((m, i) => `<div class="card"><div class="grid">${field('mn' + i, 'Name', m.name, 'Car value', 'text')}${field('mb' + i, 'Balance', m.balance, '0')}
    <label>Type<select id="ml${i}"><option value="0"${m.liability ? '' : ' selected'}>Something I own</option><option value="1"${m.liability ? ' selected' : ''}>Something I owe</option></select></label></div>
    <button type="button" class="ghost" data-del-man="${i}" style="color:var(--bad)">Remove</button></div>`).join('');
}
const readGoals = () => goals.map((g, i) => ({...g, name: $('#gn' + i).value, amount: $('#ga' + i)?.value, price: $('#gp' + i)?.value, down_pct: $('#gd' + i)?.value, closing_pct: $('#gc' + i)?.value, saved: $('#gs' + i).value, by: $('#gb' + i).value}));
const readManual = () => manual.map((m, i) => ({...m, name: $('#mn' + i).value, balance: $('#mb' + i).value, liability: $('#ml' + i).value === '1'}));
drawGoals(); drawManual();
$('#addGoal').onclick = () => { goals = readGoals(); goals.push({id: 'g' + Math.random().toString(36).slice(2, 7), kind: 'custom', name: ''}); drawGoals(); };
$('#addHome').onclick = () => { goals = readGoals(); goals.push({id: 'home' + Math.random().toString(36).slice(2, 5), kind: 'home', name: 'Home down payment', down_pct: 20, closing_pct: 3}); drawGoals(); };
$('#addManual').onclick = () => { manual = readManual(); manual.push({id: Math.random().toString(36).slice(2, 9), name: ''}); drawManual(); };
document.addEventListener('click', e => {
  if (e.target.dataset.delGoal) { goals = readGoals(); goals.splice(+e.target.dataset.delGoal, 1); drawGoals(); }
  if (e.target.dataset.delMan) { manual = readManual(); manual.splice(+e.target.dataset.delMan, 1); drawManual(); }
});
const vals = n => [...document.querySelectorAll(`input[name=${n}]:checked`)].map(x => x.value);
$('#f').onsubmit = async e => {
  e.preventDefault(); $('#saveBtn').disabled = true; $('#msg').textContent = 'Saving and recalculating…';
  const b = {}; document.querySelectorAll('[data-cat]').forEach(i => { if (i.value !== '') b[i.dataset.cat] = i.value; });
  const g = readGoals();
  const body = {name: $('#name').value, monthly_take_home: $('#take').value, monthly_savings: $('#save').value, invest_monthly: $('#inv').value,
    emergency_months: $('#em').value, essential_monthly: $('#ess').value, savings_account_ids: vals('sav'), debt_account_ids: vals('debt'),
    hidden_accounts: vals('hide'), budget: b, goals: g, manual_accounts: readManual(), big_charge: $('#big').value, return_pct: $('#ret').value,
    savings_apy: $('#apy').value, order: ['emergency', 'debt', ...g.map(x => x.id)]};
  const r = await fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r.ok) location.href = '/'; else { $('#msg').textContent = "Couldn't save. Try again."; $('#saveBtn').disabled = false; }
};
"""

CONNECT_JS = r"""
const $ = s => document.querySelector(s);
let handler = null, updateFor = null;
const say = (t, cls = '') => { $('#msg').className = cls; $('#msg').textContent = t; };
const post = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
async function init(itemId) {
  const oauth = location.search.includes('oauth_state_id');
  let token = oauth ? localStorage.getItem('wp_link_token') : null;
  updateFor = oauth ? localStorage.getItem('wp_link_item') : (itemId || null);
  if (!token) {
    const r = await post('/api/plaid/link_token', {item_id: updateFor});
    const j = await r.json();
    if (!r.ok) { say(j.error || 'Could not start Plaid.', 'err'); return null; }
    token = j.link_token;
  }
  try { localStorage.setItem('wp_link_token', token); localStorage.setItem('wp_link_item', updateFor || ''); } catch (e) {}
  if (handler) handler.destroy();
  handler = Plaid.create({token, receivedRedirectUri: oauth ? location.href : undefined,
    onSuccess: async (public_token, meta) => {
      say('Saving ' + (meta.institution?.name || 'your bank') + '…');
      const r = await post('/api/plaid/exchange', {public_token, metadata: meta, item_id: updateFor || null});
      const j = await r.json();
      localStorage.removeItem('wp_link_token');
      if (j.duplicate) { say(j.message, 'err'); return; }
      location.href = '/connect?added=1';
    },
    onExit: (err, meta) => {
      post('/api/plaid/event', {event: 'EXIT', link_session_id: meta?.link_session_id, institution_id: meta?.institution?.institution_id,
        institution_name: meta?.institution?.name, status: meta?.status, error_type: err?.error_type, error_code: err?.error_code, request_id: meta?.request_id});
      localStorage.removeItem('wp_link_token');
      if (err) say(err.display_message || 'That didn\'t connect. You can try again.', 'err');
      if (oauth) history.replaceState(null, '', '/connect');
      init();
    },
    onEvent: (name, meta) => { if (['HANDOFF', 'ERROR', 'OPEN_OAUTH', 'FAIL_OAUTH'].includes(name)) post('/api/plaid/event', {event: name, link_session_id: meta.link_session_id, institution_id: meta.institution_id, institution_name: meta.institution_name, view_name: meta.view_name, error_type: meta.error_type, error_code: meta.error_code, request_id: meta.request_id}); }});
  return handler;
}
const add = $('#add');
if (add) {
  init().then(h => { if (h) add.disabled = false; if (h && location.search.includes('oauth_state_id')) h.open(); });
  add.onclick = () => handler && handler.open();
}
document.querySelectorAll('[data-reconnect]').forEach(b => b.onclick = async () => { b.disabled = true; const h = await init(b.dataset.reconnect); b.disabled = false; if (h) h.open(); });
document.querySelectorAll('[data-remove]').forEach(b => b.onclick = async () => {
  if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'Tap again to disconnect'; return; }
  b.disabled = true; const r = await post('/api/plaid/remove', {item_id: b.dataset.remove});
  if (r.ok) location.reload(); else { say('Could not disconnect. Try again.', 'err'); b.disabled = false; }
});
"""


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    server_version = "waypoint"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def ip(self) -> str:
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def send(self, code: int, body: bytes | str, ctype="text/html; charset=utf-8", headers: dict | None = None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        return self.send(code, json.dumps(obj), "application/json")

    def authed(self) -> bool:
        return valid_session(self.headers.get("Cookie"))

    def same_origin(self) -> bool:
        """Cheap CSRF guard for JSON/form posts from a logged-in browser."""
        o = self.headers.get("Origin") or self.headers.get("Referer") or ""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        return not o or urllib.parse.urlparse(o).netloc == host

    def body(self, limit=200_000) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n)

    def login_page(self, err=""):
        if not PASSWORD or not SECRET:
            return self.send(200, page("Almost ready", "<h1>Almost ready</h1><p>Set <b>DASHBOARD_PASSWORD</b> and <b>SESSION_SECRET</b> on your Railway service, then redeploy. See the README.</p>"))
        body = (f'<h1>{APP}</h1><p>Your private money dashboard.</p>' + (f'<p class="err">{err}</p>' if err else "")
                + '<form method="post" action="/login"><input type="password" name="password" placeholder="Password" '
                  'autocomplete="current-password" autofocus required><p></p><button type="submit">Sign in</button></form>')
        self.send(200, page("Sign in", body))

    def setup_problems(self) -> list[str]:
        p = []
        if not plaid_sync.configured():
            p.append("Set <b>PLAID_CLIENT_ID</b> and <b>PLAID_SECRET</b> (from dashboard.plaid.com → Developers → Keys).")
        if not PUBLIC_URL.startswith("https://"):
            p.append("Set <b>PUBLIC_URL</b> to this site's https address so Plaid can send updates.")
        if plaid_sync.ENV == "production" and not os.environ.get("PLAID_REDIRECT_URI"):
            p.append("Set <b>PLAID_REDIRECT_URI</b> to <code>" + (PUBLIC_URL or "https://your-app") + "/connect</code> and add the same address in the Plaid Dashboard → Developers → API → Allowed redirect URIs (needed for Chase, Wells Fargo, Capital One and other banks that use their own login page).")
        return p

    # ---------------- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/manifest.webmanifest":
            return self.send(200, json.dumps(MANIFEST), "application/manifest+json")
        if path == "/icon.svg":
            return self.send(200, ICON, "image/svg+xml")
        if path in ("/icon-180.png", "/icon-192.png", "/icon-512.png"):
            n = int(path[6:9])
            PNGS.setdefault(n, _png(n))
            return self.send(200, PNGS[n], "image/png", {"Cache-Control": "public, max-age=604800"})
        if path == "/sw.js":
            return self.send(200, SW, "text/javascript")
        if path == "/logout":
            return self.send(302, "", headers={"Location": "/", "Set-Cookie": "wp_session=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"})
        if not self.authed():
            return self.login_page()
        if path == "/":
            if not plaid_sync.items() and not settings():
                return self.send(302, "", headers={"Location": "/connect"})
            f = DATA / "dashboard.html"
            if not f.exists():
                run_build()
            html = f.read_text(encoding="utf-8").replace("<head>", "<head>" + HEAD_EXTRA, 1)
            html = html.replace("</body>", "<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{});</script></body>", 1)
            return self.send(200, html)
        if path == "/connect":
            return self.connect_page()
        if path == "/settings":
            body = (f'<h1>Settings</h1><p>Everything here stays on your server. <a href="/">Back to dashboard</a></p>'
                    '<form id="f"><p class="muted">Loading…</p></form><script type="module">' + SETTINGS_JS + "</script>")
            return self.send(200, page("Settings", body, 640))
        if path == "/api/settings":
            return self.settings_payload()
        if path == "/status":
            f = DATA / "last_sync.txt"
            return self.send(200, f.read_text() if f.exists() else "never synced", "text/plain")
        self.send(404, "not found", "text/plain")

    def connect_page(self):
        probs = self.setup_problems()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if probs and not plaid_sync.configured():
            return self.send(200, page("Set up", "<h1>One more step</h1><p>Your server needs a few settings before you can add banks:</p><ul>"
                                       + "".join(f"<li>{p}</li>" for p in probs) + "</ul><p class='muted'>Then redeploy and reload this page.</p>"))
        its = plaid_sync.public_items()
        label = {"login_required": "needs login", "expiring": "expires soon", "revoked": "disconnected at bank",
                 "new_accounts": "new accounts found", "error": "sync problem"}
        rows = "".join(
            f"<div class='bank'><div><b>{i['institution']}</b><div class='muted'>{len(i.get('accounts', []))} accounts"
            + (" · importing history…" if i.get("history") == "importing" else "")
            + (f" · <span class='err'>{label.get(i.get('status'), '')}</span>" if i.get("status") not in (None, "ok") else "")
            + "</div></div><div class='row'>"
            + (f"<button class='small' data-reconnect='{i['item_id']}'>Reconnect</button>" if i.get("status") in ("login_required", "expiring", "new_accounts", "error") else "")
            + f"<button class='small danger' data-remove='{i['item_id']}'>Disconnect</button></div></div>" for i in its)
        warn = "".join(f"<p class='err' style='font-size:14px'>{p}</p>" for p in probs)
        sandbox = ("<p class='card' style='font-size:14px'>Test mode (Plaid Sandbox): pick any bank and log in with "
                   "<b>user_good</b> / <b>pass_good</b>. Switch PLAID_ENV to production to use real banks.</p>") if plaid_sync.ENV != "production" else ""
        added = "<p class='ok'>Bank added. Your transactions are importing; this can take a minute or two.</p>" if q.get("added") else ""
        nxt = ("<p style='margin-top:18px'><a class='btn' href='/settings'>Next: set your goals</a></p>" if its and not settings()
               else "<p style='margin-top:18px'><a href='/'>Back to dashboard</a> · <a href='/settings'>Settings</a></p>" if its else "")
        body = f"""<h1>Add your bank accounts</h1><p>Connect checking, savings, credit cards, loans and investments. Add each bank once.</p>
{warn}{sandbox}{added}
<button id="add" type="button" disabled>Add a bank account instantly</button>
<ul class="trust"><li>You log in on your bank's own screen through Plaid. This site never sees your password.</li>
<li>Read-only: nothing here can move your money.</li><li>Disconnect any bank here, any time.</li></ul>
<p id="msg"></p>
<div style="margin-top:10px">{rows}</div>
<form method="post" action="/api/sync"><button type="submit" class="ghost">Sync now</button></form>
{nxt}
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>{CONNECT_JS}</script>"""
        self.send(200, page("Add banks", body, 520))

    def settings_payload(self):
        import build
        s = settings()
        accts = build.build_accounts(s)
        from datetime import date as _d
        spend, flows = build.classify(build.load("plaid/transactions.json", []), accts, s)
        today = _d.today()
        inc = build.income_info(flows, today, {})
        avg = build.averages(spend, today)
        fixed = sum(v for c, v in avg.items() if c in build.FIXED)
        ess = fixed + sum(avg.get(c, 0) for c in ("Groceries", "Gas", "Transportation", "Health"))
        inv = sum(f["amount"] for f in flows if f["kind"] == "invest" and f["date"][:7] in build.month_list(today, 3)) / 3
        ms3 = build.month_list(today, 3)
        net = (sum(f["amount"] for f in flows if f["kind"] == "savings_in" and f["date"][:7] in ms3)
               - sum(f["amount"] for f in flows if f["kind"] == "savings_out" and f["date"][:7] in ms3)) / 3
        sug = {"debts": build.default_debts(accts, build.liability_details()), "income": inc["detected"], "essentials": round(ess), "invest": round(inv),
               "savings": round(net) if net > 0 else max(0, round(inc["detected"] - sum(avg.values()) - inv)),
               "budget": {c: round(v / 10) * 10 for c, v in avg.items() if v >= 15}}
        return self.json({"settings": s, "suggestions": sug,
                          "accounts": [{k: a.get(k) for k in ("id", "name", "institution", "kind", "liability", "balance", "source")} for a in accts]})

    # ---------------- POST
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/plaid/webhook":
            return self.webhook()
        if path == "/login":
            ip = self.ip()
            if throttled(ip):
                return self.login_page("Too many attempts. Try again in 15 minutes.")
            pw = (urllib.parse.parse_qs(self.body(10_000).decode()).get("password") or [""])[0]
            if PASSWORD and SECRET and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                exp = int(time.time()) + SESSION_DAYS * 86400
                return self.send(302, "", headers={"Location": "/", "Set-Cookie": f"wp_session={sign(exp)}; Max-Age={SESSION_DAYS * 86400}; Path=/; Secure; HttpOnly; SameSite=Lax"})
            _fails.setdefault(ip, []).append(time.time())
            time.sleep(1)
            return self.login_page("Wrong password.")
        if not self.authed() or not self.same_origin():
            return self.send(401, "unauthorized", "text/plain")
        try:
            if path == "/api/sync":
                res = run_sync()
                if "application/x-www-form-urlencoded" in (self.headers.get("Content-Type") or ""):
                    return self.send(302, "", headers={"Location": "/connect"})
                return self.json({"result": res.splitlines()[0]})
            req = json.loads(self.body() or b"{}")
            if path.startswith("/api/plaid/") and not plaid_sync.configured():
                return self.json({"error": "Plaid keys aren't set on the server yet. See the README."}, 400)
            if path == "/api/plaid/link_token":
                return self.json({"link_token": plaid_sync.link_token(req.get("item_id") or None)})
            if path == "/api/plaid/exchange":
                return self.exchange(req)
            if path == "/api/plaid/event":
                plaid_sync.record_link_event(req)
                return self.json({"ok": True})
            if path == "/api/plaid/remove":
                with _lock:
                    plaid_sync.remove(str(req.get("item_id")))
                    run_build()
                return self.json({"ok": True})
            if path == "/api/settings":
                (DATA / "settings.json").write_text(json.dumps(clean_settings(req), indent=1))
                with _lock:
                    run_build()
                return self.json({"ok": True})
            if path == "/api/override":
                return self.override(req)
        except plaid_sync.PlaidError as e:
            return self.json({"error": e.display or f"Plaid error {e.code}. Try again.", "code": e.code, "request_id": e.request_id}, 400)
        except (ValueError, KeyError) as e:
            return self.json({"error": f"bad request: {type(e).__name__}"}, 400)
        self.send(404, "not found", "text/plain")

    def exchange(self, req: dict):
        meta = req.get("metadata") or {}
        if req.get("item_id"):  # update mode finished: same token keeps working, nothing to exchange
            plaid_sync.set_status(req["item_id"], status="ok", error=None)
            threading.Thread(target=run_sync, args=(req["item_id"],), daemon=True).start()
            return self.json({"ok": True})
        dup = plaid_sync.find_duplicate((meta.get("institution") or {}).get("institution_id"), meta.get("accounts") or [])
        if dup:
            return self.json({"duplicate": True, "message": f"{dup['institution']} is already connected. Use Reconnect on it instead of adding it again."})
        rec = plaid_sync.exchange(req["public_token"], meta)
        threading.Thread(target=run_sync, args=(rec["item_id"],), daemon=True).start()  # first sync starts the webhooks
        return self.json({"ok": True, "institution": rec["institution"]})

    def override(self, req: dict):
        cat = str(req.get("category") or "").strip()[:60]
        if not cat:
            return self.json({"error": "category required"}, 400)
        f = DATA / "overrides.json"
        rules = json.loads(f.read_text()) if f.exists() else []
        if req.get("scope") == "merchant":
            key = "^" + re.escape(str(req.get("desc") or "")[:80]) + "$"
            rules = [r for r in rules if r.get("desc") != key]
            rule = {"desc": key, "category": cat}
        else:
            rules = [r for r in rules if r.get("id") != req.get("id")]
            rule = {"id": str(req.get("id"))[:80], "category": cat}
        rules.insert(0, rule)
        f.write_text(json.dumps(rules, indent=1))
        with _lock:
            run_build()
        return self.json({"ok": True})

    def webhook(self):
        try:
            raw = self.body(1_000_000)
        except ValueError:
            return self.send(413, "too large", "text/plain")
        if not plaid_sync.verify_webhook(dict(self.headers.items()), raw):
            return self.send(401, "unverified", "text/plain")
        try:
            ev = json.loads(raw)
        except ValueError:
            return self.send(400, "bad json", "text/plain")
        print("[webhook]", ev.get("webhook_type"), ev.get("webhook_code"), ev.get("item_id"), flush=True)
        try:
            iid = plaid_sync.handle_webhook(ev)
        except Exception:
            traceback.print_exc()
            return self.send(500, "retry", "text/plain")  # Plaid retries 5xx
        if iid:
            queue_sync(iid)
        elif ev.get("webhook_type") == "ITEM":
            threading.Thread(target=lambda: (_lock.acquire(), run_build(), push_new_alerts(), _lock.release()), daemon=True).start()
        return self.send(200, "ok", "text/plain")


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"{APP} serving on :{port}, data in {DATA}, Plaid {plaid_sync.ENV}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
