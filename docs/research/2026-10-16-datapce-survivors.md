# datapce survivors check — 2026-10-16

Follow-up to [the top-10 finish](2026-10-14-datapce-worlds-top10.md). Run `wf_42b22d90-b97`, 11 agents, mail run `survivors-2026-10-16`.

## BOTTOM LINE
Two small games still survive. Every business-tool idea is dead.
The best next move is a 7-day free browser demo, not a full game.
Even a good result is tens of thousands of dollars. A typical game earns about $10k over 3 years.

PRIORITIES (highest first)
1. Coding automation game, $9.99 on Steam: GO-IF-TEST-PASSES, for the 7-day demo only.
   You type short Python-like programs that keep a parcel depot running.
   Why: code-runs-a-world games sell best in the coding niche (3 paid games, median 352 reviews).
   Risk: nobody hears about it. Most hits had a publisher, an earlier game or years of early access.
   Odds (cold start): about 12% for $25k a year held 3 years, about 1% for $100k a year.
2. Free browser idle game, then $7.99 on Steam: GO-IF-TEST-PASSES, but PARKED.
   The coding demo's r/incremental_games players give a first read. No separate demo now.
   Typical result is near zero. The average comes from a few rare hits.
3. Citation checker for Canadian, UK and Australian court filings: NOT TESTED.
   Only business idea not yet killed. Open only if you want a non-game backup.

## WHAT CHANGED
- Money per copy is lower: about $3.60 at $9.99 (the developer keeps about 36%, not 50%).
- One Steam review stands for about 41 copies (24 games, middle half 29-66).
- $25k a year needs about 6,950 copies, or 168 reviews a year.
- The demo changed from 5 grid puzzles to a parcel depot. Puzzle-level games are the weakest type.
- A free web build works like a Steam demo. It is not an extra edge.
- One Hacker News post plus two Reddit posts usually brings a few hundred players. Hits got buyers from a Steam demo and Steam festivals.
- A reviewer model from another company (Codex) forced 3 test fixes. It said no to a 9-month build now, and yes to the demo.
- Grant finder: dead (Candid sells it at $100/mo). FDA predicate finder: dead (Complizen at $66/mo).

THE TEST (short form, full text in the repo report)
1. Day 0: fix all pass/kill numbers before any data.
2. Day 1: decide on the $100 Steam page (refunded after $1,000 gross).
3. Days 1-7: build the demo. 5 goals, 20-40 minutes of play, desktop only.
4. Days 8-12: post on Show HN and 5 subreddits. E-mail 10-20 coding YouTubers yourself.
5. Day 14: PASS = 200+ players, 25%+ finish goal 5, 8%+ wishlist. KILL = under 10% finish or under 3% wishlist.
6. Day 21: hard stop. Under 200 players means KILL.
7. After PASS: Steam Next Fest (Feb 22-Mar 1, 2027). 10,000+ wishlists = GO for the full build. Under 3,000 = STOP.

## Pre-registration v2 (verbatim)

```
Pre-registration v2, coding-automation demo, amended 2026-10-16 before any data (fixes from the cross-family review).
Unit: player = unique browser id that ran >=1 program. n_min = 200.
M2 depth = players reaching goal 5 / players. M3 = Steam UTM wishlists (72h) / players; no Steam page: sign-ups / players.
PASS (day 14 or 21): n>=200 AND M2>=25% AND M3>=8% (sign-ups>=10%). Total wishlists are reported, not gating.
KILL: n>=200 AND (M2<10% OR M3<3%). Also KILL at day 21 if n<200 (cannot reach players).
INCONCLUSIVE: all else -> one 7-day re-shot (new title + GIF, 2 new channels); still INCONCLUSIVE at day 21 -> no full build.
Idle read (r/incremental_games ref only, needs >=50 players from it): later-day return >=20% merge idle angle; <10% drop it; else INCONCLUSIVE.
Stage B (Next Fest Feb 22-Mar 1 2027, build review by Feb 1; or Jun 14-21 2027): wishlists at fest end >=10,000 GO;
3,000-9,999 capped 3-month early-access slice, keep building only if >=150 reviews in its first 30 days; <3,000 STOP.
All thresholds ASSUMED (own guesses grounded on public benchmarks), fixed now, not changed after data.
```

## Open questions

- Will you accept about $6.50 an hour at the median, or about $19 an hour at the average, for a 9-month build? If not, the demo is the whole project.
- Will you pay the $100 Steam Direct fee on day 1? Stage B cannot run without a Steam page.
- How many hours a week can you give this? The 7-day build and the 9-month estimate both assume full-time.
- How many Programming-tag games were announced on Steam in 2024-2025 but never released? A high count lowers the 12% odds. This was not measured.
- Is the parcel-depot theme different enough from The Farmer Was Replaced and its DLC on 2026-11-02? The theme is ASSUMED.
- Do wishlists still predict sales? Units per wishlist roughly halved from 2020-22 to 2023-25 (MEASURED). The 10,000-wishlist GO line may be too low.
- Non-US citation checker: does vLex/Vincent, Lexis+ AI or PelAIkan already check UK, Canadian or Australian citations for $100 a month or less, and can CanLII API access be secured? Not checked.

## Process notes

- The browser-first agent made about 1,500 automated GET requests against a brief of about 20. Nothing was posted or spent.
- The reach agent read public Reddit `about.json` counts through the browser. Read-only.
