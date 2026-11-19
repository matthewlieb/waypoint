#!/usr/bin/env python3
"""Plaid integration: Link tokens, token exchange, sync, webhooks, disconnect.

Env (server-side only, never sent to the browser):
  PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV (sandbox | production)
  PLAID_REDIRECT_URI  https://<your-app>/connect  (must also be allow-listed in the Plaid Dashboard)
  PUBLIC_URL          https://<your-app>  (webhook receiver = PUBLIC_URL + /api/plaid/webhook)
  SESSION_SECRET      also used to derive the key that encrypts access tokens at rest

Link setup (Plaid's recommended personal-finance pattern): transactions in `products`;
investments and liabilities in `additional_consented_products` (consented now, billed only when used).
Storage: $FIN_DATA/plaid/items.json (encrypted tokens + cursors), transactions.json, accounts.json,
holdings.json, liabilities.json, link_events.jsonl.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FIN_DATA", ROOT / "data"))
OUT = DATA / "plaid"
ITEMS = OUT / "items.json"
ENV = os.environ.get("PLAID_ENV", "sandbox").lower()
HOSTS = {"production": "https://production.plaid.com", "sandbox": "https://sandbox.plaid.com"}
HOST = HOSTS.get(ENV, HOSTS["sandbox"])
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_URL = PUBLIC_URL + "/api/plaid/webhook" if PUBLIC_URL.startswith("https://") else None
RETRYABLE = {"INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING", "PRODUCT_NOT_READY", "INTERNAL_SERVER_ERROR"}
NEEDS_USER = {"ITEM_LOGIN_REQUIRED", "INVALID_CREDENTIALS", "ITEM_LOCKED", "ACCESS_NOT_GRANTED",
              "USER_PERMISSION_REVOKED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"}


class PlaidError(RuntimeError):
    def __init__(self, err: dict, status: int):
        self.type = err.get("error_type") or ""
        self.code = err.get("error_code") or str(status)
        self.display = err.get("display_message")
        self.request_id = err.get("request_id")
        super().__init__(f"{self.code} (request_id {self.request_id})")


def log(*a) -> None:
    print("[plaid]", *a, file=sys.stderr, flush=True)


def configured() -> bool:
    return bool(os.environ.get("PLAID_CLIENT_ID") and os.environ.get("PLAID_SECRET"))


# ---------------------------------------------------------------- token encryption at rest
def _fernet() -> Fernet:
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("SESSION_SECRET is required to encrypt bank tokens")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"plaid-token:" + secret.encode()).digest()))


def seal(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def unseal(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        raise RuntimeError("Can't decrypt a stored bank token. Was SESSION_SECRET changed? Reconnect the bank.") from None


# ---------------------------------------------------------------- API
def call(path: str, body: dict, attempts: int = 3) -> dict:
    """POST to Plaid. Retries transient errors and rate limits with jittered backoff; logs request_id, never tokens."""
    payload = json.dumps({"client_id": os.environ["PLAID_CLIENT_ID"], "secret": os.environ["PLAID_SECRET"], **body}).encode()
    for i in range(attempts):
        req = urllib.request.Request(HOST + path, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            pe = PlaidError(err, e.code)
            transient = pe.code in RETRYABLE or pe.type == "RATE_LIMIT_EXCEEDED" or e.code >= 500
            if transient and pe.code != "PRODUCT_NOT_READY" and i < attempts - 1:
                time.sleep(min(30, 2 ** i * 2) + random.random())
                continue
            log(path, "failed:", pe)
            raise pe from None
        except urllib.error.URLError as e:
            if i < attempts - 1:
                time.sleep(2 ** i + random.random())
                continue
            raise RuntimeError(f"network error calling Plaid: {e.reason}") from None
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- items
def items() -> list[dict]:
    return json.loads(ITEMS.read_text()) if ITEMS.exists() else []


def save_items(xs: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = ITEMS.with_suffix(".tmp")
    tmp.write_text(json.dumps(xs, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(ITEMS)


def public_items() -> list[dict]:
    """What the browser may see: no tokens."""
    return [{k: v for k, v in it.items() if k not in ("access_token", "cursor")} for it in items()]


def _base_link_body() -> dict:
    body = {"user": {"client_user_id": "owner"}, "client_name": os.environ.get("APP_NAME", "Waypoint")[:30],
            "language": "en", "country_codes": ["US"]}
    if os.environ.get("PLAID_REDIRECT_URI"):
        body["redirect_uri"] = os.environ["PLAID_REDIRECT_URI"]  # every call, update mode included
    if WEBHOOK_URL:
        body["webhook"] = WEBHOOK_URL
    return body


def link_token(item_id: str | None = None) -> str:
    body = _base_link_body()
    if item_id:  # update mode: existing token, empty products
        it = next((i for i in items() if i["item_id"] == item_id), None)
        if not it:
            raise RuntimeError("unknown bank connection")
        body["access_token"] = unseal(it["access_token"])
        body["products"] = []
    else:
        body["products"] = ["transactions"]
        body["additional_consented_products"] = ["investments", "liabilities"]
        body["transactions"] = {"days_requested": 730}
    return call("/link/token/create", body)["link_token"]


def find_duplicate(institution_id: str | None, accounts: list[dict]) -> dict | None:
    """Same bank already connected? (checked before exchanging so we never create a second, separately billed Item)."""
    if not institution_id:
        return None
    for it in items():
        if it.get("institution_id") != institution_id or it.get("status") == "revoked":
            continue
        old = {(a.get("mask"), a.get("subtype")) for a in it.get("accounts", [])}
        new = {(a.get("mask"), a.get("subtype")) for a in accounts}
        if not old or not new or old & new:
            return it
    return None


def exchange(public_token: str, meta: dict) -> dict:
    r = call("/item/public_token/exchange", {"public_token": public_token})
    inst = meta.get("institution") or {}
    rec = {"item_id": r["item_id"], "access_token": seal(r["access_token"]),
           "institution_id": inst.get("institution_id"), "institution": inst.get("name") or "Bank",
           "accounts": [{"id": a.get("id"), "name": a.get("name"), "mask": a.get("mask"), "type": a.get("type"),
                         "subtype": a.get("subtype")} for a in meta.get("accounts", [])],
           "link_session_id": meta.get("link_session_id"), "cursor": None, "status": "ok",
           "history": "importing", "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_items([x for x in items() if x["item_id"] != rec["item_id"]] + [rec])
    log("linked item", rec["item_id"], rec["institution"], "link_session_id", rec["link_session_id"])
    return {k: v for k, v in rec.items() if k not in ("access_token", "cursor")}


def set_status(item_id: str, **fields) -> None:
    xs = items()
    for it in xs:
        if it["item_id"] == item_id:
            it.update(fields)
    save_items(xs)


def remove(item_id: str) -> None:
    """Disconnect: /item/remove (stops billing) and purge local data for the Item."""
    it = next((i for i in items() if i["item_id"] == item_id), None)
    if not it:
        return
    try:
        call("/item/remove", {"access_token": unseal(it["access_token"])})
    except PlaidError as e:
        if e.code != "ITEM_NOT_FOUND":
            raise
    save_items([x for x in items() if x["item_id"] != item_id])
    for name in ("recurring.json", "liabilities.json"):
        f = OUT / name
        if f.exists():
            d = json.loads(f.read_text())
            d.pop(item_id, None)
            f.write_text(json.dumps(d))
    for name in ("transactions.json", "accounts.json", "holdings.json"):
        f = OUT / name
        if f.exists():
            f.write_text(json.dumps([x for x in json.loads(f.read_text()) if x.get("item_id") != item_id]))


def record_link_event(ev: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    keep = {k: ev.get(k) for k in ("event", "link_session_id", "institution_id", "institution_name", "view_name",
                                    "error_type", "error_code", "status", "request_id")}
    keep["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT / "link_events.jsonl", "a") as f:
        f.write(json.dumps(keep) + "\n")


# ---------------------------------------------------------------- sync
def _load(name: str, default):
    f = OUT / name
    return json.loads(f.read_text()) if f.exists() else default


def _sync_transactions(tok: str, start_cursor: str | None) -> tuple[list, list, list, str, str | None]:
    """Full /transactions/sync loop. Buffers every page; restarts from the starting cursor on mutation."""
    for _ in range(4):
        added, modified, removed, cursor, status = [], [], [], start_cursor, None
        try:
            while True:
                body = {"access_token": tok, "count": 500,
                        "options": {"include_original_description": True, "personal_finance_category_version": "v2"}}
                if cursor:
                    body["cursor"] = cursor
                r = call("/transactions/sync", body)
                added += r["added"]
                modified += r["modified"]
                removed += r["removed"]
                cursor = r["next_cursor"]
                status = r.get("transactions_update_status")
                if not r["has_more"]:
                    return added, modified, removed, cursor, status
        except PlaidError as e:
            if e.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                continue
            raise
    raise RuntimeError("transactions kept changing during sync; will retry next time")


def _tx_record(t: dict, item_id: str) -> dict:
    pfc = t.get("personal_finance_category") or {}
    return {"transaction_id": t["transaction_id"], "item_id": item_id, "account_id": t["account_id"],
            "date": t.get("authorized_date") or t["date"], "posted": t["date"], "amount": t["amount"],
            "currency": t.get("iso_currency_code") or t.get("unofficial_currency_code"),
            "merchant": t.get("merchant_name"), "name": t.get("name"), "original": t.get("original_description"),
            "pfc_primary": pfc.get("primary"), "pfc_detailed": pfc.get("detailed"), "pfc_confidence": pfc.get("confidence_level"),
            "pending": bool(t.get("pending")), "pending_transaction_id": t.get("pending_transaction_id"),
            "channel": t.get("payment_channel")}


def sync_item(it: dict, txs: dict, accounts: list, holdings: list, liabilities: dict) -> str:
    tok = unseal(it["access_token"])
    iid = it["item_id"]
    accts = call("/accounts/get", {"access_token": tok})["accounts"]
    accounts[:] = [a for a in accounts if a.get("item_id") != iid]
    for a in accts:
        b = a.get("balances") or {}
        accounts.append({"item_id": iid, "account_id": a["account_id"], "persistent_account_id": a.get("persistent_account_id"),
                         "institution": it["institution"], "name": a.get("official_name") or a.get("name"), "short_name": a.get("name"),
                         "mask": a.get("mask"), "type": a.get("type"), "subtype": a.get("subtype"),
                         "balance": b.get("current"), "available": b.get("available"), "limit": b.get("limit"),
                         "currency": b.get("iso_currency_code") or b.get("unofficial_currency_code")})
    it["accounts"] = [{"id": a["account_id"], "name": a.get("name"), "mask": a.get("mask"), "type": a.get("type"),
                       "subtype": a.get("subtype")} for a in accts]
    types = {a.get("type") for a in accts}

    added, modified, removed, cursor, status = _sync_transactions(tok, it.get("cursor"))
    for t in added + modified:
        txs[t["transaction_id"]] = _tx_record(t, iid)
    for t in removed:
        txs.pop(t["transaction_id"], None)
    it["cursor"] = cursor  # persisted only after the full loop finished
    if status:
        it["history"] = {"HISTORICAL_UPDATE_COMPLETE": "complete", "INITIAL_UPDATE_COMPLETE": "importing",
                         "NOT_READY": "importing"}.get(status, it.get("history"))

    if it.get("history") == "complete" and not it.get("recurring_unavailable") and types & {"depository", "credit"}:
        try:  # paid add-on; falls back to the app's own detection when the team isn't entitled
            r = call("/transactions/recurring/get", {"access_token": tok, "options": {"personal_finance_category_version": "v2"}})
            rec = _load("recurring.json", {})
            rec[iid] = {"inflow": r.get("inflow_streams", []), "outflow": r.get("outflow_streams", [])}
            (OUT / "recurring.json").write_text(json.dumps(rec))
        except PlaidError as e:
            if e.code in ("INVALID_PRODUCT", "PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED", "ADDITIONAL_CONSENT_REQUIRED",
                          "INVALID_FIELD", "INVALID_REQUEST"):
                it["recurring_unavailable"] = e.code
            elif e.code != "PRODUCT_NOT_READY":
                raise
    if "investment" in types:
        try:
            r = call("/investments/holdings/get", {"access_token": tok})
            sec = {s["security_id"]: s for s in r.get("securities", [])}
            holdings[:] = [h for h in holdings if h.get("item_id") != iid]
            for h in r.get("holdings", []):
                s = sec.get(h["security_id"], {})
                holdings.append({"item_id": iid, "account_id": h["account_id"], "security_id": h["security_id"],
                                 "name": s.get("name"), "ticker": s.get("ticker_symbol"), "type": s.get("type"),
                                 "is_cash": bool(s.get("is_cash_equivalent")), "quantity": h.get("quantity"),
                                 "value": h.get("institution_value"), "cost_basis": h.get("cost_basis")})
        except PlaidError as e:
            if e.code not in ("PRODUCT_NOT_READY", "PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS", "ADDITIONAL_CONSENT_REQUIRED"):
                raise
    if types & {"credit", "loan"}:
        try:
            liabilities[iid] = call("/liabilities/get", {"access_token": tok}).get("liabilities", {})
        except PlaidError as e:
            if e.code not in ("PRODUCT_NOT_READY", "PRODUCTS_NOT_SUPPORTED", "NO_LIABILITY_ACCOUNTS", "ADDITIONAL_CONSENT_REQUIRED"):
                raise
    return f"{it['institution']}: {len(accts)} accounts, +{len(added)} ~{len(modified)} -{len(removed)} transactions"


def sync(only_item: str | None = None) -> str:
    if not configured():
        return "Plaid keys not set"
    xs = items()
    if not xs:
        return "no banks connected yet"
    OUT.mkdir(parents=True, exist_ok=True)
    txs = {t["transaction_id"]: t for t in _load("transactions.json", [])}
    accounts, holdings, liabilities = _load("accounts.json", []), _load("holdings.json", []), _load("liabilities.json", {})
    lines = []
    for it in xs:
        if (only_item and it["item_id"] != only_item) or it.get("status") == "revoked":
            continue
        try:
            lines.append(sync_item(it, txs, accounts, holdings, liabilities))
            if it.get("status") == "login_required":
                it["status"] = "ok"
            it["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            it.pop("error", None)
        except PlaidError as e:
            it["error"] = e.code
            if e.code in NEEDS_USER:
                it["status"] = "login_required"
            elif e.code == "ITEM_NOT_FOUND":
                it["status"] = "revoked"
            lines.append(f"{it['institution']}: {e.code}")
        except RuntimeError as e:
            it["error"] = str(e)
            lines.append(f"{it['institution']}: {e}")
    save_items(xs)
    (OUT / "transactions.json").write_text(json.dumps(sorted(txs.values(), key=lambda t: t["date"])))
    (OUT / "accounts.json").write_text(json.dumps(accounts, indent=1))
    (OUT / "holdings.json").write_text(json.dumps(holdings, indent=1))
    (OUT / "liabilities.json").write_text(json.dumps(liabilities, indent=1))
    return "plaid: " + "; ".join(lines)


# ---------------------------------------------------------------- webhooks
_keys: dict[str, dict] = {}


def verify_webhook(headers, raw: bytes) -> bool:
    """Plaid-Verification JWT: ES256 only, key by kid from Plaid, iat within 5 min, SHA-256 of the raw body."""
    import jwt  # PyJWT with the cryptography backend

    token = next((v for k, v in headers.items() if k.lower() == "plaid-verification"), None)
    if not token or not configured():
        return False
    try:
        head = jwt.get_unverified_header(token)
        if head.get("alg") != "ES256" or not head.get("kid"):
            return False
        kid = head["kid"]
        jwk = _keys.get(kid)
        if not jwk or (jwk.get("expired_at") and jwk["expired_at"] < time.time()):
            jwk = call("/webhook_verification_key/get", {"key_id": kid})["key"]
            _keys[kid] = jwk
        if jwk.get("expired_at") and jwk["expired_at"] < time.time():
            return False
        key = jwt.PyJWK(jwk, algorithm="ES256").key
        claims = jwt.decode(token, key=key, algorithms=["ES256"], options={"verify_aud": False})
        if time.time() - float(claims.get("iat", 0)) > 300:
            return False
        return hmac.compare_digest(hashlib.sha256(raw).hexdigest(), str(claims.get("request_body_sha256", "")))
    except Exception as e:  # malformed token, unknown key, bad signature
        log("webhook verification failed:", type(e).__name__)
        return False


def handle_webhook(ev: dict) -> str | None:
    """Returns an item_id to sync, or None. Webhooks are triggers; data is always re-fetched from the API."""
    wtype, code, iid = ev.get("webhook_type"), ev.get("webhook_code"), ev.get("item_id")
    if not iid or not any(i["item_id"] == iid for i in items()):
        return None
    if wtype == "TRANSACTIONS" and code == "SYNC_UPDATES_AVAILABLE":
        if ev.get("historical_update_complete"):
            set_status(iid, history="complete")
        return iid
    if (wtype in ("HOLDINGS", "INVESTMENTS_TRANSACTIONS", "LIABILITIES") and code == "DEFAULT_UPDATE") or \
            (wtype == "TRANSACTIONS" and code == "RECURRING_TRANSACTIONS_UPDATE"):
        return iid
    if wtype == "ITEM":
        if code == "ERROR":
            err = (ev.get("error") or {}).get("error_code")
            set_status(iid, status="login_required" if err in NEEDS_USER or err == "ITEM_LOGIN_REQUIRED" else "error", error=err)
        elif code in ("PENDING_DISCONNECT", "PENDING_EXPIRATION"):
            set_status(iid, status="expiring", error=code)
        elif code == "LOGIN_REPAIRED":
            set_status(iid, status="ok", error=None)
            return iid
        elif code == "USER_PERMISSION_REVOKED":
            set_status(iid, status="revoked", error=code)
        elif code == "NEW_ACCOUNTS_AVAILABLE":
            set_status(iid, status="new_accounts", error=code)
    return None


if __name__ == "__main__":
    print(sync() if len(sys.argv) > 1 and sys.argv[1] == "sync" else __doc__)
