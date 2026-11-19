# Stats

_Generated 2026-11-19T08:13:07.387027+00:00_

Resolved markets: **0** (scored with p and q: 0)

## Model vs market

No resolved markets with both p and q yet.

### Brier by evidence source

| evidence_source | n | Brier(p) | Brier(p) - Brier(q) |
|---|---|---|---|

### Brier by runner backend

| runner_backend | n | Brier(p) | Brier(p) - Brier(q) |
|---|---|---|---|

### Brier by model

| model | n | Brier(p) | Brier(p) - Brier(q) |
|---|---|---|---|

### Brier by provider

| provider | n | Brier(p) | Brier(p) - Brier(q) |
|---|---|---|---|

### Shadow: quick vs full vs market

Resolved shadow markets (quick no_divergence skips run through FULL anyway): 0.

| quick p | full p | market q |
|---|---|---|
| n/a | n/a | n/a |

### Signal coverage (missing / total of gated markets)

| signal | latest tick | % | last 7 days | % |
|---|---|---|---|---|
| volume_24h | 33/100 | 33.0% | 33/100 | 33.0% |
| day_price_change | 0/11 | 0.0% | 0/11 | 0.0% |
| bid_ask | 39/100 | 39.0% | 39/100 | 39.0% |
| holders | 0/15 | 0.0% | 0/15 | 0.0% |
| price_history | 0/11 | 0.0% | 0/11 | 0.0% |
| trades | 0/11 | 0.0% | 0/11 | 0.0% |
| book | 0/11 | 0.0% | 0/11 | 0.0% |

### Triage shadow: quick p vs market (resolved triage rejects)

| reason | n | Brier(quick p) | Brier(q) |
|---|---|---|---|
| not_researchable | 0 | n/a | n/a |
| manipulation_risk | 0 | n/a | n/a |
| all | 0 | n/a | n/a |

### Independent evidence sample

Research rows with indep_p: 0; mean |indep_gap|: n/a. Resolved: 0; Brier(median p) n/a vs Brier(indep_p) n/a.

### Gate flags vs market calibration

Brier(q) on resolved markets, by gate flag vs markets that passed the gate.

| group | n | Brier(q) |
|---|---|---|
| passed_gate | 0 | n/a |

### Market integrity

Brier(q) on resolved markets by integrity score band, and by integrity gate-reject reason.

| group | n | Brier(q) |
|---|---|---|
| score >=0.8 | 0 | n/a |
| score 0.5-0.8 | 0 | n/a |
| score <0.5 | 0 | n/a |
| score unknown | 0 | n/a |
| gate_reject ramp_thin | 0 | n/a |
| gate_reject wallet_dominated | 0 | n/a |
| gate_reject rules_changed | 0 | n/a |
| gate_reject disputed_before | 0 | n/a |

## Offers & votes

Pending: 0. Approved 0, declined 0, expired 0, stale 0.

| group | n resolved | Brier(p) | Brier(q) | hypothetical P&L USD |
|---|---|---|---|---|
| approved | 0 | n/a | n/a | 0.00 |
| declined | 0 | n/a | n/a | 0.00 |
| expired | 0 | n/a | n/a | 0.00 |
| stale | 0 | n/a | n/a | 0.00 |

## Calibration (p in 10% buckets)

| bucket | n | mean p | hit rate |
|---|---|---|---|

## Realised P&L (traded; annual_offset: 25% of net gains per calendar year, losses offset within the year)

Pre-tax: **0.00 USD** over 0 resolved trades; tax: 0.00 USD; after-tax: **0.00 USD**.

### Per category

| key | n | P&L USD |
|---|---|---|

### Per stage reason

| key | n | P&L USD |
|---|---|---|

## Counterfactual P&L (hypothetical, at stated side/price; 1 USD stake if size missing)

| stage | n | P&L USD |
|---|---|---|

Counterfactual total: pre-tax 0.00, tax 0.00, after-tax 0.00 USD.

## False-negative rate (PROXY)

Among 0 resolved gate/triage/research rejects with q, share where |outcome - q| > 0.5: n/a. This is a cheap proxy, not a true missed-edge rate.

## Fitted lambda

argmin Brier of q + lambda(p - q): n/a (needs n >= 50)


## LLM cost

No LLM usage rows yet.


## Web evidence filter

Total: kept 12, dropped (no url) 0, dropped (unretrieved url) 0; drop rate 0.0000.

| model | kept | dropped no url | dropped unretrieved | drop rate |
|---|---|---|---|---|
| stub | 12 | 0 | 0 | 0.0000 |
