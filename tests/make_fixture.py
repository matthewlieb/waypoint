#!/usr/bin/env python3
"""Write fake Plaid-shaped data for a fictional person into a data folder, for offline testing and demos.

    python3 tests/make_fixture.py /tmp/wp-demo && FIN_DATA=/tmp/wp-demo python3 build.py
No network, no keys. Amounts follow Plaid's sign convention (+ money out, - money in).
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

out = Path(sys.argv[1] if len(sys.argv) > 1 else "data")
(out / "plaid").mkdir(parents=True, exist_ok=True)
random.seed(7)
today = date.today()
start = today - timedelta(days=200)
IT = "item-demo-bank"
acc = [
    ("chk", "Everyday Checking", "0001", "depository", "checking", 3120.55, None),
    ("sav", "High Yield Savings", "0002", "depository", "savings", 8400.00, None),
    ("card", "Rewards Card", "0003", "credit", "credit card", 1840.22, 9000),
    ("stu", "Student Loan", "0004", "loan", "student", 14250.00, None),
    ("ira", "Roth IRA", "0005", "investment", "roth", 12880.10, None),
    ("brk", "Brokerage", "0006", "investment", "brokerage", 4210.00, None),
]
accounts = [{"item_id": IT, "account_id": a, "persistent_account_id": None, "institution": "Demo Bank", "name": n, "short_name": n,
             "mask": m, "type": t, "subtype": s, "balance": b, "available": b, "limit": l, "currency": "USD"} for a, n, m, t, s, b, l in acc]
txs, n = [], 0


def tx(d, acct, amt, merchant, primary, detailed, conf="VERY_HIGH"):
    global n
    n += 1
    txs.append({"transaction_id": f"t{n:05d}", "item_id": IT, "account_id": acct, "date": d.isoformat(), "posted": d.isoformat(),
                "amount": round(amt, 2), "currency": "USD", "merchant": merchant, "name": merchant, "original": merchant.upper(),
                "pfc_primary": primary, "pfc_detailed": detailed, "pfc_confidence": conf, "pending": False,
                "pending_transaction_id": None, "channel": "in store"})


d = start
while d <= today:
    if d.weekday() == 4 and (d - start).days % 14 < 7:
        tx(d, "chk", -2400, "Acme Corp Payroll", "INCOME", "INCOME_WAGES")
        tx(d, "chk", 500, "Transfer to savings", "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS")
        tx(d, "sav", -500, "Transfer from checking", "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
        tx(d, "chk", 250, "Roth IRA contribution", "TRANSFER_OUT", "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")
    if d.day == 1:
        tx(d, "chk", 1650, "Maple Apartments", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT")
        tx(d, "card", 15.49, "Netflix", "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES")
        tx(d, "card", 10.99, "Spotify", "ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO")
    if d.day == 5:
        tx(d, "chk", 320, "Student Loan Servicer", "LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT")
        tx(d, "chk", 95 + random.random() * 30, "City Power & Light", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY")
        tx(d, "card", 128, "Safe Auto Insurance", "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE")
    if d.day == 20:
        pay = 900 + random.random() * 400
        tx(d, "chk", pay, "Rewards Card Payment", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        tx(d + timedelta(days=1), "card", -pay, "Payment received", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    for _ in range(random.choice([0, 1, 1, 2])):
        kind = random.random()
        if kind < .3:
            tx(d, "card", 20 + random.random() * 70, random.choice(["Fresh Market", "Green Grocer"]), "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
        elif kind < .55:
            tx(d, "card", 12 + random.random() * 45, random.choice(["Taco Spot", "Noodle House", "Burger Barn"]), "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT")
        elif kind < .65:
            tx(d, "card", 4 + random.random() * 4, "Bean Coffee", "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
        elif kind < .75:
            tx(d, "card", 35 + random.random() * 20, "QuickFuel", "TRANSPORTATION", "TRANSPORTATION_GAS")
        elif kind < .9:
            tx(d, "card", 15 + random.random() * 120, random.choice(["Online Store", "Target-ish"]), "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "MEDIUM")
        else:
            tx(d, "card", 30 + random.random() * 60, "Mystery Merchant", None, None, "LOW")
    d += timedelta(days=1)
tx(today - timedelta(days=9), "sav", 700, "Transfer to checking", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
tx(today - timedelta(days=9), "chk", -700, "Transfer from savings", "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
tx(today - timedelta(days=3), "card", 640, "Big TV Store", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ELECTRONICS")
(out / "plaid" / "accounts.json").write_text(json.dumps(accounts))
(out / "plaid" / "transactions.json").write_text(json.dumps(txs))
(out / "plaid" / "holdings.json").write_text(json.dumps([
    {"item_id": IT, "account_id": "ira", "security_id": "s1", "name": "Total Market Index Fund", "ticker": "TMIF", "type": "mutual fund", "is_cash": False, "quantity": 100, "value": 11880.10},
    {"item_id": IT, "account_id": "ira", "security_id": "s2", "name": "Cash", "ticker": "CUR:USD", "type": "cash", "is_cash": True, "quantity": 1000, "value": 1000},
    {"item_id": IT, "account_id": "brk", "security_id": "s3", "name": "Bitcoin", "ticker": "BTC", "type": "cryptocurrency", "is_cash": False, "quantity": .05, "value": 4210.00}]))
(out / "plaid" / "liabilities.json").write_text(json.dumps({IT: {
    "credit": [{"account_id": "card", "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 24.99}], "minimum_payment_amount": 45,
                "next_payment_due_date": (today + timedelta(days=4)).isoformat(), "last_statement_balance": 1500, "last_payment_amount": 900, "is_overdue": False}],
    "student": [{"account_id": "stu", "interest_rate_percentage": 5.5, "minimum_payment_amount": 320, "next_payment_due_date": (today + timedelta(days=12)).isoformat(), "is_overdue": False}]}}))
(out / "plaid" / "items.json").write_text(json.dumps([{"item_id": IT, "access_token": "not-a-real-token", "institution": "Demo Bank",
    "institution_id": "ins_demo", "accounts": [], "status": "ok", "history": "complete", "last_sync": today.isoformat()}]))
print(f"wrote {len(txs)} transactions for 'Demo Bank' to {out}")
