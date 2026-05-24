You are modifying the Polyfon project at /home/user/MyProjects/polyfon.

Goal:
Review and, if supported by the current Polymarket market websocket protocol, fix subscription lifecycle so token subscriptions do not grow indefinitely.

Context:
- polyfon/collector/book_collector.py currently sends an initial subscribe message and later sends {"operation": "subscribe", "assets_ids": [...]} in update_assets().
- polyfon/collector/orchestrator.py assumes the active token set can be updated and reduced over time.
- There is no explicit unsubscribe path in the code.

Tasks:
1. Inspect the existing code and confirm whether update_assets() currently performs additive-only subscription behavior.
2. If the protocol supports unsubscribe or replacement semantics, implement the correct behavior.
3. If the protocol does NOT support unsubscribe via the current endpoint, make the code behavior explicit and document the limitation in code comments so the orchestrator does not imply tokens are removed server-side when they are not.
4. Remove debug stderr prints for unknown/resolution events or convert them to proper logging at an appropriate level.

Requirements:
- Do not invent protocol behavior. If uncertain, prefer explicit code comments over unsupported logic.
- Keep the implementation minimal.
- Do not add dependencies.
- Validate by running at least import/CLI checks and summarize what is now guaranteed vs not guaranteed.
