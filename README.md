# AutoScout-Engine

The "engine" to [AutoScout-Lab](https://github.com/sathiya-22/AutoScout-Lab)'s
"fuel." AutoScout-Lab generates a new agentic-AI prototype repo every day and
gives each one small daily increments. This repo runs a separate, *deeper*
pass: it researches what's currently happening around a repo's specific
problem, then advances that repo toward it — a real upgrade, not just a
small tweak.

## Why a separate repo, and why Groq

- **Separate from AutoScout-Lab** so the two systems don't compete for the
  same budget or step on each other's commits.
- **Powered by Groq** instead of Gemini — its free tier renews every day
  (unlike a one-time credit pool), so this can run indefinitely without ever
  needing a manual top-up.
- **Optimized for a small model**: `llama-3.1-8b-instant` gets the largest
  daily quota Groq offers (14,400 requests/day), at the cost of a tight
  6,000-tokens-per-minute limit. That TPM limit — not the request count — is
  what actually constrains this engine, so the repo context and output size
  fed to the model per cycle are deliberately small (see the caps in
  `scripts/advance_repo.py`) to comfortably fit within it.
- **Runs daily** — since Groq's quota resets every day, there's no scarcity
  reason to throttle the cadence the way NVIDIA's one-time pool would have
  required.

## How a cycle works

1. Sync the local repo registry against GitHub's actual repo list (adds any
   AutoScout-generated repo not yet tracked, drops any deleted).
2. Pick the ONE repo most overdue for review — never-reviewed repos first
   (oldest created), then oldest-last-reviewed.
3. Fetch that repo's current files (capped to fit the model's token budget).
4. Research signals specific to that repo's problem — targeted Hacker News
   and GitHub searches (free, no Groq tokens spent) — so the model isn't
   reasoning from training-data knowledge alone.
5. Ask Groq's `llama-3.1-8b-instant` to combine that research with its own
   knowledge and propose ONE substantial advancement.
6. Commit the change straight to that repo's `main`, and log it in that
   repo's own `ADVANCEMENT_LOG.md`.

## Setup

This repo needs two secrets (add via Settings → Secrets and variables →
Actions → New repository secret — never paste them anywhere else):

- `SCOUT_PAT` — a GitHub PAT with `repo` scope (same one AutoScout-Lab uses)
- `GROQ_API_KEY` — a free key from [console.groq.com](https://console.groq.com)
  (sign up, go to API Keys, create one — no credit card needed)

## State

[`state/registry.jsonl`](state/registry.jsonl) tracks every repo this engine
knows about and when it last reviewed each one. It's independent from
AutoScout-Lab's own `repos/registry.jsonl` — same discovery method (GitHub
repos with the description "Auto-generated AI prototype by AutoScout"), kept
separate on purpose so neither system depends on the other's internals.
