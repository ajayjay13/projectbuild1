# Hyderabad Restaurant Hygiene Map

A free, crowdsourced map of CMC (Cyberabad Municipal Corporation) food
safety inspection results, built entirely on GitHub's free tier.

## How it works
1. Anyone files a **GitHub Issue** using the "New inspection report" form,
   copying details from a `@CMC_Offcl` post.
2. A maintainer checks it against the source post and adds the `verified` label, then closes the issue.
3. A **GitHub Action** (`process-inspection.yml`) parses the issue, geocodes
   the address via OSM Nominatim, and merges it into `data/inspections.geojson`.
4. A second Action (`deploy-pages.yml`) rebuilds the static site on
   **GitHub Pages** — a Leaflet map reading straight from the GeoJSON file.
5. Visitors get a "restaurants near me" button powered by the browser's
   Geolocation API — no server, no cost.

## Data model
- Each **kitchen** (physical location) is one map pin.
- A kitchen has one or more **brands** — the name(s) it trades under on
  Zomato/Swiggy/dine-in. This is what makes cloud kitchens work: one raid
  can expose several storefronts at once.
- Each kitchen has a history of **inspections** over time, so repeat
  offenders and improving kitchens are both visible.

## Setup checklist
- [ ] Push this repo to GitHub, enable Pages (Settings → Pages → source: GitHub Actions)
- [ ] Update `USER_AGENT` in `scripts/process_issue.py` with your repo URL (Nominatim requires this)
- [ ] Set repo Settings → Actions → General → Workflow permissions to "Read and write"
- [ ] Recruit a couple of trusted people to review/label issues as `verified`
- [ ] Add a second Issue Form for corrections/removal requests

## Attribution & disclaimer
All data originates from CMC's public inspection posts on X. This project
is an unofficial, community-run aggregation for public awareness — not
affiliated with CMC. Every entry links back to its source post.
