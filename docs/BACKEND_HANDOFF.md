# Backend Handoff — Social Bot (SalesRobot-class outbound engine)

Audience: backend engineers continuing this system. It covers the real
architecture, an honest status of every subsystem (especially the **mobile
transport** and whether LinkedIn actions actually execute today), the
**multi-account** model, the **containerization** path from one person to many,
and how the **admin dashboard** aggregates activity across all accounts.

> Status legend: **REAL** = implemented & tested · **SCAFFOLD** = interfaces/
> structure real, behavior not implemented · **BROKEN** = present but non-
> functional (known) · **PLANNED** = designed, not written.

---

## 0. TL;DR for the impatient

- The **web service** (FastAPI API + React SPA) is **REAL and deployed** on
  Railway with Postgres and Redis. Identity/tenancy (Clerk), campaign CRUD,
  global rate-limiter and the OpenRouter LLM provider are done (99 tests).
- **The outreach loop is REAL and end-to-end**: connect account → define ICP →
  import people → agent suggests who to contact and what to say → a human
  approves/edits/rejects → paced send under a global per-account cap. This is
  the product's spine; see §4a. Nothing reaches LinkedIn without an approval.
- **Every new account is warmed up before it may do outreach** (§4b): a staged
  programme where each stage unlocks capability rather than just raising a
  limit, minimum ~21 days, graduation gated on time + activity + acceptance
  rate, with automatic demotion on a challenge or a collapsed acceptance rate.
- **The mobile transport now calls real Voyager endpoints** (whoami, invite,
  message, like, comment, post, profile, inbox) over a TLS-fingerprinted
  session with a stable per-account device identity, with Playwright as the
  fallback. It is validated against a recording transport in tests but has not
  yet been proven against a live LinkedIn account — see §4 for exactly what
  that means.
- Multi-account is modeled *and used* (Org → User → ConnectedAccount), and the
  admin dashboard aggregates across accounts today (`GET /outreach/dashboard`).
  Per-account worker containerization remains PLANNED — design in §8.

---

## 1. Architecture

Two processes, one Redis, one Postgres:

```
                    ┌──────────────── Postgres (tenancy, accounts, campaigns) ─────────┐
                    │                                                                    │
  Browser ── SPA ──▶ FastAPI web service (src/api/main.py)  ──Redis pub/sub──▶ Agent runtime (main.py)
                    │   /api/v1/* + serves SPA                 events:* / commands       │  orchestrator + agents
                    └──────────────── Redis (state, rate limits, task map, events) ──────┘
```

- **API ↔ agents coupling is Redis-only.** The API never touches the agents'
  in-memory objects. It publishes commands and consumes `events:*`. This is the
  seam that makes multi-account/multi-worker scaling clean.
- **Deployed today:** the **web service**, with Postgres and Redis attached.
  The outreach loop runs entirely inside it — generation, approval and sending
  are all synchronous API calls, so it works without the agent runtime. The
  agent runtime (which will drive *scheduled* sending via `execute.run_due`)
  is not yet deployed, so today a due send is triggered by
  `POST /outreach/accounts/{id}/run` rather than a background tick.
- `GET /healthz` reports component readiness (redis, database, encryption key,
  LLM, and whether sending is enabled) — check it first when something is off.

Key modules:
| Area | Path | Status |
|---|---|---|
| FastAPI app + SPA serving | `src/api/main.py` | REAL |
| Campaign CRUD (authed, org-scoped) | `src/campaigns/*`, `src/api/routes/campaigns.py` | REAL |
| Schema migrations | `alembic/`, `scripts/migrate.py` | REAL |
| Identity/tenancy (Clerk) | `src/api/middleware/clerk.py`, `src/tenancy/*` | REAL |
| Connected accounts | `src/accounts/*`, `src/api/routes/accounts.py` | REAL |
| Credential encryption (Fernet) | `src/accounts/crypto.py` | REAL |
| Caps & pacing policy | `src/accounts/caps.py`, `src/outreach/pacing.py` | REAL |
| ICP + relevance scoring | `src/targeting/*` | REAL |
| Suggestion engine | `src/outreach/suggest.py` | REAL |
| Copy quality gate (anti-spam) | `src/outreach/quality.py` | REAL |
| Humanistic copywriting | `src/outreach/copy.py` | REAL (LLM + template fallback) |
| Approve / paced send executor | `src/outreach/execute.py` | REAL |
| Warm-up programme + gating | `src/warmup/program.py` | REAL |
| Daily activity planner | `src/warmup/planner.py` | REAL |
| Activity ledger | `src/warmup/models.py`, `src/warmup/service.py` | REAL |
| Acceptance-rate governor | `src/outreach/health.py` | REAL |
| Sequences + pause-on-reply | `src/outreach/sequences.py` | REAL |
| Acceptance/reply sync | `src/outreach/sync.py` | REAL (needs live validation) |
| Read-only preflight | `src/accounts/preflight.py` | REAL |
| Global rate limiter | `src/infrastructure/rate_policy.py` | REAL |
| OpenRouter LLM (2 slots) | `src/infrastructure/llm/*` | REAL |
| Campaign→agent bridge | `src/infrastructure/task_bridge.py` | REAL |
| Transport interface + router | `src/infrastructure/api_client.py`, `transports/*` | REAL interface |
| Mobile transport endpoints | `src/infrastructure/transports/mobile.py` | REAL, unvalidated live |
| Playwright transport | `src/infrastructure/transports/playwright.py` | REAL adapter, not bound |
| Agent runtime | `src/agents/*`, `src/infrastructure/orchestrator.py` | mixed (see §4) |
| Skeleton agents | `safety/scheduler/whatsapp_monitor/analytics_agent.py` | SCAFFOLD |

---

## 2. Data model (Postgres, SQLAlchemy 2.0 async)

Single `Base` (`src/database/models.py`), cross-dialect types (works on Postgres
and SQLite).

**Alembic is the production path (REAL).** `scripts/migrate.py` runs before the
server in the Docker/Railway start command and is idempotent. Revisions:

- `0001_baseline` — the schema exactly as `AUTO_CREATE_TABLES` was building it.
- `0002_campaign_org_scope` — adds `campaigns.org_id` + `created_by_user_id`,
  backfilling rows that predate tenancy into the oldest organization.

`0002` **never deletes data.** If campaigns predate tenancy and no organization
exists to adopt them, it aborts the deploy with the row count and two ways
forward (create the owning org, or delete the rows deliberately) rather than
guessing an owner. The check runs before any DDL, so the abort is a true no-op
and re-running after fixing it just works — on SQLite too, where DDL is not
transactional.

The split matters: a database created by `create_all` before Alembic existed has
every table but no `alembic_version`, so `alembic upgrade head` would try to
re-create them and fail forever. `scripts/migrate.py` detects that case, stamps
`0001`, and upgrades from there — no manual step, no crash-loop. Don't squash the
two revisions or that path breaks.

`AUTO_CREATE_TABLES` is now a **local-only** shortcut and is `false` in the
image. Leave it off anywhere deployed: create_all inventing tables no migration
describes is how a schema drifts out from under its own history.

`tests/test_migrations.py` runs the migrations for real and fails if the
resulting schema differs from the models — so a model change without a migration
is caught in CI rather than in production.

REAL tables:
- **Organization** (`clerk_org_id`, `whatsapp_admin_number`, `settings`)
- **User** (`clerk_user_id`, `org_id`, `role`)
- **ConnectedAccount** (`org_id`, `user_id`, `status`, encrypted `auth_blob`,
  `device_fingerprint`, `daily_caps`, `mode`, `active_icp_id`, `last_post_at`)
- **Campaign**, **CampaignTask**, **IdempotencyKey**

PLANNED tables (designed in the plan file): ICPProfile, EngagementDirective,
SharedLinkEvent, LinkEvaluation, Post, ContentCalendarEntry, Cadence /
SequenceStep / SequenceEnrollment, InboxThread / InboxMessage, OrgModelSettings.

---

## 3. Multi-account & tenancy — how "multiple logins" work

The model already supports many accounts under one admin:

```
Organization (the admin's workspace / Clerk org)
├── User (admin)         ── owns/oversees
├── User (team member)
└── ConnectedAccount ×N   ── each = one LinkedIn identity the system acts AS
      ├─ auth_blob (encrypted cookies/li_at)      ← how it logs in as that person
      ├─ device_fingerprint (per-account)         ← mobile identity, stable
      ├─ mode + active_icp                         ← its "thought process"
      └─ daily_caps                                ← per-account limits
```

- Every account belongs to an org, so the admin dashboard is just "all
  ConnectedAccounts where org_id = my org."
- Credentials are encrypted at rest (Fernet; reuse the `AccountManagerAgent`
  key handling). **Do not** log `auth_blob`.
- **To build for one person first:** implement the `/api/v1/accounts` CRUD
  (§7 of the Frontend Handoff), the connect flow (capture `li_at`/cookies →
  encrypt → store), and generate the device fingerprint (already implemented:
  `transports/fingerprints.generate_fingerprint`).

---

## 4. The mobile system — honest status (READ THIS)

**Question: is it working with the mobile system?** The endpoints are now real
code rather than scaffolding, but they have not yet been exercised against a
live LinkedIn account. Precisely:

What's REAL and tested:
- `LinkedInTransport` protocol and the `CompositeTransport` router
  (`api_client.py`): tries mobile first, falls back to Playwright on
  `TransportUnavailable`/`TransportChallenge`.
- Per-account **device fingerprint** generation (stable UA / device-id / OS /
  app-version + curl_cffi TLS-impersonation profile): `transports/fingerprints.py`.
- Mobile **session builder**: a curl_cffi session with fingerprint headers, TLS
  impersonation, proxy, cookies, and the `csrf-token` header Voyager requires
  (its value must equal the `JSESSIONID` cookie — a common thing to get wrong).
- **All action methods are implemented** against Voyager: `whoami`,
  `fetch_profile`, `connect`, `send_message`, `like`, `comment`, `create_post`,
  `fetch_activity`, `fetch_inbox`. Each tries the current-generation ("dash")
  endpoint and then the legacy one before giving up and falling back.
- Auth failures (401/403) and throttling (429/999) raise `TransportChallenge`,
  which pauses the account rather than retrying into a restriction.

What is NOT yet proven:
- **No live-account validation.** Voyager is undocumented and its request
  shapes drift; the endpoint bodies here are written against the shapes the
  first-party clients use, but until they run against a real session we cannot
  claim they work. Expect to iterate on the exact payloads.
- The **Playwright fallback executor is still not bound**
  (`get_transport(..., playwright_executor=None)`), so if a mobile shape is
  wrong today the fallback cannot rescue it — the action fails cleanly and the
  suggestion is marked `failed` with the error recorded.
- The agent runtime's session bridge (`InteractionAgent._wait_for_response`)
  remains BROKEN, so the *agent-runtime* path still can't act. The outreach
  loop does not depend on it — it calls transports directly from the API.

**How to validate against a real account** (this is the next concrete step):
1. Connect a real account via `POST /api/v1/accounts` with a live `li_at` +
   `JSESSIONID`. The response `status` tells you immediately whether `whoami`
   worked — that alone validates session construction, headers and CSRF.
2. Import one target, generate a suggestion, approve it, and `POST .../send`.
   Watch `suggestion.result.detail.shape` to see which endpoint generation
   answered, or `suggestion.error` for the failure from every shape tried.
3. Fix payloads as needed. Because each action tries multiple shapes and the
   composite falls back, a wrong guess degrades to a clean failure rather than
   a corrupted send.
4. Then bind the Playwright executor as the safety net for shapes that break.

Ban-risk posture (why mobile-first): mobile-API traffic with stable per-account
fingerprints + the global daily caps (§5) is far lower risk than headless
Chromium. Playwright stays as the fallback only.

---

## 4b. The warm-up programme — how a fresh account becomes safe

A freshly-connected account is the most fragile object in this system. It has
no history on this device, no recent activity, and the single strongest
predictor of a restriction is a quiet account that suddenly starts doing
outreach. Volume limits don't protect against that — the *shape of the ramp*
does.

Every new account walks a fixed programme (`src/warmup/program.py`):

```
observe  2d   like only                    build device/session history
react    3d   + follow                     establish an interest graph
converse 4d   + comment                    earn profile views
publish  5d   + post                       contribute, don't only consume
connect  7d   + connection requests        small volume, warm targets only
full     --   + follow-up messages         full programme
```

**Stages declare capability, not just volume.** `program.is_allowed(stage,
action)` is a hard gate: during `observe`, an invitation isn't rate-limited to
zero, it is unreachable — `suggest.py` won't propose it and `execute.py` won't
send it. This is the property to preserve if you touch this code. Capability is
earned with tenure rather than being available on day one and discouraged by
policy.

**Graduation needs three things together**: elapsed days, completed activity
(`requires`, counted from the durable ledger), and a healthy acceptance rate.
An account that waited a month and did nothing does not advance; nor does one
that crammed a stage's activity into a single day. Time alone is the mistake
that gets accounts banned in week three.

**Demotion is a feature.** A LinkedIn challenge, or acceptance falling below
15%, steps the account *back* a stage automatically. That is the system acting
before LinkedIn does.

### Organic behaviour (`src/warmup/planner.py`)

Turning a stage into a day's activity is where "looks human" is won or lost:

- Volumes are sampled from **ranges**, never fixed. Exactly 12 likes every day
  is as detectable as 500.
- Optional actions carry a **probability below 1.0**, so some days are genuinely
  empty. Real people miss days; schedulers don't.
- Actions are **scattered unevenly** across the active-hours window with a
  minimum gap, producing the clustered-then-quiet rhythm real usage has.
- **Weekends are reduced, not skipped** — an account that works exactly
  Monday-to-Friday is its own signature.

The plan is deterministic per `(account, day)`, so re-running the planner is
safe and the output is testable; it changes tomorrow because the date seeds it.

### Two corrections to conventional automation advice

Both came from checking how LinkedIn actually behaves rather than what tools
advertise, and both changed the design:

1. **Invitations are capped on a rolling week (~100), and Premium does not
   raise it.** The limiter originally modelled only hour/day windows, so an
   account could sit under its daily cap every single day and still be
   restricted by Friday. `AccountRateLimiter` now enforces a seven-day sliding
   window. Vendor-quoted volumes around 800/month (~185/week) are roughly
   double the standard allowance — that lives in the opt-in `aggressive` tier,
   never as a default.

2. **Acceptance rate restricts accounts more reliably than volume.** Below ~15%
   acceptance, LinkedIn treats the account as spam however modest the volume,
   and a meaningful share of restricted accounts never exceeded the published
   limits. `src/outreach/health.py` measures real acceptance and converts it
   into a throttle that scales caps down, stops invitations at the danger line,
   and demotes the stage. It only ever reduces — nothing there can raise an
   account above its stage and tier ceiling.

### Sequences and the reply rule (`src/outreach/sequences.py`)

```
invite → (accepted) → welcome +2d → value +5d → ask +6d → [stop]
                                       ↓ they reply
                          sequence cancelled, human takes over
                                       ↓
                        qualify → book (scheduler link allowed here)
```

**A reply stops the sequence immediately and permanently**, cancelling anything
already queued or awaiting review (`sync.py::_cancel_pending_for_target`). An
automated follow-up landing after someone has answered is the clearest possible
tell that they were talking to software, and it costs the meeting the sequence
existed to book. `sync.py` pulls acceptances and replies back from LinkedIn to
drive this, and is deliberately conservative — an ambiguous signal is treated
as "they replied" rather than "carry on", because a false positive costs one
unsent follow-up and a false negative costs the relationship.

**Scheduler links are step-aware, not banned.** Blocked in every automated
touch; allowed at the booking step once someone has replied and been qualified.
The rule is about *when*, not about the link.

### Preflight (`src/accounts/preflight.py`)

Validates a live account using only read-only calls — `whoami`,
`fetch_profile`, `fetch_inbox`, `fetch_activity`. If `whoami` succeeds the hard
part is proven (headers, cookies, TLS and CSRF are all correct, because Voyager
rejects the request outright otherwise); the rest tell you which features will
work. Nothing is liked, connected, messaged or posted, so it is safe against a
production account at any time. **This is the right first step after tying in a
real account.**

---

## 4a. The outreach loop — the product's spine

The path from "we have a list of people" to "a message was sent" runs entirely
through these modules, and every one of them is a place where the system can
decide *not* to act:

```
  targeting/scoring.py     is this the right person?        -> score + reasons
        │  (below the ICP floor, or excluded -> stop)
        ▼
  outreach/copy.py         what would we say to them?       -> draft
        │  (LLM via OpenRouter content slot; templates if no key)
        ▼
  outreach/quality.py      is this copy fit to send?        -> blockers/warnings
        │  (blockers -> stored as `blocked`, never shown as ready)
        ▼
  outreach/suggest.py      should we ask the user at all?   -> OutreachSuggestion
        │  (dedupe, remaining caps, daily approval budget)
        ▼
  ── HUMAN APPROVES / EDITS / REJECTS ──   (an edit is re-checked by the gate)
        │
        ▼
  outreach/pacing.py       when may it fire?                -> scheduled_for
        │  (active hours, cooldown, jitter)
        ▼
  outreach/execute.py      send it                          -> transport call
           re-checks: approved? due? in hours? copy still clean? under caps?
```

Design notes worth preserving:

- **Scoring is deterministic, not a model call.** Every suggestion can explain
  itself ("Title matches 'head of growth'"), the same person always scores the
  same, and inference cost is spent only on the few who survive. An ICP with no
  criteria matches *nobody* — it fails closed.
- **The quality gate is rules-based on purpose.** A model asked "is this spammy?"
  approves its own output. Leaked `{{placeholders}}`, booking links, pitches in
  connection notes and over-length copy are blockers; tired phrasing, generic
  openers and sender-focused copy are scored penalties surfaced to the reviewer.
- **A human edit is not an exemption.** `approve(edited_text=...)` runs the same
  gate; a person pasting a Calendly link is refused exactly like a model doing it.
- **The daily approval budget** (default 20/account) exists because a 200-item
  queue gets rubber-stamped, which is identical to having no review at all.
- **Caps are checked twice**: once when building the queue (so we don't suggest
  what can't be sent) and again at send time against the live Redis counters,
  which is the authoritative check. A refusal never consumes allowance.
- **Suppression is permanent.** Rejecting with `suppress_target` puts a person
  out of reach of every future suggestion, for every action, regardless of ICP
  changes.

## 5. Rate limiting — real, global, per-account

`src/infrastructure/rate_policy.py: AccountRateLimiter` (REAL, tested). Redis
sliding-window keyed per `connected_account_id` + action. Enforces hourly cap,
daily cap, and cooldown together; consumes a slot only on success. This replaces
the legacy in-memory per-instance limiter (which multiplied caps across the pool).
Caps come from `ConnectedAccount.daily_caps` (fallback to `LinkedInConfig`
defaults). This is the guardrail that keeps N accounts within real LinkedIn
limits — critical for the multi-account safety story.

---

## 6. LLM — OpenRouter, two configurable slots

`src/infrastructure/llm/` (REAL, tested). `LLMProvider.complete(slot, ...)` with
two slots: **content** (humanistic copywriting) and **classification** (fast
intent/relevance). Model per slot resolves: per-org override (`OrgModelSettings`,
PLANNED) → backend config default. Any OpenRouter model id. Configure via
`OPENROUTER_API_KEY`, `OPENROUTER_CONTENT_MODEL`, `OPENROUTER_CLASSIFICATION_MODEL`,
and later the admin settings UI.

---

## 7. Admin dashboard aggregation — one view over many accounts

Everything needed is derivable from Postgres (accounts, campaigns, tasks) +
Redis (live status, rate-limit usage, `events:*`). Proposed backend work:

- **Activity source of truth:** have the interaction path emit `events:*`
  (`interaction_completed`, `reply_received`, `connection_accepted`, ...). The
  `AnalyticsAgent` (SCAFFOLD) rolls these into per-account/per-org counters in
  Redis + periodic snapshots in Postgres.
- **Endpoints** (PLANNED, contracts in Frontend Handoff §8):
  - `GET /api/v1/dashboard/overview` — org totals + per-account summary (today's
    throughput vs caps, acceptance %, reply rate, status).
  - `GET /api/v1/accounts/{id}/activity` — per-account feed.
  - `GET /api/v1/dashboard/metrics?range=` — time series.
- **Live:** extend the (PLANNED) WS server with `ACCOUNT_STATUS` /
  `ACCOUNT_ACTIVITY` events so the dashboard updates without polling.
- Rate-limit usage per account is already queryable: `AccountRateLimiter.usage(
  account_id, action)`.

---

## 8. One person → many: containerization plan

Goal: isolate each LinkedIn account's automation so one account's challenge/ban
never affects others, while a single admin dashboard aggregates them.

Recommended shape (control plane + per-account workers):
- **Control plane** = the web service (API + dashboard) + Redis + Postgres.
  Shared, stateless-ish, already deployed.
- **Per-account worker** = a container running the agent runtime scoped to one
  (or a few) ConnectedAccount(s). Each worker:
  - loads only its account(s)' credentials + fingerprint,
  - owns its own browser context / mobile session,
  - reads work from Redis (`agent_type:interaction` + per-account queues),
  - honors the **global** `AccountRateLimiter` (shared Redis) so caps are
    correct regardless of worker count,
  - emits `events:*` the analytics/dashboard consume.
- **Isolation options** (pick per scale/cost):
  1. One worker container **per account** — maximum isolation (best for ban
     containment), higher cost. Natural for high-value accounts.
  2. One worker per **N accounts** (pool) — cheaper, coarser isolation.
  - Proxy per account (`ConnectedAccount.proxy`) for network isolation.
- **Orchestration:** the existing `AgentOrchestrator` already scales agent pools;
  extend it to shard by `connected_account_id`. On Railway this can be a second
  service (the agent runtime) scaled horizontally; for strict per-account
  isolation, provision a worker service per account or move to K8s (the repo has
  `deployment/` manifests to build on).

Start simple: one shared agent-runtime service handling one account, prove the
vertical slice (§4), then shard to per-account workers.

---

## 9. Current API surface (implemented)

`/healthz` (with component readiness); `/api/v1/me`; `/api/v1/agents` (stub
data); `/api/v1/campaigns` CRUD + `/start` `/pause` `/status` `/tasks`.

**Accounts** — `POST /accounts` (connect + verify), `GET /accounts`,
`GET|PATCH|DELETE /accounts/{id}`, `POST /accounts/{id}/credentials` (rotate
cookies), `POST /accounts/{id}/verify`.

**Targeting** — `POST|GET /targeting/icps`, `PATCH|DELETE /targeting/icps/{id}`,
`POST /targeting/preview` (score a hypothetical person against a draft ICP,
saves nothing, unauthenticated), `POST|GET /targeting/targets`,
`POST /targeting/targets/{id}/suppress`.

**Outreach** — `POST /outreach/suggestions` (generate), `GET
/outreach/suggestions`, `GET /outreach/suggestions/{id}`,
`POST /outreach/suggestions/{id}/approve|reject|send`,
`POST /outreach/accounts/{id}/run` (execute everything due),
`GET /outreach/activity`, `GET /outreach/dashboard` (multi-account roll-up).

All of the above are org-scoped and require auth. Full request/response shapes
in the Frontend Handoff. OpenAPI at `/docs`.

**Campaigns are now org-scoped and authenticated** (they were neither). Every
route depends on `get_request_context`, and `CampaignService` /
`CampaignRepository` take a required `org_id` — there is no unscoped mode and no
`org_id=None` sentinel, because a repository you can build without a tenant is
the defect itself. Another org's campaign returns **404, not 403**: 403 confirms
the id exists. Idempotency keys are namespaced per org for the same reason —
they are client-chosen, so a shared namespace let a replayed key return another
org's campaign as the "cached" response.

The one campaign write with no request context is the orchestrator callback; it
is `campaigns.service.apply_task_result`, a module function that derives the
tenant from the task's own campaign, so the service could keep `org_id`
mandatory.

Known gaps to close: the WS server is not implemented, so the UI polls.

---

## 10. Deployment & config

- **Image:** 3-stage `Dockerfile` (node build → pip → slim runtime) serving
  SPA + API on `$PORT`. `railway.json` sets the start command (shell-wrapped so
  `$PORT` expands) and `/healthz` healthcheck.
- **Live:** Railway project "Social Bot" / production / service `social-bot-web`
  + Postgres + Redis. URL: `https://social-bot-web-production.up.railway.app`.
- **Env vars** (see `.env.example`): `DATABASE_URL` (Railway Postgres) or
  `USE_SQLITE=true`; `AUTO_CREATE_TABLES=true`; optional `REDIS_URL`;
  `CLERK_JWKS_URL`/`CLERK_ISSUER` + `VITE_CLERK_PUBLISHABLE_KEY`;
  `OPENROUTER_API_KEY` (+ model slots); `MOBILE_TRANSPORT_ENABLED`.
- **`ENCRYPTION_KEY` is required to connect accounts** (Fernet key; credentials
  are encrypted at rest with it). Rotating it makes existing stored cookies
  undecryptable — accounts then need `POST /accounts/{id}/credentials`.
- **Sending is refused without Redis**, because the per-account caps cannot be
  enforced globally without it. `ALLOW_UNCAPPED_SENDING=true` overrides this;
  don't, outside local testing.
- **Agent runtime** is a separate process (`python main.py --agents ...`) not yet
  deployed; deploy it as a second Railway service with Redis attached to activate
  execution.
- **Tests:** `pytest` (172 passing, stable across repeated runs). CI-friendly,
  no external services —
  in-memory SQLite, fakeredis, injected Clerk JWKS, and a recording transport
  (`tests/conftest.py`) that captures what *would* have been sent.

---

## 11. Recommended next steps (ordered)

1. **Validate the mobile endpoints against one real account** (§4). This is the
   single highest-value next step: everything else is built and tested, and this
   is the only thing standing between the loop and real sends.
2. **Bind the Playwright executor** as the fallback, so a drifted Voyager shape
   degrades to a slower send rather than a failure.
3. ~~Deploy the agent runtime / a scheduled job that drives warm-up, sync and
   send on a tick.~~ **DONE** — `src/scheduler`, see §12. Off by default until
   the mobile transport is validated (step 1); `SCHEDULER_ENABLED=true
   SCHEDULER_DRY_RUN=true` shows what it would do without acting.
4. ~~Bind the warm-up planner to real content.~~ **DONE** — `src/warmup/runner.py`
   draws engagement from the account's own ICP via `fetch_activity`, auto-runs
   likes/follows, and routes comments and posts to the approval queue.
5. ~~Fix the flaky warm-up runner tests.~~ **DONE** — they failed ~50% of runs
   because the plan is seeded by `(account_id, day)` with a random account and
   the real clock, and `observe` plans likes at `probability=0.8` (one day in
   five is deliberately empty). Tests now search for a day this account really
   is scheduled to act, and ask at end-of-day so nothing is merely early. See
   the module docstring in `tests/test_warmup_runner.py`.
6. WebSocket server (`/ws/updates`) so the approval queue updates live instead
   of polling.
7. ~~Org-scope + auth the legacy campaign routes.~~ **DONE** — see §9.
8. Per-account worker containerization (§8); WhatsApp ingestion; smart inbox;
   content calendar/coaching. (Full phasing in the plan file.)

---

## 12. The scheduler — what makes the programme autonomous

`src/scheduler`, run as `python -m src.scheduler`. Every few minutes it sweeps
every account, across every organization, and does three things per account:

1. `outreach.sync.sync_account` — pull acceptance and reply state back
2. `warmup.runner.run_today` — perform whatever the day's plan says is due
3. `outreach.execute.run_due` — send approved suggestions that are due

**The order is not alphabetical.** Sync runs first because it is what cancels a
sequence when somebody has replied. Sending first would let a tick deliver a
follow-up into a conversation that already had an answer waiting — the worst
thing this product can do, because the prospect sees it. One tick's delay
noticing a reply is acceptable; talking over it is not.

**Why a plain loop rather than Celery or APScheduler.** The engine was already
built to be ticked: `run_today` performs only what is due, subtracts work already
done today and honours the pause flag; `run_due` filters on `scheduled_for` in
SQL. So a missed tick is not a missed action and a duplicated tick is not
duplicated activity, which removes the reasons to want cron semantics, misfire
policy or a durable job store. A job store would also become a second source of
truth about what should run, drifting from the DB whenever an account is paused
or deleted.

**Two things it refuses to do**, both in `src/scheduler/config.py`:

- **Run when not explicitly enabled.** `SCHEDULER_ENABLED` defaults to false. It
  drives real writes to real accounts, and step 1 above has not happened yet.
- **Run live without Redis.** The caps are enforced by a Redis-backed limiter,
  and it does **not** honour `ALLOW_UNCAPPED_SENDING`. That override exists for a
  human pressing a button; an unattended loop with no ceiling is a different
  decision, and it is how an account gets restricted. A dry run needs no Redis,
  since it executes nothing.

Refusals exit non-zero with an operator-facing explanation rather than idling — a
process that is up but doing nothing is the state this module exists to prevent.

**One ticker at a time.** A Redis lease (`scheduler:lease`, SET NX EX with an
owner token and compare-and-delete release) means two concurrent sweeps cannot
both see the same rate budget and both spend it. That is easy to arrange by
accident — a second web replica with `SCHEDULER_IN_PROCESS` set, a worker
deployed alongside one, or an overlapping deploy — and the symptom is *more
activity*, not an error, so it is enforced in code rather than by convention.

**Liveness is reported separately from output**, on `/healthz` as `scheduler` and
`scheduler_last_tick`. This matters more than it sounds: the warm-up programme has
deliberately quiet days (`observe` plans likes at `probability=0.8`, so one day in
five does nothing, and the planner says *"A deliberately quiet day — real accounts
have them"*). "No activity" is therefore never evidence of a fault, so absence of
activity can never be the alarm — the scheduler has to assert its own aliveness.

**Deploying it on Railway.** A second service from the same repo and image, with
start command `python -m src.scheduler` and no healthcheck path (it serves no
HTTP; use the heartbeat on the API's `/healthz` instead). It needs `DATABASE_URL`,
`REDIS_URL`, `ENCRYPTION_KEY` and `SCHEDULER_ENABLED=true`. Do not set
`SCHEDULER_IN_PROCESS` there — that is the local-development shape, and with more
than one web replica it would put a ticker in each.

Sync gets its own slower cadence (`SCHEDULER_SYNC_INTERVAL_SECONDS`, default 30
minutes) because unlike the other two stages it always costs LinkedIn requests
and can never return early. Cadence is tracked as a Redis key with a TTL — the
key existing *means* "synced recently" — so there is no timestamp arithmetic and
losing the record costs one extra read-only sync.

Pacing: accounts are spaced within a sweep and the interval is jittered, because
every account acting at `:00` and ticks landing on exact multiples of five minutes
forever are both patterns, and not being a pattern is the entire premise.

⚠️ **`SchedulerAgent` in `src/agents/core/` is not this.** It is a no-op skeleton
kept so the legacy orchestrator can boot; its docstring now points here. The
`docker-compose` service that used to run it (booting healthy and doing nothing)
now runs the real scheduler.
