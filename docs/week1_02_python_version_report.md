# Week 1 — Doc 2: Python Version Standardization Report
**Repo:** `ai-voice-agent-dev` | **Date:** 2026-06-17 | **Author:** Cowork Audit

---

## 1. Summary of Version Signals Found

| Location | Python Version | Source |
|---|---|---|
| `Dockerfile` | **3.11** (`python:3.11-slim`) | Line 1 |
| `requirements.txt` comment | **3.13** (implied) | Comment: "Windows has no pre-built wheel for Python 3.13" |
| `docker-compose.yml` | Inherits Dockerfile → **3.11** | Implicit |
| `.github/workflows/ci.yml` | Not read (excluded) | Unknown — verify |
| Repo dev machine (inferred) | **3.13** (Windows) | `requirements.txt` comment |
| AWS EC2 production | **Unknown** | Not documented |

### Verdict: Mismatch
- **Dev machine** appears to be running Python 3.13 on Windows.
- **Docker container** is locked to Python 3.11.
- **EC2 production** Python version is undocumented.
- There is no `.python-version` file, `pyproject.toml`, or `setup.cfg` pinning the Python version.

---

## 2. Port Mismatch (Critical for Local Dev)

This is documented here because it directly affects running the stack locally.

| Source | Port | Notes |
|---|---|---|
| `run_server.py` default | **8001** | `int(os.getenv("PORT", 8001))` — line 19 |
| `Dockerfile` EXPOSE | **8000** | Line 41 |
| `docker-compose.yml` PORT env | **8000** | `PORT=8000` |
| `docker-compose.yml` host mapping | **8000:8000** | Host port 8000 maps to container 8000 |
| `README.md` Quick Start ngrok | **8085** | `ngrok http 8085` — **wrong** |
| `Dockerfile` healthcheck | **8000** | `curl http://localhost:8000/healthz` |
| `telephony/server.py` `__main__` default | **8000** | `int(os.getenv("PORT", 8000))` |

### Verdict: Three-Way Mismatch

- Running `python run_server.py` **without** Docker → binds to **port 8001** (unless PORT env var is set).
- Running inside Docker (docker-compose) → binds to **port 8000** (PORT=8000 is set by docker-compose).
- README says ngrok port 8085 — this is **wrong for both scenarios**.

**Correct ngrok commands:**
- Bare Python local: `ngrok http 8001`
- Docker local: `ngrok http 8000`

**Discrepancy root cause:** `run_server.py` changed its default from 8000 to 8001 to "avoid conflicts" (see comment on line 18) but the README was not updated. `telephony/server.py` still has 8000 as its `__main__` default, creating further confusion.

---

## 3. fasttext-wheel: Platform-Specific Dependency

`fasttext-wheel` is the language detection model used by `contracts/language_guard.py`.

| Environment | Status | Notes |
|---|---|---|
| Windows + Python 3.13 | ❌ No wheel available | Comment in `requirements.txt` line 15–19 |
| Windows + Python 3.11 | ❓ Unclear — no wheel documented | Likely same issue |
| Linux (Docker / EC2) | ✅ Available | Dockerfile installs it with `numpy<2.0` pin |
| macOS | ❓ Not documented | Check manually |

**Key env var:** `FASTTEXT_MODEL_PATH=/app/models/lid.176.ftz`
- Inside Docker: automatically downloaded at build time (~126 MB).
- Windows local dev: fasttext is commented out in `requirements.txt` → language detection falls back to `langdetect` + `lingua` only.

**Consequence:** Language governance behavior differs between local dev (no fasttext) and Docker/EC2 (fasttext). This is an **intentional workaround** but it is not documented anywhere other than in a comment.

---

## 4. `requirements.txt` — No Version Pins

```
fastapi
uvicorn
python-dotenv
websockets>=12.0
httpx
google-generativeai
asyncpg>=0.29.0
pgvector>=0.3.0
pydantic
langdetect
lingua-language-detector
redis
boto3>=1.34.0
tenacity>=8.2.0
```

- **Only 4 packages have version constraints** (`websockets`, `asyncpg`, `pgvector`, `boto3`, `tenacity`).
- The rest (`fastapi`, `uvicorn`, `pydantic`, `httpx`, etc.) are unpinned — any version installs.
- This means two different devs running `pip install -r requirements.txt` on different dates may get different dependency trees.
- Dockerfile adds `numpy<2.0` separately for fasttext compatibility — this is not in `requirements.txt` and will not apply on bare installs.

**Risks:**
- `pydantic` v1 vs v2 has breaking API changes — not pinned.
- `google-generativeai` major version changes can break Brain initialization.
- `fastapi` v0.10x deprecates several patterns used in the code (`@app.on_event("startup")` is deprecated in FastAPI 0.93+).

---

## 5. `@app.on_event("startup")` Deprecation

`telephony/server.py` line 41 uses the deprecated `@app.on_event("startup")` decorator. FastAPI 0.93+ recommends `lifespan` context managers instead. This doesn't break today but will generate deprecation warnings and break on FastAPI 1.0.

---

## 6. Recommended Standardization Actions (for human approval)

1. **Pin Python to 3.11** across all environments — add `.python-version` file with `3.11` and document in README.
2. **Pin all requirements** — run `pip freeze > requirements.lock` inside the Docker container to get a deterministic lockfile.
3. **Fix the port** — decide on one port (recommendation: **8000**), update `run_server.py` default from 8001 → 8000, update README ngrok command.
4. **Document fasttext status** — add a `## Platform Notes` section to README explaining fasttext behavior on Windows vs Docker.
5. **Add `PYTHONPATH=.` to local dev notes** — `run_server.py` manually appends root to `sys.path`; this should be explicit in the dev setup guide.
6. **Check CI workflow** — read `.github/workflows/ci.yml` to confirm it tests against Python 3.11 (not 3.13 or 3.10).

---

## 7. File Notes

### `Dockerfile`
```
File: Dockerfile
Importance: P0
Purpose: Defines the Docker image for production and local docker-compose use.
Key settings: python:3.11-slim, installs fasttext-wheel + lid.176.ftz at build time, EXPOSE 8000
Inputs: requirements.txt, .env (via docker-compose env_file)
Outputs: Docker image cila-ai-agent:latest
Runtime role: Production and local docker-compose container
Risks: EXPOSE 8000 vs run_server.py default 8001 mismatch when run outside Docker
Questions for human: Should Python be pinned to 3.11 everywhere? Should lid.176.ftz be cached/volume-mounted instead of downloaded at build time?
```

### `requirements.txt`
```
File: requirements.txt
Importance: P0
Purpose: Python dependency list for pip install.
Key functions: Defines runtime dependencies; fasttext-wheel commented out for Windows.
Inputs: None
Outputs: Installed packages
Runtime role: Used by both Dockerfile (pip install) and local venv setup
Risks: No version pins = non-deterministic builds; numpy<2.0 only in Dockerfile, not here
Questions for human: Should we add a requirements.lock? Should numpy<2.0 be added here?
```
