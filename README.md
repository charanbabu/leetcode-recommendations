# LeetCode AI Agent

A WhatsApp-based LeetCode coaching assistant that sends daily problem recommendations, tracks progress, reviews code, and helps you prepare for technical interviews — powered by Claude AI.

## How It Works

1. **Daily Problem Delivery** — Every day at your preferred hour, the agent sends a curated LeetCode problem to your WhatsApp with a hint.
2. **Smart Recommendations** — Problems are selected based on topic rotation, weak areas, difficulty progression, and company targeting.
3. **Conversational AI** — Reply naturally on WhatsApp to mark problems as solved, ask for hints, submit code for review, or get topic explanations.
4. **LeetCode Sync** — Connects to the LeetCode API to sync your solved problems and pull company-tagged questions.

## Features

- **Topic Rotation** — Cycles through 24 DSA topics (arrays, trees, DP, graphs, etc.) to ensure broad coverage.
- **Weak Area Detection** — Prioritizes topics where you've struggled (skipped/attempted but unsolved).
- **Company Targeting** — Set a target company (e.g., Google, Meta) to prioritize frequently asked problems.
- **Difficulty Progression** — Starts with Easy, gradually introduces Medium and Hard based on your solve history.
- **Code Review** — Paste your solution and get feedback on correctness, complexity, and improvements.
- **Solution Explanations** — Get full walkthroughs with approach, intuition, code, and complexity analysis.
- **Progress Stats** — Track solved counts by difficulty, weak areas, and current topic rotation.

## Tech Stack

- **Backend**: FastAPI + Uvicorn
- **Database**: SQLite (default) / PostgreSQL (via Docker Compose)
- **AI**: Anthropic Claude API (tool-use agent loop)
- **Messaging**: Twilio WhatsApp API
- **Scheduler**: APScheduler (hourly cron, filters by user timezone)
- **ORM**: SQLAlchemy (async) + Alembic for migrations

## Project Structure

```
├── main.py                  # FastAPI app entrypoint
├── app/
│   ├── agent.py             # Claude AI agent with tool-use loop
│   ├── config.py            # Settings (env vars via pydantic-settings)
│   ├── database.py          # Async SQLAlchemy engine + session
│   ├── models.py            # DB models (User, Problem, Submission, etc.)
│   ├── routes.py            # API endpoints + WhatsApp webhook
│   ├── recommender.py       # Problem recommendation engine
│   ├── scheduler.py         # Daily problem scheduler
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── leetcode_client.py   # LeetCode GraphQL API client
│   ├── leetcode_sync.py     # Sync solved problems + company data
│   └── whatsapp.py          # Twilio WhatsApp messaging
├── alembic/                 # Database migrations
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd leetcode-agent
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

- `ANTHROPIC_API_KEY` — Your Anthropic API key
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER` — Twilio credentials for WhatsApp
- `DEFAULT_LEETCODE_USERNAME` — (Optional) Default LeetCode username for auto-registered users

### 3. Run the server

```bash
uvicorn main:app --reload
```

Or with Docker:

```bash
docker compose up --build
```

### 4. Seed the problem database

```bash
curl -X POST http://localhost:8000/api/admin/seed-problems
```

### 5. Set up the Twilio webhook

Point your Twilio WhatsApp sandbox webhook to:

```
https://<your-domain>/api/webhook/whatsapp
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/users` | Register a new user |
| `GET` | `/api/users/{whatsapp_number}` | Get user details |
| `DELETE` | `/api/users/{whatsapp_number}` | Deactivate a user |
| `POST` | `/api/webhook/whatsapp` | Twilio WhatsApp webhook |
| `POST` | `/api/admin/seed-problems` | Seed problems from LeetCode |
| `POST` | `/api/admin/send-now/{user_id}` | Manually send a problem to a user |
| `POST` | `/api/admin/sync/{user_id}` | Sync a user's solved problems from LeetCode |
| `POST` | `/api/admin/seed-company/{company_slug}` | Seed company-tagged problems |

## WhatsApp Commands

Just message the bot naturally. It understands:

- **"Give me a problem"** — Get a new recommended problem
- **"I solved it"** — Mark current problem as solved
- **"Skip"** — Skip to a different problem
- **"Hint"** — Get a hint for the current problem
- **"Explain the solution"** — Full solution walkthrough
- **Paste code** — Get a code review
- **"My stats"** — View your progress
- **"Set my username to ..."** — Link your LeetCode profile
- **"I'm preparing for Google"** — Set target company
- **"What is dynamic programming?"** — Get a topic explanation
