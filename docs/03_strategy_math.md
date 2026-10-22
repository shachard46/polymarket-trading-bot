# Trade Sizing Math Protocol

Handled by the `calculate_trade_allocation` Python skill. Constants live in `config/trading_constants.py`.

Inputs: $p$ (blind researcher estimate, optional $p_{low},p_{high}$), $q$ (YES ask; NO ask defaults to $1-q$; Phase 4 passes the real CLOB asks for both sides, and shrinks $L$ to ask-depth-within-2c / `LIQ_FRAC` when the book is thinner than the order), $L$ liquidity, $D$ days to resolution, current `bankroll` $B$, `open_exposure_total`, `open_exposure_event`.
Tax (`TAX = 0.25`, `TAX_MODE`): Israel taxes 25% of NET annual capital gains, with losses offsetting gains within the calendar year. Default `TAX_MODE = "annual_offset"`: trade maths is pre-tax and realised P&L is taxed as `TAX * max(0, net P&L per resolution year)` (see `orchestrator/stats.py`; `stats.md` reports pre-tax, tax and after-tax). `"per_trade"` is the legacy mode (tax on every winning trade, no offset); the skill takes an optional `tax_mode` input defaulting to the constant.

## Step 1: Shrink toward the market
$$p_{adj}=q+\lambda\,(p-q)\cdot\max\!\Big(0,\,1-\frac{p_{high}-p_{low}}{0.5}\Big),\quad \lambda=0.3$$
The range factor is 1 when no range is given; a range of width $\ge 0.5$ collapses $p_{adj}$ to $q$ (no trade).

## Step 2: Side and edge
$e_{YES}=p_{adj}-c_{YES}$, $e_{NO}=(1-p_{adj})-c_{NO}$ (with $c_{YES}=q_{yes\_ask}$, $c_{NO}=q_{no\_ask}$). Take the larger edge; require $e\ge$ `MIN_EDGE` $=0.04$ (live), else allocation 0 and `below_edge_threshold=true`.

## Step 3: Kelly
Buying one share at price $c$ paying 1.

**`annual_offset` (default):** pre-tax odds $b=(1-c)/c$. The annual tax scales wins and losses alike at portfolio level, so it changes neither the sign of the edge nor the Kelly fraction; we deliberately do NOT inflate $f$ by $1/(1-\tau)$ (conservative). With side probability $p_s$:
$$f^*=\frac{p_s b-(1-p_s)}{b}=\frac{p_s-c}{1-c},\qquad f=0.25\,f^*$$
so the edge gate $p_s-c\ge$ `MIN_EDGE` already guarantees $f^*>0$.

**`per_trade` (legacy):** win profit $(1-c)(1-\tau)$, loss $c$, $\tau=0.25$, odds $b=(1-c)(1-\tau)/c$, same $f^*$ formula. If $f^*\le 0$ the allocation is 0 (the per-trade tax raises the break-even probability above $c$, so an edge of 0.04 can still have $f^*\le0$).

## Step 4: Caps
$$\text{alloc}=\max\Big(0,\min\big(fB,\ 0.10B,\ 0.05L,\ 0.15B-\text{exp}_{event},\ 0.60B-\text{exp}_{total}\big)\Big)$$
Below `MIN_ORDER_USD` (5) the allocation is 0. `binding_cap` reports the smallest term. Shares $=\text{alloc}/c$.

## Step 5: Time
$D$ does not affect size. The skill returns informational `edge_per_day = edge / max(D,1)`. The gate (caller) uses `MIN_DAYS_TO_RESOLUTION = 7` and `MAX_DAYS_TO_RESOLUTION = 60`.

## Worked example 1 (YES, `annual_offset`)
$p=0.80$, $q=0.50$, no range, $B=1000$, $L=50{,}000$, no exposure.
- $p_{adj}=0.5+0.3(0.3)=0.59$; $e_{YES}=0.09$, $e_{NO}=0.41-0.50=-0.09$ -> YES at 0.50.
- $b=0.5/0.5=1$; $f^*=(0.59-0.41)/1=0.18$; $f=0.045$.
- Caps: Kelly 45, $F_{max}$ 100, liquidity 2500, event 150, total 600 -> **alloc = 45 USD**, shares 90, binding cap `kelly`.
- `per_trade`: $b=0.75$; $f^*=0.043333$; $f=0.0108333$; **alloc = 10.8333 USD**, shares 21.6667.

## Worked example 2 (NO, `annual_offset`)
$p=0.20$, $q_{yes}=0.50$, $q_{no}=0.48$, same bankroll/liquidity.
- $p_{adj}=0.5-0.09=0.41$; $e_{YES}=-0.09$, $e_{NO}=0.59-0.48=0.11$ -> NO at 0.48.
- $b=0.52/0.48=1.08333$; $f^*=(0.59-0.48)/0.52=0.211538$; $f=0.052885$.
- **alloc = 52.8846 USD**, shares 110.1763, binding cap `kelly`.
- `per_trade`: $b=0.8125$; $f^*=0.085385$; $f=0.021346$; **alloc = 21.3462 USD**, shares 44.4712.

## Worked example 3 (small edge)
$p=0.70$, $q=0.50$: $p_{adj}=0.56$, edge 0.06 $\ge$ 0.04. `annual_offset`: $f^*=0.12$, $f=0.03$ -> alloc 30 USD. `per_trade`: $b=0.75$, $f^*=(0.56\cdot0.75-0.44)/0.75<0$ -> no trade (`kelly_nonpositive`).

## Effective minimum $|p-q|$ to trade
Edge gate: $\lambda\,|p-q|\ge$ `MIN_EDGE` (no range) -> $|p-q|\ge 0.04/0.3\approx0.133$ in `annual_offset` mode at any $q$ (also bounded by the 0.40 misread gap above). Legacy `per_trade` needs $|p-q|\approx0.238$ at $q=0.5$ (YES or NO), $\approx0.167$ (YES) / $0.141$ (NO) at $q=0.2$. A range narrows the shrink and raises the bar.

## Paper-only sizing overrides

While `PAPER_TRADE_MODE` is true the skill defaults to the looser `PAPER_LAMBDA = 0.5` and `PAPER_MIN_EDGE = 0.03` (via `effective_lambda()` / `effective_min_edge()`, read at call time) so outcomes accumulate faster for `fit_lambda`. Live (`PAPER_TRADE_MODE = False`) uses `LAMBDA = 0.3` / `MIN_EDGE = 0.04`, unchanged. Example: $p=0.42$, YES ask $0.29$ gives $e=0.065$ in paper (trades) but $0.039$ live (`below_edge`). `traded` / `below_edge` ledger rows record the `lam` and `min_edge` used.
