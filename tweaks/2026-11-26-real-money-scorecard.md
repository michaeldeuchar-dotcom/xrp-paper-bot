# 2026-11-26 (8:15 PM PT) — real-money scorecard (reporting only; fake money)

Reporting change only. No trading behavior changed, and the GitHub Actions bot does not import or run `summarize.py`, so the running slice was not restarted.

`summarize.py` now grades the week-2 finalists against a fixed pass/fail bar that was set before the two-week test ends. The finalist list lives in `scorecard.json` so it can change without a code edit.

Bar (all four must pass): at least 300 settled bets; net fake P&L above $0 after fees; no Pacific Time calendar day worse than -$20; net P&L above $0 on bets settled on or after Nov 30, 2026 PT. Daily and second-half figures use settlement time in Pacific Time.

Verdicts: PASS, FAIL, or NOT YET (only the bet count is still short, and/or the second half has not started, and nothing else has failed). Nothing is a green light for real money until the verdict is PASS.

Commands (from a checkout that also has the `data` branch's `data/week2` folder):

    python3 summarize.py --scorecard --run week2
    python3 summarize.py --run week2          # usual recap, now with the scorecard at the end

`pull_results.sh --scorecard` also works, because that script forwards extra args to `summarize.py`. Paper mode only; data/week1 untouched.
