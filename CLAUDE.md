# Waypoint: setup runbook for Claude Code

This file tells Claude Code how to set Waypoint up for the person running it. If they ask you to "set this up", "deploy this" or similar, follow these steps in order. Explain each step in plain language; assume they are not a developer.

## Ground rules (do not skip)

- **Never ask the person to paste a password, Plaid secret or client_id into the chat**, and never print, echo or `cat` them. They type secrets into Railway themselves (dashboard or their own terminal). The one exception is `SESSION_SECRET`, which you generate and set in a single command without displaying it (step 5).
- **Never type a password or log into a site for them.** Logins (GitHub, Railway, Plaid, banks) are theirs. Bank logins happen only inside the Plaid window in the app.
- Interactive logins (`gh auth login`, `railway login`) need a real terminal. Ask them to run these by typing `! <command>` in Claude Code, or in their own terminal.
- Confirm before anything that costs money or is hard to undo (creating the Railway project, deleting anything).

## Steps

### 1. Check tools
Check for `git`, `gh` and `railway` (`railway --version`, 4.x expected). On a Mac, missing ones install with `brew install git gh railway`. Without Homebrew, point them to https://brew.sh or https://docs.railway.com/guides/cli.

### 2. Get their own copy of the code
- `gh auth status`; if logged out, ask them to run `! gh auth login`.
- If this folder is not already their fork: `gh repo fork matthewlieb/waypoint --clone --remote` and work inside the clone. A fork lets them pull future improvements.

### 3. Plaid account (they do this in the browser)
Walk them through it:
1. Sign up at https://dashboard.plaid.com and pick the free **Trial** plan (up to 10 banks).
2. Complete the company profile: their own name, use case "personal finance app for myself". Banks like Chase stay unavailable until this is done.
3. Leave the **Developers → Keys** page open for step 5.

### 4. Create the Railway service
Ask them to run `! railway login` if `railway whoami` fails. Then, after confirming (Railway costs about $5/month after its trial):
```
railway init --name waypoint
railway add --service waypoint
railway service waypoint
railway volume add --mount-path /data
railway domain
```
Note the domain `railway domain` prints, e.g. `https://waypoint-production-1234.up.railway.app`. Call it `$URL` below.

The volume at `/data` is required: without it every redeploy erases their bank connections and data.

### 5. Variables
You set the non-secret ones, plus `SESSION_SECRET` without displaying it:
```
railway variables --skip-deploys \
  --set "PLAID_ENV=production" \
  --set "PUBLIC_URL=$URL" \
  --set "PLAID_REDIRECT_URI=$URL/connect" \
  --set "SESSION_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
```
Optionally `TZ_NAME` (e.g. `America/Chicago`); ask which time zone they're in.

Then ask them to set the three secrets **themselves**, either in the Railway dashboard (project → waypoint service → Variables) or by typing in their own terminal from this folder:
```
railway variables --set "DASHBOARD_PASSWORD=<a long password they choose>"
railway variables --set "PLAID_CLIENT_ID=<from Plaid → Developers → Keys>"
railway variables --set "PLAID_SECRET=<the Production secret>"
```
Confirm they're set with `railway variables --kv | cut -d= -f1` (names only, never values).

`SESSION_SECRET` also encrypts their bank tokens. Tell them never to change it, or every bank must be reconnected.

To try fake banks first, use `PLAID_ENV=sandbox` with the Sandbox secret; test logins are `user_good` / `pass_good`. Switch both back to production afterwards.

### 6. Plaid redirect URI (they do this)
In Plaid: **Developers → API → Allowed redirect URIs**, add exactly `$URL/connect`. Chase, Wells Fargo, Capital One and other banks with their own login page need it.

### 7. Deploy and check
```
railway up --detach
```
Wait for the build (`railway logs --build`, then `railway logs`). Check `curl -s $URL/healthz` returns OK. If it fails, read the logs and fix; common causes are a missing variable or no volume.

### 8. Hand off
Tell them to open `$URL`, sign in with their password, and follow the app: **Connect** (add each bank once) → **Settings** (goals, budget, savings accounts, debts, all pre-filled) → dashboard. On a phone: Safari → Share → Add to Home Screen (Android Chrome: ⋮ → Add to Home screen).

Optional phone alerts: install the **ntfy** app, subscribe to a hard-to-guess topic name they make up, and have them set `NTFY_TOPIC` to it in Railway themselves.

## Updating later
`git pull upstream main` (or `gh repo sync`), then `railway up --detach`. The volume keeps their data.

## Working on the code
- `server.py` (stdlib HTTP server, pages, API, scheduler) → `plaid_sync.py` (Plaid calls, encrypted tokens, `/transactions/sync`, verified webhooks) → `build.py` (categories, transfers, budget, goals, alerts) → `template.html` (dashboard).
- Offline demo with no keys: `python3 tests/make_fixture.py ./data && FIN_DATA=./data python3 build.py`, then open `data/dashboard.html`.
- `PLAID_PLAN.md` records the Plaid integration decisions. If the Plaid MCP is available, consult its build guidance before changing Plaid code.
