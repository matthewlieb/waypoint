# Waypoint

A private money dashboard you host yourself. Connect your banks once and it shows:

- **Net worth**, today and projected, with your goals marked on the timeline
- **A plan** for your goals (emergency fund, paying off debt, a down payment, anything else), one at a time or split
- **Budget** suggested from your real spending, with "safe to spend today"
- **Spending** by month, category and merchant; tap any transaction to recategorize it or mark it one-time
- **Subscriptions and bills**, upcoming due dates and paydays
- **Alerts** for running over budget, money leaving savings, big charges, due dates and bank connections that need attention (optional phone push)
- **Investments**: holdings and mix
- **Calculators**: net worth projector, home affordability, "cut a habit", compound growth

It installs on your phone like an app (Add to Home Screen). Your data lives only on your own server. Nobody else, including whoever shared this with you, can see it.

Bank connections use [Plaid](https://plaid.com). You log in on your bank's own screen; this app never sees your bank password, and it's read-only.

---

## Set it up (about 20 minutes)

You need three accounts: **GitHub** (free), **Railway** (hosting, about $5/month after the trial), and **Plaid** (bank connections; the free Trial covers up to 10 banks).

There are two ways to do it. Pick one.

### Option A: let Claude Code do it

If you have [Claude Code](https://claude.com/claude-code), open a terminal in an empty folder, start `claude`, and paste this:

> Set up Waypoint for me: https://github.com/matthewlieb/waypoint. Fork it to my GitHub, clone it, then follow the CLAUDE.md in the repo step by step to deploy it on Railway with my own Plaid keys. I'm not a developer, so explain each step, tell me exactly what to click when I need to do something in a browser, and never ask me to paste passwords or keys into the chat. I'll set those myself.

Claude handles the commands. You'll still do a few things yourself: sign in to GitHub, Railway and Plaid; create your Plaid account; and type your password and Plaid keys into Railway. Claude is told never to see those.

### Option B: do it by hand

Follow steps 1–4 below. No coding needed, just clicking and copying.

### 1. Copy the code
Sign in to GitHub, open this repository and click **Fork**. That makes your own copy.

### 2. Get Plaid keys
1. Sign up at **dashboard.plaid.com** and choose the free **Trial** plan.
2. Fill in the company profile. Use your own name; the use case is "personal finance app for myself".
3. Go to **Developers → Keys**. Keep this tab open: you need the **client_id** and the **Production** secret (or the Sandbox secret if you want to try it with fake data first).

### 3. Deploy on Railway
1. Sign in at **railway.com** with GitHub → **New Project** → **Deploy from GitHub repo** → pick your fork.
2. Open the service → **Settings → Networking → Generate Domain**. Copy the address, for example `https://waypoint-production-1234.up.railway.app`.
3. Right-click the service → **Attach volume**, mount path **`/data`**. This is where your data is stored; without it, a redeploy erases everything.
4. Open **Variables** and add:

| Variable | Value |
|---|---|
| `DASHBOARD_PASSWORD` | A long password you'll use to sign in |
| `SESSION_SECRET` | Any long random text (40+ characters). It also encrypts your bank tokens, so don't change it later |
| `PLAID_CLIENT_ID` | From Plaid → Developers → Keys |
| `PLAID_SECRET` | The **Production** secret (or Sandbox to test) |
| `PLAID_ENV` | `production` (or `sandbox` to test with fake banks) |
| `PUBLIC_URL` | Your Railway address from step 2 |
| `PLAID_REDIRECT_URI` | Your Railway address + `/connect` |
| `TZ_NAME` | Optional. Your time zone, e.g. `America/New_York` |
| `APP_NAME` | Optional. Rename the app |

5. Back in Plaid: **Developers → API → Allowed redirect URIs** → add exactly the same `PLAID_REDIRECT_URI` value. Chase, Wells Fargo, Capital One and other banks with their own login page need this.

### 4. Use it
Open your Railway address and sign in. The app walks you through it:
1. **Add your bank accounts**: checking, savings, credit cards, loans, investments. Add each bank once.
2. **Set your goals**: savings accounts, emergency fund, which debts to pay off, savings goals and budget. Everything is pre-filled from your data; change what you like.
3. On your phone, open the address in Safari → **Share → Add to Home Screen**. In Chrome on Android, use **⋮ → Add to Home screen**.

Data refreshes automatically when your bank has new transactions, plus every morning at 7. The **Sync** button forces a refresh.

### Optional: phone alerts
Install the free **ntfy** app, subscribe to a made-up topic name nobody could guess (e.g. `waypoint-7f3k29xq`), and set `NTFY_TOPIC` to that name on Railway.

---

## Good to know

- **"Already connected" message**: each bank only needs to be added once, and adding one twice costs you a second slot. To fix a broken login, use **Reconnect** on the Connect page.
- **Reconnect warnings**: banks sometimes require a fresh login (password changes, every ~12 months at some banks). The app alerts you, and **Reconnect** fixes it without losing history.
- **Fidelity and some brokerages** may not show up right away on a new Plaid account. Plaid grants access to them separately.
- **Subscriptions**: if your Plaid team has the Recurring Transactions add-on, the app uses Plaid's detection. Otherwise it spots repeating charges itself.
- **Not financial advice**: projections are estimates.
- **Disconnecting**: Connect page → **Disconnect** removes the bank from Plaid and deletes its data from your server.

## Trying it without a bank (developers)

```
pip install -r requirements.txt
python3 tests/make_fixture.py ./data && FIN_DATA=./data python3 build.py
open data/dashboard.html
```

To run the server locally against Plaid Sandbox, put Sandbox keys in `.env` (see `.env.example`); the Plaid CLI does it safely with `plaid keys write --env sandbox --gitignore`. Then run `FIN_DATA=./data python3 server.py` and log in to any test bank with `user_good` / `pass_good`.

## How it works

`server.py` (stdlib HTTP server) → `plaid_sync.py` (Link tokens, token exchange, `/transactions/sync`, holdings, liabilities, verified webhooks; bank tokens encrypted at rest) → `build.py` (categorizes with Plaid's personal finance categories, matches transfers between your own accounts, detects income, builds budget, goals and alerts) → `template.html` (the dashboard, Chart.js). Data lives in `/data` on your volume.
