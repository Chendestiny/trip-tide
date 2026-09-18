# AI 旅行搭子 (TripTide) — AI Itinerary Planner

[中文](README.md) ｜ **English**

A self-service trip planner for travelling in China: **pick a city → tick attractions → the AI produces a day-by-day itinerary** (geographically sensible ordering, a timeline, meal areas, hotel districts, and the reasons behind its trade-offs).

V1 ships as a **responsive web app** (Vue 3 + Vite, desktop-first — it only switches to the mobile-app form at ≤640px).
The WeChat Mini Program lives in a **separate directory**, `miniprogram/`, and is **written independently of the web app with no shared code**;
its pages map one-to-one — see [`docs/MINIPROGRAM.md`](docs/MINIPROGRAM.md).

---

## Tech Stack

| Layer | Technology |
|----|------|
| Frontend | Vue 3 + Vite (`vue-router`, **no UI framework** — 4 pages hand-built) |
| Backend | Python 3.11+ / FastAPI + SQLAlchemy 2.0 |
| Database | MySQL 8 (falls back to SQLite automatically when `DATABASE_URL` is empty — runs with zero config) |
| LLM | DeepSeek (default alias `deepseek-flash`), OpenAI-compatible endpoint |
| Coordinates | Amap (高德) Open Platform **Web Service** Key (POI text search, returns GCJ-02) |
| Map rendering | Amap **JS API** Key (`frontend/.env`); falls back to an SVG schematic when the key is missing |

---

## Interface

| Wide · Home: separate tabs for cities / regions | Wide · Attraction pool: settings panel on the right + live tightness estimate |
|---|---|
| ![Home](docs/screenshots/wide-home.png) | ![Attraction pool](docs/screenshots/wide-pick.png) |

| Wide · Itinerary result: per-day timeline + plan notes on the right | Mobile · Itinerary result (same DOM, switched by media query) |
|---|---|
| ![Result page](docs/screenshots/wide-plan.png) | ![Mobile](docs/screenshots/mobile-plan.png) |

The visuals are a hand-rolled design system (35 CSS variables at the top of `style.css`): warm neutral background, layered shadows,
8-colour cycling city icons, and a per-day colour-coded timeline (7-colour cycle). No UI framework was pulled in — this interface is only 4 pages, so building it by hand is lighter and more controllable than wiring up Element Plus.

---

## Page Flow

```
① /trip                Home: two tabs, "Cities | Regions" (filtered by kind) + AI engine status
      ↓ pick a city
② /trip/pick?city=成都  Attractions sorted by heat descending, multi-select highlights
      Settings panel on the right (a bottom bar on mobile):
        · Trip length 1~7 days   · Pace: relaxed / balanced / packed
        · Transport: 🚗 self-drive / 🚕 taxi + public / 🚇 public
        · Daily departure → target return time
        · ☐ Set first and last day separately (you may arrive at noon on day 1, or need to catch a flight on the last day)
        · Live tightness estimate: easy / moderate / tight / overloaded + which attractions will be dropped
      ↓ Start planning → POST /plan → loading (usually 15~30 s)
③ /trip/plan           Day-by-day timeline + collapsible sections (AI review notes / dropped attractions / must-see reasons / hotel advice) + half-screen route map
      ↻ Regenerate → dialog to change parameters; either "tweak by pace" (seconds, no tokens) or "redo everything" (goes through the LLM)
④ /trip/me             Past itineraries (localStorage, capped at 30, tap to reopen)
```

---

## Quick Start

### 1. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\python -m pip install -r requirements.txt   # Windows
# source venv/bin/activate && pip install -r requirements.txt   # macOS / Linux

cp .env.example .env      # fill in DATABASE_URL / DEEPSEEK_API_KEY / AMAP_KEY
```

The three `.env` entries that matter:

| Variable | What happens if you leave it blank |
|---|---|
| `DATABASE_URL` | Falls back to `backend/data/triptide.db` (SQLite) — functionality is identical |
| `DEEPSEEK_API_KEY` | Planning endpoints fall back to the local rule engine and still return a complete itinerary (`source=fallback`) |
| `AMAP_KEY` | Seeding uses the hand-checked coordinates in the offline seed file; no recalibration |

### 2. Load the data (required on first setup)

```bash
cd backend
venv\Scripts\python -m app.trip.seed              # all 68 destinations (very slow — see below)
venv\Scripts\python -m app.trip.seed --city 成都   # one city only (recommended)
venv\Scripts\python -m app.trip.seed --list       # inspect what is in the database
```

With a key present it runs "LLM produces the shortlist → Amap **geocoding** fills in real coordinates (POI search is only a fallback)"; without one it uses the offline seed
(`data/seed_attractions.json`, **all 68 destinations match the database field for field**). Every entry in the seed carries
`coord_source=amap` plus its sub-attraction coordinates, so `--source offline` **makes no Amap requests at all**.

> 📌 **The seed must stay in sync with the database**: it drifted once (an older city's `heat` values were still on the pre-migration 0~100 scale,
> which meant offline seeding could not identify a single "must-see"). Remember to realign after changing attraction data in the database.

One city is roughly 22 attractions + 60~80 sub-attractions ≈ 100 Amap requests, throttled serially by `AMAP_SLEEP=0.8s` — **measured at 2~3 minutes**.
**When adding data to an older city you must go city by city with `--city`**: run without arguments and it walks every city, calling DeepSeek once per city
and overwriting the hand-checked shortlists.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev        # → http://localhost:5176
```

### 4. Start both ends with one command (recommended)

```bash
npm install        # at the repo root, installs concurrently
npm run dev        # frontend 5176 + backend 8002
npm run seed       # equivalent to the backend initialisation above
```

Open http://localhost:5176 in a browser; `/api` is proxied to 8002 by Vite automatically.

### 5. Regression checks

```bash
# Backend: 15 rule-engine cases, validating node by node that times are self-consistent, meal
# placement, theme consistency, and that attractions never silently vanish
cd backend && venv/Scripts/python ../scripts/check_planner.py

# Home-screen ranking health check: whether the ranking scores are stale and whether the order
# matches the definition (read-only)
cd backend && venv/Scripts/python ../scripts/check_rank.py

# Frontend: headless browser across both wide and mobile viewports, walking all 4 pages plus a
# real planning run (requires both ends to be up)
python scripts/smoke_ui.py
python scripts/smoke_ui.py --skip-plan
```

The frontend smoke test is not optional — a blank white screen compiles without error and every endpoint still returns 200; only an actual browser run catches it.

---

## Design Notes

**Geographic day-splitting → LLM review → parallel LLM writing → deterministic timeline materialisation → LLM final review.**

```
① Deterministic day-splitting   Large/outlying attractions get a day of their own → the rest are
                                clustered by straight-line distance into "district clusters" →
                                clusters are assigned to days
                                A district cluster is atomic and cannot be split — adjacent
                                attractions are guaranteed to land on the same day
①.5 LLM review                 Hands the model a quantified travel-time matrix for suggestions;
                                accepted only after passing 5 hard validations, otherwise ① stands
② Parallel LLM writing         One thread per day, concurrent via ThreadPoolExecutor
③ Deterministic materialisation materialize_day() — the only place in the project that produces a time
④ LLM final review             Reads the finished timeline, gives feedback + polishes the overview
```

**Why the LLM does not orchestrate.** The first version let the model orchestrate with 10 tools (function calling).
It worked, but cost **26 tool calls and 1.1 minutes**, and it broke the day-splitting: in testing it moved the Panda Base to 14:20 (the pandas are asleep in the afternoon)
and scheduled Kuanzhai Alley on two separate days. Writing the instruction twice in the prompt did not stop it — because "does it fit" was never something the model should have been judging.
Switching to the parallel pipeline brought it to **17.7 s, 3.7× faster**; turning the model's thinking off on 2026-09-16 brought it down further to **6~7 s**.

**Why the LLM was later invited back to review the day-splitting.** Because **the input changed**: the new design hands it a
**pre-computed travel-time matrix**, so it no longer guesses distances and merely reads a table. Add 5 hard validations (adjacent attractions must share a day /
standalone attractions get a day of their own / must not exceed budget …) and it **can only improve things, never break them**.

**Time must be self-consistent.** Inside `materialize_day()` there is exactly one `cur_time` advance point, so
`arrival = previous departure + travel time` holds unconditionally. This invariant is guarded twice over: by the runtime audit and by the regression script.

**The time budget is hard.** Departure and return times are hard constraints set by the user and are **never multiplied by a pace factor**.
On the day you have to rush back (last day + an explicitly set return time) it pins the clock: if dinner does not fit, dinner is dropped;
if necessary the last attraction's dwell time is compressed.

**Transport is tiered by distance and computed entirely locally.** Walking / metro & bus / taxi / self-drive / carpool / intercity coach / intercity rail,
tiered into "within the city / between inner suburbs / outlying", further split by the three preferences of self-drive, taxi + public transport, and public transport.
**No routing API is called**: a single plan has a dozen or more legs, hitting an API is both slow and quota-hungry, and a ±10-minute error does not affect whether the plan is usable.

**Dining gives an area, never a shop name.** Shops change constantly, so naming one would be actively misleading. What you get is "area + local specialty snacks".

**Accommodation is decided once for the whole trip.** The long-stay district is chosen from the geometric centre of all attractions on the trip; only when a given day sits more than 45 km away (say a Dujiangyan day from Chengdu) is staying nearby that night suggested.

**One-tap AI.** When you would rather not tick attractions one by one, giving just **city + days + pace** is enough (`POST /auto-plan`) —
the server picks attractions by "must-see first, then heat" and then runs through **the very same** generation pipeline,
so the output structure is identical to a hand-picked plan.

**Tightness is measured by "average daily sightseeing load".** The total dwell time of the ticked attractions is spread across the days and compared with the pace targets (relaxed 240 / balanced 360 /
packed 450 minutes). This is more intuitive than a "time occupancy rate" and is **sensitive to the number of days** (add a day and it loosens).

**Degradation is never silent.** With no key configured, or when the pipeline fails, it switches to the rule engine and states the reason in plain terms at the end of `summary`;
the frontend also tags the result "generated by rule engine".

---

## Responsive Design

**Desktop-first**: the wide web layout renders by default, and only below 640px does it switch to the mobile-app form. It is one DOM switched by media queries, not two sets of templates.

| | Wide (≥641px) | Mobile (≤640px) |
|---|---|---|
| Container | 1120px, centred | full-bleed |
| Navigation | top nav bar (`SiteHeader`) | bottom tab bar (`TripTabBar`) |
| Home city grid | `auto-fill minmax(238px, 1fr)`, about 4 columns | 2 columns |
| Attraction pool | list on the left (multi-column cards) + sticky settings panel on the right | single column + fixed bottom bar |
| Result page | timeline + sticky "plan notes" on the right | single column, notes move below the timeline |
| Map button | top right of the header | bottom bar |
| Regenerate dialog | centred card | bottom slide-up sheet |
| History | card grid | single column |

**Why desktop-first**: the primary scenario is "plan on a computer, read on a phone", and the wide layout demands a higher information density.
A mobile-first implementation very easily turns the wide layout into "a stretched phone", whereas going the other way costs only a handful of overrides.

Almost all of the difference is plain CSS. Only two things need JS to know which form it is in (`useIsWide()` in `use-media.js`):
whether the settings panel is a persistent sidebar or a collapsible bar, and whether the map button sits in the header or the bottom bar.

---

## Documentation

| Document | What it covers |
|---|---|
| [**`docs/README.md`**](docs/README.md) | **Documentation index + 5-minute overview + directory map (read this first)** |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | **Changes that affected the architecture, by date** (including the cause of and fix for every pitfall) |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layering and design trade-offs: why it is cut this way, the transport model, tightness, pitfalls hit |
| [`docs/BACKEND.md`](docs/BACKEND.md) | Symbol-level map of the backend: what each file exports, the four table schemas |
| [`docs/FRONTEND.md`](docs/FRONTEND.md) | Symbol-level map of the frontend: routes, components, state, design-system variable table, responsiveness |
| [`docs/OFFLINE.md`](docs/OFFLINE.md) | **The offline single-file build**: how the two planners mirror each other, the build switch (`@api`), consistency verification |
| [`docs/MINIPROGRAM.md`](docs/MINIPROGRAM.md) | **The WeChat Mini Program**: why it is a separate directory, the duplication list versus the web app, the three local dev tiers, the page mapping |
| [`docs/API.md`](docs/API.md) | Contracts for the 9 HTTP endpoints, error codes, derivation of the tightness thresholds |
| [`docs/PLAN_SCHEMA.md`](docs/PLAN_SCHEMA.md) | Every field of the timeline data structure `PlanResult` |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | Environment setup, common commands, tests, troubleshooting table, to-do |
| [`docs/testing-prompt.md`](docs/testing-prompt.md) | A self-contained prompt for handing unit-test writing to another model (Python baseline: `backend/tests/test_planner.py`, 122 cases; JS side: `frontend/tests/`, 84 cases) |
| [`AGENTS.md`](AGENTS.md) | The operating manual and 25 iron rules for AI agents |

> The `docs/` tree is written in Chinese only; [`docs/README.md`](docs/README.md) is the index.