# AutoScout-Engine

The "engine" to [AutoScout-Lab](https://github.com/sathiya-22/AutoScout-Lab)'s
"fuel." AutoScout-Lab generates a new agentic-AI prototype repo every day and
gives each one small daily increments. This repo runs a separate, slower,
*deeper* pass: it researches what's currently state-of-the-art for a repo's
specific problem, then advances that repo toward it — a real upgrade, not
just a small tweak.

## Why a separate repo, and why slower

- **Separate from AutoScout-Lab** so the two systems don't compete for the
  same budget or step on each other's commits.
- **Powered by NVIDIA NIM** instead of Gemini — its free tier gives 40
  requests/minute, plenty for a single deep call per cycle.
- **Runs every ~3 days (Mondays and Thursdays)**, not daily. NVIDIA's free
  tier is a **one-time allocation of 1,000 inference credits that never
  renews** — unlike Gemini's free tier, which resets daily. At roughly one
  call per cycle and two cycles a week, the credit pool lasts years even as
  AutoScout-Lab keeps generating one new repo every single day.

## How a cycle works

1. Sync the local repo registry against GitHub's actual repo list (adds any
   AutoScout-generated repo not yet tracked, drops any deleted).
2. Pick the ONE repo most overdue for review — never-reviewed repos first
   (oldest created), then oldest-last-reviewed.
3. Fetch that repo's current files.
4. Research signals specific to that repo's problem — targeted Hacker News
   and GitHub searches (free, no NVIDIA credits spent) — so the model isn't
   reasoning from training-data knowledge alone.
5. Ask NVIDIA's `llama-3.1-nemotron-70b-instruct` (tuned for agentic/tool-use
   tasks) to combine the research with its own knowledge and propose ONE
   substantial advancement — grounded in what's actually current, not
   invented from scratch.
6. Commit the change straight to that repo's `main`, and log it in that
   repo's own `ADVANCEMENT_LOG.md`.

## Setup

This repo needs two secrets (add via Settings → Secrets and variables →
Actions → New repository secret — never paste them anywhere else):

- `SCOUT_PAT` — a GitHub PAT with `repo` scope (same one AutoScout-Lab uses)
- `NVIDIA_API_KEY` — a free key from [build.nvidia.com](https://build.nvidia.com)
  (sign up, open any model, click "Get API Key")

## State

[`state/registry.jsonl`](state/registry.jsonl) tracks every repo this engine
knows about and when it last reviewed each one. It's independent from
AutoScout-Lab's own `repos/registry.jsonl` — same discovery method (GitHub
repos with the description "Auto-generated AI prototype by AutoScout"), kept
separate on purpose so neither system depends on the other's internals.
