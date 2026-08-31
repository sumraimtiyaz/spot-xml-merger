# UnisciSPOT — web service

A free, no-sign-up web tool that merges the several monthly ISTAT/SPOT XML
exports a property produces (one per room listing) into the single file
DMS Puglia accepts, and validates the result against the Region of Puglia's
official schema before handing it back.

Uploads are **never written to disk and never logged**. They are parsed in
memory, merged, and discarded when the response is sent. The merged XML and the
CSV summary are built in the visitor's browser from the JSON response.

---

## Run it

```bash
./run-dev.sh            # http://127.0.0.1:8000
```

or

```bash
pip install -r requirements.txt
python3 wsgi.py
```

Tests:

```bash
python3 tests/test_api.py
```

Twelve checks, including two that matter more than the rest: **nothing appears
on disk during a merge**, and **the web output is byte-identical to the desktop
tool's**.

---

## Contact and lightweight metrics

The landing page includes a simple support form for issues, questions, suggestions and improvements. It accepts input from visitors without storing personal data, and it records only anonymous counters for operational reporting.

The service now writes a small XML metrics file with counters such as total visitors, actions, successful/failed actions, satisfied/not satisfied responses, and basic conversion rates. The file is protected with a file lock and rewritten atomically to avoid corruption when multiple requests arrive at once. This is intentionally limited to counts and status values only; no visitor names, email addresses or uploaded document contents are retained.

## Deploy

```bash
docker build -t unisci-spot .
docker run -p 8000:8000 --env-file .env unisci-spot
```

The image runs gunicorn as a non-root user with a healthcheck on `/healthz`.
There is no database, no queue, no persistent volume — the service holds no
state at all, so it scales by running more copies and costs almost nothing
idle. Any container host works: Fly.io, Railway, Render, Hetzner + Caddy, a
€5 VPS.

Behind a reverse proxy, keep `TRUST_PROXY_HEADER=1` so rate limiting sees real
client addresses, and terminate TLS at the proxy.

Copy `.env.example` to `.env` and set at least `SECRET_KEY` and
`CONTACT_EMAIL`. If you want contact-form email notifications via Gmail, set
`SMTP_ENABLED=1`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and optionally
`SMTP_FROM`/`SMTP_TO`.

---

## Layout

```
app/
├── __init__.py       application factory
├── config.py         env-driven settings + feature flags
├── views.py          routes: /, /healthz, /api/merge
├── regions.py        region registry — where a second region plugs in
├── messages.py       IT/EN wording for the engine's notes
├── accounts.py       STUB: where accounts and billing plug in later
├── engine/
│   ├── merge_istat.py    the merge engine (shared with the desktop tool)
│   └── schema/           official movimentogiornaliero-0.6.xsd + datatype
└── templates/index.html  the whole front end, one file, no build step
tests/test_api.py
```

### The engine is shared, not forked

`app/engine/merge_istat.py` is the same file shipped as the desktop tool. It is
standard-library only and has its own 31-case suite. `test_matches_desktop_tool`
asserts the two produce identical bytes, so the copies cannot drift silently.

When you change the engine, change it in one place and copy it to the other,
then run both suites.

---

## Adding a region

Every Italian region has its own portal and its own file layout. Only Puglia is
implemented, because it is the only one with an official XSD and real sample
files in hand.

To add one:

1. Get the region's official schema and technical specification. **Do not
   implement from a sample file alone** — the Puglia work turned up rules that
   no sample would have revealed, such as guest-code uniqueness and the
   date-continuity rule.
2. Add a `RegionProfile` in `regions.py` with a `merge` callable matching
   `merge(uploads, prefix_mode=..., listing_state=...) -> result dict`.
3. Add its schema under `app/engine/schema/`.
4. Add cases to `tests/test_api.py` using real exports.

The UI, the API and the privacy properties come along unchanged.

---

## Adding accounts later

`app/accounts.py` is the seam. Everything already asks `current_actor()` who is
calling and `can(actor, capability)` what they may do; today that is always an
anonymous actor with the `merge` capability. Turning `FEATURE_ACCOUNTS` on is
where sessions, plans and storage attach.

One warning worth repeating there: **the moment you store arrivals you are
storing personal data.** Guest records carry age, sex, citizenship and
residence. That is a materially different compliance proposition from this
service, and the "nothing is stored" copy on the landing page has to change with
it. Storage is what makes history and reminders possible — the features people
actually pay for — so it is probably where the product goes, but go there
deliberately.

---

## Guest codes and browser state

SPOT identifies each guest by `codiceclientesr` and uses it to track who is
currently in the property. Independent listings number their guests
independently, so two rooms can both call a guest "1". Merged naively, SPOT
treats them as one person.

The engine detects the clash and prefixes each code with a per-listing tag. The
mapping is returned in the response and kept in the visitor's `localStorage`,
then sent back with the next merge — so a guest who arrives in one month and
leaves in the next keeps the same code, without the server storing anything.

The prefix is derived from the part of the filename that identifies the listing,
so it is stable even if the browser data is cleared, as long as exports keep
their naming pattern.

---

## What this service deliberately does not do

* No Alloggiati Web (Polizia di Stato) reporting — a separate legal obligation,
  involving guest identity documents.
* No submission to the portal on the user's behalf.
* No storage, accounts, history or reminders — yet.
* No region other than Puglia.

---

Independent tool. Not affiliated with Regione Puglia, Pugliapromozione or
InnovaPuglia.
