# Deployment

How to run this prototype somewhere other than the machine it was written on, and what each of
those places costs you. Every supported path is below as numbered steps.

> **Read this first.** The application has **no authentication, no authorisation and no rate
> limiting**. A deployed instance is open to anyone who finds the URL: they can upload evidence,
> read back the parsed text of everything already uploaded, run assessments, generate reports and
> delete projects. That is stated in the OpenAPI description, in `GET /health` and at `GET /`
> because it is not a detail. Deploy with **synthetic data only**, or put an authenticating proxy
> in front of it. [`SECURITY.md`](SECURITY.md) is the companion to this document and should be read
> before, not after.

Companion documents:
[SETUP.md](SETUP.md) (local installation and configuration) ·
[SECURITY.md](SECURITY.md) (posture, threat model, responsible use) ·
[USER_GUIDE.md](USER_GUIDE.md) (operating the console) ·
[DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) (the schema you are about to put on a server).

**Contents**

- [Which path should I take?](#which-path-should-i-take)
- [Before you deploy this publicly — the checklist](#before-you-deploy-this-publicly--the-checklist)
- [What is in the repository, and which file does what](#what-is-in-the-repository-and-which-file-does-what)
- [Path 1 — Streamlit Community Cloud](#path-1--streamlit-community-cloud)
- [Path 2 — Render (API and console as two services)](#path-2--render-api-and-console-as-two-services)
- [Path 3 — Railway, Heroku and other Procfile platforms](#path-3--railway-heroku-and-other-procfile-platforms)
- [Path 4 — Docker and docker-compose on a VM](#path-4--docker-and-docker-compose-on-a-vm)
- [PostgreSQL](#postgresql)
- [Custom domains and TLS](#custom-domains-and-tls)
- [Environment-variable reference for deployed instances](#environment-variable-reference-for-deployed-instances)
- [Verifying a deployment](#verifying-a-deployment)
- [What was verified for this document, and what was not](#what-was-verified-for-this-document-and-what-was-not)
- [Troubleshooting](#troubleshooting)

---

## Which path should I take?

| Path | Good for | Persistence | Effort | Access control |
|---|---|---|---|---|
| **Streamlit Community Cloud** | A student demo, a supervisor link, a viva. **Recommended.** | **None.** Ephemeral disk: the SQLite database, uploaded evidence and generated reports are lost on every restart and redeploy. | One form. No server. | Community Cloud's own viewer setting — the only built-in access control available on any path here. |
| **Render (Blueprint)** | Showing the two-service architecture the research design describes: console → HTTP API → engine. | None on the free plan; a paid disk or PostgreSQL fixes it. | ~15 minutes, plus one redeploy to fill in the URLs. | None. Put a proxy in front or keep it synthetic. |
| **Railway / Heroku / Dokku** | The same shape on a different platform, driven by the `Procfile`. | Platform-dependent; Heroku's dyno filesystem is ephemeral. | Similar to Render. | None. |
| **Docker / docker-compose on a VM** | A machine you control, a private network, a reverse proxy that can authenticate. The only path where "real-ish" use is even arguable. | A named volume survives restarts and redeploys. | An hour, including the proxy. | Whatever you put in front of it. |
| **Nothing — run it locally** | Everything else. `python run.py` is the supported configuration and the one every result in this project was produced on. | Your disk. | None. | Your machine. |

The honest recommendation for a dissertation: **run it locally for real work, and deploy to
Streamlit Community Cloud only as a link someone can click.** Nothing in this system needs a
server. The console calls the service layer in-process, so a deployment buys you a URL and costs
you persistence, privacy and a set of failure modes that have nothing to do with the research.

---

## Before you deploy this publicly — the checklist

Work through this before the deploy, not after. Every item is a thing this prototype does **not**
do for you.

1. **There is no login.** Confirm you are willing for any person or crawler that reaches the URL to
   see, add to and delete everything in it. If you are not, stop here and use a platform viewer
   restriction (Streamlit Community Cloud), a private network, or an authenticating reverse proxy.
2. **Upload nothing real.** No client evidence, no employer evidence, no coursework containing a
   real organisation's system exports, no screenshots with real account names. The repository ships
   synthetic data precisely so you never need to. See
   [`SECURITY.md` § Data handling](SECURITY.md#data-handling-and-why-real-evidence-must-not-be-uploaded).
3. **Assume the deployment is public and permanent.** Evidence you upload is stored unencrypted,
   its text is copied into the database as chunks, and again into `Assessment.prompt_snapshot`.
   Deleting the project deletes rows, not bytes.
4. **Decide whether a model provider gets your text.** With `LLM_PROVIDER=mock` (the default)
   nothing leaves the host. The moment you set `LLM_PROVIDER=claude` or `openai`, retrieved
   evidence chunks are transmitted to that provider and become subject to its retention terms.
5. **Never commit a key.** `.env` is gitignored, `.env.example` holds no values, `render.yaml`
   marks every secret `sync: false`, and `docker-compose.yml` only passes `${ANTHROPIC_API_KEY:-}`
   through from your shell. Keep it that way, and check the history once before you push:

   ```bash
   git log -p | grep -nE "sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{24,}"
   ```

   On this repository that returns exactly one line — the deliberately fake key in
   `tests/test_anthropic_provider.py`. Anything else is a real finding.
6. **Set `CORS_RESTRICT_TO_CONFIGURED=true` on anything internet-facing**, with an explicit
   `CORS_ALLOW_ORIGINS`. Understand what that does and does not buy you: it constrains *browsers*
   on other origins. It is not an access control and stops nobody using `curl`.
7. **Turn off Streamlit's error details**: `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=none`. Otherwise an
   unhandled exception prints filesystem paths into a page anyone can open.
8. **Leave the safety rails on.** `FORCE_HUMAN_REVIEW=true` and `MOCK_HALLUCINATION_RATE=0.0`. The
   second one deliberately fabricates citations; it is a research lever, not a setting.
9. **Label the deployment.** Anyone who opens the console must be able to tell in one screen that it
   is a research prototype running on synthetic data. The banners do this — do not remove them, and
   do not screenshot the console in a way that hides the provider badge.
10. **Know how to take it down.** Have the platform's delete/suspend path open in another tab before
    you deploy. For a viva demo, take it down afterwards.

If you cannot tick all ten, the deployment is not ready to be public. That is a normal outcome for
a research prototype and not a criticism of it.

---

## What is in the repository, and which file does what

Everything below already exists in the repository root; none of it needs to be written.

| File | Read by | What it does |
|---|---|---|
| `scripts/start_api.sh` | Dockerfile, Procfile, `render.yaml` | The **single definition** of the API start command. Reads `$PORT` (then `API_PORT`, then 8000), binds `$HOST` (default `0.0.0.0`), one uvicorn worker by default, `--proxy-headers` so URLs behind a TLS terminator say `https`. |
| `scripts/start_ui.sh` | Dockerfile, Procfile, `render.yaml` | The same for Streamlit: `$PORT` → `STREAMLIT_PORT` → 8501, `--server.headless true`. |
| `Procfile` | Heroku, Railway, Dokku | `web: sh scripts/start_api.sh` and `ui: sh scripts/start_ui.sh`. Two process types because they are two servers. |
| `render.yaml` | Render Blueprints | Both services declared, with the production environment variables and the secrets left as `sync: false`. |
| `runtime.txt` | Heroku-style platforms | `python-3.12.11`. Render reads the `PYTHON_VERSION` variable in `render.yaml`, which is set to the same string — **keep the two in step**. |
| `Dockerfile` | Docker | Multi-stage build on `python:3.12-slim-bookworm`, non-root uid 10001, one entrypoint that runs `api`, `ui` or `all`, plus a health check that probes whichever role `APP_ROLE` names. |
| `.dockerignore` | Docker | Keeps `.env`, the local virtualenv, the database and any uploaded evidence out of every image layer. |
| `docker-compose.yml` | Docker Compose v2.24+ | API and console as two containers sharing one named volume, with a commented-out PostgreSQL service. |
| `.streamlit/config.toml` | Streamlit | Headless, `0.0.0.0`, CORS and XSRF protection on, 50 MB upload cap, and the dark console theme copied from `app/frontend/theme.py`. |
| `.env.example` | You | Every application variable with a working default. Copy to `.env` locally; on a platform, use its own environment/secrets mechanism instead. |

**Why the local `python run.py` path is different.** `run.py api` binds `API_HOST` (127.0.0.1 by
default) because an unauthenticated API on a laptop should not be on the network. The deployment
scripts bind `0.0.0.0` because a container or dyno that listens on loopback is reachable by nothing
but itself. This difference is deliberate; do not "fix" one to match the other.

**One asymmetry to know about.** The *console* does not follow that rule locally:
`.streamlit/config.toml` sets `address = "0.0.0.0"`, so `run.py ui` listens on `*:8501` on your
laptop too — verified with `lsof`. On a shared network that puts an unauthenticated audit console in
front of everyone on it. For a local session on a network you do not control:

```bash
STREAMLIT_SERVER_ADDRESS=127.0.0.1 .venv/bin/python run.py ui    # verified: binds 127.0.0.1:8501
```

---

## Path 1 — Streamlit Community Cloud

The simplest path, and the one to use for a student demo. One service, no API, no server to
maintain. The console runs the service layer in-process — exactly the configuration described in
[`USER_GUIDE.md`](USER_GUIDE.md) — so nothing needs `USE_API`.

### Steps

1. **Push the repository to GitHub.** Public or private; Community Cloud can deploy from either
   with the GitHub authorisation it asks for. Confirm first that no secret went with it:

   ```bash
   git ls-files | grep -E '^\.env$|secrets\.toml' && echo "STOP: a secret is tracked"
   ```

   Nothing should print. `.env`, `.streamlit/secrets.toml`, `*.db` and everything under
   `data/uploads/`, `data/reports/`, `data/synthetic/` and `data/evaluation/` are already ignored;
   `data/case_studies/` and `data/controls/` are tracked on purpose, because the app needs them.

2. **Create the app.** share.streamlit.io → **Create app** → **Deploy a public app from GitHub**,
   then set:

   | Field | Value |
   |---|---|
   | Repository | `your-user/your-repo` |
   | Branch | `main` |
   | **Main file path** | `app/frontend/streamlit_app.py` |

   The entry script inserts the repository root into `sys.path` itself (`streamlit run` puts only
   the script's own directory there), so `import app...` resolves with no extra configuration.

3. **Pick the Python version** in *Advanced settings*. Choose **3.12**. The source is written to
   Python 3.9 syntax and runs unchanged on 3.12; 3.12 is what the pinned wheels and the modern
   Anthropic SDK are happiest on. (`runtime.txt` is for Heroku-style platforms and Render; it is not
   what Community Cloud reads.)

4. **Add secrets — only if you want a real model.** *Advanced settings → Secrets* takes TOML.
   Community Cloud exports every **top-level** string/int/float secret as an environment variable,
   which is exactly what `app/config.py` reads, so this works with no code change:

   ```toml
   LLM_PROVIDER = "claude"
   ANTHROPIC_API_KEY = "sk-ant-..."
   ANTHROPIC_MODEL = "claude-opus-5"
   ANTHROPIC_EFFORT = "high"
   FORCE_HUMAN_REVIEW = "true"
   MOCK_HALLUCINATION_RATE = "0.0"
   ```

   Two rules that follow from how this works, both verified against `streamlit==1.50.0`:

   * **Top-level keys only.** A value inside a `[table]` is *not* exported to the environment, so
     `app/config.py` will never see it.
   * **`STREAMLIT_*` variables do not belong here.** Streamlit parses its own configuration when the
     server starts and loads `secrets.toml` afterwards, so a `STREAMLIT_…` key in this box arrives
     too late. Change `.streamlit/config.toml` and redeploy instead.

   With no secrets at all the app deploys and works: the default provider is the offline
   deterministic mock.

5. **Restrict who can view it.** In the app's settings, set viewer access to the specific people who
   need it rather than "anyone with the link". **This is the only real access control available on
   any deployment path in this document** — the application itself has none.

6. **Open the app and check the sidebar.** The provider badge must say what you expect. `AI
   Provider: Mock` means the deterministic rule engine answered; a red mismatch badge means a real
   provider was configured but could not be constructed (usually a missing key) and the mock
   answered instead. Results from a mock run measure the pipeline, not a model.

7. **Load the demo data.** Dashboard → **Load demo project + synthetic evidence**. That gives a
   visitor something to look at without uploading anything.

### What you are giving up

* **The filesystem is ephemeral.** On every restart, redeploy and idle shutdown, `data/audit.db`,
  everything under `data/uploads/` and every generated report are gone. The app rebuilds an empty
  schema and reseeds the 14-control library on the next start, so it comes back *working* and
  *empty*. For a demo this is often fine — press the demo-data button again.
* **Fixing it means an external database.** Set `DATABASE_URL` to a hosted PostgreSQL instance (see
  [PostgreSQL](#postgresql)) as a top-level secret. That makes projects, controls, assessments,
  reviews and reports survive a restart. It does **not** make *uploaded evidence files* survive:
  the bytes live under `data/uploads/`, which is still ephemeral, so their rows will point at
  missing files and re-parsing or re-downloading them will fail. There is no object-storage backend
  in this prototype; adding one is future work.
* **`psycopg` is not installed by default.** `requirements.txt` has it commented out. To use
  PostgreSQL here you must uncomment `psycopg[binary]>=3.1` in `requirements.txt` and push, because
  Community Cloud installs exactly that file.
* **Resource limits.** The Community Cloud free tier is a small container. The mock provider is
  fast; a full evaluation run over six datasets in three modes is heavier and may be slow or be
  killed. Run experiments locally and deploy the console for reading.

---

## Path 2 — Render (API and console as two services)

`render.yaml` is a Blueprint that declares both services from one repository. This is the shape the
research design describes — a console that talks HTTP to a backend — and the one to deploy if the
point is to show the architecture.

### Steps

1. **Push to GitHub**, as in Path 1 step 1.

2. **Render → New → Blueprint**, point it at the repository. Render reads `render.yaml` and offers
   to create two web services:

   | Service | Start command | Health check |
   |---|---|---|
   | `llm-it-auditor-api` | `sh scripts/start_api.sh` | `/health` |
   | `llm-it-auditor-ui` | `sh scripts/start_ui.sh` | `/_stcore/health` |

3. **Leave the `sync: false` variables empty for the first deploy.** Render assigns each service its
   hostname only once the service exists, and a blueprint cannot compose `https://` with a hostname.
   The first deploy is expected to come up with no cross-service wiring.

4. **Deploy, then read the two hostnames** from the top of each service's page. They look like
   `https://llm-it-auditor-api.onrender.com` and `https://llm-it-auditor-ui.onrender.com`.

5. **Fill in the environment variables** and redeploy both services. This is the whole wiring, and
   it is worth understanding rather than copying:

   On **`llm-it-auditor-api`**:

   | Variable | Value | Why |
   |---|---|---|
   | `PUBLIC_API_URL` | `https://llm-it-auditor-api.onrender.com` | Published in the OpenAPI `servers` block, so "Try it out" in `/docs` hits the right host. |
   | `PUBLIC_APP_URL` | `https://llm-it-auditor-ui.onrender.com` | Named in the API description and at `GET /`, **and trusted by CORS**. |
   | `CORS_ALLOW_ORIGINS` | `https://llm-it-auditor-ui.onrender.com` | The console's origin. Comma-separate more if you have them. |
   | `CORS_RESTRICT_TO_CONFIGURED` | `true` | Already set in `render.yaml`. Stops trusting loopback and permits only the origins above. |
   | `ANTHROPIC_API_KEY` | your key, or leave empty | Empty means the app falls back to the offline mock, loudly, rather than failing. |
   | `LLM_PROVIDER` | `mock`, or `claude` once the key is set | `render.yaml` ships `mock` so a deployment works before any key exists. |

   On **`llm-it-auditor-ui`**:

   | Variable | Value | Why |
   |---|---|---|
   | `USE_API` | `true` | Already set. Routes the console through the API instead of the in-process service layer. |
   | `API_BASE_URL` | `https://llm-it-auditor-api.onrender.com` | **The value the console actually calls** (`app/frontend/api_client.py`). |
   | `PUBLIC_API_URL` / `PUBLIC_APP_URL` | the two hostnames | Display only. |

   No `ANTHROPIC_API_KEY` goes on the console service, deliberately: with `USE_API=true` the console
   never calls a model itself, so the key does not belong in the process that serves the browser.

6. **Confirm the wiring**, from your own machine:

   ```bash
   curl -s https://llm-it-auditor-api.onrender.com/ | python3 -m json.tool
   ```

   The `cors` block should read `"mode": "restricted"` with your console URL in `allowed_origins`
   and `"loopback_allowed": false`. If `allowed_origins` is empty, step 5 has not taken effect and
   **every cross-origin browser request will be refused** — which is the intended failure mode:
   refusing traffic is safer than falling back to allowing everything.

7. **Open the console.** If the Findings and Controls pages populate, the console reached the API.
   If every page shows a connection error, `API_BASE_URL` is wrong or the API service is asleep.

### What you are giving up

* **Free instances sleep** after a period of inactivity (Render's documented behaviour for the free
  plan; not verified here). The next request wakes the service, which takes tens of seconds, so the
  console may show a timeout on the first page load after a quiet period. `API_REQUEST_TIMEOUT`
  (default 300s) governs how long the console waits before giving up.
* **The free filesystem is ephemeral**, exactly as on Streamlit Cloud, and here it matters twice:
  each service has its *own* filesystem, so even if it persisted, the console and the API would not
  see the same files. That is why `USE_API=true` is set — all evidence and report access goes
  through the API process, which owns the disk.
* **Two fixes, both documented in `render.yaml` and neither enabled by default**: attach a paid disk
  at `/opt/render/project/src/data`, or move the database to PostgreSQL (which still leaves uploaded
  bytes ephemeral, as in Path 1).
* **One worker.** `scripts/start_api.sh` defaults to `WEB_CONCURRENCY=1` because concurrent writers
  on one SQLite file produce `database is locked` rather than throughput. Raise it only together
  with a PostgreSQL `DATABASE_URL`.

---

## Path 3 — Railway, Heroku and other Procfile platforms

The `Procfile` declares two process types:

```
web: sh scripts/start_api.sh
ui:  sh scripts/start_ui.sh
```

A platform routes HTTP to exactly one process type per service, so **each of these platforms needs
two services (Railway) or two apps (Heroku) built from the same repository** — one running the API,
one running the console.

### Railway

1. New Project → Deploy from GitHub repo.
2. The first service becomes the API. Set its start command to `sh scripts/start_api.sh`, or leave
   Railway to use the `Procfile`'s `web` entry. Add a public domain from the service's Settings.
3. Add a second service from the **same** repository. Set its start command to
   `sh scripts/start_ui.sh` and give it its own domain.
4. Set variables exactly as in [Path 2 step 5](#path-2--render-api-and-console-as-two-services),
   substituting the Railway hostnames. Railway injects `$PORT`; both scripts read it.
5. Add a volume to the API service, mounted at `/app/data` (or the equivalent path for your build),
   if you want the database and evidence to survive a redeploy.

### Heroku / Dokku

1. `heroku create` twice — one app for the API, one for the console — both from this repository.
2. On the API app, run the `web` process type. On the console app, scale `web=0 ui=1`.
3. `runtime.txt` selects the Python version. Both apps must be on the same one.
4. Heroku's dyno filesystem is ephemeral **and per-dyno**, so PostgreSQL (`heroku addons:create
   heroku-postgresql`) is effectively mandatory here. Heroku sets `DATABASE_URL` to a
   `postgres://…` URL; SQLAlchemy needs the driver named, so translate it:
   `postgresql+psycopg://…`. Set `DATABASE_URL` explicitly rather than relying on the addon's value.

---

## Path 4 — Docker and docker-compose on a VM

The only path where the deployment is under your control end to end, and therefore the only one
where putting an authenticating proxy in front of it is straightforward.

### One container

```bash
docker build -t llm-it-auditor .

# the audit console (default role)
docker run --rm -p 8501:8501 -v audit-data:/app/data llm-it-auditor

# the API instead
docker run --rm -p 8000:8000 -e APP_ROLE=api -v audit-data:/app/data llm-it-auditor api

# both in one container (convenient for a demo, not the production shape)
docker run --rm -p 8501:8501 -p 8000:8000 -e APP_ROLE=all -v audit-data:/app/data llm-it-auditor all
```

Three things about the image worth knowing before you debug it:

* **`APP_ROLE` must match the command.** The entrypoint takes the role from its argument; the
  health check is a separate process that cannot see those arguments and reads `APP_ROLE`. Set both.
* **Anything that is not `api`, `ui` or `all` is executed verbatim**, so
  `docker run llm-it-auditor python run.py init` and `docker run -it llm-it-auditor sh` work.
* **The image sets no application configuration.** `APP_ROLE` is the only variable baked in, and it
  is not a `Settings` field. That is deliberate: an environment variable beats a `.env` file, so a
  baked-in default would silently override the operator's own configuration.

### Two containers (recommended)

```bash
docker compose up --build      # console on :8501, API docs on :8000/docs
docker compose down            # stop; the named volume keeps the data
docker compose down -v         # stop and delete the database, evidence and reports
```

Needs Compose v2.24 or newer (for `env_file: required: false`). It works on a fresh clone with no
configuration: `.env` is optional and the default provider is the offline mock.

The compose file sets three URL variables on the console, and the distinction matters:

| Variable | Value | Meaning |
|---|---|---|
| `API_BASE_URL` | `http://api:8000` | Where *this container* reaches the API, over Compose's internal DNS. Meaningless in a browser. This is what `app/frontend/api_client.py` calls. |
| `PUBLIC_API_URL` | `http://localhost:8000` | Where a *human* reaches the API. Display and documentation only. |
| `PUBLIC_APP_URL` | `http://localhost:8501` | Where a human reaches the console. Also trusted by CORS. |

Both services mount the same `audit-data` volume, because uploaded evidence and generated reports
are files on disk and both halves must see the same ones. With `USE_API=true` the console reads and
writes them *through the API* rather than touching the database itself, which is what keeps two
processes off one SQLite file.

### Putting it on the internet

Do not publish ports 8000/8501 directly. Terminate TLS and authenticate at a reverse proxy on the
same host, and publish only the proxy. A minimal nginx sketch — **not exercised in this
environment**, so treat it as a starting point rather than a tested configuration:

```nginx
server {
    listen 443 ssl;
    server_name audit.example.edu;
    # ssl_certificate / ssl_certificate_key from your ACME client

    auth_basic           "Research prototype";
    auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd -c /etc/nginx/.htpasswd you

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;   # Streamlit needs the websocket
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

Then set, on the application side:

```bash
PUBLIC_APP_URL=https://audit.example.edu
PUBLIC_API_URL=https://audit.example.edu/api      # only if you also proxy the API
CORS_ALLOW_ORIGINS=https://audit.example.edu
CORS_RESTRICT_TO_CONFIGURED=true
STREAMLIT_CLIENT_SHOW_ERROR_DETAILS=none
```

`scripts/start_api.sh` already passes `--proxy-headers`, so uvicorn trusts `X-Forwarded-Proto` and
generated URLs say `https`. Restrict `FORWARDED_ALLOW_IPS` from its default `*` to the proxy's
address if the container is reachable from anywhere else.

---

## PostgreSQL

The schema is portable by construction — `app/database/models.py` uses no SQLite-specific types,
timestamps are timezone-aware, and constraints are named through the metadata naming convention.
Moving is a configuration change, not a migration.

### Steps

1. **Create the database and a user:**

   ```sql
   CREATE DATABASE itaudit;
   CREATE USER audit WITH PASSWORD 'change-me';
   GRANT ALL PRIVILEGES ON DATABASE itaudit TO audit;
   -- PostgreSQL 15 and later: the database grant alone does NOT allow creating tables,
   -- because CREATE on schema public is no longer granted to PUBLIC. Connect to itaudit
   -- and add:
   --   GRANT ALL ON SCHEMA public TO audit;
   -- (or simply make `audit` the database owner). Without this, init_db() fails with
   -- "permission denied for schema public" on an otherwise correct configuration.
   ```

2. **Install the driver — it is not installed by default.** In `requirements.txt` it is a commented
   line; uncomment it, or install it alongside:

   ```bash
   # locally
   .venv/bin/python -m pip install "psycopg[binary]>=3.1"
   ```

   * **Docker:** uncomment `psycopg[binary]>=3.1` in `requirements.txt` and `docker compose build`.
   * **Render:** append it to the service's `buildCommand`, or uncomment it in `requirements.txt`.
   * **Streamlit Community Cloud:** it must be in `requirements.txt`; that file is the only thing
     the platform installs.

3. **Set the URL. Exactly this form** — the `+psycopg` driver suffix is required, because
   SQLAlchemy defaults to `psycopg2`, which is not installed:

   ```dotenv
   DATABASE_URL=postgresql+psycopg://audit:change-me@localhost:5432/itaudit
   ```

   In `docker-compose.yml`, with the commented `db` service enabled, the host is the service name:

   ```yaml
   DATABASE_URL: postgresql+psycopg://audit:change-me@db:5432/itaudit
   ```

   Set it on **both** the API and the console service if both talk to the database.

4. **Build the schema and seed the control library:**

   ```bash
   .venv/bin/python run.py init
   ```

   or simply start the API — `init_db()` runs at startup and is idempotent.

5. **Now you may raise `WEB_CONCURRENCY`** above 1. PostgreSQL handles concurrent writers; SQLite
   answers them with `database is locked`.

### What `init_db()` actually does, in order

`app/database/base.py::init_db()` runs three steps, and the order matters:

1. **`Base.metadata.create_all`** — creates missing *tables*. It is completely blind to columns: a
   table that already exists is left exactly as it was found.
2. **`app.database.migrations.reconcile_schema()`** — closes that gap. For every mapped table that
   already exists it compares the model's columns against the live columns and issues one
   `ALTER TABLE … ADD COLUMN` per missing column. This is what stops `git pull` from breaking an
   existing `data/audit.db` with `OperationalError: no such column`.
3. **`app.database.views.create_findings_view()`** — (re)creates the `findings` view, which is the
   join of an assessment to its latest human review. `CREATE VIEW IF NOT EXISTS` on SQLite (with a
   staleness check that drops a view whose definition has changed), `CREATE OR REPLACE VIEW`
   elsewhere.

### What the migration helper does **not** handle

`reconcile_schema` is a deliberate lightweight substitute for Alembic, appropriate for a single-user
prototype that ships as a repository someone clones. It is additive only, and it will not:

| Not handled | What happens instead |
|---|---|
| **Column renames** | A rename looks like "one column missing, one column unknown". It adds the new name and leaves the old column in place, holding the data. |
| **Type changes** | An existing column is never inspected, altered or rebuilt. It keeps its original storage type. |
| **Data migrations** | Nothing is backfilled by the helper. A new column is NULL for existing rows unless the model declares a `server_default` (as `EvidenceFile.provenance` does — existing rows land on `SYNTHETIC`). |
| **Constraint changes** | No constraint is added, dropped or modified. A new column carrying a `ForeignKey` or `UniqueConstraint` in the model is added as a plain column without it. |
| **Dropping anything** | A column in the database but not in the models is left alone. Extra columns are harmless; a wrong `DROP` is not recoverable. |
| **Indexes** | A column declared `index=True` is added **without** its index. It costs a scan, never correctness. |
| **Moving data between backends** | There is no SQLite → PostgreSQL data copy. `run.py init` builds an empty schema. |

Verified on a SQLite database made deliberately old (the `provenance` column and its index removed
by hand, one pre-existing row left in place):

```
before: [... 'is_synthetic', 'created_at', 'updated_at']
reconcile_schema returned: ['evidence_files.provenance: added column VARCHAR(32)']
after : [... 'is_synthetic', 'created_at', 'updated_at', 'provenance']
existing row backfilled to: SYNTHETIC
indexes present: ['ix_evidence_files_sha256', 'ix_evidence_files_filename', 'ix_evidence_files_project_id']
second run (should be empty): []
```

Note the two documented consequences in that output: the index did **not** come back, and the added
column sits at the end of the table rather than in model order. Neither affects correctness.

**Alembic is the upgrade path** if this prototype ever needs renames, type changes or reversible
history. The metadata already declares a naming convention so autogeneration produces reversible
migrations. At that point `migrations.py` should be deleted rather than extended.

### Honest caveat

**The PostgreSQL path has not been exercised in this environment.** No server was available and
neither `psycopg` nor `psycopg2` is installed in the virtualenv (checked). The portability claim
rests on the schema's construction, on the dialect-compiled DDL, and on tests that compile the DDL
and the view SELECT against the PostgreSQL dialect — not on a live run. Every result reported by
this project was produced on SQLite. The first real PostgreSQL start should be treated as the first
test of that path, and the two places to watch are `VARCHAR(n)` lengths (SQLite ignores them,
PostgreSQL enforces them) and the `CREATE OR REPLACE VIEW` fallback in `app/database/views.py`.

---

## Custom domains and TLS

1. **Point DNS at the platform.** A `CNAME` from `audit.example.edu` to the platform hostname
   (Render, Railway and Streamlit Community Cloud each document their own target), or an `A` record
   to your VM.
2. **Add the domain in the platform's dashboard** so it provisions a certificate, or run an ACME
   client on your own VM. Nothing in this application terminates TLS.
3. **Tell the application its own address.** These are display and policy values; nothing is baked
   into code:

   ```bash
   PUBLIC_APP_URL=https://audit.example.edu
   PUBLIC_API_URL=https://api.audit.example.edu
   CORS_ALLOW_ORIGINS=https://audit.example.edu
   CORS_RESTRICT_TO_CONFIGURED=true
   ```

   Trailing slashes are stripped and whitespace trimmed before use, so `https://audit.example.edu/`
   and `https://audit.example.edu` behave identically. That normalisation exists because browsers
   send an `Origin` header with no trailing slash and Starlette compares it as a literal string — a
   misconfiguration that would otherwise fail invisibly.
4. **Check the proxy passes WebSocket upgrades** if the console is behind one. Streamlit's session
   is a WebSocket; without `Upgrade`/`Connection` headers the page loads and then hangs.
   Note that the documented "fix" of setting `enableCORS = false` is not one: Streamlit ignores that
   value while XSRF protection is on and logs that it is overriding it. A proxy serving the app from
   a single origin needs neither change.
5. **Confirm the API agrees.** `GET /` reports `public_api_url`, `console_url`, and the effective
   CORS mode and origin list. If what it prints is not what you configured, the process did not
   restart with the new environment.

---

## Environment-variable reference for deployed instances

Only the variables that matter for a deployment are listed. `.env.example` documents the full
application set with inline commentary, and `app/config.py` is the specification.

### Platform and process (read by the start scripts and the image, not by `Settings`)

| Variable | Default | What it does |
|---|---|---|
| `PORT` | — | Injected by Render/Railway/Heroku. Both start scripts read it first. |
| `API_PORT` | `8000` | Used when `$PORT` is absent. |
| `STREAMLIT_PORT` | `8501` | Used when `$PORT` is absent. |
| `HOST` | `0.0.0.0` | What the start scripts bind. Only `run.py api` uses `API_HOST` instead. |
| `WEB_CONCURRENCY` | `1` | uvicorn workers. **Raise only with PostgreSQL.** |
| `FORWARDED_ALLOW_IPS` | `*` | Which upstreams uvicorn trusts for `X-Forwarded-*`. Narrow it to your proxy. |
| `APP_ROLE` | `ui` | Docker image only: tells the health check which service this container runs. Must match the command. |
| `PYTHON` | auto | Interpreter override for the start scripts; they prefer `.venv/bin/python` when it exists. |

### Deployment identity and browser policy

| Variable | Default | What it does |
|---|---|---|
| `PUBLIC_APP_URL` | unset | The console's public URL. Named in the OpenAPI description and at `GET /`, **and trusted by CORS**. |
| `PUBLIC_API_URL` | unset | The API's public URL. Populates the OpenAPI `servers` block so `/docs` "Try it out" works. Deliberately *not* the console's URL — that host serves none of these paths. |
| `CORS_ALLOW_ORIGINS` | `""` | Comma-separated origins. Normalised (trimmed, trailing `/` removed). |
| `CORS_RESTRICT_TO_CONFIGURED` | `false` | `false`: loopback on any port **plus** the configured origins. `true`: configured origins and `PUBLIC_APP_URL` **only**; loopback is no longer trusted. **Set `true` on anything internet-facing.** |
| `API_BASE_URL` | `http://127.0.0.1:8000` | Where the console calls the API when `USE_API` is on. Also appears as "Local development" in the OpenAPI `servers` block. |
| `USE_API` | `false` | Routes the console through HTTP instead of the in-process service layer. **See the trap below.** |
| `ENVIRONMENT` | `development` | Reported at `/health` and on the Settings page. Set `production` on a deployment. |

`allow_credentials` is `False` in every branch, the code never emits `"*"` on its own, and a
production policy with nothing configured allows **no** origin and logs a warning — rather than
falling back to permitting everything, which would silently widen a policy someone deliberately
narrowed.

### Model provider

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` \| `claude` \| `openai`. `mock` is offline and deterministic. |
| `ANTHROPIC_API_KEY` | unset | Secret. Absent → the factory falls back to the mock, loudly, in the log, at `/health` and on the Settings page. |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_EFFORT` | `high` | `low`…`max`. An unrecognised value is clamped to `high` rather than sent. |
| `ANTHROPIC_THINKING` | `true` | Adaptive thinking. Depth is controlled by effort, not by sampling — the provider never sends `temperature`. |
| `ANTHROPIC_BASE_URL` | unset | Only for Anthropic-compatible gateways. **See the trap below.** |
| `LLM_API_KEY` / `LLM_BASE_URL` | unset | The OpenAI-compatible provider. `LLM_BASE_URL` alone is enough for a local server. |
| `MOCK_HALLUCINATION_RATE` | `0.0` | Deliberate fabrication rate for the mock. Never anything but `0.0` outside a controlled experiment. |
| `FORCE_HUMAN_REVIEW` | `true` | The safety rail that keeps an AI draft from being presented as an audit conclusion. Pin it `true` explicitly on a deployment. |

### Storage

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///<repo>/data/audit.db` | `postgresql+psycopg://user:pass@host:5432/itaudit` to move. |
| `MAX_UPLOAD_MB` | `50` | Keep in step with `maxUploadSize` in `.streamlit/config.toml`, or the two halves disagree about what is acceptable. |
| `DB_ECHO` | `false` | Logs every SQL statement. On a deployment that means evidence text in the logs — leave it off. |
| `UPLOAD_DIR` / `REPORT_DIR` / `DATA_DIR` | under `<repo>/data` | Point at a mounted volume if the platform gives you one. |

### Streamlit

| Variable | Deployed value | Notes |
|---|---|---|
| `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` | `none` | `.streamlit/config.toml` ships `"full"`, which is right locally and wrong in public: a traceback prints filesystem paths into the browser. |
| `STREAMLIT_BROWSER_GATHER_USAGE_STATS` | `false` | Already `false` in the config file; set explicitly on platforms that ignore it. |

### Three traps that are easy to hit

1. **`USE_API` must be a real environment variable — putting it in `.env` does nothing.**
   `app/frontend/data_access.use_api()` reads `os.environ` directly (it selects a *transport*, not
   an application setting), and `pydantic-settings` loads `.env` into `Settings` without exporting
   anything to the process environment. Verified: with `USE_API=true` in an env file,
   `Settings.use_api` is `True` while `data_access.use_api()` is still `False`. `render.yaml`,
   `docker-compose.yml` and Streamlit Cloud secrets all set it as a real variable, so all three are
   correct. Locally, export it: `USE_API=true .venv/bin/python run.py ui`.
2. **The environment always beats `.env`.** `pydantic-settings` reads the process environment first.
   This is not theoretical: while writing this document, a shell with `ANTHROPIC_BASE_URL` exported
   for unrelated tooling silently became the effective base URL for the application. If the Settings
   page shows a URL your `.env` does not name, run `env | grep ANTHROPIC` in the terminal you
   started from.
3. **`.env` is read from the repository root and nowhere else.** `app/config.py` pins
   `env_file=<repo>/.env`. A `.env` in your home directory, or beside a subdirectory you happened to
   `cd` into, is not read. Platforms have no `.env` at all — use their environment or secrets UI.

---

## Verifying a deployment

The commands below were run against this application started through `scripts/start_api.sh` with
`PORT=8123`, `CORS_RESTRICT_TO_CONFIGURED=true` and `CORS_ALLOW_ORIGINS=https://console.example.edu`.
Substitute your own host.

```bash
# 1. Is it up, and which deployment did I reach?
curl -s https://your-api/ | python3 -m json.tool
```

Expect `public_api_url`, `console_url`, and a `cors` block. Under a restricted policy:

```json
"cors": {"mode": "restricted", "allowed_origins": ["https://console.example.edu"], "loopback_allowed": false}
```

```bash
# 2. Does the database answer, and which provider will serve the next assessment?
curl -s https://your-api/health | python3 -m json.tool
```

Expect `"status": "ok"`, `"database_ok": true`, `"authentication": "none"`, and a `provider` block.
`"fell_back_to_mock": true` means a real provider was configured but could not be constructed —
results produced now measure the pipeline, not a model. **No secret appears in this payload**: the
Anthropic key is reported as `"api_key_configured": true/false` only.

```bash
# 3. Is the browser policy really what I configured?
curl -s -i -X OPTIONS https://your-api/api/v1/controls \
  -H "Origin: https://console.example.edu" -H "Access-Control-Request-Method: GET" | head -8
```

Expect `HTTP/1.1 200` with `access-control-allow-origin` echoing your origin. Repeat with an origin
you did **not** configure and expect `HTTP/1.1 400 Bad Request` (`Disallowed CORS origin`) — which
is what the restricted mode is for.

```bash
# 4. Does /docs point at the right host?
curl -s https://your-api/openapi.json | python3 -c "import json,sys; print(json.load(sys.stdin)['servers'])"
```

Expect your `PUBLIC_API_URL` as `"Deployed API"`. The console's URL is deliberately **not** here —
it serves none of these paths, and listing it would make "Try it out" fire requests at a host that
404s. It appears in the API description and at `GET /` instead.

```bash
# 5. Streamlit liveness
curl -s https://your-console/_stcore/health     # expect: ok
```

Finally, open the console and read the sidebar provider badge. That badge is the difference between
"a language model produced this" and "a deterministic rule engine produced this", and it is the
first thing to check on any deployment you intend to demonstrate.

---

## What was verified for this document, and what was not

Stated plainly, because a deployment guide that implies more testing than happened is worse than no
guide.

**Executed here, on this machine:**

* `.venv/bin/python -m pytest tests -q` → `761 passed` before and after this document was written.
* `sh scripts/start_api.sh` with `PORT=8123` against a temporary SQLite database: `/`, `/health`,
  `/openapi.json` and both CORS preflight cases, all as quoted above.
* `cors_policy()` in development, restricted-with-origins and restricted-with-nothing modes, plus
  trailing-slash normalisation.
* `Settings.redacted_dict()`, `provider_summary()` and `provider_health()` inspected for secret
  leakage — two real gaps found (the Anthropic key and the database DSN) and recorded in
  [`SECURITY.md`](SECURITY.md#known-gaps).
* `reconcile_schema()` against a database made deliberately old, twice, with the output quoted
  above; `create_findings_view()` on the same database.
* A full case-study import into a temporary SQLite database — project, six evidence files at
  `HISTORICAL_PUBLIC` provenance, a mode-C assessment and a `SELECT * FROM findings`.
* Streamlit's secrets-to-environment behaviour, against `streamlit==1.50.0`: top-level string, int
  and float secrets become environment variables; values nested in a `[table]` do not.
* `USE_API` being read from `os.environ` and not from `.env`.
* Which interface Streamlit binds locally (`*:8501`), and that `STREAMLIT_SERVER_ADDRESS=127.0.0.1`
  restricts it to loopback — both checked with `lsof` against a running server.

**Not executed here:**

* **Docker.** `docker` is not installed on this machine, so the `Dockerfile` has never been built
  and `docker compose up` has never been run. The pip install inside the image, the base image
  resolving those wheels, file ownership after `COPY --chown`, and the `HEALTHCHECK` firing in a
  live container are all unproven. Treat your first `docker build` as the first test of them.
* **Any live platform.** No Streamlit Community Cloud, Render, Railway or Heroku deployment was
  performed. Platform UIs change; where this document describes a form field or a settings page,
  check it against the platform's current documentation.
* **PostgreSQL**, for the reasons given in that section.
* **The nginx configuration** in Path 4, which is a sketch.
* **`runtime.txt` / `PYTHON_VERSION = 3.12.11`.** A real 3.12 release, but which patch versions a
  platform currently offers was not confirmed. If a build rejects it, bump the patch in
  `runtime.txt` and `render.yaml` **together** — they must stay in step.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Console loads, every page shows a connection error | `USE_API=true` with a wrong or unreachable `API_BASE_URL`, or the API service is asleep. Check `curl $API_BASE_URL/health` from the same network. |
| Console works locally but not deployed, browser console shows a CORS error | The console's origin is not in `CORS_ALLOW_ORIGINS` (or `PUBLIC_APP_URL`) on the **API** service. Check `GET /` on the API and compare `allowed_origins` with the origin the browser is actually sending. |
| `GET /` shows `"allowed_origins": []` with `"mode": "restricted"` | Restricted mode with nothing configured refuses every cross-origin request, by design. Set `CORS_ALLOW_ORIGINS` or `PUBLIC_APP_URL` and restart. |
| Everything disappeared after a redeploy | Ephemeral filesystem. Expected on Streamlit Cloud and on Render's free plan. Attach a disk, or move to PostgreSQL, or press the demo-data button again. |
| `OperationalError: no such column: …` | A database that predates a model column, where `init_db()` did not run. Start the API once, or run `.venv/bin/python run.py init`. |
| `database is locked` | Two writers on one SQLite file. Use one service per database, keep `WEB_CONCURRENCY=1`, or move to PostgreSQL. |
| `ModuleNotFoundError: psycopg` | The PostgreSQL driver is not installed. See [PostgreSQL](#postgresql) step 2. |
| Provider badge says Mock although a key is set | The key is not reaching the process, or the `anthropic` package is missing. `GET /health` → `provider.fallback_reason` names which. On Streamlit Cloud, check the secret is a **top-level** key. |
| The Settings page shows a base URL your `.env` does not name | Something in the shell or platform exported `ANTHROPIC_BASE_URL`. The environment beats `.env`. |
| Streamlit page loads then hangs, no interaction | The reverse proxy is not passing the WebSocket upgrade. See [Custom domains and TLS](#custom-domains-and-tls) step 4. Do **not** try to fix it by setting `enableCORS = false`. |
| A traceback with filesystem paths appears in the browser | `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` is not `none` on that service. |
| Streamlit auto-page navigation looks wrong when a page URL is opened directly | Opening e.g. `/settings` without first loading `/` renders that page without the shell, because Streamlit's `pages/` discovery and the `st.navigation` shell collide. Reached through the app's own navigation it is correct. Pre-existing application structure, not a deployment fault. |
| Upload rejected in the UI but accepted by the API (or vice versa) | `MAX_UPLOAD_MB` and `maxUploadSize` in `.streamlit/config.toml` disagree. |
