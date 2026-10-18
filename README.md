# Prospect Desk: Lead Intelligence Tool (Sales track)

Score a lead out of 100, see exactly why, get a segment (Hot, Warm, Nurture, Park), a next action with a reply time,
and a personalised outreach message. Every lead is stored, and an admin dashboard shows how the system is being used.

**Input:** role, company, context (plus industry, size, source and days since last contact)
**Output:** lead score, segment, outreach message, next action
**Logic:** scoring rules, segmentation, override rules, action mapping

## How it works

```
Browser form --> Flask API --> validation --> LOGIC LAYER --> outreach writer --> SQLite --> result page
 (live preview)                              score, overrides,   AI (optional)     leads        + admin
                                             segment, action     or rules          settings     dashboard
                                                                                   events
```

The logic layer runs first and is deterministic. The AI, when a key is configured, only phrases the message. It cannot change
the score, segment or action, and its output is checked before it is shown. If the AI is off, fails, or is rejected by the
checks, a rule-based writer produces the message, so the app is fully usable with no API key.

| File | Purpose |
|---|---|
| `logic.py` | validation, scoring (5 categories, 100 points), override rules, segmentation, action mapping |
| `outreach.py` | rule-based writer, optional LLM writer (Gemini, OpenAI or Anthropic), output checks |
| `db.py` | SQLite tables: `leads`, `settings`, `events`; dashboard queries |
| `app.py` | routes and the request pipeline |
| `templates/`, `static/` | pages, styles, live-scorecard script |
| `tests/test_app.py` | 14 tests for logic, guardrails, fallback and routes |
| `docs/` | documentation PDF and screenshots |
| `sample_data/leads.csv` | 14 example leads to paste into the CSV importer |

## Run locally

```
pip install -r requirements.txt
python app.py                 # http://localhost:5000
python tests/test_app.py      # run the tests
```

Optional environment variables (see `.env.example`): `ADMIN_PASSWORD`, `SECRET_KEY`, `DB_PATH`, and one of
`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`.
The admin dashboard is at `/admin`. The default password is `admin`, so set `ADMIN_PASSWORD` before sharing the link.

## Deploy

Any Python host works. Start command: `gunicorn app:app`. SQLite needs a persistent disk, or point `DB_PATH` at a mounted volume.
