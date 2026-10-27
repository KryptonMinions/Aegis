# GCP setup runbook

One-time setup for the Cloud Run + Firebase Hosting deployment. Do it in order;
after that, pushes to `main` deploy through GitHub Actions. Commands assume a
shell with `gcloud`, `firebase` and `gh` installed and logged in.

```bash
export PROJECT_ID=ksp-datathon            # pick a globally unique id
export REGION=asia-south1
export AR_REPO=ksp
export GITHUB_REPO=<owner>/<repo>
```

## 1. Project, billing, budget

```bash
gcloud projects create "$PROJECT_ID"
gcloud billing projects link "$PROJECT_ID" --billing-account=<BILLING_ACCOUNT_ID>
gcloud config set project "$PROJECT_ID"
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
```

Set a **budget alert** now (Console > Billing > Budgets & alerts). Two
always-warm Cloud Run instances are the main fixed cost, roughly $15–40/month.

## 2. Enable APIs

```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com firestore.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com firebase.googleapis.com firebasehosting.googleapis.com
# Only if you call Gemini through Vertex AI instead of the public API:
# gcloud services enable aiplatform.googleapis.com
```

## 3. Artifact Registry

```bash
gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION"
```

## 4. Service accounts

```bash
gcloud iam service-accounts create ksp-runtime
gcloud iam service-accounts create ksp-deployer
RUNTIME_SA=ksp-runtime@$PROJECT_ID.iam.gserviceaccount.com
DEPLOY_SA=ksp-deployer@$PROJECT_ID.iam.gserviceaccount.com

# Runtime identity of the Cloud Run services
for role in roles/secretmanager.secretAccessor roles/datastore.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME_SA" --role="$role"
done
# (add roles/aiplatform.user if you use Vertex AI)

# Identity GitHub Actions deploys as
for role in roles/run.admin roles/artifactregistry.writer roles/firebasehosting.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOY_SA" --role="$role"
done
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:$DEPLOY_SA" --role=roles/iam.serviceAccountUser
```

## 5. Workload Identity Federation (GitHub -> GCP, no JSON keys)

```bash
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='$GITHUB_REPO'"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$GITHUB_REPO"
```

## 6. Secrets

Names must match `deploy-backend.yml`, `deploy-embeddings.yml` and
`deploy-pdf-service.yml`:

```bash
for name in SUPABASE_URL SUPABASE_PUBLISHABLE_KEY SUPABASE_SECRET_KEY SARVAM_API_KEY \
            LLM_PROFILE_FAST_API_KEY LLM_PROFILE_SMART_API_KEY; do
  read -rsp "$name: " v; echo
  printf %s "$v" | gcloud secrets create "$name" --data-file=-
done
# Service-to-service keys: generate random values
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create EMBEDDINGS_API_KEY --data-file=-
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create PDF_SERVICE_API_KEY --data-file=-
```

## 7. Firestore (thread memory)

```bash
gcloud firestore databases create --location="$REGION" --type=firestore-native
gcloud firestore fields ttls update expires_at --collection-group=ask_threads --enable-ttl
```

## 8. Firebase Hosting

```bash
firebase projects:addfirebase "$PROJECT_ID"
firebase hosting:sites:list --project "$PROJECT_ID"   # default site is $PROJECT_ID.web.app
```

## 9. GitHub repo variables

```bash
PROVIDER=projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-provider
gh variable set GCP_PROJECT_ID   --body "$PROJECT_ID"
gh variable set GCP_REGION       --body "$REGION"
gh variable set GCP_AR_REPO      --body "$AR_REPO"
gh variable set GCP_WIF_PROVIDER --body "$PROVIDER"
gh variable set GCP_DEPLOY_SA    --body "$DEPLOY_SA"
gh variable set GCP_RUNTIME_SA   --body "$RUNTIME_SA"
gh variable set FRONTEND_ORIGIN  --body "https://$PROJECT_ID.web.app"
# LLM profiles (non-secret parts)
gh variable set LLM_PROFILE_FAST_BASE_URL  --body "https://generativelanguage.googleapis.com/v1beta/openai/"
gh variable set LLM_PROFILE_FAST_MODEL     --body "<fast model id from backend/.env.example>"
gh variable set LLM_PROFILE_SMART_BASE_URL --body "https://generativelanguage.googleapis.com/v1beta/openai/"
gh variable set LLM_PROFILE_SMART_MODEL    --body "<smart model id from backend/.env.example>"
```

For India-only data residency, point the LLM base URLs at Vertex AI in
`asia-south1` instead of the public Gemini API.

## 10. First deploy (order matters)

The backend needs the other two services' URLs, so deploy those first.

1. Run **Deploy Embeddings Service** and **Deploy PDF Service** from the Actions
   tab (`workflow_dispatch`). The first embeddings build is slow (torch plus the
   model download).
2. Copy their URLs from the workflow logs or
   `gcloud run services describe <name> --region $REGION --format='value(status.url)'`:
   ```bash
   gh variable set EMBEDDINGS_BASE_URL  --body "<ksp-embeddings url>"
   gh variable set PDF_SERVICE_BASE_URL --body "<ksp-pdf url>"
   ```
3. Run **Deploy Backend**, then copy the `ksp-backend` URL:
   ```bash
   gh variable set NEXT_PUBLIC_API_URL --body "<ksp-backend url>"
   ```
4. Run **Deploy Frontend**.

## 11. Smoke tests

- `curl <url>/healthz` for the backend, embeddings and pdf services.
- Embeddings output must be 768-dim (matches the seeded `multilingual-e5-base`
  vectors).
- Click each of the four role tiles on the login page: each should sign in
  with no password (this needs `PASSWORDLESS_LOGIN_ENABLED=true`, which the
  backend workflow sets, and the four seeded demo users in Supabase). Then
  `/ask` with streaming.
- Rate limit: send more than 10 `/ask` requests within a minute from one
  machine; the 11th should return 429 `rate_limited` with a `Retry-After`
  header. Limits are `ASK_RATE_LIMIT_PER_MINUTE`/`_PER_HOUR` (defaults 10 and
  100) and are per Cloud Run instance, so the real cap is multiplied by
  `max_instances` (default 3).
- Two-turn conversation on one thread: the second answer should use context from
  the first, proving the Firestore store works. Check an `ask_threads` document
  exists with `expires_at` set.
- Export a PDF that includes Kannada text and open it, to check glyphs, tables
  and layout.
- Mic button, seal and navigation in the deployed frontend (no `/app` prefix).

## 12. Cutover and cleanup

- Confirm all four pipelines are green on `main`.
- If you add a custom domain to Firebase Hosting, add its origin to
  `FRONTEND_ORIGIN` (comma-separated, no wildcards) and re-run Deploy Backend.
- Leave the old Catalyst project untouched for about a week as a rollback.
  Rollback is "git revert the migration commits and redeploy on a paid Catalyst
  plan", not instant.
- Delete your local `backend/.env` Catalyst variables and
  `frontend/.env.production`, which still points at the old backend.
