# Ternary — Industry Architecture V1

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Elephant-logic/Ternary)

**"Profitable backtest" is not "industry-grade."** Ternary is built around deterministic research, governed risk, controlled execution, reconstruction, and auditability rather than treating backtest returns as proof of production readiness.

## Pipeline

```text
market data → deterministic signal → OOS-qualified universe → governed AI risk review
            → portfolio optimiser → independent risk gateway → paper/exchange execution
                                    ↓
                     tamper-evident event log + reconciliation
```

The strategy proposes orders; the gateway validates and authorizes them; the broker refuses execution without the matching one-time authorization. Every stage is recorded.

## Safety architecture

- Deterministic research and walk-forward/OOS qualification.
- AI can TAKE, REDUCE, DELAY, or VETO; it cannot enlarge a mechanical position.
- Independent risk gateway: size, exposure, drawdown, daily loss, turnover, concentration, stale-data and duplicate-order protection, plus kill switch.
- Broker execution requires a short-lived one-time authorization bound to the exact order fields.
- DEV / PAPER / LIVE profiles are separated and LIVE refuses to boot without credentials.
- Browser dashboard is read-only; mutating control-plane endpoints require `TERN_CONTROL_TOKEN`.
- Event history is hash-chained and tamper-evident, with reconstruction from BALANCE/FILL events.

## Free Render deployment — no SQL

The root `render.yaml` uses Render's **free** web-service plan and does **not** attach a paid persistent disk.

Ternary supports a plain-JSON persistence path:

- `TERN_STATE=/tmp/ternary/state.json` — application settings/goals.
- `TERN_EVENTLOG=/tmp/ternary/eventlog.json` — hash-chained event journal. A `.json` event-log path automatically selects the JSON backend instead of SQLite.
- JSON writes are atomic, and a new worker reconstructs cash/positions from the journal on boot.

Render Free files are ephemeral, so local JSON alone cannot reliably survive a Render redeploy/restart. For durable free-tier recovery, Ternary can save one versioned JSON snapshot to GitHub through the GitHub Contents API.

The Blueprint asks for only one extra secret:

- `GITHUB_STATE_TOKEN` — a fine-grained GitHub token with **Contents: Read and write** access to the state repository.

The remaining GitHub state settings are already supplied by `render.yaml`:

- `GITHUB_STATE_REPO=Elephant-logic/Ternary`
- `GITHUB_STATE_BRANCH=state`
- `GITHUB_STATE_PATH=runtime/ternary-state.json`
- `TERN_REMOTE_SAVE_MIN_SECONDS=300`

The dedicated `state` branch prevents runtime-state commits from redeploying the Render service. Ternary restores the latest snapshot **before** `AppState` and the worker boot, then continues using local JSON during the running instance. `/status` exposes whether remote JSON persistence is enabled, whether the instance restored from GitHub, the last-save time, and any persistence error.

Remote saves are rate-limited to at most one every five minutes by default, and unchanged snapshots are not committed again. No Render/GitHub/API secrets are included in the snapshot because persistence serializes application state and event history, not process environment variables.

If `GITHUB_STATE_TOKEN` is absent, Ternary still runs and saves local JSON, but that state is not durable across Render filesystem resets.

### Deploy

1. Click **Deploy to Render** above, or choose **New → Blueprint** in Render and select `Elephant-logic/Ternary`.
2. Set `TERN_CONTROL_TOKEN` to a long random secret.
3. Create a fine-grained GitHub personal access token restricted to the state repository with **Contents: Read and write** permission, then paste it into Render as `GITHUB_STATE_TOKEN`.
4. Approve the Blueprint. The build overlays the current persistence changes, installs dependencies, runs the authoritative tests, starts FastAPI/Uvicorn, and health-checks `/health`.
5. The service starts in **PAPER** mode and automatically restores the most recent GitHub JSON snapshot when one exists.

**Privacy note:** `Elephant-logic/Ternary` is currently public, so a snapshot stored on its `state` branch is also public. For private runtime history, point `GITHUB_STATE_REPO` at a separate private repository instead.

**Important:** Render Free web services can sleep while idle. This profile is intended for PAPER/testing and a hosted control surface, not continuous unattended LIVE execution.

## Tests

```bash
python -m tests.run_all
```

The suite includes the original safety/integration/property checks plus explicit no-SQL persistence tests proving that:

1. the JSON event journal can be reopened and reconstruct positions/cash without SQLite; and
2. a remote JSON snapshot can restore state and event history after the local files are deleted.

Current result: **60/60 passing**.

## Current hardening boundary

The broker/gateway authorization protection is real and tested, but gateway and broker still live inside one Python service. Before unrestricted LIVE capital, execution and exchange credentials should move into a separate authenticated trust domain.

Likewise, SQLite/JSON hash chaining is **tamper-evident**, not physically immutable. Production audit-head checkpoints should be anchored outside the trading service/trust domain.
