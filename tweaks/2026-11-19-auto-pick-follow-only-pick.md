## 2026-11-19 (about 5:45 PM PT) — auto_pick follows ONLY its current pick (fake money only)
- Change: auto_pick no longer falls back to the next-ranked qualifying variant. If its current pick doesn't bet in a window, auto_pick sits out that window.
- Why: the fallback meant most of its bets copied lower-ranked variants (mostly `control`) instead of the pick it reports.
- Unchanged: 6h net-P&L ranking (since-start fallback), 5+ bets and positive P&L to qualify, two-check rule to switch leaders, flat $3 stake with round-up to the 5-share minimum capped at $5 total, $10 hard max, end time.
