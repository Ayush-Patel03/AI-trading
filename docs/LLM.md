# LLM memos — the extractor pattern, and why the model is never in the order path

*P-07, added 2026-09-10. Engine: `engine/memo.py`, hooked from `scanner.py`, resolved by
`history.py --resolve-memos`. Tests: `tests/test_memo.py`.*

## 0. The statement that governs everything below

**The LLM is never in the sizing or order path.** It writes a memo. The engine validates the
memo, keeps five numeric fields from it as *logged research features*, and logs every
probability the memo makes against what the price then did. No pillar reads a memo field,
`pm.py` never sees one, and the calibration log cannot flip that on: even when the log says
the extractor beats the base rate, the flag it clears is `advisory`, which only tells the
Saturday validation reader that the feature has earned a look in `ic.py --by-feature`. A
score is the same number with or without a memo — `tests/test_memo.py` pins that against
the golden.

## 1. The evidence this is built on (research synthesis, Appendix G)

| Finding | What it forces |
|---|---|
| LLM headline sentiment has a real but small 1–2 day effect that decays with adoption | A logged feature at the 5-session horizon, never a pillar weight; re-measured continuously, not once |
| Anonymising the company before scoring improves the read (Glasserman & Lin) | The extractor receives `COMPANY_A` / `EXEC_1`, never the ticker or name; the engine maps the `symbol_hash` back |
| Any evaluation on dates before the model's training cutoff is uninterpretable (Lopez-Lira, Tang & Zhu) | Every memo carries `model.cutoff_date`; a memo whose `as_of` precedes it is rejected, and the calibration log is live-only by construction |
| Zero-shot LLM probabilities are poorly calibrated — Brier ≈ 0.21 vs a crowd's 0.15 | `p_up_5d` is optional, is logged against the realised outcome, and the feature stays `advisory: true` until Brier beats the base-rate Brier over ≥ 100 resolved calls |

## 2. Where it sits in a scan slot

```
fetch news (get_equity_news, 1 symbol/call)            session
  └─ stage news_payload.json  (raw, keyed by symbol)     session
       └─ anonymise → extractor prompt → memos/<SYM>.json   session, one sub-call per symbol
            └─ scanner.py                                    engine (box)
                 ├─ memo.validate(memo, payload)             reject with reasons → meta.memo_rejections
                 ├─ memo.to_features(memo) → row.features.llm_*
                 └─ archive/calibration.jsonl  ← one pending row per p_up_5d
                      └─ history.py --resolve-memos --bars    Saturday validation: fill outcomes, print Brier
```

Memo-writing happens **after the news fetch and before `scanner.py`**, inside the session's
collection budget (see `docs/runner/prompts/README.md`). It is optional at every slot: no
`memos/` directory means the run is byte-identical to a run before P-07 existed.

## 3. The files

### `news_payload.json` — what the session staged, raw

```json
{"as_of": "2026-09-10T12:30:00Z", "salt": "2026-09-10-midday",
 "symbols": {
   "NVDA": {"symbol_hash": "9f3c…", "company_name": "NVIDIA Corp",
            "aliases": ["Nvidia"], "executives": ["Jensen Huang", "Colette Kress"],
            "items": [{"url": "…", "published_at": "2026-09-10T11:02:00Z",
                       "source": "…", "title": "…", "text": "…full body…"}]}}}
```

`symbol_hash = memo.symbol_hash(SYMBOL, salt)` — sixteen hex chars of a salted SHA-256. The
salt is per run, so the id cannot be looked up across days. `company_name`, `aliases` and
`executives` are what `anonymise()` strips; they never reach the extractor.

### `memos/<SYMBOL>.json` — one object per (symbol, slot), schema 1

| Field | Type | Rule |
|---|---|---|
| `schema` | `1` | exactly |
| `as_of` | ISO-8601 | the slot's timestamp |
| `symbol_hash` | string | must equal the payload's; never the ticker |
| `sources` | `[{url, published_at, source}]` | every `published_at ≤ as_of − 5 min` (`latency_min`, default 5) |
| `event_type` | enum | `earnings, guidance, mna, regulatory, product, management, litigation, short_report, macro, other, none` |
| `direction` | enum | `positive, negative, mixed, none` |
| `magnitude_bucket` | enum | `none, small, medium, large` |
| `confidence` | float | in [0, 1] |
| `p_up_5d` | float or null | in [0, 1]; optional; logged, never scored |
| `quote` | string ≤ 300 chars | a verbatim substring of one source text (raw or anonymised) |
| `numerals` | list | every number the memo cites; each must appear in the payload text — **copied, never computed** |
| `model` | `{id, cutoff_date, prompt_hash, temperature}` | `temperature == 0`; `as_of ≥ cutoff_date` |

An `event_type: none` memo may have empty `sources`, `quote` and `numerals`. Any other
event type needs at least one source and a non-empty quote.

### `archive/calibration.jsonl` — one row per probability call

```
{as_of, symbol, slot, run_id, p_up_5d, direction, event_type, confidence, model_id,
 prompt_hash, realised_up_5d: null, fwd_ret_5d: null, brier: null, resolved_on: null}
```

Appended by `scanner.py` when a valid memo carries `p_up_5d` (once per `(as_of, symbol)`).
`history.py --resolve-memos --archive <dir> --bars bars.json` fills `realised_up_5d` (close
five sessions after `as_of` above the last close on or before `as_of`), `fwd_ret_5d` and
`brier = (p − y)²`, and prints the report.

## 4. Validation rules, and what each one catches

| Rule | Rejection reason | What it catches |
|---|---|---|
| `published_at ≤ as_of − latency_min` | `…is later than as_of - 5 min` | A memo citing news the slot could not have had; a session that ran late and read the next headline |
| every numeral in the payload | `numeral '61' does not appear in the payload` | A number the model computed ("61% growth" from two revenue figures) or invented |
| quote is a substring | `quote is not a verbatim substring of any source text` | Paraphrase presented as evidence |
| `temperature == 0` | `model.temperature 0.7 != 0` | A sampled memo that cannot be reproduced from its `prompt_hash` |
| `as_of ≥ cutoff_date` | `…before model.cutoff_date … uninterpretable` | A replay the model may remember |
| enums, bounds, schema | `event_type 'rumour' not in […]` | Free text leaking into a numeric feature |
| `symbol_hash` matches | `symbol_hash does not match the payload` | A memo written for a different payload, or one that carries the ticker |

Every reason is collected, not just the first; `meta.memo_rejections[SYMBOL]` carries the
list and `meta.data_warnings` carries one summary line. A rejected memo attaches nothing
(the `llm_*` keys are `null` on that row) and logs no calibration row. Nothing in this path
can fail the scan: a missing payload, a corrupt memo file or an exception inside `memo.py`
becomes a warning and the board renders.

Numerals match on **value**, not on formatting: `54.5`, `"$54.5"` and `"54.5"` are the same
copied number; `12` and `"12%"` likewise. The article's `$54.5 billion` does *not* license a
memo numeral of `54500000000` — that is a computation.

## 5. The extractor prompt (verbatim)

The session runs this once per symbol as a **separate sub-call with no other context** —
not in the collection conversation, which knows the ticker. Substitute `{as_of}`,
`{symbol_hash}`, `{model_id}`, `{cutoff_date}` and the anonymised items. `prompt_hash` is
`memo.prompt_hash()` of this text with the placeholders filled, so a changed prompt is a
different experiment in the calibration log.

```
You are a news extractor. You will be given news items about a company identified only as
COMPANY_A. People are identified as EXEC_1, EXEC_2, ... You do not know, and must not guess,
which company this is.

Return ONE JSON object and nothing else — no prose, no markdown fence.

{"schema": 1,
 "as_of": "{as_of}",
 "symbol_hash": "{symbol_hash}",
 "sources": [{"url": "...", "published_at": "...", "source": "..."}],
 "event_type": one of "earnings","guidance","mna","regulatory","product","management",
               "litigation","short_report","macro","other","none",
 "direction": one of "positive","negative","mixed","none",
 "magnitude_bucket": one of "none","small","medium","large",
 "confidence": a number from 0 to 1,
 "p_up_5d": a number from 0 to 1, or null if you decline to make a call,
 "quote": "one verbatim passage of at most 300 characters, copied exactly from one item",
 "numerals": [every number you relied on, copied exactly as it appears in the text],
 "model": {"id": "{model_id}", "cutoff_date": "{cutoff_date}",
           "prompt_hash": "{prompt_hash}", "temperature": 0}}

Rules:
1. Copy numbers verbatim. Never compute, convert, round or combine them. If a figure you
   want is not printed in an item, do not cite it.
2. `sources` lists only items you actually used, with their published_at copied exactly.
   Never cite an item published after {as_of}.
3. `quote` must be an exact substring of one item. Do not paraphrase, trim words mid-way,
   or join two passages.
4. If the items contain no company-specific news — market wrap, unrelated mention,
   nothing dated within the last two sessions — return event_type "none", direction "none",
   magnitude_bucket "none", empty sources, empty quote, empty numerals, p_up_5d null.
5. `p_up_5d` is your probability that this company's shares close higher five trading
   sessions after {as_of} than at {as_of}. Give it only when the news itself moves that
   probability; otherwise null. It will be scored against the outcome.
6. `confidence` is how sure you are of event_type and direction, not of the price.
7. Do not name or guess the company or the people. Do not use any knowledge of what
   happened after {as_of}.

Items:
{anonymised items, each as: [n] source | published_at | url \n title \n text}
```

Token budget: the anonymised items are capped at the eight deep-dive names × the two most
recent items × ~1,500 characters of body each, so a slot spends at most ~12 k input tokens
on extraction and returns eight objects of ~300 tokens. A slot that is late (`minutes_late`
> 15 at the point of the news fetch) skips memo-writing altogether rather than trade
collection time for it.

## 6. The model card

Every memo carries the card in `model`: `id` (the exact model identifier the sub-call ran
on), `cutoff_date` (its published training cutoff), `prompt_hash` (of the filled prompt),
`temperature` (must be 0). The calibration row keeps `model_id` and `prompt_hash`, so the
report can be cut by model and by prompt version. A new model or a changed prompt starts a
new calibration count; the ≥ 100 rule applies per (model, prompt), not to the union.

## 7. The calibration loop

```bash
# Saturday validation, after the bar fetch for the followed set:
python3 history.py --resolve-memos --archive archive/ --bars bars.json
# resolved 14 memo call(s) now; 3 still pending; 57 resolved in total
# Brier 0.2380 vs base-rate Brier 0.2431 (base rate 0.58)
#   0.3-0.4  n=9    mean_p=0.33  mean_outcome=0.44
#   0.6-0.7  n=31   mean_p=0.64  mean_outcome=0.61
# ADVISORY: True — beats the base rate but only 57 of 100 calls resolved
```

`memo.calibration_report(path)` returns `{n, pending, brier, base_rate, base_rate_brier,
reliability_bins: [{bin, n, mean_p, mean_outcome}], min_n, advisory, advisory_reason}`.
The rule: **`advisory` is `true` until `brier < base_rate_brier` over `n ≥ 100` resolved
calls.** The base-rate Brier is what a forecaster who always says "the historical up-rate"
would score; an extractor that cannot beat it has no information beyond drift. The scan's
`meta.memos.advisory` carries the current flag on every board.

What `advisory` does *not* do: it never enables sizing, weighting or ranking on the
feature. Clearing it is the pre-condition for proposing an experiment in
`docs/BACKTEST.md` §6b, through the ledger, on a hold-out — the same road every other
feature walks.

## 8. What the engine consumes — and only this

`memo.to_features(memo)`:

| Row feature | From | Values |
|---|---|---|
| `llm_event_type` | `event_type` | the enum string (or null) |
| `llm_direction` | `direction` | −1 / 0 / +1 (`mixed` and `none` are 0) |
| `llm_magnitude` | `magnitude_bucket` | 0–3 |
| `llm_confidence` | `confidence` | 0–1 |
| `llm_p_up_5d` | `p_up_5d` | 0–1 or null |

The quote, the sources and the prose never leave the memo file. The archive record and the
scan snapshot keep the features with the rest of `features`; `ic.py --by-feature` is the
only reader.
