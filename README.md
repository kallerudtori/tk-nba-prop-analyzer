# NBA Prop Analyzer 🏀

A full-stack web app that identifies high-value NBA player prop bets by comparing a weighted statistical projection model against live DraftKings lines.

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.11+ · Flask 3 · Flask-Caching |
| NBA Data | `nba_api` (no key required) |
| Odds Data | [The Odds API](https://the-odds-api.com) |
| Frontend | Vanilla JS · Chart.js 4 |
| Cache | In-memory SimpleCache (1h stats · 15min odds) |

---

## Quick Start

### 1. Clone / navigate to the project

```bash
cd nba-prop-analyzer
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up your API key

```bash
cp .env.example .env
# Open .env and paste your Odds API key
```

Get a free key (500 requests/month) at https://the-odds-api.com

### 5. Run the server

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

---

## Environment Variables

| Variable | Description |
|---|---|
| `ODDS_API_KEY` | Your key from the-odds-api.com **(required)** |
| `FLASK_ENV` | `development` or `production` (optional) |
| `FLASK_PORT` | Server port, default `5000` (optional) |

---

## Project Structure

```
nba-prop-analyzer/
├── app.py                    # Flask app & all API routes
├── models/
│   └── projection.py         # Weighted projection model
├── services/
│   ├── nba_stats.py          # nba_api data fetching + caching
│   └── odds.py               # The Odds API integration
├── static/
│   ├── css/styles.css        # Dark premium theme
│   └── js/
│       ├── app.js            # Main app logic & state
│       ├── charts.js         # Chart.js visualisations
│       └── parlay.js         # Parlay builder
├── templates/index.html      # Single-page layout
├── .env.example
└── requirements.txt
```

---

## Projection Model

For each prop (Points / Rebounds / Assists / PRA):

```
Base = L5 × 0.40 + L10 × 0.35 + Season × 0.25

× Opponent factor  (opponent defensive rating vs league avg, capped ±15%)
× Home/Away factor (split vs overall avg, applied if diff > 5%, capped ±10%)
× Minutes trend    (L5 minutes vs season avg, applied if diff > 5%, capped ±15%)

Edge        = Projection − DraftKings line
Value label:  Edge ≥ 2.5 → Strong Value
              Edge ≥ 1.0 → Slight Value
              Edge < 1.0 → Avoid
```

Confidence score is derived from games played + coefficient of variation (std_dev / mean).

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web app |
| `GET` | `/api/health` | Server health + Odds API quota |
| `GET` | `/api/games/today` | Today's NBA games (with matched Odds event IDs) |
| `GET` | `/api/team/<id>/roster` | Team roster |
| `GET` | `/api/player/<id>/stats` | Raw player stats |
| `GET` | `/api/player/<id>/analysis` | Full analysis (stats + projection + odds) |
| `POST` | `/api/odds/refresh` | Clear odds cache |

Query params for `/api/player/<id>/analysis`:

| Param | Type | Description |
|---|---|---|
| `opponent_team_id` | int | Opponent's NBA team ID |
| `is_home` | bool string | `"true"` / `"false"` |
| `odds_event_id` | string | The Odds API event ID |

---

## Notes

- The `nba_api` library makes live calls to stats.nba.com. First load per player can take 5–10 seconds due to NBA's rate limiting — subsequent loads are cached for 1 hour.
- The Odds API free tier covers ~500 requests/month. Player props are fetched per-event (one call per game, cached 15 minutes).
- If no DraftKings lines are found for a player, the app falls back to showing the model projection only, clearly flagged in the UI.
- The "Refresh Odds" button bypasses the 15-minute cache and fetches fresh lines on demand.
