"""SQLite storage: leads, settings, events.

leads    one row per scored lead, including the full scorecard and the outreach
settings the ideal customer profile, thresholds and sender details (one JSON value per key)
events   an audit trail of what the system did, used by the admin dashboard
"""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import logic

DB_PATH = os.environ.get("DB_PATH", "prospect.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  name TEXT NOT NULL, role TEXT NOT NULL, company TEXT NOT NULL,
  industry TEXT, size TEXT, source TEXT, context TEXT, last_contact_days INTEGER,
  score INTEGER NOT NULL, base_score INTEGER, segment TEXT NOT NULL, seniority TEXT,
  action_title TEXT, action_detail TEXT, action_sla TEXT, action_channel TEXT,
  breakdown_json TEXT, signals_json TEXT, flags_json TEXT, overrides_json TEXT, thresholds_json TEXT,
  outreach_mode TEXT,                 -- ai | template | none
  outreach_subject TEXT, outreach_body TEXT, outreach_notes_json TEXT,
  provider TEXT, model TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new'  -- new | contacted | meeting_booked | won | lost
);
CREATE INDEX IF NOT EXISTS idx_leads_segment ON leads(segment);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
  lead_id INTEGER, detail TEXT
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with closing(_conn()) as c:
        c.executescript(SCHEMA)
        c.commit()


def log_event(type_, lead_id=None, detail=""):
    with closing(_conn()) as c:
        c.execute("INSERT INTO events (ts, type, lead_id, detail) VALUES (?,?,?,?)", (_now(), type_, lead_id, detail[:300]))
        c.commit()


# ---------- settings ----------
def get_settings():
    with closing(_conn()) as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = dict(logic.DEFAULT_SETTINGS)
    for r in rows:
        try:
            s[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            pass
    return s


def save_settings(s):
    with closing(_conn()) as c:
        for k, v in s.items():
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (k, json.dumps(v)))
        c.commit()
    log_event("settings_updated", None, "Scoring settings changed")


# ---------- leads ----------
def add_lead(clean, result, out):
    a = result["action"]
    with closing(_conn()) as c:
        cur = c.execute(
            """INSERT INTO leads (created_at, name, role, company, industry, size, source, context, last_contact_days,
               score, base_score, segment, seniority, action_title, action_detail, action_sla, action_channel,
               breakdown_json, signals_json, flags_json, overrides_json, thresholds_json,
               outreach_mode, outreach_subject, outreach_body, outreach_notes_json,
               provider, model, tokens_in, tokens_out, latency_ms, cost_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), clean["name"], clean["role"], clean["company"], clean["industry"], clean["size"], clean["source"],
             clean["context"], clean["last_contact_days"], result["score"], result["base_score"], result["segment"],
             result["seniority"], a["title"], a["detail"], a["sla"], a["channel"],
             json.dumps(result["breakdown"]), json.dumps(result["signals"]), json.dumps(result["flags"]),
             json.dumps(result["overrides"]), json.dumps(result["thresholds"]), out["mode"], out["subject"], out["body"], json.dumps(out["notes"]),
             out["provider"], out["model"], out["tokens_in"], out["tokens_out"], out["latency_ms"], out["cost_usd"]))
        lid = cur.lastrowid
        c.commit()
    log_event("lead_scored", lid, f"{result['segment']} {result['score']}")
    if out["mode"] == "ai":
        log_event("outreach_ai", lid, f"{out['provider']} {out['model']}")
    elif out.get("notes_flag") == "ai_failed":
        log_event("ai_failed", lid, "; ".join(out["notes"]))
    elif out.get("notes_flag") == "ai_rejected":
        log_event("ai_rejected", lid, "; ".join(out["notes"]))
    return lid


def _decode(row):
    d = dict(row)
    for k in ("breakdown", "signals", "flags", "overrides", "outreach_notes", "thresholds"):
        raw = d.pop(k + "_json", None)
        d[k] = json.loads(raw) if raw else ({} if k in ("breakdown", "thresholds") else [])
    return d


def get_lead(lid):
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
    return _decode(r) if r else None


def list_leads(segment=None, status=None, q=None, limit=300):
    sql, args = "SELECT * FROM leads WHERE 1=1", []
    if segment in logic.SEGMENTS:
        sql += " AND segment=?"
        args.append(segment)
    if status in logic.STATUSES:
        sql += " AND status=?"
        args.append(status)
    if q:
        sql += " AND (name LIKE ? OR company LIKE ? OR role LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY score DESC, id DESC LIMIT ?"
    args.append(limit)
    with closing(_conn()) as c:
        return [_decode(r) for r in c.execute(sql, args).fetchall()]


def update_status(lid, status):
    if status not in logic.STATUSES:
        return False
    with closing(_conn()) as c:
        cur = c.execute("UPDATE leads SET status=? WHERE id=?", (status, lid))
        c.commit()
        ok = cur.rowcount > 0
    if ok:
        log_event("status_changed", lid, status)
    return ok


def delete_lead(lid):
    with closing(_conn()) as c:
        c.execute("DELETE FROM leads WHERE id=?", (lid,))
        c.commit()
    log_event("lead_deleted", lid, "")


def recent_events(n=25):
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,))]


def stats():
    with closing(_conn()) as c:
        one = dict(c.execute("""SELECT COUNT(*) n, AVG(score) avg_score,
            SUM(outreach_mode='ai') ai, SUM(outreach_mode='template') tpl, SUM(outreach_mode='none') none_,
            SUM(cost_usd) cost, SUM(tokens_in) tin, SUM(tokens_out) tout,
            AVG(CASE WHEN outreach_mode='ai' THEN latency_ms END) ai_latency
            FROM leads""").fetchone())
        seg = {r["segment"]: r["n"] for r in c.execute("SELECT segment, COUNT(*) n FROM leads GROUP BY segment")}
        st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
        src = {r["source"]: r["n"] for r in c.execute("SELECT source, COUNT(*) n FROM leads GROUP BY source")}
        ev = {r["type"]: r["n"] for r in c.execute("SELECT type, COUNT(*) n FROM events GROUP BY type")}
        daily = [dict(r) for r in c.execute(
            "SELECT substr(created_at,1,10) day, COUNT(*) n FROM leads GROUP BY day ORDER BY day DESC LIMIT 14")][::-1]
        ov = [r["overrides_json"] for r in c.execute("SELECT overrides_json FROM leads WHERE overrides_json != '[]'")]
    override_hits = {}
    for raw in ov:
        for o in json.loads(raw):
            key = o.split(":")[0]
            override_hits[key] = override_hits.get(key, 0) + 1
    return one, seg, st, src, ev, daily, override_hits


def export_rows():
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(
            """SELECT id, created_at, name, role, company, industry, size, source, last_contact_days, score, segment,
               action_title, action_sla, status, outreach_mode FROM leads ORDER BY id""")]
