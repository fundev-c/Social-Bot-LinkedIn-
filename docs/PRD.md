# PRD / Change Record — Social Bot hardening

**Living document.** Every change made to this repo in this workstream is
recorded here with the reasoning behind it, how it was verified, and anything
that still needs a human decision. Append to it; don't rewrite history.

- **Owner:** backend
- **Started:** 2026-08-10
- **Last updated:** 2026-08-12
- **Baseline:** commit `10f9fcd` (merge of product-roadmap branches)
- **State:** changes 1–4d and 7 **done, verified and committed** on
  `hardening/campaign-auth-and-migrations`. The system is now autonomous but
  shipped switched off (§5). Change 5 (live transport validation) is deferred
  until a throwaway account exists and is the next action; change 8 (§5.4) is an
  open question that should be answered before a non-UTC account runs
  autonomously.

> ### ✅ Committed as of 2026-08-12
> The three-commit split described in §3.2 was made on branch
> `hardening/campaign-auth-and-migrations`. §3.2's "commit first" instruction is
> kept below as the record of what was done, not as an outstanding action.

---

## 0. Why this workstream exists

The product is roughly 85% built: the outreach loop, warm-up programme, rate
limiting, targeting and LLM integration are all real and tested. What stood
between it and a working system was a small number of load-bearing gaps, not
missing features.

This workstream closes the ones that do not require a live LinkedIn account, so
that the one that *does* (validating the mobile transport) can be attempted
against a codebase that is safe to deploy and a test suite that can be trusted.

**Ordering principle:** anything that makes a later mistake unrecoverable comes
first. Migrations before schema changes; a trustworthy test suite before
delicate live work.

---

## 1. Status summary

| # | Change | Status | Verified by |
|---|---|---|---|
| 1 | Campaign routes authenticated + org-scoped | **Done** | 13 new tests, incl. cross-tenant |
| 2 | Alembic migrations + safe adoption of the live DB | **Done** | 4 tests + 3 paths run manually |
| 3 | Flaky warm-up runner tests fixed | **Done** | 10/10 file runs, 8/8 full-suite runs |
| 4 | `status` parameter shadowing bug (found in passing) | **Done** | covered by the 404 assertions |
| 4b | Transport fallback no longer hides the primary error | **Done** | 1 new test + live probe |
| 4c | One-command account validator (`scripts/validate_account.py`) | **Done** | 19 tests + 5 end-to-end checks |
| 4d | `0002` aborts instead of deleting orphan campaigns | **Done** | 2 tests, incl. the recovery path |
| 7 | Scheduler to drive warm-up / sync / send | **Done, off by default** | 30 tests + 2 refusal paths + a live dry run |
| 5 | Validate mobile transport on a real account | **Deferred** | account to be obtained later |
| 6 | Bind the Playwright fallback executor | **Not started** | blocked behind 5 by design |
| 8 | Planner ignores the account timezone (found in passing) | **Open — needs your call** | see §5.4 |

Test suite: **223 passing.**
Baseline before this work: 161 passing + 11 unreliable (~50% failure rate).
Stability was measured at the time of change 3 — the previously flaky file passed
10/10 runs and the whole suite 8/8. Nothing since has reintroduced clock or seed
dependence.

### Decisions

**Resolved — the destructive branch in migration `0002`, 2026-08-12: it aborts.**
The user chose abort over delete. `0002` no longer deletes anything under any
circumstances; with orphans and no organization to own them it fails the deploy
and hands the operator the row count plus two ways forward. See §2.7.

**Resolved — the scheduler's shape, 2026-08-12.** Separate worker process with an
in-process flag for local dev; refuses to start without Redis; off by default with
a dry-run mode. See §5.1.

**Open — the planner ignores each account's configured timezone.** Found while
building the scheduler. Every account's active window is 08:00–19:00 **UTC**
regardless of `timezone`, so an account set to `America/Los_Angeles` is planned to
act 00:00–11:00 local — overnight, every day, which is itself a detection
signature. The fix is one argument, but it changes when every non-UTC account
acts, so it is a behaviour change rather than a scheduler bug and is not bundled
in. Full reasoning in §5.4. **Should be decided before the first non-UTC account
runs autonomously.**

---

## 2. Changes

### 2.1 Campaign routes: authentication and tenant isolation

**Severity:** security. **Status:** done.

**The defect.** Every other route in the system resolved a tenancy context and
filtered by `org_id`. The campaign routes did neither — they had no auth
dependency at all, and the `campaigns` table had no owner column, so there was
nothing to scope them by even in principle. Any unauthenticated caller could
list, read, modify, start, pause and delete every organization's campaigns.

**What changed.**

| File | Change |
|---|---|
| `src/campaigns/models.py` | `Campaign.org_id` (required, FK, indexed) and `created_by_user_id` (nullable, `SET NULL`) |
| `src/campaigns/repository.py` | `CampaignRepository` / `IdempotencyRepository` take a required `org_id`; all reads go through one scoped base query |
| `src/campaigns/service.py` | `CampaignService` requires `org_id`; `update_task_status` extracted to module-level `apply_task_result` |
| `src/api/routes/campaigns.py` | `get_campaign_service` depends on `get_request_context`; every route inherits auth |
| `src/campaigns/__init__.py`, `src/infrastructure/task_bridge.py` | export + docstring follow-through |

**Decisions worth keeping.**

- **No unscoped mode, no `org_id=None` sentinel.** A repository that can be
  constructed without a tenant is the defect itself; an optional parameter would
  leave the same hole one forgotten argument away.
- **404, not 403, across tenants.** A 403 confirms the id exists, which is a
  disclosure to a caller with no right to know. Cross-org rows read as absent.
- **Idempotency keys namespaced per org** (`{org_id}:{key}`). Keys are chosen by
  the client, so collisions between tenants are expected, not exotic. In a
  shared namespace, replaying another org's key returns *their* campaign as the
  cached response — the same leak by a quieter route. Namespacing the stored key
  avoided a composite-primary-key migration.
- **The orchestrator callback has no request context**, so it does not use the
  service. `apply_task_result` derives the tenant from the task's own campaign,
  which keeps `org_id` mandatory everywhere else.

**Tests.** `tests/test_campaigns_api.py` was rewritten. The previous version
mocked the service layer out entirely, which meant it exercised none of the
wiring the defect lived in — it would have passed identically with or without
the bug. It now runs against the real app, real Clerk-shaped auth and a real
in-memory database, and covers: anonymous refusal on every route; garbage
tokens; cross-org invisibility on all six routes; list scoping; teammates in one
org sharing visibility; and idempotency keys not crossing orgs.

Shared fixtures (`clerk_keypair`, `issue_token`, `api_client`) were added to
`tests/conftest.py` so any future authenticated-route test can reuse them.

### 2.2 Database migrations (Alembic)

**Severity:** operational risk. **Status:** done.

**The problem.** Schema was created by `AUTO_CREATE_TABLES` (`create_all`) on
boot. That is fine until the first change to a table holding real data — at
which point there is no safe path forward. Change 2.1 was exactly that change.

**What changed.** `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`,
two revisions, `scripts/migrate.py`, plus `Dockerfile` / `railway.json` /
`.env.example` wiring.

**The two-revision split, and why it must not be squashed.**

- `0001_baseline` — the schema exactly as `create_all` was producing it,
  deliberately *without* the new campaign columns.
- `0002_campaign_org_scope` — adds them, with a backfill.

The deployed database has every table but no `alembic_version` row. A plain
`alembic upgrade head` would try to re-create existing tables and fail on every
boot until somebody ran a manual `stamp`. `scripts/migrate.py` detects the three
possible states and handles them:

| State | Action |
|---|---|
| `alembic_version` exists | upgrade |
| No `alembic_version`, but `campaigns` exists | stamp `0001`, then upgrade |
| Empty database | upgrade from scratch |

Squashing the revisions removes the stamp target and breaks adoption.

**Other decisions.**

- `env.py` takes the URL from `src.database.session`, not `alembic.ini` —
  duplicating URL resolution is how you migrate a database the app never opens.
- Models load via `import_all_models()`, the same registry `create_all` uses; a
  module nobody imports is invisible to autogenerate and gets silently omitted.
- `AUTO_CREATE_TABLES` is now `false` in the image and documented as local-only.
  Running both would let `create_all` invent tables no migration describes.
- The start command is `migrate && uvicorn`. The `&&` is load-bearing: a schema
  that cannot reach head must fail the healthcheck, not serve traffic.

**Backfill.** `0002` must give every existing campaign an owner, and that
information was never recorded. It assigns orphans to the **oldest organization**
(in this deployment, the only one, so it is exact rather than a guess). If **no**
organization exists at all it aborts — see §2.7, which supersedes the destructive
branch this section originally described.

**Verification.** `tests/test_migrations.py` (4 tests): migrations produce a
schema identical to the models (drift guard — a model change without a migration
now fails CI); `head → base → head` round-trips; and both backfill branches.
All three adoption paths were also run by hand, including simulating the live
database with a campaign in it — the campaign survived and was adopted.

### 2.3 Flaky warm-up runner tests

**Severity:** blocks trustworthy CI. **Status:** done.

**The problem.** `tests/test_warmup_runner.py` failed roughly half the time —
confirmed on unmodified code with all other changes stashed, so it was
pre-existing, not introduced here.

**Root cause — two independent sources, both correct behaviour:**

1. The daily plan is seeded by `(account_id, day)`. Each test creates a fresh
   random account UUID and used the real date. The `observe` stage plans likes
   with `probability=0.8`, so **one day in five is a deliberately quiet day**
   with zero likes. A test asserting "it liked something" was asserting a coin
   flip.
2. The active window is 08:00–19:00, but tests asked at `now=18:00`. Anything
   the planner scattered into the final hour was not yet due.

Neither is a product bug — quiet days and pacing are the design.

**Fix.** Pinning a lucky seed would work until the next volume-band change
silently un-pinned it. Instead `day_that_plans()` searches forward from a fixed
date for a day on which *this* account really is scheduled to perform the action
under test, using the same planner the runner uses; `end_of_day()` puts the
clock past every scheduled time. Deterministic, and self-correcting if the bands
are retuned. A failed search raises a message naming the account, action and
stage rather than an opaque assertion.

**Two tests were also strengthened while in there** — they had been passing
vacuously. `test_the_same_post_is_never_engaged_with_twice` now asserts that
something *was* liked before checking for repeats (an empty day trivially has no
duplicates), and `test_only_actions_that_are_due_are_performed` now runs on a
day with actions planned, so it proves pacing rather than passing because there
was nothing to do.

**Verification.** 10/10 runs of the file; 8/8 full-suite runs. Other tests using
the real clock (`test_sequences`, `test_warmup`, `test_outreach_flow`) were
inspected and derive their values relatively, so they are time-independent.

### 2.4 `status` parameter shadowing (found in passing)

**Severity:** latent 500. **Status:** done.

In `src/api/routes/campaigns.py`, the `status` query parameter shadowed the
imported `fastapi.status` module inside `list_campaigns` and
`get_campaign_tasks`. Any `status.HTTP_404_NOT_FOUND` reference in those
handlers raised `AttributeError`, so "campaign not found" returned a 500 instead
of a 404. It had always been broken and nothing exercised it; the new
cross-tenant 404 assertions surfaced it immediately.

Both parameters are renamed `status_filter` and exposed unchanged as `?status=`
via `alias`, so the API contract is identical.

### 2.5 Transport fallback hid the error that matters

**Severity:** blocks change 5 (live validation). **Status:** done.
**Found by:** running preflight locally against a deliberately invalid session
while preparing the task-5 runbook.

**The defect.** In `CompositeTransport._dispatch`, when the primary (mobile)
*and* the fallback (Playwright) both failed, the returned error was
`str(exc2)` — the fallback's — and the primary's was discarded.

The Playwright executor is not bound yet, so its error is always the same
sentence: `no playwright executor bound for <action>`. That means **every**
Voyager failure was being reported as a Playwright wiring problem, with the
actual LinkedIn response thrown away.

This made the documented procedure for change 5 impossible: BACKEND_HANDOFF §4
says to "watch `suggestion.error` for the failure from every shape tried", but
no Voyager error could reach that field.

**Fix.** Both errors are now reported — joined in `error`, and separated in
`detail` as `primary_error` / `primary_via` / `fallback_error` / `fallback_via`.

**Before:**
```
no playwright executor bound for whoami
```
**After (same invalid session):**
```
mobile: voyager request failed: Failed to perform, curl: (47) Maximum (30)
redirects followed || playwright: no playwright executor bound for whoami
```

The second one is a diagnosis: LinkedIn is bouncing the request to the login
page, i.e. the cookie is invalid or expired.

**Noted, not fixed:** the mobile session follows redirects, so an invalid cookie
surfaces as a redirect loop rather than a clean 401. Disabling redirect-following
would give a cleaner signal, but it changes live transport behaviour that has
never been exercised against a real account — the wrong thing to alter on the eve
of validating it. Revisit once change 5 has a known-good baseline.

### 2.6 One-command account validator

**Severity:** enabler for change 5. **Status:** done.

**Context.** The runbook for validating a live account was a sequence of curl
calls, a hand-minted dev JWT and JSON piping — fine once, bad as an inner loop,
and payload iteration *is* an inner loop.

The user asked whether Claude in Chrome could drive it. It cannot, and the
reasons are worth keeping:

1. Its MCP tools are not connected in this environment.
2. `li_at` is **HttpOnly** — page JavaScript cannot read it by design, which is
   why the docs point at the DevTools Application panel.
3. Decisively: **driving a browser would validate nothing.** Change 5 exists to
   prove `src/infrastructure/transports/mobile.py` works. A browser exercises
   none of that code, so success there would be a false positive about the exact
   thing under test.

Cookie capture therefore stays manual (agreed with the user). A Playwright
capture helper was considered and rejected as unnecessary: it would add a ~150MB
browser download, and logging in through an automated browser can itself trigger
a LinkedIn checkpoint.

**`scripts/validate_account.py`** — connect + preflight in one command.

| Invocation | Purpose |
|---|---|
| `validate_account.py` | connect a new account, then preflight |
| `--account-id <id>` | preflight again — **the iteration loop**, creates no duplicate account |
| `--account-id <id> --rotate` | replace expired cookies, then preflight |

Design decisions:

- **Drives the HTTP API, not the Python internals.** It exercises the same path
  the product does — auth, Fernet round-trip, transport selection — so it cannot
  pass while the real flow is broken, and `--api-base` retargets it at Railway.
- **Normalises `JSESSIONID`.** DevTools shows it quoted; the transport re-quotes
  it at `src/infrastructure/transports/mobile.py:153`, so a pasted quoted value
  becomes double-quoted, the CSRF header stops matching, and `whoami` *still
  passes* while every write fails. Silent, expensive, and now impossible.
- **Fails early and specifically** via `/healthz` — missing `ENCRYPTION_KEY`,
  unreachable API, database not ready — instead of surfacing as a confusing 500
  during connect. Warns loudly if the insecure dev encryption key is in use,
  because a real session cookie would then sit under a publicly known key.
- **Credentials never appear anywhere.** Read from env or a no-echo prompt, never
  argv (shell history), masked to a 4-char prefix in all output including errors.
  No password handling: the legacy `account_manager_agent._authenticate_with_playwright`
  password path is explicitly not reused — it contradicts
  `src/accounts/schemas.py:14-20`.
- Exit code 0/1 so it can drive a loop.

**Verification.** 19 unit tests on the pure helpers. End-to-end against a running
server with a deliberately invalid cookie: the real Voyager error surfaces
(`mobile: voyager request failed ... redirects followed`, not the Playwright
placeholder — see 2.5), the quoted `JSESSIONID` is stored stripped (17 chars in,
15 stored), `--account-id` and `--rotate` leave the account count at 1, the
server-down path gives a start command rather than a traceback, and a grep of
stdout/stderr confirms no cookie value or unmasked prefix ever appears.

Suite: **192 passing**.

### 2.7 Migration `0002` aborts instead of deleting

**Severity:** data safety. **Status:** done. **Decided by:** the user, 2026-08-12.

The open decision from §2.2 is closed: **abort, never delete.** The reasoning
behind the delete was sound (no organizations ⇒ no users ⇒ nobody can own or
reach those rows) but it rested on an assumption about production that only the
owner can confirm, and the asymmetry is decisive — an aborted deploy is a
five-minute conversation, a wrong delete is unrecoverable.

`0002` now raises with the orphan count and two documented ways forward: create
the organization that should own them and re-deploy (the oldest org adopts them
automatically), or delete the rows deliberately by hand and re-deploy. It also
prints the `SELECT` to inspect them first.

**A bug in this change, found by its own test.** The abort was first placed where
the delete had been — *after* the two `add_column` calls. On SQLite there is no
transactional DDL (Alembic logs `Will assume non-transactional DDL`), so those
columns survived the rollback and the re-run died on `duplicate column name:
org_id`. The abort message tells the operator to fix the data and re-deploy, and
that instruction would have failed on the second step.

Postgres would have rolled it back cleanly, so production was never at risk — but
local development runs on SQLite (`USE_SQLITE=true`, and the change-5 runbook uses
it), so this would have been found the hard way by whoever tried the recovery
path first.

**Fix:** the orphan check moved *above* all DDL, so an abort touches nothing on
any backend. At revision `0001` the `org_id` column does not exist yet, which
makes `SELECT COUNT(*) FROM campaigns` exactly the orphan count — the pre-check
is simpler than the post-check it replaced, not more complex.

**Tests.** The delete assertion became `test_backfill_aborts_when_no_org_can_own_
the_orphans`, which now asserts the row survives, `alembic_version` still reads
`0001`, **and** that neither column was added — the last of these is what caught
the bug. A new `test_upgrade_succeeds_once_an_org_exists_to_adopt_them` walks the
documented recovery path end to end (abort → create org → re-run → adopted),
because an instruction given to an operator in a failure message is worth proving.

Suite: **193 passing**.

---

## 3. Picking this back up

### 3.1 Environment

No project dependencies were installed on this machine when the work started, so
nothing could be verified. A virtual environment now exists at `.venv`
(Python 3.12, gitignored). It should still be there; recreate it if not:

```bash
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Confirm the starting point before changing anything — expect **192 passed**:

```bash
./.venv/Scripts/python.exe -m pytest -q
```

Run the app locally (this is also the setup change 5 needs):

```bash
# One-off: generate a key. Do not use the insecure dev fallback with real cookies.
./.venv/Scripts/python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

export USE_SQLITE=true CLERK_DEV_UNSAFE=true ENCRYPTION_KEY="<the key>"
./.venv/Scripts/python.exe scripts/migrate.py
./.venv/Scripts/python.exe -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000
```

`GET /healthz` should report `database: ok` and `credentials_encryption: ok`.
`redis: not configured` and `sending: disabled` are expected and fine — preflight
does not send, so change 5 does not need Redis. Sending (change 5, step 2) does.

`CLERK_DEV_UNSAFE=true` skips JWT signature verification. **Local only.**

### 3.2 First action on resuming

Commit. `HEAD` is still the baseline and every change below is working-tree only.
Suggested split, so the security fix is reviewable on its own:

1. `alembic/`, `alembic.ini`, `scripts/migrate.py`, `tests/test_migrations.py`,
   `Dockerfile`, `railway.json`, `.env.example`, `src/api/main.py` — migrations
2. campaign auth/org-scoping + its tests
3. flaky-test fix, transport fallback fix, `scripts/validate_account.py`, docs

Branch first — the repo's default branch is `main` and these are not trivial.

### 3.3 File manifest

**New (10):**
```
alembic.ini
alembic/env.py
alembic/script.py.mako
alembic/versions/0001_baseline.py
alembic/versions/0002_campaign_org_scope.py
scripts/migrate.py
scripts/validate_account.py
tests/test_migrations.py
tests/test_validate_account.py
docs/PRD.md                      (this file)
```

**Modified (17):** `src/campaigns/{models,repository,service,__init__}.py`,
`src/api/routes/campaigns.py`, `src/api/main.py`,
`src/infrastructure/{api_client,task_bridge}.py`,
`tests/{conftest,test_campaigns_api,test_transports,test_warmup_runner}.py`,
`Dockerfile`, `railway.json`, `.env.example`,
`docs/{BACKEND_HANDOFF,CONNECTING_AN_ACCOUNT}.md`.

**Not mine:** `HOW_IT_WORKS.md` was already untracked when this workstream began.

**Gitignored, safe to delete:** `.venv/`, `test_campaigns.db` (local scratch DB;
delete it to start from a clean schema).

---

## 4. Next up

### 4.1 Change 5 — validate the mobile transport on a real account

The highest-value remaining step, and the only one that cannot be done from a
keyboard alone. Everything needed to attempt it is now in place.

**You need:** a **throwaway** LinkedIn account (not yours, not the company's),
ideally aged a day or two with a photo and a plausible headline. The request
shapes are unproven, and finding that out is exactly what gets an account
restricted.

**Do:**
1. Bring up the local server (§3.1).
2. Get `li_at` and `JSESSIONID` from DevTools → Application → Cookies →
   `https://www.linkedin.com`. **Both from the same browser session** — mixing
   them yields a session that passes `whoami` and fails every write.
3. `CLERK_DEV_UNSAFE=true python scripts/validate_account.py`
4. Iterate: edit `src/infrastructure/transports/mobile.py`, re-run
   `--account-id <id>`. Use `--rotate` when the cookie expires mid-session.

**Reading the result.** If `whoami` passes, the hard part is proven — Voyager
rejects the request outright unless headers, cookies, TLS fingerprint and CSRF
are *all* correct at once. The other three probes are graded, not pass/fail:
`fetch_profile` failing costs copy quality, `fetch_activity` failing leaves
warm-up nothing to like, and **`fetch_inbox` failing is the one that matters** —
reply detection goes blind and sequences would talk over a real conversation.

**Known failure signature.** `curl: (47) Maximum (30) redirects followed` means
LinkedIn is bouncing to the login page: the cookie is wrong or expired. Anything
else is a payload shape worth reading closely.

**Then, and only then**, do one real write and confirm it in a browser with your
own eyes before trusting the success response.

### 4.2 After that

1. **Bind the Playwright fallback** (`get_transport(..., playwright_executor=None)`
   in `src/infrastructure/api_client.py`) — deliberately *after* 4.1, so it is
   bound against known-good behaviour rather than guesses.
2. ~~A scheduler.~~ **DONE, ahead of 4.1** — see §5. It needed no live account, so
   it was brought forward when change 5 was deferred. Ships disabled; enabling it
   is the natural first real exercise of the transport once 4.1 has passed.
3. WebSocket server so the approval queue stops polling.

**When you enable the scheduler for the first time**, the order that keeps it
boring:

1. `SCHEDULER_ENABLED=true SCHEDULER_DRY_RUN=true python -m src.scheduler` and
   read a sweep. It touches no LinkedIn endpoint and needs no Redis.
2. Decide §5.4 (the planner timezone) if the account is not UTC — otherwise its
   first autonomous day runs overnight in local time.
3. Turn off the dry run with a single account connected, and watch
   `/healthz` → `scheduler_last_tick`.

### 4.3 Smaller things noticed but not done

- No preflight button on the Accounts page — you connect on `/accounts`, then go
  to `/warmup`, select the account and press "Test connection".
- No credential-rotation UI, though `accountApi.rotateCredentials` exists
  (`frontend/src/lib/api.ts:176-182`) and the docs tell users to rotate there.
  `scripts/validate_account.py --rotate` covers it from the CLI meanwhile.
- `proxy_url` and `timezone` are supported by the API and typed in
  `frontend/src/types/index.ts`, but absent from the connect form. (`timezone` is
  worse than merely missing from the form — the planner ignores it entirely; see
  §5.4.)
- The mobile session follows redirects, so a bad cookie surfaces as a redirect
  loop rather than a clean 401 (see §2.5). Worth cleaning up *after* 4.1
  establishes a known-good baseline, not before.

---

## 5. Change 7 — the scheduler

**Severity:** the largest remaining gap. **Status:** done, off by default.

**The gap.** `warmup.runner.run_today`, `outreach.sync.sync_account` and
`outreach.execute.run_due` were each reachable from exactly one HTTP route and
nothing else. `main.py` never called them. So the programme was fully built and
fully tested but **not autonomous**: a warm-up day only happened if somebody poked
an endpoint.

Confirmed rather than assumed: Railway's start command is `migrate && uvicorn` —
the API process only. `requirements.txt` contains no scheduler of any kind.

**And a trap.** `docker-compose.yml` had a `scheduler-agent` service running
`main.py --agents scheduler`, which boots `SchedulerAgent` — a `SkeletonAgent`
whose `_start_agent_tasks` returns `[]`. A container named `linkedin-scheduler`
came up healthy and did nothing, while the class docstring described cadence and
timing in convincing detail. Anyone reading either would conclude scheduling was
handled. The service now runs the real scheduler and the docstring points at it.

### 5.1 Decisions (yours, 2026-08-12)

| Decision | Choice |
|---|---|
| Deployment shape | Separate worker process, with `SCHEDULER_IN_PROCESS` for local dev |
| No Redis | Refuse to start |
| Default on merge | Off, with a dry-run mode |

### 5.2 Why a plain loop, not Celery or APScheduler

The engine was already built to be ticked, and that removes most of what a
scheduling framework offers. `run_today` performs only actions whose planned time
has passed, subtracts work already done today, and honours the pause flag;
`run_due` filters on `scheduled_for` in SQL. So a **missed tick is not a missed
action** and a **duplicated tick is not duplicated activity**.

That makes misfire policy, cron expressions and a durable job store answers to
questions this system does not ask. A job store would additionally become a second
source of truth about what should run, drifting from the database every time an
account is paused or deleted — which then needs account-lifecycle events to
reconcile. Celery was rejected on top of that for fighting the async SQLAlchemy
stack and costing three processes to tick a handful of accounts.

External cron over HTTP was rejected for a more concrete reason: there is no
machine-auth concept. The routes need a Clerk JWT, `CLERK_DEV_UNSAFE` is local-only,
and the endpoints are per-account — so it would have needed a service-token path
plus a *publicly exposed* cross-org endpoint, which is more attack surface than the
in-process query it replaced.

### 5.3 What was built

```
src/scheduler/__init__.py   the reasoning, in one place
             config.py      env parsing + the two refusals
             accounts.py    the one deliberately cross-tenant reader
             lease.py       Redis lease: exactly one ticker
             tick.py        one sweep
             runner.py      the loop
             heartbeat.py   liveness + sync cadence
             __main__.py    python -m src.scheduler
```

**Stage order is load-bearing.** Sync, then warm-up, then send. Sync runs first
because it is what cancels a sequence when somebody has replied; sending first
would let a tick deliver a follow-up into a conversation that already had an
answer waiting. That is the worst thing this product can do — the prospect sees
it, and it is unmistakably robotic. One tick's delay noticing a reply is
acceptable; talking over it is not.

**Two refusals, both explicit.** Not enabled → exit 2 with instructions. Live
without Redis → exit 2, and `ALLOW_UNCAPPED_SENDING` is deliberately **not**
honoured: that override exists for a human pressing a button, and extending it to
an unattended loop is a different decision. An uncapped autonomous sender is
precisely how an account gets restricted. Refusals exit non-zero rather than
idling, because a process that is up but doing nothing is the exact state this
module exists to make unreachable.

**The lease.** The rate limiter makes one sweep safe, not two concurrent ones —
both can read the same usage window, conclude there is budget, and spend it. The
symptom is *more activity*, not an error, and it is easy to arrange by accident (a
second web replica with `SCHEDULER_IN_PROCESS`, a worker deployed beside one, an
overlapping deploy). `SET NX EX` with an owner token, compare-and-delete release
via Lua so a stalled ticker cannot delete its successor's claim, and the TTL is
the recovery story: a crashed ticker's claim expires and the next process takes
over with nothing to clean up.

**Liveness is reported separately from output**, and this is the subtle one. The
warm-up programme has *deliberately* quiet days — `observe` plans likes at
`probability=0.8`, so one day in five an account does nothing and the planner
records *"A deliberately quiet day — real accounts have them."* (This is what made
the tests in §2.3 flaky.) So "nothing happened" is normal, absence of activity can
never be the alarm, and a dead scheduler is indistinguishable from a quiet one
unless it asserts its own aliveness. Hence `scheduler` and `scheduler_last_tick`
on `/healthz`.

**Cadence.** Warm-up and send run every tick because both return on a DB-only path
when nothing is due (verified: `run_today` returns before `_live_account`, and
`run_due`'s empty result set never builds a transport). Sync gets its own slower
clock, because it always costs LinkedIn requests and can never return early. Its
cadence is a Redis key with a TTL — existence *means* "synced recently" — so there
is no timestamp arithmetic and losing the record costs one extra read-only sync.

**Pacing.** Accounts are spaced within a sweep and the interval is jittered. Every
account acting at `:00`, and ticks landing on exact multiples of five minutes
forever, are both patterns — and not being a pattern is the entire premise.

**Failure isolation** at two levels: one account with expired cookies must not
cost the other nineteen their day, and one failed stage must not cost that account
its other two. Errors are counted and reported, never raised, so a partial sweep
is still a tick that tells you what went wrong. Exception *type* names are
recorded because a transport failure, an expired cookie and a bug can all arrive
as a bare string.

**Tests (30).** Deliberately about judgement rather than loop mechanics: the two
refusals; `ALLOW_UNCAPPED_SENDING` being ignored; the lease excluding a second
holder and not deleting a successor's claim; sync-before-send ordering; isolation
at both levels; sync cadence suppressing a second sync; account spacing; jitter;
a dry run not reaching the engine at all; the loop surviving a failing tick; and
the heartbeat being recorded, including without Redis.

Verified beyond the suite by running it: both refusal paths print their guidance
and exit 2, and a dry run against SQLite with two seeded accounts swept **one** —
correctly excluding the `SUSPENDED` one — and previewed the real plan from the
real planner (`stage: observe`, five likes).

**A bug the suite could not have caught, found by running the API.** With
`SCHEDULER_IN_PROCESS=true` the scheduler produced **no observable output at
all**. Two causes stacking:

1. Uvicorn attaches handlers only to the `uvicorn.*` loggers, so the root logger
   has none and Python falls back to `logging.lastResort`, which emits WARNING and
   above. Every tick summary is INFO, so all of them were dropped — as was
   "scheduler starting".
2. The heartbeat was stored only in Redis, and the mode guaranteed not to have
   Redis is the dry run.

So the configuration most likely to be *watched* — a dry run inside the API, which
is how you inspect the scheduler before letting it act — was the one that reported
nothing. That is exactly the "up but doing nothing, invisibly" state this package
is written to prevent, reproduced by the package itself.

Fixed both ways: `ensure_logging_is_visible()` attaches a handler only when
nobody else has configured logging (so the worker is not double-logged), and the
tick is recorded in process memory as well as Redis, with `last_tick` preferring
Redis — which is still required when the API must report on a *separate* worker it
cannot see. Confirmed by booting the API and reading both the log and
`/healthz` → `scheduler_last_tick`.

Suite: **223 passing**, stable across 3 consecutive runs.

### 5.4 Found while building: the planner ignores the account's timezone

**Not fixed — it needs your call.** `plan_day` calls
`_scatter(count, start_hour, end_hour, day, rng)` without a `tz`, and `_scatter`
defaults to `tz: timezone = timezone.utc` (`src/warmup/planner.py:86`). So
`caps.timezone_of()` is stored on the account, returned by `describe()` and typed
in the frontend, but **the planner never reads it**: every account's active window
is 08:00–19:00 **UTC** regardless of configuration.

Until now this was invisible, because nothing ticked — the window was theoretical.
The scheduler makes it real.

The consequence is not subtle. An account configured `America/Los_Angeles` has its
activity planned for 08:00–19:00 UTC, which is 00:00–11:00 local: it does its
LinkedIn engagement overnight, every day, forever. That is *itself* a detection
signature — precisely what the warm-up programme exists to avoid.

The scheduler deliberately does **not** work around it. It has no active-window
logic of its own: it defers entirely to the planner, so the two cannot disagree.
Had it filtered on local hours it would have skipped exactly the accounts the
planner had scheduled work for, and most actions would never have fired.

**The fix is one argument** (`_scatter(..., tz=ZoneInfo(timezone_of(account) or
"UTC"))`), but it changes *when* every non-UTC account acts, which is a behaviour
change to the warm-up programme rather than a bug fix in the scheduler — so it is
recorded here rather than bundled in. It should be done before the first non-UTC
account runs autonomously.

---

## 6. Change log

| Date | Entry |
|---|---|
| 2026-08-10 | Document created. Sections 2.1, 2.2, 2.3, 2.4 completed and verified. |
| 2026-08-10 | 2.5 added: transport fallback was masking every Voyager error behind the unbound-Playwright message. Found while dry-running the change-5 runbook locally; fixed and tested. Suite now 173 passing. |
| 2026-08-10 | 2.6 added: `scripts/validate_account.py` replaces the curl runbook for change 5. Claude in Chrome evaluated and ruled out (reasons recorded in 2.6). Suite now 192 passing. |
| 2026-08-10 | **Workstream paused after 4c.** §3 rewritten as a resume guide (environment, commit plan, file manifest); §4 expanded into an executable runbook for change 5. Flagged that nothing is committed and that the §2.2 backfill decision is still outstanding. |
| 2026-08-12 | Resumed. State verified against the repo: `HEAD` still `10f9fcd`, 27 files uncommitted, 192 passing — the §1 table was accurate. |
| 2026-08-12 | 2.7 added: the §2.2 open decision resolved by the user — `0002` aborts, never deletes. Writing the test for it exposed that the abort left DDL behind on SQLite and broke its own documented recovery path; check moved above all DDL. Suite 193. |
| 2026-08-12 | **Work committed** on `hardening/campaign-auth-and-migrations` in the three-commit split from §3.2. §3's "nothing is committed" warning is now historical. |
| 2026-08-12 | Change 5 deferred by the user (account to be obtained later). Change 7 (scheduler) brought forward, since it needs no live account — see §5. |
| 2026-08-12 | §5 added: the scheduler (`src/scheduler`). Separate worker, refuses without Redis, off by default with a dry run — all three chosen by the user. The no-op `SchedulerAgent` / `scheduler-agent` compose service was found and retired. Suite 223. |
| 2026-08-12 | §5.4 added as an **open item**: the planner ignores each account's configured timezone, so every window is 08:00–19:00 UTC. Harmless until now because nothing ticked; the scheduler makes it real. Not fixed — it changes warm-up behaviour and needs a decision. |
