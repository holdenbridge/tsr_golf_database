# TSR Golf Database (Standalone)

This folder contains a standalone FastAPI app with local JSON data storage.

## Requirements

- Python 3.10+ recommended

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

From the project root:

```bash
uvicorn web.main:app --host 127.0.0.1 --port 8002 --reload
```

Open: <http://127.0.0.1:8002/>

## Google OAuth admin lock (public insights, private writes)

Write actions are admin-only. Configure these environment variables before deploy:

- `GOOGLE_CLIENT_ID`: Google OAuth web client id
- `GOOGLE_CLIENT_SECRET`: Google OAuth web client secret
- `ADMIN_EMAIL`: your exact Google account email (only this user can write)
- `SESSION_SECRET`: long random secret for signed session cookies
- `SESSION_COOKIE_SECURE`: set to `true` in production HTTPS (recommended), `false` for local HTTP

### OAuth callback URL

In Google Cloud Console, add an authorized redirect URI:

- Local: `http://127.0.0.1:8002/auth/callback`
- Hosted: `https://<your-domain>/auth/callback`

### Local run with env vars

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export ADMIN_EMAIL="you@example.com"
export SESSION_SECRET="replace-with-long-random-string"
export SESSION_COOKIE_SECURE=false
uvicorn web.main:app --host 127.0.0.1 --port 8002 --reload
```

Anonymous users can still load `View Player Insights`; write endpoints return `401/403` unless logged in as `ADMIN_EMAIL`.

## Run behind a URL prefix (optional)

If this app is mounted behind a proxy path prefix (example: `/golf`), run with:

```bash
uvicorn web.main:app --host 127.0.0.1 --port 8002 --reload --root-path /golf
```

The frontend uses `root_path` to build API URLs correctly in that mode.

## Simple free hosting path

Use a managed platform free tier (for example Render/Fly/Railway) with one web service:

1. Connect this repo/folder and deploy `uvicorn web.main:app --host 0.0.0.0 --port $PORT`.
2. Set the OAuth/session/admin env vars above in the platform dashboard.
3. Add your deployed URL callback (`https://.../auth/callback`) in Google Cloud Console.
4. Keep secrets in host env vars only; do not commit them.
