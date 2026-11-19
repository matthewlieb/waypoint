# PLAID_PLAN

## Goal

A shareable, self-hosted copy of my personal finance dashboard (net worth, spending, budget, goals, alerts) that friends can deploy for themselves with their own Plaid free-trial keys, with none of my personal data or goals. (workflow_id: wf_d17b18f644656bcf)

## Scope

- Products (confirmed with the developer, 2026-11-16):
  - **transactions** in `products`: spending, budget, cash flow, income detection, plus daily balances via /accounts/get (SELECT-001, SELECT-005).
  - **investments** in `additional_consented_products`: holdings and mix; called only on Items with investment accounts (INV-015, LIAB-007).
  - **liabilities** in `additional_consented_products`: card and loan APRs, minimum payments and due dates; called only on Items with credit/loan accounts (LIAB-007, LIAB-008).
  - Recurring Transactions add-on: used when the team has it, with an in-app fallback when it doesn't (TXN-016, GUIDE-016).
- Rejected: Balance product (no real-time need, billed per call; BAL-001); investments/liabilities in `products` or `optional_products` (cuts coverage and bills at creation; CONV-002, PITFALL-001); auth/identity (no money movement or verification; SELECT-007).
- Geography: US only. No money movement, no verification, not a lending decision, no account numbers.
- Platform: Python stdlib server + vanilla JS PWA, Plaid Link web SDK from the CDN (WEB-010), deployed on Railway. One deployment per person, each with its own Plaid keys.
- Environment: sandbox until acceptance passes; each friend switches to production with their own keys.

## Human tasks

| Task | Why it matters | Where | State |
|------|----------------|-------|-------|
| Install and sign in to the Plaid CLI (`brew install plaid/plaid-cli/plaid`, `plaid login`), then let the agent write Sandbox keys with `plaid keys write --env sandbox --gitignore` | End-to-end sandbox test (CHECK-008) needs keys the agent never sees | terminal | needs_human |
| Rotate the developer's Plaid production secret and update it wherever it is deployed | Part of the secret and the client_id were displayed during the build conversation (CHECK-003) | https://dashboard.plaid.com/developers/keys | needs_human |
| Each friend: create a Plaid account (Trial), complete the company profile | Production keys; OAuth banks like Chase are gated on the profile (TASK-006, TASK-009) | https://dashboard.plaid.com | not_applicable here (documented in README) |
| Each friend: allow-list `<their app>/connect` as an OAuth redirect URI | OAuth banks fail or convert poorly without it (OAUTH-001, CHECK-007) | https://dashboard.plaid.com/developers/api | not_applicable here (documented in README; the app warns when PLAID_REDIRECT_URI is unset) |
| Recurring Transactions entitlement (optional) | Better subscription detection; the app falls back without it | Dashboard → Settings → Team → Products | not_applicable (optional add-on with fallback) |
| Fidelity access (some brokerages) | May not be available right away on new teams (INV-014) | Dashboard OAuth institutions page | not_applicable here (documented in README) |

## Implementation checklist

- [x] `/link/token/create` server-side: transactions + additional_consented investments/liabilities, days_requested 730, redirect_uri and webhook on every call, update mode with access_token and empty products (GUIDE-010, OAUTH-002/005, ITEM-003, TXN-009, WEBHOOK-002)
- [x] Link pre-initialized on page load, button disabled until ready, opened on tap; destroy() before re-create; OAuth return resumes with the persisted link_token + receivedRedirectUri (WEB-001/004/011, OAUTH-003/004)
- [x] Duplicate-institution check before exchange (ITEM-001/011)
- [x] `/item/public_token/exchange` server-side; access_token encrypted at rest (Fernet, key derived from SESSION_SECRET); item_id, institution_id, accounts and link_session_id stored (GUIDE-002/015, DATA-001, WEB-003)
- [x] onExit/onEvent forwarded to the server (HANDOFF, EXIT, ERROR, OPEN_OAUTH, FAIL_OAUTH) with link_session_id (CONV-014/015)
- [x] `/transactions/sync` loop with count 500, all pages buffered, cursor persisted only after has_more=false, restart from the starting cursor on mutation during pagination, added/modified/removed applied, PFC v2 pinned (TXN-001/002/004/006/007/011)
- [x] History import state from transactions_update_status and the historical_update_complete webhook flag, shown in the UI (TXN-008)
- [x] /investments/holdings/get joined to securities locally; /liabilities/get with per-field nulls and APR selected by apr_type (INV-001/010, LIAB-002/003)
- [x] /transactions/recurring/get only after the history is complete, with a fallback when not entitled (TXN-016/017)
- [x] Webhook receiver: ES256-only Plaid-Verification JWT via PyJWT, key fetched by kid and cached, iat ≤ 5 min, constant-time SHA-256 of the raw body; responds fast and queues a coalesced sync; 5xx on internal error (WEBHOOK-001/003/004/005/007)
- [x] ITEM webhooks: ERROR, PENDING_DISCONNECT/EXPIRATION, LOGIN_REPAIRED, USER_PERMISSION_REVOKED, NEW_ACCOUNTS_AVAILABLE drive reconnect prompts and alerts (WEBHOOK-006)
- [x] Errors branched on error_code; retries with jittered backoff for transient and rate-limit errors; request_id logged, tokens never logged (ITEM-002/004/013, GUIDE-006)
- [x] Disconnect calls /item/remove and purges local data (ITEM-005)
- [x] Daily 7am reconcile sync as a fallback for missed webhooks (WEBHOOK-007)
- [ ] End-to-end sandbox run (blocked on Sandbox keys: see Human tasks)

## Acceptance

- Round 0: not yet submitted. Offline fixture build and the local server run were verified; the sandbox end-to-end run is pending keys.

## Maintenance

This integration is maintained with the Plaid MCP; this file is its durable state. Agents working in this repository: consult build_guidance before modifying any Plaid-touching code, and re-run build_check_acceptance before reporting such a change as working. The acceptance above applies only to the code as it was verified — later changes invalidate it.

## How to test

### Offline demo (no keys)
1. `python3 tests/make_fixture.py /tmp/wp && FIN_DATA=/tmp/wp python3 build.py`
2. Open `/tmp/wp/dashboard.html`: a fictional person with checking, savings, a card, a student loan, a Roth IRA and a brokerage.

### Sandbox end to end
1. `plaid keys write --env sandbox --gitignore` in this folder (writes `.env`).
2. Add `DASHBOARD_PASSWORD` and `SESSION_SECRET` to `.env`; run `FIN_DATA=./data python3 server.py`; open http://localhost:8080 and sign in.
3. Click **Add a bank account instantly**, choose First Platypus Bank, log in with `user_good` / `pass_good`.
4. Expected: the bank is listed with "importing history…", the dashboard fills with transactions within a minute or two, and Settings shows the detected income.
5. Second persona: add a bank with `user_transactions_dynamic` (any password); then Disconnect it and confirm its data disappears.

## Decisions made

- One deployment per person with their own Plaid keys (no shared multi-user server), so nobody else holds a friend's bank data. Developer's request, 2026-11-16.
- Route paths: `/connect`, `/settings`, `/api/plaid/{link_token,exchange,event,remove,webhook}`, `/api/settings`, `/api/sync`, `/api/override`. These carry over from the developer's existing app.
- Webhook verification uses PyJWT + cryptography, the app's only third-party dependencies (WEBHOOK-003 requires a vetted JWT library).

## Open questions

- Publish as a public GitHub repo so friends can fork it, or share another way? (awaiting the developer)
