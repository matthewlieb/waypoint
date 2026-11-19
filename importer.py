"""Import past transactions from bank CSV downloads (statements / "download activity").

Plaid gives up to ~2 years of history, depending on the bank. Imports fill in what's older, or cover a bank
you don't connect through Plaid. Rows are normalized to Plaid's sign (+ money out, - money in) and given
a best-guess Plaid category so build.py treats them like any other transaction.

Duplicates: for an account that's also connected through Plaid, only imported rows *older* than Plaid's
earliest transaction for that account are used. Overlapping imports of the same account are de-duplicated.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
from collections import Counter
from datetime import date, datetime
from pathlib import Path

DATA = Path(os.environ.get("FIN_DATA", Path(__file__).resolve().parent / "data"))
STORE = DATA / "imports.json"
KINDS = {"cash": "Checking", "savings": "Savings", "credit_card": "Credit card"}

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y",
                "%d-%b-%y", "%Y/%m/%d", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%B %d, %Y")


# ---------------------------------------------------------------- parsing
def parse_date(s: str) -> date | None:
    s = (s or "").strip().strip('"')
    if not s or len(s) > 30:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def parse_amount(s: str) -> float | None:
    s = (s or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.endswith("-"):  # some banks write 12.34-
        neg, s = True, s[:-1]
    if s.upper().endswith(("CR", "DR")):
        neg, s = s.upper().endswith("CR") != neg, s[:-2]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _rows(text: str) -> list[list[str]]:
    text = text.lstrip("﻿")
    sample = "\n".join(text.splitlines()[:40])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if sample.count("\t") > sample.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(c.strip() for c in r)]


def _find_header(rows: list[list[str]]) -> int | None:
    """Index of the header row (skipping summary lines some banks put on top), or None if there isn't one."""
    for i, r in enumerate(rows[:40]):
        low = [c.strip().lower() for c in r]
        if any("date" in c for c in low) and any(k in c for c in low for k in ("amount", "debit", "credit", "withdrawal", "deposit")):
            return i
    return None


def _guess(headers: list[str], body: list[list[str]]) -> dict:
    low = [h.strip().lower() for h in headers]
    n = len(headers)

    def col(*names, avoid=()):
        for name in names:
            for i, h in enumerate(low):
                if name in h and not any(a in h for a in avoid):
                    return i
        return None

    def share(i, fn):
        vals = [r[i] for r in body[:200] if i < len(r) and r[i].strip()]
        return sum(1 for v in vals if fn(v) is not None) / len(vals) if vals else 0

    m = {"date": col("transaction date", "trans date", "trans. date", "date", avoid=("post",)) if headers else None}
    if m["date"] is None:
        m["date"] = col("posting date", "post date", "posted", "date")
    debit, credit = col("debit", "withdrawal"), col("credit", "deposit", avoid=("card",))
    amount = col("amount", avoid=("balance",))
    if amount is None and debit is not None and credit is not None and debit != credit:
        m.update(debit=debit, credit=credit)
    else:
        m["amount"] = amount
    m["desc"] = col("description", "merchant", "payee", "name", "memo", "details", "transaction", avoid=("date", "type", "amount"))
    m["category"] = col("category")
    # headerless files (or headers we didn't recognize): look at the values
    if m["date"] is None:
        scores = [share(i, parse_date) for i in range(n)]
        m["date"] = max(range(n), key=lambda i: scores[i]) if n and max(scores) > 0.8 else None
    if m.get("amount") is None and "debit" not in m:
        cands = [i for i in range(n) if i != m["date"] and share(i, parse_amount) > 0.9]
        m["amount"] = cands[0] if cands else None
    if m["desc"] is None:
        used = {m["date"], m.get("amount"), m.get("debit"), m.get("credit")}
        text_cols = [i for i in range(n) if i not in used]
        if text_cols:
            m["desc"] = max(text_cols, key=lambda i: sum(len(r[i]) for r in body[:100] if i < len(r)))
    return {k: v for k, v in m.items() if v is not None}


MONEY_IN = re.compile(r"payroll|direct dep|dir dep|salary|thank you|interest (paid|earned)|int earned|dividend|refund|"
                      r"deposit|cashback|cash back|statement credit|reversal|return", re.I)


def _guess_flip(raw: list[dict]) -> bool:
    """True if this file writes money out as negative (so we flip to Plaid's + = out)."""
    ins = [x["raw"] for x in raw if MONEY_IN.search(x["desc"])]
    if len(ins) >= 2:  # paychecks, card payments received, refunds: their sign means "money in"
        return sum(1 for v in ins if v > 0) > len(ins) / 2
    return sum(1 for x in raw if x["raw"] < 0) > len(raw) / 2  # otherwise most rows are purchases


def parse(text: str, mapping: dict | None = None, flip: bool | None = None) -> dict:
    """Parse a bank CSV. Returns headers, the (guessed) column mapping, sign choice and normalized rows."""
    rows = _rows(text)
    if not rows:
        raise ValueError("That file is empty or isn't a CSV.")
    h = _find_header(rows)
    width = Counter(len(r) for r in rows).most_common(1)[0][0]
    if h is None:
        body = [r for r in rows if len(r) == width]
        headers = [f"Column {i + 1}" for i in range(width)]
    else:
        headers, body = rows[h], [r for r in rows[h + 1:] if len(r) >= 2]
    m = {k: int(v) for k, v in (mapping or _guess(headers, body)).items() if v not in (None, "")}
    if "date" not in m or "desc" not in m or ("amount" not in m and not ("debit" in m and "credit" in m)):
        return {"headers": headers, "mapping": m, "rows": [], "skipped": len(body), "flip": bool(flip),
                "error": "Couldn't find the date, description and amount columns. Pick them below."}

    def cell(r, k):
        i = m.get(k)
        return r[i].strip() if i is not None and i < len(r) else ""

    raw, skipped = [], 0
    for r in body:
        d = parse_date(cell(r, "date"))
        if "amount" in m:
            a = parse_amount(cell(r, "amount"))
        else:
            dr, cr = parse_amount(cell(r, "debit")) or 0, parse_amount(cell(r, "credit")) or 0
            a = abs(dr) - abs(cr) if (dr or cr) else None
        desc = re.sub(r"\s+", " ", cell(r, "desc"))[:120]
        if d is None or a is None or not desc:
            skipped += 1
            continue
        raw.append({"date": d.isoformat(), "desc": desc, "raw": a, "hint": cell(r, "category")[:60]})
    if flip is None:
        flip = "amount" in m and _guess_flip(raw)
    if "debit" in m and "amount" not in m:
        flip = bool(flip) and mapping is not None  # debit/credit columns are already "out minus in"
    out = [{"date": x["date"], "desc": x["desc"], "amount": round(-x["raw"] if flip else x["raw"], 2), "hint": x["hint"]} for x in raw]
    out.sort(key=lambda x: x["date"])
    return {"headers": headers, "mapping": m, "flip": bool(flip), "rows": out, "skipped": skipped,
            "first": out[0]["date"] if out else None, "last": out[-1]["date"] if out else None}


# ---------------------------------------------------------------- categories (best guess, editable in the app)
# (regex on description or the bank's own category, Plaid primary, Plaid detailed). First match wins.
RULES = [
    (r"payment.{0,12}thank|autopay|auto pay|card ?payment|crd ?pmt|credit crd|epay|payment received|online payment|mobile payment|e-?payment",
     "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
    (r"payroll|direct dep|dir dep|salary|paycheck|\bpay ?roll", "INCOME", "INCOME_WAGES"),
    (r"interest (paid|earned|payment)|^interest$|int earned|dividend", "INCOME", "INCOME_INTEREST_EARNED"),
    (r"interest charge|finance charge|overdraft|\bnsf\b|service fee|monthly fee|late fee|annual fee|foreign transaction", "BANK_FEES", "BANK_FEES_OTHER_BANK_FEES"),
    (r"\batm\b.*(withdraw|w/d|wd)|cash withdrawal|withdrawal.*\batm\b", "TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL_ATM"),
    (r"vanguard|fidelity|schwab|robinhood|betterment|wealthfront|e\*?trade|acorns|m1 finance|merrill|brokerage|401k|\bira\b",
     "TRANSFER_OUT", "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS"),
    (r"to savings|savings transfer|transfer to sav", "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS"),
    (r"transfer|xfer|\bzelle\b|wire", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER"),
    (r"navient|nelnet|mohela|sallie mae|great lakes|aidvantage|edfinancial|student loan|dept education|\bfedloan", "LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT"),
    (r"mortgage|rocket mortgage|mr\.? cooper|loan pmt|loan payment|auto loan|financial services|\bfinance\b", "LOAN_PAYMENTS", "LOAN_PAYMENTS_OTHER_PAYMENT"),
    (r"\brent\b|apartment|apts\b|property manag|leasing|realty|bilt", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT"),
    (r"electric|energy|power|\bwater\b|sewer|utilit|gas co|natural gas|pg&e|con ed|duke|comcast|xfinity|spectrum|verizon|at&t|\batt\b|t-mobile|tmobile|internet|wireless|bills",
     "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY"),
    (r"geico|progressive|state farm|allstate|liberty mutual|lemonade|usaa|insurance|nationwide|farmers ins", "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE"),
    (r"whole foods|trader joe|safeway|kroger|aldi|publix|wegmans|\bh-?e-?b\b|giant|food lion|stop & shop|sprouts|albertsons|ralphs|vons|"
     r"harris teeter|shoprite|meijer|instacart|grocer|supermarket|market", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    (r"starbucks|dunkin|coffee|cafe|caf[eé]\b|peet|blue bottle|dutch bros", "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
    (r"\bbar\b|pub\b|tavern|brewery|brewing|taproom|liquor|wine|spirits|saloon|lounge", "FOOD_AND_DRINK", "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR"),
    (r"restaurant|grill|pizza|doordash|uber ?eats|grubhub|postmates|seamless|chipotle|mcdonald|sweetgreen|taco|sushi|burger|kitchen|"
     r"diner|bistro|chick-fil|wendy|subway|panera|shake shack|cava|deli|bakery|bbq|ramen|thai|noodle|tst\*|sq \*.*(cafe|kitchen)|dining|food & drink|restaurants?",
     "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
    (r"shell|exxon|chevron|\bbp\b|mobil|sunoco|citgo|marathon|speedway|valero|wawa|sheetz|7-eleven|fuel|gasoline|\bgas\b",
     "TRANSPORTATION", "TRANSPORTATION_GAS"),
    (r"uber|lyft|transit|\bmta\b|parking|toll|e-?zpass|metro|amtrak|septa|bart|clipper|taxi|cab\b|automotive|transportation",
     "TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),
    (r"airline|airways|delta air|united|american air|southwest|jetblue|alaska air|spirit air|frontier|hotel|marriott|hilton|hyatt|"
     r"airbnb|vrbo|expedia|booking\.com|travel|lodging|car rental|hertz|avis|enterprise rent", "TRAVEL", "TRAVEL_FLIGHTS"),
    (r"netflix|spotify|hulu|disney|hbo|\bmax\b|youtube|paramount|peacock|apple\.com/bill|audible|ticketmaster|stubhub|steam|"
     r"playstation|xbox|nintendo|cinema|theater|theatre|amc |movie|concert|entertainment", "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),
    (r"cvs|walgreens|rite aid|pharmac|doctor|dental|dentist|medical|hospital|clinic|health|optometr|vision|urgent care|labcorp|quest diag",
     "MEDICAL", "MEDICAL_PRIMARY_CARE"),
    (r"gym|fitness|planet fitness|equinox|crunch|orangetheory|peloton|yoga|salon|barber|spa\b|nails|personal care|beauty|sephora|ulta",
     "PERSONAL_CARE", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS"),
    (r"amazon|amzn|etsy|ebay|online|\.com\b", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
    (r"target|walmart|costco|sam'?s club|bj'?s|superstore|warehouse", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"),
    (r"home depot|lowe'?s|ikea|wayfair|ace hardware|hardware|home improvement|\bhome\b", "HOME_IMPROVEMENT", "HOME_IMPROVEMENT_HARDWARE"),
    (r"\birs\b|tax|donation|charity|church|non-?profit|gifts? & donations|dmv|government", "GOVERNMENT_AND_NON_PROFIT", "GOVERNMENT_AND_NON_PROFIT_DONATIONS"),
    (r"tuition|school|university|college|education|childcare|daycare", "GENERAL_SERVICES", "GENERAL_SERVICES_EDUCATION"),
    (r"shopping|merchandise|clothing|apparel|store|shop", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"),
    (r"service|professional", "GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"),
]
_RULES = [(re.compile(p, re.I), a, b) for p, a, b in RULES]


def guess_category(desc: str, hint: str, amount: float, kind: str) -> tuple[str | None, str | None, str]:
    """Best-guess (pfc_primary, pfc_detailed, confidence) for one imported row."""
    for text, conf in ((desc, "MEDIUM"), (hint, "LOW")):
        if not text:
            continue
        for rx, p, d in _RULES:
            if rx.search(text):
                if p == "LOAN_PAYMENTS" and d.endswith("CREDIT_CARD_PAYMENT") and amount < 0:
                    return "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER", conf  # the card side of a card payment
                if p == "TRANSFER_OUT" and amount < 0:
                    return "TRANSFER_IN", d.replace("TRANSFER_OUT", "TRANSFER_IN"), conf
                if p == "INCOME" and amount > 0:
                    continue
                return p, d, conf
    if amount < 0 and kind in ("cash", "savings"):
        return "TRANSFER_IN", "TRANSFER_IN_DEPOSIT", "LOW"  # unknown deposit: not spending, not income
    return None, None, "LOW"


# ---------------------------------------------------------------- storage
def load() -> list[dict]:
    try:
        return json.loads(STORE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(imports: list[dict]) -> None:
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(imports))
    tmp.replace(STORE)


def add(filename: str, rows: list[dict], account: dict) -> dict:
    """account: {"id": <Plaid account_id>} to attach to a connected account, or {"name", "kind"} for a new one."""
    imports = load()
    if account.get("id") and not str(account["id"]).startswith("import:"):
        acct = {"account_id": str(account["id"])[:80]}
    else:
        name = (str(account.get("name") or "").strip() or "Imported account")[:60]
        kind = account.get("kind") if account.get("kind") in KINDS else "cash"
        slug = account.get("id") or "import:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        acct = {"account_id": slug, "account_name": name, "kind": kind}
    rec = {"id": secrets.token_hex(4), "filename": str(filename)[:80], "created": date.today().isoformat(), **acct,
           "count": len(rows), "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
           "rows": [{"date": r["date"], "desc": str(r["desc"])[:120], "amount": float(r["amount"]), "hint": str(r.get("hint") or "")[:60]}
                    for r in rows]}
    imports.append(rec)
    save(imports)
    return {k: v for k, v in rec.items() if k != "rows"}


def remove(import_id: str) -> None:
    save([i for i in load() if i["id"] != import_id])


def summary() -> list[dict]:
    return [{k: v for k, v in i.items() if k != "rows"} for i in load()]


# ---------------------------------------------------------------- merge into the build
def accounts() -> list[dict]:
    """Accounts that exist only as imports (not attached to a Plaid account)."""
    seen, out = set(), []
    for i in load():
        if i["account_id"].startswith("import:") and i["account_id"] not in seen:
            seen.add(i["account_id"])
            out.append({"id": i["account_id"], "name": i.get("account_name") or "Imported account", "institution": "Imported",
                        "mask": None, "kind": i.get("kind", "cash"), "liability": i.get("kind") == "credit_card", "balance": None,
                        "source": "import", "hidden": False})
    return out


def transactions(plaid_txs: list[dict], accounts_by_id: dict) -> list[dict]:
    """Imported rows as Plaid-shaped transactions, minus anything Plaid already covers."""
    earliest: dict[str, str] = {}
    for t in plaid_txs:
        a = t["account_id"]
        if a not in earliest or t["date"] < earliest[a]:
            earliest[a] = t["date"]
    imports = load()
    # a row can legitimately repeat inside one file (two identical coffees); across overlapping files it's a duplicate
    allowed: Counter = Counter()
    for imp in imports:
        c = Counter((imp["account_id"], r["date"], round(r["amount"], 2), r["desc"].lower()) for r in imp["rows"])
        for k, n in c.items():
            allowed[k] = max(allowed[k], n)
    out, seen = [], Counter()
    for imp in imports:
        aid = imp["account_id"]
        kind = (accounts_by_id.get(aid) or {}).get("kind", imp.get("kind", "cash"))
        cutoff = earliest.get(aid)
        for r in imp["rows"]:
            if cutoff and r["date"] >= cutoff:
                continue
            key = (aid, r["date"], round(r["amount"], 2), r["desc"].lower())
            if seen[key] >= allowed[key]:
                continue
            seen[key] += 1
            p, d, conf = guess_category(r["desc"], r.get("hint", ""), r["amount"], kind)
            tid = "imp:" + hashlib.sha1(f"{aid}|{r['date']}|{r['amount']}|{r['desc']}|{seen[key]}".encode()).hexdigest()[:16]
            out.append({"transaction_id": tid, "item_id": None, "account_id": aid, "date": r["date"], "amount": r["amount"],
                        "merchant": None, "name": r["desc"], "original": r["desc"], "pfc_primary": p, "pfc_detailed": d,
                        "pfc_confidence": conf, "pending": False, "source": "import"})
    return out

