# FRIDAY Architecture

```text
Voice / Typed Command
        |
        v
FRIDAY Persona + Claude Reasoning
        |
        +----> Research Memory <---- Books / PDFs / Papers
        |
        v
Market Data -> Feature Store -> Scanner
                              |
                    +---------+----------+
                    |                    |
               Signal Engine       Market Regime
                    |                    |
                    +---------+----------+
                              v
                         Trade Planner
                              |
                         Risk Governor
                              |
                     Final Decision Gate
                  APPROVE / WATCH / REJECT / IGNORE
                              |
                  Paper / Shadow / Future Live
                              |
                         24/7 Monitor
                              |
                    Outcomes -> Learning DB
```

The UI brain is a visualization of strategy knowledge and system state; it is not the decision engine itself. Green = approved/healthy, red = rejected/failure state, yellow = watch/uncertain, cyan = learning/analysis.
