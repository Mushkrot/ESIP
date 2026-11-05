# Local setup

The default configuration runs the complete reference workspace on a laptop. It does not require a
Google Cloud project, Zendesk account, Firebase project, API key, or email provider.

## Requirements

- Python 3.12
- Node.js 20.19+ or 22.12+
- npm 10

Local verification uses macOS; CI uses Linux, Python 3.12, and Node.js 22.

## Start the API

From the repository root:

```bash
cp .env.example .env
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Confirm the service is ready:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

The OpenAPI browser is available at `http://127.0.0.1:8000/docs`.

## Start the web workspace

Open a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:3000`. Select **Open reference workspace** on the sign-in page. The frontend
proxies `/api` requests to the backend on port `8000`.

## Start the embedded application

Build the Zendesk application, then serve its generated assets from a third terminal:

```bash
cd zendesk-app
npm ci
npm run build
python3 -m http.server 4567 --bind 127.0.0.1 --directory assets
```

Open `http://127.0.0.1:4567/reference.html`. The preview page fixes the application frame at the same
`320 x 720` viewport used by the sidebar workflow. **Details & Chat** opens the larger modal view.

## Runtime profiles

The backend and frontend default to the local profile. Their explicit settings are:

```dotenv
# .env
RUNTIME_PROFILE=reference

# frontend/.env.local
VITE_RUNTIME_PROFILE=reference
```

The frontend setting is already the default; copy `frontend/.env.example` to `frontend/.env.local`
only when you need to change it. Empty provider variables are valid in the reference profile.

The connected profile is an integration starting point, not a second quick-start path. Review its
[SDK, authentication, and deployment limits](ESIP_OVERVIEW.md#connected-profile-limits) before supplying
provider configuration. The reference profile is for loopback-only local evaluation.

## Run verification

With the Python environment active:

```bash
python -m pip install pytest
python -m pytest -q -p no:cacheprovider backend
```

Check both web clients:

```bash
(cd frontend && npm ci && npm run lint && npm run build)
(cd zendesk-app && npm ci && npm run type-check && npm run build)
```

The backend suite includes reference HTTP contracts and AI adapter tests using local HTTP responses.
Neither requires cloud credentials. See the [verification baseline](README.md#verification-baseline)
for the current results.

## Reset local state

Reference mutations are kept in the backend process. Restart the API to restore the initial users,
settings, drafts, schedules, and reports. Browser conversation tabs are stored in local storage; clear
site data for `127.0.0.1:3000` when you want a completely fresh browser session.

## Common startup problems

**The web page opens but data requests fail:** confirm the API responds on port `8000` and start the
frontend with `npm run dev`, which installs the local proxy.

**The sign-in page asks for Google:** verify both runtime variables are `reference`, then restart the
API and Vite.

**The embedded page is blank:** run `npm run build` before starting the static server. The build writes
`app.js`, `modal.js`, and `app.css` into `zendesk-app/assets`.

**A connected route reports missing credentials:** switch back to `reference` or provide the named
adapter configuration. Do not place credential files in the repository.
