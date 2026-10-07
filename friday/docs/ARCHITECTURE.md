# Architecture

```text
Polymarket CLOB/Gamma/WebSocket
          |
     Market Collector ---> SQLite/feature store
          |
       Scanner (24/7)
          |
   +------+----------------+
   |                       |
Indicator Engine      Claude Research
   |                  (books/rules)
   +----------+------------+
              |
        Signal Engine
              |
         Trade Planner
              |
          Risk Engine
              |
       Final Decision Gate
      /       |       |      \
 APPROVE    WATCH   REJECT   IGNORE
      |
 Paper/Shadow Executor ---> Monitor/Alerts
```

### Strategy library
- breakout
- pullback
- momentum/trend continuation
- reversal
- RSI
- Bollinger Bands
- moving averages/EMA
- MACD
- support/resistance (to be extracted from research corpus and backtested)

### Anti-overfitting requirements
Every strategy must be evaluated with train/validation/test splits, transaction costs, spread/slippage assumptions, walk-forward testing, maximum drawdown, profit factor, Sharpe-like risk-adjusted return, win rate, expectancy and calibration. No strategy is promoted from a single backtest.
