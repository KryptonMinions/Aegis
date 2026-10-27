# KSP Datathon 2026 

Karnataka State Police — Intelligent Conversational AI & Crime Analytics
Platform. 

## Directory structure

**Application services**

```
backend/              FastAPI service — auth, /ask orchestrator agent, voice
                       intake, PDF export. See backend/README.md.
frontend/              Next.js app (dashboard, cases, ask, admin). See
                       frontend/README.md.
embeddings-service/    Standalone microservice serving multilingual-e5-base
                       embeddings for RAG retrieval. See
                       embeddings-service/README.md.
pdf-service/           Standalone microservice rendering HTML to PDF with
                       headless Chromium (Playwright). See "PDF service" below.
```

**Data & schema**

```
db/                    Hand-authored SQL migrations + curated reference CSVs.
                       See db/README.md.
dataset/               Raw, read-only source data (boundaries, station maps,
                       BNS/IPC PDFs) the seed pipeline builds from. See
                       dataset/README.md.
scripts/seed/          Seeding pipeline: normalizes dataset/ → loads db/
                       migrations → populates Supabase with reference data,
                       golden demo threads, Kannada translations, and
                       embeddings. Order and details in db/README.md.
```

**Docs & references**

```
steering-docs/         Architecture and design specs (auth, orchestrator,
                       semantic layer, data schema, demo scenarios, etc.) —
                       the source of truth for how the system is supposed to
                       behave.
References/            Datathon challenge brief PDFs (data discovery, demo
                       script, evaluation criteria, semantic layer, trust
                       design).
```

## Running locally

### 1. Backend (FastAPI)

```bash
cd backend
cp .env.example .env   # fill in Supabase + LLM credentials — see backend/README.md
uvicorn app.main:app --reload --workers 1
```

Runs at http://localhost:8000. `--workers 1` is required — the agent's
thread store is in-process (`steering-docs/ORCHESTRATOR_STEERING.md` O-13).

Set `ASK_ENGINE=agent` in `backend/.env` to run the real orchestrator against
live data (needs the embeddings service above and working LLM credentials);
`fixture` (the default) serves canned demo responses instead. Point
`EMBEDDINGS_BASE_URL` at `http://localhost:8081/v1` when running the agent
engine locally.

> If `uvicorn` fails with a "bad interpreter" error, the committed `.venv`'s
> console scripts may have a stale shebang from an earlier repo location.
> Work around it with `python -m uvicorn app.main:app --reload --workers 1`,
> or rebuild the venv.

### 2. Frontend (Next.js)

```bash
cd frontend
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm install   # first time only
npm run dev
```

Runs at http://localhost:3000.

See `backend/README.md`, `frontend/README.md`, and
`embeddings-service/README.md` for full setup (Supabase migrations, demo
user provisioning, mock-vs-real Ask service toggle).

### Demo credentials

For evaluators — accounts seeded by `backend/scripts/demo_users.json` via
`backend/scripts/provision_users.py`:

| Username         | Password      | Role                   | Officer ID   |
| ---------------- | ------------- | ---------------------- | ------------ |
| `io.demo`         | `I0_ER`       | investigating_officer  | KSP-23417    |
| `supervisor.demo` | `$upervis0r`  | supervisor              | KSP-70068    |
| `analyst.demo`    | `An@lyst`     | analyst                 | KSP-87128    |
| `admin.demo`      | `@dmin`       | admin                   | —            |

### Database

The backend and agent expect a Supabase Postgres instance with the schema in
`db/migrations/` applied and seeded via `scripts/seed/` (reference data,
golden demo threads, Kannada translations, and embeddings). See
`db/README.md` for the migration list and full seeding order, and
`dataset/README.md` for what raw source data feeds into it.

## Demo access and rate limiting

There are no passwords: the login page shows four role tiles (Investigating
Officer, Supervisor, Crime Analyst, Admin). Clicking one calls
`POST /auth/demo-login`, which signs in the seeded Supabase demo user for that
role and returns a normal Supabase JWT, so role gating, the audit log and RLS
work exactly as before. Enable it with `PASSWORDLESS_LOGIN_ENABLED=true`; it is
off by default, and because anyone can pick any role it should only run where
all data is synthetic.

`/ask`, `/ask/stream` and `/auth/demo-login` are rate limited per client IP
(`ASK_RATE_LIMIT_PER_MINUTE`, `ASK_RATE_LIMIT_PER_HOUR`,
`LOGIN_RATE_LIMIT_PER_MINUTE`; 0 disables), returning 429 with `Retry-After`.
Counters are per Cloud Run instance, so the deploy workflow also caps
`--max-instances`.

## PDF service

`/export/pdf` turns a chat session into a PDF. The backend builds the HTML
(`backend/app/pdf_export.py`) and posts it to `pdf-service/`, a separate
FastAPI app that prints it with headless Chromium and returns the PDF. It
authenticates with a static bearer key (`PDF_SERVICE_API_KEY`), like the
embeddings service.

- **Why Chromium:** output matches what a browser renders, including Kannada
  text (Noto fonts are installed in the image) and table layout.
- **Why a separate service:** Chromium adds several hundred MB to the image
  and needs about 1 GB of RAM per render. Keeping it out of the backend avoids
  bloating `/ask` instances and stops a browser crash from affecting them. It
  mirrors how `embeddings-service` isolates torch.
- **Alternative considered:** WeasyPrint in-process is far lighter and needs no
  extra service, but renders modern CSS less faithfully. The export HTML is
  simple enough that it could be a reasonable swap if the cost of running this
  service ever outweighs the fidelity.

## Deployment

Everything runs on Google Cloud in `asia-south1` (Mumbai), apart from Supabase
(Postgres/Auth), Sarvam (speech-to-text) and the Gemini API, which are external.

| Component | Runs on | Deployed by |
|---|---|---|
| `backend` | Cloud Run (`ksp-backend`, min 1 instance) | `.github/workflows/deploy-backend.yml` |
| `embeddings-service` | Cloud Run (`ksp-embeddings`, 4 GiB, model baked into the image) | `deploy-embeddings.yml` |
| `pdf-service` | Cloud Run (`ksp-pdf`, scales to zero) | `deploy-pdf-service.yml` |
| `frontend` (static export) | Firebase Hosting | `deploy-frontend.yml` |
| Chat thread memory | Firestore (`ask_threads`, TTL on `expires_at`) | created once, see the runbook |
| Secrets | Secret Manager, mounted as Cloud Run env vars | created once, see the runbook |

The three Cloud Run workflows share `cloud-run-deploy.yml`. GitHub Actions
authenticates to GCP with Workload Identity Federation, so there are no
long-lived keys in the repo. Settings come from GitHub repo variables and
Secret Manager, not from committed files.

One-time project setup (APIs, service accounts, Workload Identity Federation,
secrets, Firestore TTL, Firebase, first deploy order and smoke tests) is in
[GCP_SETUP_RUNBOOK.md](GCP_SETUP_RUNBOOK.md).
