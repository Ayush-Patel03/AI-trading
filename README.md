# AI-trading

The engine behind a scheduled US-equity scan desk and a paper portfolio manager.

**This system places no orders.** It runs in paper mode by explicit decision: it decides in
full, logs every decision with its reason, and simulates fills against paper books. See
[`docs/PM.md`](docs/PM.md) section 1 — that lock is not a technical limitation and is not
something a contributor, a user request or a future agent may lift.

## What is here, and what is not

This repo is **code and doctrine**. It is public and carries no identifiers: no account
numbers, no positions, no keys, no board URLs.

Live state — the paper books, the decision journals, the archived scans and boards, the
health record — lives in a private claude.ai project and never appears here.

## Layout

| Path | What |
|---|---|
| `engine/scanner.py` | scoring: trend, momentum, fundamentals, catalysts, gated on the regime |
| `engine/technicals.py` | Wilder ATR(14), RSI, moving averages from daily bars |
| `engine/portfolio.py` | sizing and every per-book risk rule |
| `engine/pm.py` | the Portfolio Manager: fills, exits, rebalancing, entries, house caps |
| `engine/watch.py` | the read-only extended-session watch; no write path to a book, by construction |
| `engine/archive.py` | run identity, book fingerprints, when a run earns a frozen board |
| `engine/config.py` | reads identifiers from the private `engine-config.json` |
| `engine/history.py`, `validate.py` | the score trail and the weekly validation |
| `engine/sentiment.py` | retail sentiment parsers (ApeWisdom, StockTwits, Reddit) |
| `engine/render*.py` | the HTML boards |
| `docs/` | the doctrine. `PM.md` governs; read it before changing anything in `engine/` |

## How it runs

Scheduled tasks clone this repo and execute it:

```bash
git clone --depth 1 --branch production https://github.com/Ayush-Patel03/AI-trading "$SCAN_DIR/repo"
```

`main` is where work lands. `production` is what runs. Promotion is a deliberate
fast-forward — never automatic, so a push cannot change what the next slot does.

Every run records the commit it ran (`engine_sha`) in its journal entry, so any past
decision is attributable to an exact version of this code.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite runs offline against frozen fixtures. CI runs it on every push and pull request.
A red `main` is not promoted.
