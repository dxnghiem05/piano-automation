# PianoClip — Public Showcase Site

A brand-new, **completely separate** public-facing website for the PianoClip project. It keeps the core concepts of your existing dashboard — YouTube data, the 12-week plan, and TikTok posting — but reimagines them as a cinematic, modern site meant for a public audience.

This folder is standalone. It does not touch your existing `PianoDashboard/` project, its data, credentials, or the felt-piano master plan.

## Open it
Double-click `index.html` — it runs in any browser, no server or install needed. Everything is self-contained in one file (fonts and Chart.js load from CDNs).

## What's inside
- **Hero** — animated title, floating notes, and a **playable piano** (hover or click the keys to hear real tones via Web Audio).
- **Mission** — goals, what we do, how we amplify, plus animated headline stats.
- **12-Week Plan** — a scrollable, phase-colored timeline that auto-centers on the current week (Week 5).
- **Stats** — live-style charts: weekly views with a projection, traffic sources, retention by clip length, and top clips.
- **Queue** — staged clips with auto-written captions (`Copy caption`) and a `Send to TikTok` action.
- **TikTok** — dedicated stats + the 4-step posting loop.

## Design system
- Fonts: Apple + Spotify hybrid — the SF system stack (`-apple-system`) for UI with **Figtree** (a Circular/Spotify-like geometric sans) for display and numbers.
- Midnight-blue base with vibrant accents — electric blue, cyan, violet, pink, green. Film grain + glassmorphism.
- Scroll progress bar, scroll-reveal, animated counters, parallax notes, active-section nav.
- Full-screen-safe piano: keys are repositioned on resize (ResizeObserver), sound is softened.
- Footer social buttons for YouTube, TikTok, and Instagram.

## Real data
Numbers are pulled from your `PianoDashboard` (as of Jul 17, 2026): 492 public clips, 215,826 views, 3,416 likes, Week 6 of 12, best mood words (DREAM / MIDNIGHT / SERENE), best post hour (5 PM), and real top clips. TikTok shows 0 published (Direct Post audit still in review). Edit the `weeks[]`, `clips[]`, and `Chart` blocks to refresh.

## Editing the data
All demo content lives in the `<script>` block at the bottom of `index.html`:
- `weeks[]` + `CURRENT` — the 12-week plan and which week is active.
- `clips[]` + `HASH` — the queue cards and hashtag set.
- The `new Chart(...)` blocks — the numbers behind each chart.

Swap these for your real dashboard numbers whenever you want.
