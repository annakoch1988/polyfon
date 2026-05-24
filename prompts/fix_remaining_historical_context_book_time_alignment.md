You are modifying the Polyfon project at /home/user/MyProjects/polyfon.

Goal:
Fix the remaining historical replay correctness issue in context-building for dry mode.

Problem still remaining:
In polyfon/execution/engine.py, _simulate_fill() was fixed to use the latest OrderBook at or before eval_time for dry replay, but _build_context() still loads UP and DOWN order books using the latest row overall for the window/token. That means WDM signal generation in dry mode can still use future book information after the T-10s decision point.

What to fix:
1. In polyfon/execution/engine.py, when eval_time is provided, the queries that populate:
   - ctx.up_best_bid
   - ctx.up_best_ask
   - ctx.down_best_bid
   - ctx.down_best_ask
   must use the latest OrderBook row at or before eval_time, not the latest row overall.
2. Preserve current shadow-mode behavior when eval_time is None.
3. Keep the logic for UP and DOWN token lookup separate, as the existing code already does.

Requirements:
- Keep the patch minimal and idiomatic to the existing codebase.
- Do not add dependencies.
- Do not change unrelated behavior.
- Validate by running:
  - python3 -m scripts.run list-strategies
  - timeout 20s python3 -m scripts.run dry --strategy=WDM
- If the dry run still takes too long, capture enough output to show it starts and processes windows without crashing.
- In your summary, explicitly state that both signal context book prices and simulated fill prices are now aligned to eval_time in dry replay.
