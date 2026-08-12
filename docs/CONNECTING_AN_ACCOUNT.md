# Connecting a real LinkedIn account

This is the operator guide for tying in a live account and warming it up
safely. Read the first section before you connect anything.

---

## What this system will and won't do to your account

**It will not send anything on day one.** A newly connected account enters a
warm-up programme and physically cannot send an invitation, a message, a
comment or a post until it has earned that capability. This isn't a setting
you forgot to turn on — it's the design.

**It will never send outreach you haven't personally approved.** Invitations
and messages appear in an approval queue with the drafted copy. You approve,
edit or reject each one. Nothing bypasses that, including your own edits — a
booking link you paste into a connection note gets blocked the same as one a
model wrote.

**It will stop itself if things go wrong.** If your acceptance rate falls
below 15%, invitations halt automatically and the account steps back a stage.
If LinkedIn shows a checkpoint, the same. You don't have to be watching.

**It cannot be rushed safely.** You *can* skip stages via the API for an
established account with genuine history. Doing it on a fresh account is the
main way people get restricted with a tool like this, and the API will tell you
so when you try.

---

## Step 1 — Get your session cookies

The system asks for cookies rather than your password. A password would make
this a credential honeypot; a cookie you can revoke yourself by signing out.

1. Sign in to LinkedIn in a browser.
2. Open developer tools → **Application** → **Cookies** → `https://www.linkedin.com`
3. Copy two values:
   - **`li_at`** — the session cookie. Required.
   - **`JSESSIONID`** — strip the surrounding quotes. Required for anything
     that writes: LinkedIn's CSRF check compares a header against this value,
     so without it reads work and writes silently fail.

Both must come from the **same browser session**. Mixing cookies from two
sessions produces a session that passes `whoami` and fails everything else.

---

## Step 2 — Connect and preflight

### From the command line (best while validating endpoints)

```bash
CLERK_DEV_UNSAFE=true python scripts/validate_account.py
```

It prompts for both cookies without echoing them, connects the account, runs the
read-only preflight and prints a pass/fail line per probe. Cookies are never
printed, logged or written to disk, and are never taken as arguments — that would
put a live credential in your shell history.

It also **strips the quotes off `JSESSIONID` for you**, which is the mistake this
page warns about below and the one with the nastiest symptom.

While iterating on endpoint shapes, re-run against the same account instead of
connecting a new one each time:

```bash
python scripts/validate_account.py --account-id <id>            # preflight again
python scripts/validate_account.py --account-id <id> --rotate   # cookies expired
```

Exit code is 0 when the session is valid, 1 otherwise. `--api-base` points it at
a deployed instance; `--token` supplies a real Clerk session JWT.

### From the UI

**Accounts → Connect account.** Paste both cookies, give it a label, pick a
behaviour (Outreach for meeting new people, Engagement for nurturing an
existing network).

The response tells you immediately whether the session works:

- `active` — LinkedIn accepted the session.
- `auth_required` — the cookies were rejected. Get fresh ones; don't retry.

Then go to **Warm-up → Test connection**. This runs a read-only preflight:

| Probe | What it proves | If it fails |
|---|---|---|
| `whoami` | Session, headers, TLS fingerprint and CSRF are all correct | Nothing else will work — fix the cookies |
| `fetch_profile` | Prospects can be enriched | Copy will be less specific |
| `fetch_inbox` | Replies can be detected | **Sequences would keep following up after someone answers** |
| `fetch_activity` | Posts can be found to engage with | Warm-up has nothing to like or comment on |

Nothing is liked, connected, messaged or posted. Safe to run any time.

> **If `whoami` passes but the others fail**, the account is fine — those are
> Voyager endpoint shapes to fix, not account problems. Warm-up can still start.
> The one to care about is `fetch_inbox`: without it, reply detection is blind.

---

## Step 3 — Let it warm up

| Stage | Min days | What it does |
|---|---|---|
| Observing | 2 | A handful of likes. Establishes a consistent device and location. |
| Reacting | 3 | Likes and follows — builds an interest graph so the feed turns relevant. |
| Commenting | 4 | Real comments on ICP posts. This is what earns profile views. |
| Publishing | 5 | Own posts and group conversations, engagement continues. |
| Connecting | 7 | First invitations. Small volume, warm targets, needs 30%+ acceptance to scale. |
| Full outreach | — | Full volume plus follow-up messages. |

**~21 days minimum.** Advancing needs elapsed time **and** completed activity
**and** a healthy acceptance rate. An account that sat idle for a month doesn't
advance; nor does one that crammed a stage's work into a day.

The Warm-up screen shows exactly what's outstanding. "This account can't send
invitations yet" is the system working, not stuck.

### Setting the ICP during warm-up

Do this early, while the account is still in the observe/react stages.
**Targeting → New target profile.** Use the live preview to test criteria
against a made-up person before saving — this costs seconds and prevents the
single most expensive mistake, which is inviting the wrong people.

An ICP with no criteria matches **nobody**, deliberately. There's no accidental
"message everyone" state.

---

## Step 4 — Outreach

Once the account reaches the connecting stage:

1. **Targeting → Add people.** Paste profile URLs, one per line. Add name,
   title and company after commas — more detail means less generic copy.
2. **Approvals → Suggest who to contact.** The agent scores everyone, drafts
   copy for the best fits and queues them. It tells you who it skipped and why.
3. Review each card: who they are, why them, what we'd say, and what's weak
   about the draft. Approve, edit, or reject. "Never contact" is permanent.
4. Approved items are scheduled with human pacing — business hours, cooldowns,
   randomised gaps.

### The cadence after acceptance

```
invite → (they accept) → welcome +2d → value +5d → ask +6d → stop
                              ↓ they reply at any point
                    sequence cancelled, you take over
                              ↓
                    qualify → book (scheduler link allowed here)
```

**A reply stops everything for that person, immediately and permanently.** Run
**sync** (`POST /outreach/accounts/{id}/sync`) regularly so replies and
acceptances are detected — this is what keeps the system from talking over a
real conversation.

Scheduler links are blocked in every automated message and allowed only at the
booking step, after someone has replied and been qualified.

---

## Volume: what's realistic

LinkedIn caps invitations at roughly **100 per week**, on a rolling window, and
**Premium does not raise it**. Sales Navigator doesn't either.

Defaults here:

| Tier | Per day | Per week | For |
|---|---|---|---|
| warmup | 10 | 50 | New accounts |
| standard | 18 | 90 | Established, healthy accounts |
| aggressive | 30 | 185 | Aged, high-SSI accounts only |

If you've been quoted ~800 connections/month (~185/week), that's the
`aggressive` tier — roughly double the standard allowance, and realistic only
for an aged account with a strong profile and a proven acceptance rate. It is
opt-in, and the acceptance governor will still pull it back if the audience
isn't responding.

**Acceptance rate matters more than volume.** Below 15%, LinkedIn treats an
account as spam whatever the volume — a significant share of restricted
accounts never exceeded the published limits. That's why the governor exists,
and why a low acceptance rate is reported as a *targeting* problem: reducing
volume doesn't fix sending to the wrong people.

---

## When something goes wrong

| Symptom | What it means | Do this |
|---|---|---|
| `auth_required` | Cookies expired or LinkedIn signed the session out | Accounts → get fresh cookies → rotate credentials |
| `rate_limited` | LinkedIn challenged the account | Sign in via a browser, clear the checkpoint, leave invitations off ~48h, then re-verify |
| Invitations stopped on their own | Acceptance fell below 15% | Fix the ICP — this is a targeting problem. Messaging existing connections still works |
| Suggestions say "not unlocked yet" | Still in warm-up | Expected. Check the Warm-up screen for what's outstanding |
| `503` on send | Redis is down, so caps can't be enforced | Check `/healthz`. Sending fails closed on purpose |
| Approve returns `422` | Copy failed the quality gate | The message says which rule; edit and retry |

---

## Honest limits right now

- **The Voyager endpoints haven't been validated against a live account yet.**
  They're implemented against the shapes the first-party clients use, and each
  action tries the current-generation endpoint then the legacy one, but expect
  to iterate on exact payloads. A wrong shape fails cleanly and records the
  error — it never sends something malformed. Preflight is how you find out.
- **Warm-up activity is planned, not yet autonomous.** The system computes each
  day's activity, but nothing executes it on a schedule yet — you trigger it.
  This is the biggest remaining gap.
- **The Playwright fallback isn't bound**, so a drifted Voyager shape currently
  means a failed action rather than a slower one.
- **No OpenRouter key configured** means copy comes from templates. They're
  plain but they clear the quality gate. Adding a key is what makes the writing
  genuinely good.
