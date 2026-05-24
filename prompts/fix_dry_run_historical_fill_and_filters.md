You are modifying the Polyfon project at /home/user/MyProjects/polyfon.

Goal:
Fix dry-run correctness for historical replay without changing unrelated behavior.

Problems to fix:
1. In polyfon/execution/engine.py, run_dry() ignores the engine's coin filter. It should only process historical windows whose underlying is in self.coins when self.coins is non-empty.
2. In polyfon/execution/engine.py, _simulate_fill() uses the latest OrderBook row for the window/token, which is wrong for historical replay. For dry mode, fills must use the latest order book row at or before the evaluation time used for signal generation.
3. In polyfon/execution/engine.py, _build_context() computes window_open_price using the earliest SpotPrice after window.start_et but does not bound it by eval_time or window.end_et. Make window_open_price come from the earliest spot within [window.start_et, eval_time] for historical replay, with a sensible fallback if needed.

Requirements:
- Preserve current behavior for shadow mode unless needed for shared function signatures.
- Keep code minimal and idiomatic to the existing project.
- Do not add new dependencies.
- Update any function signatures needed to pass eval_time into historical fill simulation.
- Validate by running:
  - python3 -m scripts.run list-strategies
  - timeout 20s python3 -m scripts.run dry --strategy=WDM
- If the dry run is still long, capture enough output to confirm it starts and processes windows without crashing.
- After changes, summarize exactly what was fixed and why it matters for historical correctness.
