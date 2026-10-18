"""Prospect Desk: a lead intelligence tool (Sales track).

Request flow for POST /api/leads
  1. form data arrives as JSON from the page          (templates/index.html)
  2. validate                                         (logic.validate)
  3. score, segment, action: the logic layer          (logic.score_lead)
  4. write outreach: AI if a key is set, else rules   (outreach.generate)
  5. store the lead and an audit event                (db.add_lead)
  6. the browser opens the structured result page     (templates/lead.html)
The admin dashboard reads the same tables           (templates/admin.html)
"""
import csv
import hashlib
import hmac
import io
import os
import secrets
from datetime import datetime
from functools import wraps

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for)

import db
import logic
import outreach

app = Flask(__name__)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("prospect-desk:" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=256 * 1024)
db.init()


# ---------- helpers ----------
@app.context_processor
def inject():
    return dict(SEGMENT_LABELS=logic.SEGMENT_LABELS, SEGMENT_NOTES=logic.SEGMENT_NOTES, STATUS_LABELS=logic.STATUS_LABELS,
                STATUSES=logic.STATUSES, CATEGORY_LABELS=logic.CATEGORY_LABELS, SOURCES=logic.SOURCES,
                csrf=csrf_token, is_admin=bool(session.get("admin")))


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def check_csrf():
    # JSON API calls need no token: browsers only send application/json cross-site after a CORS preflight, which we never allow.
    if request.method == "POST" and not request.is_json:
        if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.template_filter("dt")
def fmt_dt(v):
    try:
        return datetime.fromisoformat(v).strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return v or ""


@app.template_filter("short")
def short(v, n=60):
    v = v or ""
    return v if len(v) <= n else v[: n - 1].rstrip() + "…"


def admin_only(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def api_error(errors, code=400):
    return jsonify(ok=False, errors=errors), code


# ---------- public pages ----------
@app.get("/")
def index():
    return render_template("index.html", sizes=logic.SIZES, industries=logic.INDUSTRIES,
                           ai_on=outreach.llm_configured())


@app.get("/leads")
def leads():
    seg, status, q = request.args.get("segment"), request.args.get("status"), (request.args.get("q") or "").strip()[:60]
    rows = db.list_leads(seg, status, q)
    return render_template("leads.html", rows=rows, seg=seg, status=status, q=q)


@app.get("/lead/<int:lid>")
def lead(lid):
    row = db.get_lead(lid)
    if not row:
        abort(404)
    result = dict(score=row["score"], segment=row["segment"], segment_label=logic.SEGMENT_LABELS[row["segment"]],
                  segment_note=logic.SEGMENT_NOTES[row["segment"]], breakdown=row["breakdown"], signals=row["signals"],
                  flags=row["flags"], overrides=row["overrides"],
                  thresholds=row["thresholds"] or dict(hot=75, warm=55, nurture=35),
                  action=dict(title=row["action_title"], detail=row["action_detail"], sla=row["action_sla"],
                              channel=row["action_channel"]))
    return render_template("lead.html", r=row, result=result)


@app.post("/lead/<int:lid>/status")
def lead_status(lid):
    if not db.update_status(lid, request.form.get("status", "")):
        abort(400, "That status is not valid.")
    return redirect(url_for("lead", lid=lid))


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


# ---------- API ----------
@app.post("/api/preview")
def api_preview():
    """Live scorecard while the user types. No AI call and nothing is saved."""
    payload = request.get_json(silent=True) or {}
    try:
        clean = logic.validate(payload, strict=False)
    except logic.ValidationError as e:
        return api_error(e.errors)
    if not clean["role"] and not clean["company"]:
        return jsonify(ok=True, empty=True)
    result = logic.score_lead(clean, db.get_settings())
    return jsonify(ok=True, empty=False, result=result, html=render_template("_scorecard.html", result=result))


@app.post("/api/leads")
def api_leads():
    payload = request.get_json(silent=True) or {}
    try:
        clean = logic.validate(payload)
    except logic.ValidationError as e:
        return api_error(e.errors)
    settings = db.get_settings()
    result = logic.score_lead(clean, settings)          # logic layer first
    out = outreach.generate(clean, result, settings)    # AI or template second
    lid = db.add_lead(clean, result, out)
    return jsonify(ok=True, id=lid)


_COLS = ["name", "role", "company", "industry", "size", "source", "context", "last_contact_days"]


def _match(value, options):
    v = (value or "").strip().lower()
    for o in options:
        if o.lower() == v:
            return o
    return None


@app.post("/api/batch")
def api_batch():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("csv") or "").strip()
    if not text:
        return api_error(["Paste at least one row."])
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and rows[0][0].strip().lower() in ("name", "lead", "lead name"):
        rows = rows[1:]
    if not rows:
        return api_error(["Only a header row was found."])
    if len(rows) > 50:
        return api_error(["Import up to 50 leads at a time."])
    settings, saved, problems = db.get_settings(), [], []
    labels = {v[0].lower(): k for k, v in logic.SOURCES.items()}
    for i, row in enumerate(rows, 1):
        row = [c.strip() for c in row] + [""] * (len(_COLS) - len(row))
        d = dict(zip(_COLS, row))
        d["industry"] = _match(d["industry"], logic.INDUSTRIES) or "Other"
        d["size"] = _match(d["size"], logic.SIZES) or d["size"]
        src = d["source"].lower()
        d["source"] = src if src in logic.SOURCES else labels.get(src, "signup" if not src else d["source"])
        try:
            clean = logic.validate(d)
        except logic.ValidationError as e:
            problems.append(f"Row {i}: " + " ".join(e.errors))
            continue
        result = logic.score_lead(clean, settings)
        saved.append(db.add_lead(clean, result, _template_only(clean, result, settings)))
    if not saved:
        return api_error(problems or ["No rows could be imported."])
    return jsonify(ok=True, saved=len(saved), problems=problems)


def _template_only(clean, result, settings):
    """Batch imports skip the AI to keep cost and time down."""
    out = dict(mode="none", subject=None, body=None, provider=None, model=None, tokens_in=0, tokens_out=0,
               latency_ms=0, cost_usd=0.0, notes=[])
    msg = outreach.template_outreach(clean, result, settings)
    if msg:
        out.update(msg, mode="template")
        out["notes"].append("Written by rules (batch import).")
    else:
        out["notes"].append("No outreach drafted for this segment.")
    return out


# ---------- admin ----------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    err = None
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session["admin"] = True
            db.log_event("admin_login", None, "")
            return redirect(url_for("admin"))
        err = "That password is not correct."
    return render_template("login.html", err=err)


@app.get("/admin")
@admin_only
def admin():
    one, seg, st, src, ev, daily, overrides = db.stats()
    return render_template("admin.html", s=one, seg=seg, st=st, src=src, ev=ev, daily=daily, overrides=overrides,
                           events=db.recent_events(25), rows=db.list_leads(limit=15),
                           default_pw=(ADMIN_PASSWORD == "admin"), ai_on=outreach.llm_configured(),
                           provider=outreach.provider_info()[:2])


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_only
def admin_settings():
    errors, saved = [], False
    if request.method == "POST":
        try:
            db.save_settings(logic.validate_settings(request.form))
            saved = True
        except logic.ValidationError as e:
            errors = e.errors
    return render_template("settings.html", s=db.get_settings(), industries=logic.INDUSTRIES, sizes=logic.SIZES,
                           functions=list(logic.FUNCTION_WORDS), errors=errors, saved=saved)


@app.post("/admin/delete/<int:lid>")
@admin_only
def admin_delete(lid):
    db.delete_lead(lid)
    return redirect(url_for("admin"))


@app.get("/admin/export.csv")
@admin_only
def export():
    rows = db.export_rows()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["id"])
    w.writeheader()
    for r in rows:
        # prefix cells that could be read as spreadsheet formulas
        w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.get("/admin/logout")
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.errorhandler(400)
@app.errorhandler(404)
def err(e):
    return render_template("error.html", e=e), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=bool(os.environ.get("DEBUG")))
