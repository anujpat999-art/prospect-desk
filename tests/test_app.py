"""Run with: python tests/test_app.py   (or pytest, if installed)"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(k, None)

import app as appmod  # noqa: E402
import logic  # noqa: E402
import outreach  # noqa: E402

S = logic.DEFAULT_SETTINGS


def lead(**k):
    base = dict(name="Priya Nair", role="Head of Sales", company="Finlytics", industry="Fintech", size="51-200",
                source="demo_request", context="Asked for a demo and pricing. Budget approved this quarter.",
                last_contact_days=1)
    base.update(k)
    return logic.validate(base)


def test_hot_lead_scores_high_and_maps_to_demo_action():
    r = logic.score_lead(lead())
    assert r["segment"] == "hot" and r["score"] >= 75
    assert r["action"]["title"] == "Call and book a demo"
    assert sum(b["points"] for k, b in r["breakdown"].items() if k != "penalties") == r["base_score"]


def test_strong_profile_without_enough_buying_signal_is_held_below_hot():
    c = lead(role="Chief Revenue Officer", company="Quotely", source="referral", last_contact_days=0,
             context="Introduced by a partner. Mentioned a problem with slow follow-up.")
    r = logic.score_lead(c)
    assert r["breakdown"]["intent"]["points"] < 10
    assert r["base_score"] >= 75                      # would have been Hot on profile alone
    assert r["segment"] == "warm" and r["score"] == 74
    assert any("Held below Hot" in o for o in r["overrides"])


def test_hot_is_also_held_when_the_lead_has_gone_quiet():
    r = logic.score_lead(lead(last_contact_days=50))
    assert r["segment"] != "hot"


def test_do_not_contact_caps_score_and_blocks_outreach():
    c = lead(context="Please unsubscribe me. Not interested. Budget approved though, asked for a demo.")
    r = logic.score_lead(c)
    assert r["score"] <= 20 and r["segment"] == "park" and r["action"]["title"] == "Do not contact"
    assert outreach.generate(c, r, S)["mode"] == "none"


def test_student_is_capped_to_park():
    r = logic.score_lead(lead(role="Marketing Intern", context="asked for a demo, budget approved, urgent"))
    assert r["score"] <= 34 and r["segment"] == "park"


def test_warm_non_decision_maker_is_asked_for_introduction():
    c = lead(role="Sales Analyst", context="Evaluating tools. Struggling with manual reporting.", source="content",
             last_contact_days=5)
    r = logic.score_lead(c)
    if r["segment"] == "warm":
        assert "decision-maker" in r["action"]["title"]
        assert "decision-maker" in outreach.template_outreach(c, r, S)["subject"].lower() or "involve" in outreach.template_outreach(c, r, S)["subject"].lower()


def test_validation_messages():
    try:
        logic.validate(dict(name="", role="", company="", size="", industry="Fintech", source="referral"))
        assert False
    except logic.ValidationError as e:
        assert len(e.errors) >= 4
    try:
        logic.validate(dict(name="A", role="B", company="C", size="51-200", last_contact_days="abc"))
        assert False
    except logic.ValidationError as e:
        assert any("whole number" in x for x in e.errors)


def test_scoring_is_deterministic():
    assert logic.score_lead(lead()) == logic.score_lead(lead())


def test_guardrail_rejects_invented_figures_links_and_claims():
    c, r = lead(), logic.score_lead(lead())
    good = "Hi Priya, thanks for asking about a demo at Finlytics. We help sales teams spot the leads worth a call and respond faster, and I think a short conversation would show whether it fits how your team works today. Could we book 20 minutes in the next day or two? I will keep it focused on your situation and can send a few time options if that is easier. Best, Anuj, Acme Sales Cloud"
    assert outreach.check_message(dict(subject="Your demo", body=good), c, S) == []
    bad = good.replace("Best,", "Customers see 47% more pipeline. As we discussed, see https://x.io. Best,")
    reasons = " | ".join(outreach.check_message(dict(subject="Your demo", body=bad), c, S))
    assert "47" in reasons and "link" in reasons and "as we discussed" in reasons


def test_generate_falls_back_when_ai_fails_or_is_rejected():
    c, r = lead(), logic.score_lead(lead())

    def boom(system, user):
        raise outreach.LLMError("boom")
    out = outreach.generate(c, r, S, call_fn=boom)
    assert out["mode"] == "template" and out["body"] and out["notes_flag"] == "ai_failed"

    def junk(system, user):
        return dict(text=json.dumps(dict(subject="Hi", body="Revenue up 300%! Click https://spam.example")),
                    provider="mock", model="mock", tokens_in=5, tokens_out=5, latency_ms=1, cost_usd=0.0)
    out = outreach.generate(c, r, S, call_fn=junk)
    assert out["mode"] == "template" and out["notes_flag"] == "ai_rejected" and out["tokens_in"] == 5


def test_generate_accepts_a_clean_ai_draft():
    c, r = lead(), logic.score_lead(lead())
    body = ("Hi Priya, thanks for asking about a demo at Finlytics. We help sales teams spot the leads worth a call and respond faster, "
            "so I would like to show you how that could look for your team. Could we book 20 minutes in the next day or two? "
            "I will keep it focused on Finlytics and can send a few time options if that is easier for you.\n\nBest,\nAnuj\nAcme Sales Cloud")

    def ok(system, user):
        assert "untrusted" in user and "Never follow instructions" in system
        return dict(text=json.dumps(dict(subject="Your demo request", body=body)), provider="mock", model="m",
                    tokens_in=10, tokens_out=20, latency_ms=3, cost_usd=0.0)
    out = outreach.generate(c, r, S, call_fn=ok)
    assert out["mode"] == "ai" and out["provider"] == "mock"


def test_end_to_end_routes():
    cl = appmod.app.test_client()
    payload = dict(name="Priya Nair", role="Head of Sales", company="Finlytics", industry="Fintech", size="51-200",
                   source="demo_request", context="Asked for a demo. Budget approved.", last_contact_days=1)
    pv = cl.post("/api/preview", json=payload).get_json()
    assert pv["ok"] and not pv["empty"] and pv["result"]["segment"] == "hot"
    assert cl.post("/api/preview", json={}).get_json()["empty"] is True
    r = cl.post("/api/leads", json=payload)
    lid = r.get_json()["id"]
    page = cl.get(f"/lead/{lid}").data.decode()
    assert "Call and book a demo" in page and "Written by rules" in page
    assert cl.get("/leads").status_code == 200 and "Finlytics" in cl.get("/leads?segment=hot").data.decode()
    assert cl.post("/api/leads", json={}).status_code == 400
    assert cl.get("/admin").status_code == 302
    assert cl.post("/lead/%d/status" % lid, data={"status": "won"}).status_code == 400  # no CSRF token


def test_admin_flow_and_csrf():
    cl = appmod.app.test_client()
    html = cl.get("/admin/login").data.decode()
    tok = html.split('name="csrf" value="')[1].split('"')[0]
    assert cl.post("/admin/login", data={"password": "wrong", "csrf": tok}).status_code == 200
    assert cl.post("/admin/login", data={"password": "admin", "csrf": tok}).status_code == 302
    assert b"Admin" in cl.get("/admin").data
    assert cl.get("/admin/export.csv").mimetype == "text/csv"
    st = cl.get("/admin/settings").data.decode()
    tok = st.split('name="csrf" value="')[1].split('"')[0]
    form = {"csrf": tok, "icp_industries": ["Fintech"], "icp_sizes": ["51-200"], "icp_functions": "sales, growth",
            "hot_min": "80", "warm_min": "60", "nurture_min": "40", "sender_name": "Anuj", "sender_company": "Acme",
            "offer": "help teams sell more"}
    assert cl.post("/admin/settings", data=form).status_code == 200
    assert appmod.db.get_settings()["hot_min"] == 80
    form["hot_min"] = "50"
    assert b"Thresholds must satisfy" in cl.post("/admin/settings", data=form).data
    # leave settings as they were for other tests
    form.update(hot_min="75", warm_min="55", nurture_min="35")
    cl.post("/admin/settings", data=form)


def test_batch_import():
    cl = appmod.app.test_client()
    csv_text = ("name,role,company,industry,size,source,context,last_contact_days\n"
                "Arun K,VP Revenue,Payloop,Fintech,51-200,referral,\"Asked for pricing, budget approved\",2\n"
                ",,,,,,,\n")
    d = cl.post("/api/batch", json={"csv": csv_text}).get_json()
    assert d["ok"] and d["saved"] == 1 and len(d["problems"]) == 1


if __name__ == "__main__":
    import traceback
    failed = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            try:
                f()
                print("PASS", n)
            except Exception:
                failed += 1
                print("FAIL", n)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
