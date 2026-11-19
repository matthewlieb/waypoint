#!/usr/bin/env python3
"""Turn synced Plaid data + settings.json into the dashboard (dashboard.html) and alerts.json.

Nothing here is specific to one person: categories come from Plaid's personal_finance_category,
transfers between your own accounts are matched and dropped, income is detected from INCOME
transactions, and goals/budget come from settings.json (edited on the in-app Settings page).
"""
from __future__ import annotations

import calendar
import json
import os
import re
import statistics as st
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("FIN_DATA", ROOT / "data"))
P = DATA / "plaid"

RETIREMENT = {"401k", "403b", "457b", "ira", "roth", "roth 401k", "sep ira", "simple ira", "keogh", "pension",
              "retirement", "sarsep", "tsp", "thrift savings plan", "lif", "lira", "rrsp", "tfsa", "hsa"}
FIXED = {"Housing", "Utilities", "Insurance", "Loan payments", "Childcare & education"}
ONE_TIME_SUFFIX = " (one-time)"
NOT_SPENDING = "Not spending"


def load(name: str, default):
    f = DATA / name
    return json.loads(f.read_text()) if f.exists() else default


def category(primary: str | None, detailed: str | None) -> str:
    d = (detailed or "").upper()
    p = (primary or "").upper()
    if p == "FOOD_AND_DRINK":
        if "GROCER" in d:
            return "Groceries"
        if "COFFEE" in d:
            return "Coffee"
        if "BEER_WINE" in d or "LIQUOR" in d or "BAR" in d:
            return "Bars & nightlife"
        return "Dining"
    if p == "RENT_AND_UTILITIES":
        return "Housing" if d.endswith("_RENT") else "Utilities"
    if p == "TRANSPORTATION":
        return "Gas" if "GAS" in d else "Transportation"
    if p == "GENERAL_SERVICES":
        if "INSURANCE" in d:
            return "Insurance"
        if "CHILDCARE" in d or "EDUCATION" in d:
            return "Childcare & education"
        return "Services"
    if p == "LOAN_PAYMENTS":
        return "Loan payments"
    if p == "GENERAL_MERCHANDISE":
        if "SUPERSTORE" in d or "DISCOUNT" in d:
            return "Shopping"
        if "ONLINE" in d:
            return "Online shopping"
        return "Shopping"
    return {"ENTERTAINMENT": "Entertainment", "TRAVEL": "Travel", "MEDICAL": "Health", "PERSONAL_CARE": "Personal care",
            "HOME_IMPROVEMENT": "Home", "GOVERNMENT_AND_NON_PROFIT": "Taxes & donations", "BANK_FEES": "Fees",
            "TRANSFER_OUT": "Transfers & payments", "INCOME": "Income"}.get(p, "Other")


def acct_kind(a: dict) -> str:
    t, s = a.get("type"), (a.get("subtype") or "").lower()
    if t == "depository":
        return "savings" if s in ("savings", "money market", "cd", "hsa") and s != "hsa" else "cash"
    if t == "investment":
        return "retirement" if s in RETIREMENT else "investments"
    if t == "credit":
        return "credit_card"
    if t == "loan":
        return {"student": "student_loan", "auto": "auto_loan", "mortgage": "mortgage"}.get(s, "loan")
    return "other"


def build_accounts(settings: dict) -> list[dict]:
    """Plaid accounts (deduped) + manual accounts, normalized. Liabilities are positive amounts owed."""
    out, seen = [], set()
    hidden = set(settings.get("hidden_accounts", []))
    for a in load("plaid/accounts.json", []):
        key = a.get("persistent_account_id") or (a.get("institution"), a.get("mask"), a.get("subtype"), a.get("name"))
        if str(key) in seen:
            continue
        seen.add(str(key))
        k = acct_kind(a)
        liab = a.get("type") in ("credit", "loan")
        bal = a.get("balance")
        out.append({"id": a["account_id"], "item_id": a.get("item_id"), "name": a.get("name") or a.get("short_name"),
                    "institution": a.get("institution"), "mask": a.get("mask"), "kind": k, "liability": liab,
                    "balance": None if bal is None else round(abs(bal) if liab else bal, 2), "limit": a.get("limit"),
                    "source": "plaid", "hidden": a["account_id"] in hidden})
    for m in settings.get("manual_accounts", []):
        out.append({"id": m["id"], "name": m["name"], "institution": "Manual", "mask": None, "kind": m.get("kind", "other"),
                    "liability": bool(m.get("liability")), "balance": m.get("balance"), "source": "manual",
                    "note": m.get("note"), "hidden": False})
    return out


def classify(txs: list[dict], accounts: list[dict], settings: dict) -> tuple[list[dict], list[dict]]:
    """Split Plaid transactions into spending rows and money flows (income, savings in/out, investing)."""
    by_id = {a["id"]: a for a in accounts}
    savings = set(settings.get("savings_account_ids") or [a["id"] for a in accounts if a["kind"] == "savings"])
    rows = [t for t in txs if by_id.get(t["account_id"], {}).get("kind") not in ("retirement", "investments", "student_loan",
                                                                                   "auto_loan", "mortgage", "loan", None)]
    # match transfers between your own linked accounts: same amount, opposite sign, different account, within 4 days
    matched: dict[str, str] = {}
    by_amt = defaultdict(list)
    for t in rows:
        by_amt[round(abs(t["amount"]), 2)].append(t)
    for amt, group in by_amt.items():
        if amt == 0 or len(group) < 2:
            continue
        outs = [t for t in group if t["amount"] > 0]
        ins = [t for t in group if t["amount"] < 0]
        for o in outs:
            if o["transaction_id"] in matched or (o["pfc_primary"] not in ("TRANSFER_OUT", "LOAN_PAYMENTS", "TRANSFER_IN", None)):
                continue
            od = date.fromisoformat(o["date"])
            for i in ins:
                if i["transaction_id"] in matched or i["account_id"] == o["account_id"]:
                    continue
                if abs((date.fromisoformat(i["date"]) - od).days) <= 4:
                    matched[o["transaction_id"]] = i["transaction_id"]
                    matched[i["transaction_id"]] = o["transaction_id"]
                    break

    rules = load("overrides.json", [])
    spend, flows = [], []

    def flow(t, kind, desc=None):
        flows.append({"date": t["date"], "kind": kind, "amount": round(abs(t["amount"]), 2),
                      "account": by_id[t["account_id"]]["name"], "account_id": t["account_id"],
                      "desc": desc or t.get("merchant") or t.get("name") or ""})

    for t in rows:
        acct = by_id[t["account_id"]]
        p, d = t.get("pfc_primary"), (t.get("pfc_detailed") or "").upper()
        desc = t.get("merchant") or t.get("name") or t.get("original") or "Unknown"
        amt = t["amount"]  # Plaid sign: + money out, - money in
        if t["transaction_id"] in matched:
            if acct["id"] in savings:
                flow(t, "savings_out" if amt > 0 else "savings_in", "Out of savings" if amt > 0 else "Into savings")
            continue
        if acct["kind"] == "credit_card" and (p in ("TRANSFER_IN", "LOAN_PAYMENTS") or "CREDIT_CARD_PAYMENT" in d) and amt < 0:
            continue  # card payment received; the card's purchases are what count
        if p == "LOAN_PAYMENTS" and "CREDIT_CARD_PAYMENT" in d:
            continue  # paying a card from checking (unmatched because the card isn't linked or posted later)
        if amt < 0 and p in ("INCOME", None) and ("INTEREST" in d or "DIVIDEND" in d):
            flow(t, "interest")
            continue
        if amt < 0 and p == "INCOME":
            flow(t, "income")
            if acct["id"] in savings:
                flow(t, "savings_in", "Paycheck to savings")
            continue
        if amt < 0 and p == "TRANSFER_IN":
            flow(t, "savings_in" if acct["id"] in savings else "other_in")
            continue
        if amt > 0 and p == "TRANSFER_OUT":
            if "INVESTMENT" in d or "RETIREMENT" in d:
                flow(t, "invest")
                continue
            if acct["id"] in savings:
                flow(t, "savings_out")
                continue
            if "SAVINGS" in d:
                flow(t, "savings_in", "To savings (unlinked)")
                continue
        cat = "Cash & ATM" if "WITHDRAWAL" in d and "ATM" in d else category(p, d)
        if cat == "Income":
            cat = "Other"
        review = (t.get("pfc_confidence") or "").upper() in ("LOW", "UNKNOWN", "")
        row = {"id": t["transaction_id"], "date": t["date"], "desc": desc, "amount": round(amt, 2),
               "account": acct["name"], "category": cat, "review": review, "pending": t.get("pending", False)}
        for r in rules:  # in-app edits: one transaction (id) or every one from a merchant (desc regex)
            if (r.get("id") and r["id"] == row["id"]) or (not r.get("id") and r.get("desc") and re.search(r["desc"], desc, re.I)):
                row["category"], row["review"] = r["category"], False
                break
        if row["category"] == NOT_SPENDING:
            continue
        spend.append(row)
    return sorted(spend, key=lambda r: r["date"]), sorted(flows, key=lambda f: f["date"])


def month_list(today: date, n: int) -> list[str]:
    out, y, m = [], today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}-{m:02d}")
    return out[::-1]  # n complete months before this one


def is_one_time(cat: str) -> bool:
    return cat.endswith(ONE_TIME_SUFFIX)


def income_info(flows: list[dict], today: date, settings: dict) -> dict:
    inc = [f for f in flows if f["kind"] == "income"]
    full = [m for m in month_list(today, 3) if any(f["date"].startswith(m) for f in inc)]
    per = [sum(f["amount"] for f in inc if f["date"].startswith(m)) for m in full]
    est = st.median(per) if per else sum(f["amount"] for f in inc if (today - date.fromisoformat(f["date"])).days <= 30)
    # paydays: large deposits; cadence from the gaps between them
    big = [f for f in inc if f["amount"] >= 0.25 * max([f["amount"] for f in inc] or [0])]
    days = sorted({f["date"] for f in big if (today - date.fromisoformat(f["date"])).days <= 120})
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:]) if (date.fromisoformat(b) - date.fromisoformat(a)).days >= 5]
    cadence = int(st.median(gaps)) if len(gaps) >= 2 else None
    manual = settings.get("monthly_take_home")
    return {"monthly": round(manual or est or 0, 2), "detected": round(est or 0, 2), "manual": bool(manual),
            "cadence_days": cadence, "last_payday": days[-1] if days else None,
            "typical_paycheck": round(st.median([f["amount"] for f in big[-6:]]), 2) if big else None}


def averages(spend: list[dict], today: date, months: int = 3) -> dict:
    ms = month_list(today, months)
    have = [m for m in ms if any(t["date"].startswith(m) for t in spend)] or ms
    by = defaultdict(float)
    for t in spend:
        if t["date"][:7] in have and not is_one_time(t["category"]):
            by[t["category"]] += t["amount"]
    return {c: round(v / len(have), 2) for c, v in by.items() if v > 0}


def build_budget(spend: list[dict], today: date, settings: dict) -> dict:
    avg = averages(spend, today)
    targets = settings.get("budget") or {c: round(v / 10) * 10 for c, v in avg.items() if v >= 15}
    actual = defaultdict(lambda: defaultdict(float))
    for t in spend:
        if not is_one_time(t["category"]):
            actual[t["category"]][t["date"][:7]] += t["amount"]
    lines = [{"name": c, "target": float(v), "group": "Fixed" if c in FIXED else "Flexible", "avg": avg.get(c, 0),
              "actual": {m: round(x, 2) for m, x in actual[c].items()}} for c, v in targets.items()]
    unbudgeted = sorted(c for c in avg if c not in targets)
    return {"this_month": today.strftime("%Y-%m"), "lines": sorted(lines, key=lambda l: (l["group"] != "Fixed", -l["target"])),
            "suggested": not settings.get("budget"), "unbudgeted": unbudgeted, "averages": avg}


def liability_details() -> dict:
    """account_id -> {apr, min_payment, next_due, last_statement} from /liabilities/get (fields are often null)."""
    out = {}
    for lia in load("plaid/liabilities.json", {}).values():
        for c in lia.get("credit") or []:
            aprs = c.get("aprs") or []
            purchase = next((a.get("apr_percentage") for a in aprs if a.get("apr_type") == "purchase_apr"), None)
            stmt, paid = c.get("last_statement_balance"), c.get("last_payment_amount")
            out[c["account_id"]] = {"apr": purchase, "min_payment": c.get("minimum_payment_amount"),
                                    "next_due": c.get("next_payment_due_date"), "statement_balance": stmt,
                                    "overdue": c.get("is_overdue"),
                                    "revolving": bool(stmt and paid is not None and paid < stmt * 0.98)}
        for s in lia.get("student") or []:
            out[s["account_id"]] = {"apr": s.get("interest_rate_percentage"), "min_payment": s.get("minimum_payment_amount"),
                                    "next_due": s.get("next_payment_due_date"), "overdue": s.get("is_overdue")}
        for m in lia.get("mortgage") or []:
            out[m["account_id"]] = {"apr": (m.get("interest_rate") or {}).get("percentage"), "min_payment": m.get("next_monthly_payment"),
                                    "next_due": m.get("next_payment_due_date"), "overdue": m.get("past_due_amount")}
    return out


def default_debts(accounts: list[dict], lia: dict) -> list[str]:
    """Student loans and personal loans, plus credit cards that carry a balance month to month.
    Mortgages, car loans and cards paid in full each month are left out."""
    out = []
    for a in accounts:
        if not a["liability"] or not (a["balance"] or 0) > 0 or a["source"] != "plaid":
            continue
        if a["kind"] in ("student_loan", "loan") or (a["kind"] == "credit_card" and lia.get(a["id"], {}).get("revolving")):
            out.append(a["id"])
    return out


def build_goals(accounts: list[dict], budget: dict, settings: dict, lia: dict) -> list[dict]:
    by_id = {a["id"]: a for a in accounts}
    sav_ids = settings.get("savings_account_ids") or [a["id"] for a in accounts if a["kind"] == "savings"]
    saved = round(sum(by_id[i]["balance"] or 0 for i in sav_ids if i in by_id), 2)
    essentials = settings.get("essential_monthly") or round(sum(l["avg"] for l in budget["lines"] if l["group"] == "Fixed")
                                                            + sum(budget["averages"].get(c, 0) for c in ("Groceries", "Gas", "Transportation", "Health")), 2)
    goals = []
    em = settings.get("emergency_months", 6)
    if em:
        goals.append({"id": "emergency", "kind": "emergency", "name": "Emergency fund", "target": round(em * essentials, 2),
                      "months": em, "essential_monthly": essentials, "saved": saved, "by": settings.get("emergency_by")})
    debt_ids = settings.get("debt_account_ids")
    if debt_ids is None:
        debt_ids = default_debts(accounts, lia)
    debts = [{"id": i, "name": by_id[i]["name"], "balance": by_id[i]["balance"] or 0, **lia.get(i, {})}
             for i in debt_ids if i in by_id and (by_id[i]["balance"] or 0) > 0]
    if debts:
        goals.append({"id": "debt", "kind": "debt", "name": "Pay off debt", "target": round(sum(d["balance"] for d in debts), 2),
                      "debts": sorted(debts, key=lambda d: -(d.get("apr") or 0)), "saved": 0,
                      "min_payments": round(sum(d.get("min_payment") or 0 for d in debts), 2), "by": settings.get("debt_by")})
    for g in settings.get("goals", []):
        amt = g.get("amount")
        if g.get("kind") == "home" and g.get("price"):
            amt = g["price"] * (g.get("down_pct", 20) + g.get("closing_pct", 3)) / 100
        if amt:
            goals.append({"id": g["id"], "kind": g.get("kind", "custom"), "name": g.get("name") or "Savings goal",
                          "target": round(amt, 2), "saved": g.get("saved", 0), "by": g.get("by"),
                          "price": g.get("price"), "down_pct": g.get("down_pct"), "closing_pct": g.get("closing_pct")})
    order = settings.get("order") or [g["id"] for g in goals]
    goals.sort(key=lambda g: order.index(g["id"]) if g["id"] in order else 99)
    return goals


def recurring(spend: list[dict], flows: list[dict], accounts: list[dict], today: date) -> list[dict]:
    """Plaid's recurring streams when the team has that add-on; otherwise detected from repeating charges."""
    by_id = {a["id"]: a for a in accounts}
    streams = load("plaid/recurring.json", {})
    out = []
    if streams:
        mult = {"WEEKLY": 52 / 12, "BIWEEKLY": 26 / 12, "SEMI_MONTHLY": 2, "MONTHLY": 1, "ANNUALLY": 1 / 12}
        for item in streams.values():
            for s in item.get("outflow", []):
                if s.get("status") == "TOMBSTONED":
                    continue
                avg = abs((s.get("average_amount") or {}).get("amount") or 0)
                pfc = s.get("personal_finance_category") or {}
                cat = category(pfc.get("primary"), pfc.get("detailed"))
                if cat in ("Transfers & payments", "Loan payments") and "CREDIT_CARD" in (pfc.get("detailed") or ""):
                    continue
                out.append({"name": s.get("merchant_name") or s.get("description"), "category": cat,
                            "kind": "bill" if cat in FIXED else "subscription", "frequency": (s.get("frequency") or "").lower(),
                            "monthly": round(avg * mult.get(s.get("frequency"), 1), 2), "last_amount": abs((s.get("last_amount") or {}).get("amount") or 0),
                            "last_date": s.get("last_date"), "next_date": s.get("predicted_next_date"),
                            "active": bool(s.get("is_active")), "account": by_id.get(s.get("account_id"), {}).get("name", ""),
                            "source": "plaid", "confidence": "recurring" if s.get("status") == "MATURE" else "early"})
        return sorted(out, key=lambda s: (s["kind"] != "bill", not s["active"], -s["monthly"]))
    skip = {"Groceries", "Dining", "Coffee", "Bars & nightlife", "Gas", "Transportation", "Travel", "Shopping", "Online shopping",
            "Cash & ATM", "Transfers & payments", "Health", "Fees", "Other", "Personal care", "Home"}
    groups = defaultdict(list)
    for t in spend:
        if t["amount"] > 0 and not is_one_time(t["category"]):
            groups[re.sub(r"[^a-z]", "", t["desc"].lower())[:18]].append(t)
    for key, rows in groups.items():
        cat = st.mode([t["category"] for t in rows])
        if cat in skip or not key:
            continue
        months = sorted({t["date"][:7] for t in rows})
        by_m = defaultdict(float)
        for t in rows:
            by_m[t["date"][:7]] += t["amount"]
        amts = [by_m[m] for m in months[-4:]]
        stable = len(amts) >= 2 and (max(amts) - min(amts)) <= 0.2 * st.median(amts)
        if len(months) < 3 and not (len(months) >= 2 and stable):
            continue
        last = rows[-1]
        ld = date.fromisoformat(last["date"])
        nxt = ld + timedelta(days=30)
        out.append({"name": last["desc"], "category": cat, "kind": "bill" if cat in FIXED else "subscription", "frequency": "monthly",
                    "monthly": round(st.median(amts), 2), "last_amount": last["amount"], "last_date": last["date"],
                    "next_date": nxt.isoformat(), "active": (today - ld).days <= 45, "account": last["account"],
                    "source": "detected", "confidence": "recurring" if len(months) >= 3 else "likely"})
    return sorted(out, key=lambda s: (s["kind"] != "bill", not s["active"], -s["monthly"]))


def calendar_events(subs: list[dict], accounts: list[dict], lia: dict, inc: dict, today: date, days: int = 30) -> list[dict]:
    end = today + timedelta(days=days)
    ev = []
    for s in subs:
        if s["active"] and s.get("next_date"):
            d = date.fromisoformat(s["next_date"])
            while d < today:
                d += timedelta(days=30)
            if d <= end:
                ev.append({"date": d.isoformat(), "name": s["name"], "amount": s["last_amount"], "kind": s["kind"]})
    names = {a["id"]: a["name"] for a in accounts}
    for aid, l in lia.items():
        if l.get("next_due") and today.isoformat() <= l["next_due"] <= end.isoformat():
            ev.append({"date": l["next_due"], "name": f"{names.get(aid, 'Card')} payment due", "amount": l.get("min_payment"), "kind": "due"})
    if inc.get("cadence_days") and inc.get("last_payday"):
        d = date.fromisoformat(inc["last_payday"])
        while d <= end:
            if d >= today:
                ev.append({"date": d.isoformat(), "name": "Payday", "amount": -(inc.get("typical_paycheck") or 0), "kind": "income"})
            d += timedelta(days=inc["cadence_days"])
    return sorted(ev, key=lambda e: e["date"])


def build_alerts(spend, flows, budget, subs, accounts, lia, items, settings, today) -> list[dict]:
    dim = calendar.monthrange(today.year, today.month)[1]
    progress = today.day / dim
    out = []

    def add(level, title, detail, key):
        out.append({"level": level, "title": title, "detail": detail, "id": f"{today:%Y-%m}:{key}"})

    for it in items:
        st_ = it.get("status")
        if st_ in ("login_required", "expiring", "revoked", "new_accounts", "error"):
            msg = {"login_required": "needs you to log in again", "expiring": "connection expires within 7 days",
                   "revoked": "was disconnected at the bank", "new_accounts": "has new accounts you can add",
                   "error": "had a sync problem"}[st_]
            add("critical" if st_ in ("login_required", "revoked") else "warning", f"{it['institution']} {msg}",
                "Open Connect banks and tap Reconnect.", f"item:{it['item_id']}:{st_}")
    for l in budget["lines"]:
        if l["group"] == "Fixed" or not l["target"]:
            continue
        mtd = l["actual"].get(budget["this_month"], 0)
        if mtd > l["target"]:
            add("critical", f"{l['name']}: over budget", f"${mtd:,.0f} spent of ${l['target']:,.0f} this month", f"over:{l['name']}")
        elif mtd > 50 and mtd > l["target"] * progress * 1.15:
            add("warning", f"{l['name']}: running hot", f"${mtd:,.0f} spent {today.day} days in. On pace for ${mtd / progress:,.0f} vs ${l['target']:,.0f}.", f"pace:{l['name']}")
    leak = [f for f in flows if f["kind"] == "savings_out" and f["date"][:7] == f"{today:%Y-%m}"]
    for f in leak:
        add("critical", f"${f['amount']:,.0f} moved out of savings", f"From {f['account']} on {f['date']}. ${sum(x['amount'] for x in leak):,.0f} out this month.",
            f"leak:{f['date']}:{f['amount']}")
    big = settings.get("big_charge", 250)
    for t in spend:
        age = (today - date.fromisoformat(t["date"])).days
        if 0 <= age <= 7 and t["amount"] >= big and t["category"] not in FIXED:
            add("info", f"Large charge: {t['desc'][:40]}", f"${t['amount']:,.2f} on {t['account']} ({t['date']})", f"big:{t['id']}")
    for s in subs:
        if s["active"] and s.get("last_date") and (today - date.fromisoformat(s["last_date"])).days <= 10 and s["confidence"] in ("early", "likely"):
            add("info", f"New recurring charge: {s['name']}", f"About ${s['monthly']:,.2f} a month on {s['account']}", f"newsub:{s['name']}")
    names = {a["id"]: a["name"] for a in accounts}
    for aid, l in lia.items():
        if l.get("next_due"):
            dd = (date.fromisoformat(l["next_due"]) - today).days
            if 0 <= dd <= 5:
                amt = f" Minimum ${l['min_payment']:,.2f}." if l.get("min_payment") else ""
                add("warning" if dd <= 2 else "info", f"{names.get(aid, 'Card')} due in {dd} day{'s' if dd != 1 else ''}", f"Due {l['next_due']}.{amt}", f"due:{aid}:{l['next_due']}")
        if l.get("overdue"):
            add("critical", f"{names.get(aid, 'Account')} is past due", "Pay at least the minimum to avoid fees.", f"overdue:{aid}")
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])


def investments(accounts: list[dict]) -> dict:
    seen, rows = set(), []
    names = {a["id"]: a for a in accounts}
    for h in load("plaid/holdings.json", []):
        k = (h["account_id"], h.get("security_id"), round(h.get("value") or 0, 2))
        if k in seen or not h.get("value"):
            continue
        seen.add(k)
        t = (h.get("type") or "").lower()
        cls = ("Cash" if h.get("is_cash") or t == "cash" else "Crypto" if t == "cryptocurrency" else
               "Funds" if t in ("etf", "mutual fund") else "Bonds" if t == "fixed income" else
               "Options & other" if t in ("derivative", "other") else "Stocks")
        if cls == "Funds" and re.search(r"target|retirement 20\d\d", h.get("name") or "", re.I):
            cls = "Target-date funds"
        a = names.get(h["account_id"], {})
        rows.append({"name": h.get("name") or h.get("ticker"), "ticker": h.get("ticker"), "class": cls, "value": round(h["value"], 2),
                     "account": a.get("name", ""), "institution": a.get("institution", "")})
    mix = defaultdict(float)
    for r in rows:
        mix[r["class"]] += r["value"]
    held = {a["account"] for a in rows}
    for a in accounts:
        if a["kind"] in ("retirement", "investments") and a.get("balance") and a["name"] not in held:
            mix["Not broken down"] += a["balance"]
    return {"holdings": sorted(rows, key=lambda r: -r["value"]), "mix": {k: round(v, 2) for k, v in mix.items()}}


def checkin(spend, flows, goals, today) -> dict:
    lm = month_list(today, 1)[0]
    inc = sum(f["amount"] for f in flows if f["kind"] == "income" and f["date"].startswith(lm))
    sp = sum(t["amount"] for t in spend if t["date"].startswith(lm))
    one = sum(t["amount"] for t in spend if t["date"].startswith(lm) and is_one_time(t["category"]))
    out_ = sum(f["amount"] for f in flows if f["kind"] == "savings_out" and f["date"].startswith(lm))
    return {"month": lm, "income": round(inc, 2), "spent": round(sp, 2), "one_time": round(one, 2), "kept": round(inc - sp, 2),
            "pulled_from_savings": round(out_, 2), "goals": [{"name": g["name"], "target": g["target"]} for g in goals]}


def main() -> None:
    settings = load("settings.json", {})
    items = json.loads((P / "items.json").read_text()) if (P / "items.json").exists() else []
    today = date.today()
    accounts = build_accounts(settings)
    txs = load("plaid/transactions.json", [])
    spend, flows = classify(txs, accounts, settings)
    visible = [a for a in accounts if not a["hidden"]]
    assets = sum(a["balance"] or 0 for a in visible if not a["liability"])
    liabs = sum(a["balance"] or 0 for a in visible if a["liability"])
    nw = round(assets - liabs, 2)
    hist = [h for h in load("history.json", []) if h["date"] != today.isoformat()]
    if accounts:
        hist.append({"date": today.isoformat(), "assets": round(assets, 2), "liabilities": round(liabs, 2), "net_worth": nw})
    (DATA / "history.json").write_text(json.dumps(sorted(hist, key=lambda h: h["date"]), indent=1))

    lia = liability_details()
    inc = income_info(flows, today, settings)
    budget = build_budget(spend, today, settings)
    goals = build_goals(accounts, budget, settings, lia)
    subs = recurring(spend, flows, accounts, today)
    alerts = build_alerts(spend, flows, budget, subs, accounts, lia, items, settings, today)
    avg_spend = round(sum(v for v in budget["averages"].values()), 2)
    invest_flow = [f for f in flows if f["kind"] == "invest" and f["date"][:7] in month_list(today, 3)]
    invest_monthly = settings.get("invest_monthly")
    if invest_monthly is None:
        invest_monthly = round(sum(f["amount"] for f in invest_flow) / 3, 2)
    ms3 = month_list(today, 3)
    net_saved = (sum(f["amount"] for f in flows if f["kind"] == "savings_in" and f["date"][:7] in ms3)
                 - sum(f["amount"] for f in flows if f["kind"] == "savings_out" and f["date"][:7] in ms3)) / 3
    auto_savings = round(net_saved, 2) if net_saved > 0 else max(0, round(inc["monthly"] - avg_spend - invest_monthly, 2))
    payload = {
        "app": {"name": os.environ.get("APP_NAME", "Waypoint"), "generated": datetime.now().isoformat(timespec="minutes"),
                "as_of": today.isoformat(), "env": os.environ.get("PLAID_ENV", "sandbox")},
        "person": {"name": settings.get("name", "")},
        "configured": bool(settings),
        "accounts": accounts, "history": load("history.json", []), "transactions": spend, "flows": flows,
        "income": inc, "budget": budget, "goals": goals, "subscriptions": subs, "alerts": alerts,
        "liabilities": lia, "investments": investments(accounts),
        "calendar": calendar_events(subs, accounts, lia, inc, today),
        "plan": {"monthly_savings": settings.get("monthly_savings") or auto_savings,
                 "monthly_savings_auto": settings.get("monthly_savings") is None, "invest_monthly": invest_monthly,
                 "avg_spending": avg_spend, "return": settings.get("return_pct", 7) / 100, "apy": settings.get("savings_apy", 3.5) / 100},
        "items": [{"institution": i["institution"], "status": i.get("status", "ok"), "history": i.get("history"),
                   "last_sync": i.get("last_sync")} for i in items],
    }
    (DATA / "alerts.json").write_text(json.dumps(alerts, indent=1))
    (DATA / "checkin.json").write_text(json.dumps(checkin(spend, flows, goals, today), indent=1))
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    html = tpl.replace("/*__DATA__*/null", json.dumps(payload).replace("</", "<\\/"))
    (DATA / "dashboard.html").write_text(html, encoding="utf-8")
    print(f"net worth ${nw:,.2f} | {len(accounts)} accounts | {len(spend)} spending rows | {len(flows)} flows")


if __name__ == "__main__":
    main()
