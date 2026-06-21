# Week 1 — Doc 1: Git Baseline Audit
**Repo:** `ai-voice-agent-dev` | **Date:** 2026-06-17 | **Author:** Cowork Audit

---

## 1. Current Git State

| Item | Value |
|---|---|
| Remote | `https://github.com/rahulmula-dev/aiVoiceAgent.git` |
| Commits | 2 — `272e5b1` (Baseline: inherited CILA codebase) · `9dfbf09` (Update README.md) |
| Modified (unstaged) | `.gitignore` |
| Untracked | `-H`, `-d`, `logs/`, `recordings/` |

The repo has never had a proper `git tag` or branch strategy established. All work is on `main`.

---

## 2. ⚠️ P0 — Secrets Already Committed to Git History

The following files containing real credentials are **tracked in git** (committed in `272e5b1`):

### `phone1.txt` / `twilio_nums.txt`
- **Content:** Live Twilio SIDs (`PNe626f6d9628d06e85a8081058f1e9da5`) and real phone numbers (`+18567165450`, `+18563936660`).
- **Risk:** Anyone with repo access can see the live Twilio phone numbers. If the repo is ever made public or cloned, these are exposed.
- **Action required (human):** Rotate the exposed Twilio numbers / SIDs. Remove files from git history using `git filter-branch` or BFG Repo Cleaner. Add to `.gitignore`.

### `CILA_Code_Review.docx`
- **Content:** Internal code review document. No credentials observed, but could contain design details you may not want public.
- **Risk:** Low–Medium. Adds unnecessary binary bloat to git history.
- **Action required (human):** Decide whether to remove or keep. If removed, use `git filter-branch`/BFG.

### `kb_version.json`
- **Content:** KB version metadata. No secrets seen.
- **Risk:** Low. Probably fine to track, but confirm it contains nothing sensitive.

---

## 3. `.gitignore` Coverage Assessment

Current `.gitignore` (6 lines):
```
.env
__pycache__/
*.pyc
.venv/
venv/
*.log
```

### Missing entries:

| Missing Pattern | Why |
|---|---|
| `phone1.txt` | Contains live Twilio SIDs (already in history — must clean history first) |
| `twilio_nums.txt` | Same |
| `logs/` | Log dir is untracked, but only `*.log` is ignored — `.jsonl`, `.json`, `.wav` in logs/ are not covered |
| `recordings/` | `recordings/119cab75_20260616_165530.wav` is untracked but not ignored |
| `*.wav` | WAV recordings would leak on push if `recordings/` ever gets added |
| `*.docx` | Prevents accidental commit of review docs |
| `-H` and `-d` | Two files in root with names `-H` and `-d` (appear to be artifacts of a bad shell command — `find -H -d ...`). Not ignored. |
| `debug_*.py` / `verify_*.py` | These are dev scripts you excluded from study — consider ignoring |
| `test_*.py` (root-level) | `test_dg.py`, `test_local_rag.py`, etc. in root are committed but not test suite files |
| `kb_version.json` | Confirm if this should be committed or generated |

**Recommended `.gitignore` additions:**
```gitignore
# Telephony data
phone1.txt
twilio_nums.txt
*.wav

# Logs
logs/
recordings/

# Review artifacts
*.docx

# Shell command artifacts
-H
-d

# Local test/debug scripts
debug_*.py
verify_*.py
```

---

## 4. Secrets Exposure Checklist

| Secret | Location | Committed? | Gitignored? | Risk |
|---|---|---|---|---|
| `GEMINI_API_KEY` | `.env` | No | Yes (`.env`) | ✅ Safe |
| `DEEPGRAM_API_KEY` | `.env` | No | Yes | ✅ Safe |
| `CRM_API_KEY` | `.env` | No | Yes | ✅ Safe |
| `AWS_ACCESS_KEY_ID` | `.env` | No | Yes | ✅ Safe |
| `AWS_SECRET_ACCESS_KEY` | `.env` | No | Yes | ✅ Safe |
| `POSTGRES_PASSWORD` | docker-compose (via `${POSTGRES_PASSWORD}`) | No | Requires `.env` gitignored | ✅ Safe (env var) |
| Twilio Phone SIDs | `phone1.txt` / `twilio_nums.txt` | **YES** | No | 🔴 EXPOSED |
| Twilio Phone Numbers | `phone1.txt` / `twilio_nums.txt` | **YES** | No | 🔴 EXPOSED |

---

## 5. Recommended Week 1 Git Hygiene Steps

These are for human approval before execution:

1. **Remove secrets from history** — `git filter-branch --force --index-filter 'git rm --cached --ignore-unmatch phone1.txt twilio_nums.txt CILA_Code_Review.docx' --prune-empty --tag-name-filter cat -- --all` (or use BFG)
2. **Force-push** after history rewrite (coordinate with all team members who have clones)
3. **Rotate Twilio phone SIDs** as a precaution
4. **Update `.gitignore`** with the additions above
5. **Remove `-H` and `-d` files** from root — `rm -- -H -d`
6. **Create a `v0.0.0-baseline` tag** on current HEAD to mark the Week 1 starting point
7. **Set up branch protection on `main`** via GitHub (require PR review before merge)

---

## 6. File Inventory Notes

| File | Importance | Notes |
|---|---|---|
| `README.md` | P2 | Port in Quick Start (8085) is wrong — see Python version report |
| `requirements.txt` | P0 | No version pins on most packages — risk of non-deterministic builds |
| `.gitignore` | P1 | Incomplete — update before any push |
| `Dockerfile` | P0 | Python 3.11-slim, installs fasttext-wheel |
| `docker-compose.yml` | P0 | Three services: postgres (pgvector/pg16), redis, cila-ai-agent |
| `.env` | P0 | Gitignored correctly. `PG_DATABASE_URL` and `REDIS_URL` are commented out — must uncomment for local |
| `phone1.txt` | P0 | 🔴 **LIVE TWILIO DATA IN GIT — ROTATE + REMOVE** |
| `twilio_nums.txt` | P0 | 🔴 Same as above |
| `CILA_Code_Review.docx` | P3 | Binary in git — remove from history |
| `kb_version.json` | P3 | Tracks KB version — confirm intentional |
| `-H`, `-d` (root) | P4 | Shell command artifacts — delete and add to `.gitignore` |
