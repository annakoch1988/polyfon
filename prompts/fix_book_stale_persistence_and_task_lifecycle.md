You are modifying the Polyfon project at /home/user/MyProjects/polyfon.

Goal:
Fix order book collector correctness and robustness around stale carry-forward records.

Problems to fix:
1. In polyfon/collector/book_collector.py, stale carry-forward records are generated internally but the stale flag is not passed through the on_book callback.
2. In polyfon/collector/orchestrator.py, _book_worker() always persists OrderBook.stale = False. It must persist the actual stale value from the collector.
3. In polyfon/collector/book_collector.py, the carry-forward background task is started with asyncio.create_task(...) but its task handle is not stored or managed. Store it and stop/cancel it cleanly in stop().

Requirements:
- Keep callback changes minimal but complete end-to-end.
- Ensure both live and stale order book records persist correct stale values.
- Do not add dependencies.
- Preserve current public behavior as much as possible.
- Validate with lightweight checks:
  - import the relevant modules successfully
  - run any available command that starts cleanly without syntax/runtime errors
- Summarize the exact flow of the stale flag from collector to DB after the fix.
