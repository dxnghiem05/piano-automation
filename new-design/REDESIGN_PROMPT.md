# Redesign prompt — paste this into a new Claude chat

I want to apply a new design I already built to my existing piano dashboard. Everything is in one folder: `/Users/dustinnghiem/Claude/PianoDashboard`. Please request access to that folder first, then read this whole message before doing anything.

---

## 1. The real dashboard (the target — keep it 100% working)

`dashboard.py` serves the dashboard on **localhost:8000** with real YouTube data, a 12-week experiment tracker, and TikTok posting via `tiktok_publish.py` + an OAuth token in `credentials/tiktok_token.json`.

Existing routes (these are the tabs):
`/`, `/overview`, `/stats`, `/tracker`, `/tiktok-candidates`, `/tiktok-stats`, `/experiment`, `/data-science`.
Existing actions: `/tiktok/post`, `/tiktok-schedule`, `/tiktok-stats/refresh`, `/tiktok/connect|callback|disconnect`.

**Do NOT break, rename, or change any of this.** Keep:
- the exact same **data structure** the old dashboard uses (`metadata/video_tracker.csv`, `metadata/metadata.csv`, `logs/youtube_stats_history.csv`, `logs/uploads_log.csv`, etc.) — read from the same files the same way, don't restructure them.
- the TikTok API flow, credentials, and every route/action working exactly as-is.
- the 12-week logic from `config.py` (`PROJECT_WEEK_1_START_DATE = 2026-06-15`, `PROJECT_TOTAL_WEEKS = 12`) — currently Week 5, auto-advancing. The week number must keep auto-calculating from this.
- the mood words + hashtags from `config.py` (`MOOD_WORDS`, `HASHTAGS`).

Work on a branch and git-commit as you go so I can roll back.

---

## 2. The new design (already built — match it exactly)

The finished mockup lives in this same folder under `new-design/`:
- `new-design/index.html` — the full scrollable page (nav, hero, mission, 12-week plan, moods gallery, stats charts, queue, TikTok, footer, plus the animated waveform background).
- `new-design/daily.html` — the "Daily Top 3" view (top 3 clips per day with post-to-TikTok buttons).
- `new-design/wave-mockup.html` — a focused example of the flowing waveform.

Read all three. Match this design language everywhere:

- **Fonts:** Apple + Spotify hybrid — SF system stack (`-apple-system, BlinkMacSystemFont`) for UI, **Figtree** (Google Fonts) for display and numbers.
- **Colors (new palette):** midnight-blue base `#05091a`; accents electric blue `#4d84ff`, cyan `#2fe6d6`, violet `#a273ff`, pink `#ff5c9d`, green `#37e28b`. Film grain overlay + glassmorphism panels. Replace the OLD dashboard's colors with these.
- **Components:** glass cards, Chart.js graphs styled to this palette, animated counters, footer social links (YouTube @dustin.nghiem, TikTok @dustinspiano, Instagram).

---

## 3. The structure I want: ONE main scrollable page + the old tabs, restyled

**A) One main scrollable landing page** (the new design in `new-design/index.html`).
This is the front page someone sees — the hero, the waveform, the mission, the 12-week plan, the moods, a stats snapshot, the queue. It's the "wow" page.

**B) The functional tabs from the old dashboard, kept as tabs but restyled** with the new UI + colors.
Every existing section — Stats/graphs, Tracker, TikTok Candidates, TikTok Stats, Experiment, Data-Science, Overview — stays a real tab that loads its own page with **real live data** from the same backend. These are what I actually use day to day, so keep them **fast and instant**: no preloader, no scroll-hijacking, minimal motion, and honor `prefers-reduced-motion`. Just the new look (fonts, colors, glass cards, restyled charts) on top of the existing working functionality. The TikTok post buttons must trigger the real `/tiktok/post` flow.

So: the scrollable showpiece is the landing page; clicking into a tab gives me the clean, quick working view.

---

## 4. TWO specific fixes to the new design (apply these while porting)

**(a) Slow the waveform down — a lot.** Right now the flowing sound-wave background moves too fast/busy. Make it drift **slowly, like a wave in the ocean** — gentle and calm, not frantic. Keep it prominent behind the hero but fade it to a faint glow behind the denser content sections so it never competes with data. (In `new-design/index.html` the wave speed is the `t+=0.022...` line in the waveform script — bring it way down, e.g. ~`0.004`, and soften it.)

**(b) Fix the "Fifteen moods" section — it should NOT hijack vertical scroll.** Currently that section is "pinned" so scrolling down drags the mood cards left-to-right, which is unnecessary and annoying when someone is just scrolling through the page. Change it so:
- Vertical scrolling just scrolls straight through the whole page normally — that section does not pin or capture the scroll.
- The mood cards become a **self-contained horizontal slider you manually slide left/right** (drag, swipe, and/or left/right arrow buttons) — independent of page scroll.
- Keep the same 15 mood cards and real data (avg views, clip counts).

---

## 5. How to proceed
1. Read `dashboard.py` to understand how pages/HTML are currently generated and how data is loaded.
2. Give me a short plan before editing anything.
3. Build the shared new visual style once (fonts, colors, glass, chart theme) and reuse it across the landing page and all tabs.
4. Build the main scrollable landing page (with the two fixes above).
5. Restyle each functional tab with the new look, keeping it fast and fully working on real data.
6. Run it on localhost:8000 and verify every tab still works — especially TikTok posting — before moving on. Commit as you go.
