# Ternary — Industry Architecture V1

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Elephant-logic/Ternary)

**"Profitable backtest" is not "industry-grade."** Industry-grade means the system
*cannot silently cheat, leak future information, lose state, expose credentials,
send an uncontrolled order, or produce a result you cannot reproduce.* This V1 is
built around that definition, not around strategy returns.

## The pipeline

```
 market data ─▶ deterministic signal engine ─▶ OOS-qualified universe
      │                                              │
      │                                              ▼
      │                                    AI risk review (governed)
      │                                              │
      │                                              ▼
      │                                     portfolio optimiser
      │                                              │
      │                                              ▼
      │                          ┌──── INDEPENDENT RISK GATEWAY ────┐
      │                          │  max size · exposure · daily loss │
      │                          │  drawdown · order value · turnover│
      │                          │  concentration · duplicate-order  │
      │                          │  stale-data · KILL SWITCH         │
      │                          └───────────────┬──────────────────┘
      │                                          ▼
      │                              paper / exchange execution
      │                                   (execution simulator)
      ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │  TAMPER-EVIDENT EVENT LOG + monitoring + reconciliation          │
 │  (append-only, hash-chained; every signal/decision/order/fill)   │
 └─────────────────────────────────────────────────────────────────┘
```

Nothing sends an order directly. The strategy *proposes*; the gateway *disposes*;
every step is *recorded* before and after it happens.

## Modules

| Dir | Responsibility | Status |
|-----|----------------|--------|
| `core/` | Deterministic signal engine + accounting invariants. Pure, frozen. | **real + tested** |
| `sim/` | Execution simulator: spread, slippage, partial fills, tick/lot size, latency, rejects, stale quotes, downtime, liquidity caps. | **real + tested** |
| `oos/` | Walk-forward harness: anchored + rolling, multiple train/test lengths, untouched holdout, stress periods, parameter perturbation, Monte-Carlo sequencing. | **real + tested** |
| `gateway/` | Independent risk gateway. Validates intents, enforces limits, then signs an exact short-lived execution authorization. | **real + tested** |
| `eventlog/` | Append-only, hash-chained event log. Tamper-evident; full reconstruction. | **real + tested** |
| `ai/` | Governed AI risk model. Frozen timestamped inputs → strict schema (TAKE/REDUCE/DELAY/VETO). Every call logged with model+prompt version, input hash. Auto-disables if it stops adding OOS value. | **real interface; network call is an adapter you key** |
| `execution/` | Broker adapters require a valid one-time gateway authorization bound to exact order fields; direct bypass fails closed. | **paper real + tested; live disabled pending testnet** |
| `data/` | Deterministic historical store + live feed adapter. | **real store; live is a keyed adapter** |
| `config/` | DEV / PAPER / LIVE profiles — separate credentials, DBs, deployments. | **real separation** |
| `tests/` | Authoritative CI harness: unit + integration + property tests. | **real** |

## The separation that matters (items 1 & 9)

- **Research is immutable & deterministic.** `core/` and the historical store are
  pure functions of their inputs. The AI cannot alter historical outcomes, and
  nothing tunes itself on the window it is later scored on. `oos/` freezes each
  train choice and applies it to the *next untouched* window.
- **DEV / PAPER / LIVE are fully separated** (`config/profiles.py`): different
  credentials, different event-log databases, different broker adapters. A LIVE
  order can only be produced by the LIVE profile passing through the gateway.

## What is deliberately NOT in the browser (item 6)

Credentials and order submission never live in client JS. The AI call, exchange
credentials, and live order path sit behind this backend. The browser (if used at
all) becomes a **read-only monitor** over the event log.

## Honest status

Real and covered by tests in this build: the execution simulator, the risk
gateway, the tamper-evident event log, signed external checkpoint support, the OOS harness, the accounting core, execution authorization, authenticated control writes, and the
CI test suite. Scaffolded with correct interfaces (you supply keys / a running
service): the OpenAI network call, the live exchange adapter, and the live data
feed. The point of V1 is that those are the *only* things left to wire — the
controls around them already exist and are tested.

## Deploy on Render

This repository is Render Blueprint-ready. `render.yaml` is at the repository root.

1. Click **Deploy to Render** above, or in Render choose **New → Blueprint** and select `Elephant-logic/Ternary`.
2. When prompted, set `TERN_CONTROL_TOKEN` to a long random secret. Do not commit it to GitHub.
3. Approve the Blueprint. Render installs dependencies, runs the full test suite, starts the FastAPI service, and checks `/health`.
4. The service boots in **PAPER** mode. The browser dashboard remains read-only; write endpoints require `Authorization: Bearer <TERN_CONTROL_TOKEN>`.

The Blueprint uses a paid `starter` web service because Ternary currently persists state and its SQLite audit/event log on a 1 GB Render persistent disk. Do not switch it to Render's free web-service plan unless you also move persistent state to a supported external datastore.

## Run

```bash
python -m tests.run_all         # the authoritative CI harness (58 tests)
python -m core.demo             # the FULL governed pipeline, many cycles, end-to-end
python -m sim.demo              # execution simulator + gateway + audit trail
python -m oos.demo              # walk-forward + holdout + Monte-Carlo qualify gate
```

The `core/` orchestrator wires the whole pipeline into one governed cycle:
market data → deterministic signal → OOS-qualified universe → AI risk review →
optimiser → **independent gateway** → paper execution, with reconciliation each
cycle. A shared *simulated clock* drives staleness checks (replay uses market
time, not wall-clock), every broker call requires a one-time cryptographic gateway
authorization, the AI can only shrink or veto, and every stage is recorded to the
hash-chained log. 58/58 tests are green and deterministic across runs.

## Hardening boundary (current build)

The broker now refuses any execution request that lacks a short-lived, one-time
HMAC authorization issued by the gateway and bound to the exact client id, symbol,
side, quantity, reference price and quote timestamp. This prevents accidental or
in-process direct broker bypass through the public broker interface and is covered
by adversarial tests for missing, altered, expired and replayed authorization.

This is **not yet a separate trust domain**: gateway and broker still run in the
same Python service. Before unrestricted LIVE capital, split execution into a
separate authenticated service and keep the authorization secret out of the
strategy/orchestrator process. The authorization protocol is deliberately isolated
in `execution/authorization.py` to make that split straightforward.

The SQLite log is correctly described as **tamper-evident**, not physically
immutable. `EventLog.checkpoint()` can sign and append the current chain head to a
separate checkpoint destination. For production, point that destination at storage
in a different trust domain with retention/WORM controls.

Mutating control-plane endpoints fail closed unless `TERN_CONTROL_TOKEN` is set
and supplied as `Authorization: Bearer ...`. The browser dashboard is read-only.
