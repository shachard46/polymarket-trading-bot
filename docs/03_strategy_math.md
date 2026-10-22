# Trade Sizing Math Protocol

Handled by the `calculate_trade_allocation` Python skill. Constants live in `config/trading_constants.py`.

Inputs: $p$ (blind researcher estimate, optional $p_{low},p_{high}$), $q$ (YES ask; NO ask defaults to $1-q$), $L$ liquidity, $D$ days to resolution, current `bankroll` $B$, `open_exposure_total`, `open_exposure_event`.
A 25% tax (`TAX`) applies to winnings (profit) only; losses do not offset.

## Step 1: Shrink toward the market
$$p_{adj}=q+\lambda\,(p-q)\cdot\max\!\Big(0,\,1-\frac{p_{high}-p_{low}}{0.5}\Big),\quad \lambda=0.3$$
The range factor is 1 when no range is given; a range of width $\ge 0.5$ collapses $p_{adj}$ to $q$ (no trade).

## Step 2: Side and edge
$e_{YES}=p_{adj}-c_{YES}$, $e_{NO}=(1-p_{adj})-c_{NO}$ (with $c_{YES}=q_{yes\_ask}$, $c_{NO}=q_{no\_ask}$). Take the larger edge; require $e\ge$ `MIN_EDGE` $=0.04$, else allocation 0 and `below_edge_threshold=true`.

## Step 3: Tax-aware Kelly
Buying one share at price $c$ paying 1: win profit $(1-c)(1-\tau)$, loss $c$, $\tau=0.25$. Odds $b=(1-c)(1-\tau)/c$; with side probability $p_s$:
$$f^*=\frac{p_s b-(1-p_s)}{b},\qquad f=0.25\,f^*$$
If $f^*\le 0$ the allocation is 0 (the tax raises the break-even probability above $c$, so an edge of 0.04 can still have $f^*\le0$).

## Step 4: Caps
$$\text{alloc}=\max\Big(0,\min\big(fB,\ 0.10B,\ 0.05L,\ 0.15B-\text{exp}_{event},\ 0.60B-\text{exp}_{total}\big)\Big)$$
Below `MIN_ORDER_USD` (5) the allocation is 0. `binding_cap` reports the smallest term. Shares $=\text{alloc}/c$.

## Step 5: Time
$D$ does not affect size. The skill returns informational `edge_per_day = edge / max(D,1)`. The gate (caller) uses `MIN_DAYS_TO_RESOLUTION = 7` and `MAX_DAYS_TO_RESOLUTION = 60`.

## Worked example 1 (YES)
$p=0.80$, $q=0.50$, no range, $B=1000$, $L=50{,}000$, no exposure.
- $p_{adj}=0.5+0.3(0.3)=0.59$; $e_{YES}=0.09$, $e_{NO}=0.41-0.50=-0.09$ -> YES at 0.50.
- $b=0.5\cdot0.75/0.5=0.75$; $f^*=(0.59\cdot0.75-0.41)/0.75=0.043333$; $f=0.0108333$.
- Caps: Kelly 10.8333, $F_{max}$ 100, liquidity 2500, event 150, total 600 -> **alloc = 10.8333 USD**, shares 21.6667, binding cap `kelly`.

## Worked example 2 (NO)
$p=0.20$, $q_{yes}=0.50$, $q_{no}=0.48$, same bankroll/liquidity.
- $p_{adj}=0.5-0.09=0.41$; $e_{YES}=-0.09$, $e_{NO}=0.59-0.48=0.11$ -> NO at 0.48.
- $b=0.52\cdot0.75/0.48=0.8125$; $f^*=(0.59\cdot0.8125-0.41)/0.8125=0.085385$; $f=0.021346$.
- **alloc = 21.3462 USD**, shares 44.4712, binding cap `kelly`.
