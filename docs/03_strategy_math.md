# Trade Execution Math Protocol

This logic is explicitly handled by the `calculate_trade_allocation(p, q, D, L, V)` Python skill.
*   $p$: Estimated probability from Researcher
*   $q$: Market price / implied probability
*   $D$: Days to resolution
*   $L$: Market Liquidity
*   $V$: Market Volume
*   Constants correspond to variables in `trading_constants.py`.

## Step 1: Expected Return (After-Tax & Fees)
Calculates expected return assuming a 25% tax on winning trades.
$$e_{\mathrm{tax}}(p,q)=\frac{0.75p-q+0.25pq}{q}$$

## Step 2: Time Adjustment
Adjusts the return based on capital lock-up duration, enforcing a minimum floor ($D_{\min}$) to prevent division by zero for near-term markets.
$$\frac{e_{\mathrm{tax}}(p,q)}{\max(D,D_{\min})}$$

## Step 3: Rarity Bonus
Identifies statistical outliers by comparing the gap between $p$ and $q$ against historical means ($\mu_x$) and standard deviations ($\sigma_x$).
$$z=\frac{|p-q|-\mu_x}{\sigma_x+\varepsilon}$$
$$B_{\mathrm{rarity}} = 1+\beta \tanh(\max(0,z))$$

## Step 4: Execution Penalty
Applies a penalty ($P_{\mathrm{exec}}$) for markets with low liquidity ($L$) or volume ($V$) to account for expected slippage.
$$P_{\mathrm{exec}}=\frac{c_1}{\log(1+L)}+\frac{c_2}{\log(1+V)}$$

## Step 5: Final Score
Combines the time-adjusted return, rarity bonus, and execution penalty to generate the final execution score ($S$).
$$S = \frac{e_{\mathrm{tax}}(p,q)}{\max(D,D_{\min})} \cdot B_{\mathrm{rarity}} - P_{\mathrm{exec}}$$

## Step 6: Bankroll Allocation
Determines the fraction of the available bankroll to deploy ($f$). If $S$ does not exceed the threshold $S_0$, no trade is placed.
$$f= \begin{cases} 0, & S\le S_0 \\ \min\big(f_{\max}, \alpha(S-S_0)\big), & S>S_0 \end{cases}$$