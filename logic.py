"""Prospect Desk logic layer.

Everything in this file is deterministic and runs BEFORE any AI call.
It validates input, scores a lead out of 100, applies override rules,
picks a segment and maps the segment to a next action.
The AI (if configured) only writes the outreach message afterwards, and never
changes the score, segment or action.
"""
import re

# ---------- reference lists (used by the form and by validation) ----------
SIZES = ["1-10", "11-50", "51-200", "201-1000", "1001-5000", "5000+"]
INDUSTRIES = ["SaaS / Software", "Fintech", "E-commerce", "Healthcare IT", "Education Technology",
              "Logistics", "Manufacturing", "Real estate", "Consulting / Agency", "Public sector", "Other"]
SOURCES = {
    "referral": ("Referral from a customer or partner", 10),
    "demo_request": ("Demo or pricing request", 9),
    "event": ("Event or conference", 7),
    "content": ("Webinar or content download", 6),
    "signup": ("Website sign-up", 5),
    "outbound": ("Outbound outreach", 4),
    "cold_list": ("Purchased or cold list", 2),
}
STATUSES = ["new", "contacted", "meeting_booked", "won", "lost"]
STATUS_LABELS = {"new": "New", "contacted": "Contacted", "meeting_booked": "Meeting booked",
                 "won": "Won", "lost": "Lost"}
SEGMENTS = ["hot", "warm", "nurture", "park"]
SEGMENT_LABELS = {"hot": "Hot", "warm": "Warm", "nurture": "Nurture", "park": "Park"}
SEGMENT_NOTES = {"hot": "Sales-ready", "warm": "Worth a conversation",
                 "nurture": "Not ready yet", "park": "Not a fit right now"}

# Maximum points per category. They add up to 100.
MAX_POINTS = {"role": 30, "company": 20, "intent": 30, "source": 10, "recency": 10}
CATEGORY_LABELS = {"role": "Role", "company": "Company fit", "intent": "Buying signals",
                   "source": "Lead source", "recency": "Recent contact"}

# The ideal customer profile. Editable from the admin settings page.
DEFAULT_SETTINGS = {
    "icp_industries": ["SaaS / Software", "Fintech", "E-commerce", "Healthcare IT"],
    "icp_sizes": ["51-200", "201-1000"],
    "icp_functions": ["sales", "revenue", "growth", "marketing", "operations", "business development",
                      "customer success"],
    "hot_min": 75, "warm_min": 55, "nurture_min": 35,
    "sender_name": "Anuj",
    "sender_company": "Acme Sales Cloud",
    "offer": "help sales teams spot the leads worth a call and respond faster",
}

# ---------- seniority and function ----------
SENIORITY_RULES = [  # checked in order, first match wins
    ("executive", 20, "Executive or owner", r"\b(ceo|cfo|coo|cto|cmo|cro|chief|founder|co-?founder|owner|president|managing director|partner)\b"),
    ("senior", 16, "Director level or above", r"\b(vp|vice president|svp|evp|head of|director|general manager|gm)\b"),
    ("manager", 10, "Manager or team lead", r"\b(manager|team lead|lead|principal|supervisor)\b"),
    ("student", 1, "Student, intern or trainee", r"\b(student|intern|trainee|fresher)\b"),
    ("ic", 5, "Individual contributor", r"\b(analyst|associate|executive|specialist|coordinator|engineer|representative|rep|consultant|assistant|officer|developer|designer)\b"),
]
FUNCTION_WORDS = {
    "sales": r"\bsales?\b", "revenue": r"\b(revenue|revops)\b", "growth": r"\bgrowth\b",
    "marketing": r"\bmarketing\b", "operations": r"\b(operations?|ops)\b",
    "business development": r"\b(business development|bd)\b", "customer success": r"\b(customer success|support)\b",
    "finance": r"\b(finance|accounting)\b", "engineering": r"\b(engineering|technology|it|data)\b",
    "hr": r"\b(hr|people|talent)\b", "product": r"\bproduct\b", "legal": r"\blegal\b",
}

# ---------- buying signals found in the free-text context ----------
# (key, points, label, regex). Positive points count towards the 30-point intent cap.
POSITIVE_SIGNALS = [
    ("demo", 10, "Asked for a demo, pricing or trial", r"\b(demo|pricing|price quote|quote|proposal|trial|pilot)\b"),
    ("budget", 8, "Budget confirmed", r"\b(budget (is )?(approved|allocated|available|set|confirmed)|approved budget|have (a )?budget|funding approved)\b"),
    ("timeline", 7, "Near-term timeline", r"\b(this (week|month|quarter)|next (week|month)|asap|urgent|immediately|by (the )?(end of )?(q[1-4]|month|quarter)|deadline|go[- ]live)\b"),
    ("evaluating", 6, "Actively comparing options", r"\b(evaluat\w+|compar\w+|shortlist\w*|rfp|alternatives? to|switching from|replac\w+)\b"),
    ("pain", 5, "Described a problem to solve", r"\b(struggl\w+|pain|problems?|challenges?|manual|losing|churn|inefficien\w+|bottleneck|missing targets?|too slow)\b"),
    ("decision", 5, "Is or reaches the decision-maker", r"\b(decision[- ]maker|final say|sign[- ]off|owns? the budget|i decide|reports? (directly )?to the (ceo|cfo|coo|cro))\b"),
    ("referral", 4, "Came via a referral", r"\b(referred|referral|introduced by|intro from|recommended by)\b"),
]
NEGATIVE_SIGNALS = [
    ("dnc", -20, "Asked not to be contacted", r"\b(not interested|unsubscribe|stop emailing|remove me|do not contact|don't contact)\b"),
    ("no_budget", -10, "Said there is no budget", r"\b(no budget|not budgeted|budget freeze|no funding)\b"),
    ("not_buyer", -12, "Likely not a buyer", r"\b(just (browsing|looking|curious)|for (a|my) (class|assignment|thesis|project)|student project|competitor)\b"),
    ("low_urgency", -6, "Low urgency", r"\b(maybe next year|next year|not (a )?priority|no (urgency|rush)|later this year)\b"),
]
INTENT_CAP = 30


class ValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------- validation ----------
def _clean_text(v, limit):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit] if limit < 400 else str(v or "").strip()[:limit]


def validate(payload, strict=True):
    """Return a clean dict. In non-strict mode (live preview) missing fields are allowed."""
    errors = []
    name = _clean_text(payload.get("name"), 80)
    role = _clean_text(payload.get("role"), 100)
    company = _clean_text(payload.get("company"), 100)
    industry = (payload.get("industry") or "Other").strip()
    size = (payload.get("size") or "").strip()
    source = (payload.get("source") or "signup").strip()
    context = str(payload.get("context") or "").strip()
    days_raw = payload.get("last_contact_days")

    if strict:
        if not name:
            errors.append("Lead name is required.")
        if not role:
            errors.append("Role is required, for example 'Head of Sales'.")
        if not company:
            errors.append("Company is required.")
        if not size:
            errors.append("Choose a company size.")
    if len(str(payload.get("name") or "")) > 80:
        errors.append("Lead name is longer than 80 characters.")
    if len(str(payload.get("role") or "")) > 100:
        errors.append("Role is longer than 100 characters.")
    if len(str(payload.get("company") or "")) > 100:
        errors.append("Company is longer than 100 characters.")
    if size and size not in SIZES:
        errors.append("Company size is not a valid option.")
    if industry not in INDUSTRIES:
        errors.append("Industry is not a valid option.")
    if source not in SOURCES:
        errors.append("Lead source is not a valid option.")
    if len(context) > 1500:
        errors.append("Context is longer than 1,500 characters. Shorten it to the key facts.")
    days = None
    if days_raw not in (None, ""):
        try:
            days = int(float(str(days_raw).strip()))
            if days < 0 or days > 3650:
                raise ValueError
        except ValueError:
            errors.append("Days since last contact must be a whole number from 0 to 3650.")
            days = None
    if errors:
        raise ValidationError(errors)
    return dict(name=name, role=role, company=company, industry=industry, size=size, source=source,
                context=context, last_contact_days=days)


def validate_settings(form):
    errors, s = [], {}
    s["icp_industries"] = [i for i in form.getlist("icp_industries") if i in INDUSTRIES]
    s["icp_sizes"] = [i for i in form.getlist("icp_sizes") if i in SIZES]
    funcs = [f.strip().lower() for f in (form.get("icp_functions") or "").split(",") if f.strip()]
    s["icp_functions"] = [f for f in funcs if f in FUNCTION_WORDS][:12]
    if not s["icp_industries"]:
        errors.append("Pick at least one target industry.")
    if not s["icp_sizes"]:
        errors.append("Pick at least one target company size.")
    if not s["icp_functions"]:
        errors.append("List at least one target function from: " + ", ".join(FUNCTION_WORDS) + ".")
    try:
        s["hot_min"], s["warm_min"], s["nurture_min"] = (int(form.get(k, "")) for k in ("hot_min", "warm_min", "nurture_min"))
        if not (0 < s["nurture_min"] < s["warm_min"] < s["hot_min"] <= 100):
            errors.append("Thresholds must satisfy 0 < nurture < warm < hot <= 100.")
    except ValueError:
        errors.append("Thresholds must be whole numbers.")
    for k, lim in (("sender_name", 60), ("sender_company", 80), ("offer", 160)):
        v = _clean_text(form.get(k), lim)
        if not v:
            errors.append(f"{k.replace('_', ' ').capitalize()} cannot be empty.")
        s[k] = v
    if errors:
        raise ValidationError(errors)
    return s


# ---------- scoring ----------
def _first_match(text, rules):
    for r in rules:
        if re.search(r[3], text):
            return r
    return None


def score_lead(clean, settings=None):
    """Return the full scorecard. Pure function: same input, same output."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    role_l, ctx_l = clean["role"].lower(), clean["context"].lower()
    breakdown, flags, overrides, signals = {}, [], [], []

    # 1. role (max 30): seniority up to 20 + function fit up to 10
    sen = _first_match(role_l, SENIORITY_RULES)
    if sen:
        sen_key, sen_pts, sen_label = sen[0], sen[1], sen[2]
    else:
        sen_key, sen_pts, sen_label = "unknown", 4, "Seniority unclear"
        if clean["role"]:
            flags.append("Seniority could not be read from the role title.")
    functions = [f for f, rx in FUNCTION_WORDS.items() if re.search(rx, role_l)]
    fit = [f for f in functions if f in s["icp_functions"]]
    if fit:
        fn_pts, fn_label = 10, f"{fit[0].capitalize()} matches a target function"
    elif functions:
        fn_pts, fn_label = 3, f"{functions[0].capitalize()} is outside the target functions"
    else:
        fn_pts, fn_label = 2, "Function could not be read from the title"
    breakdown["role"] = dict(points=sen_pts + fn_pts, max=MAX_POINTS["role"], reason=f"{sen_label}. {fn_label}.")

    # 2. company fit (max 20): industry 10 + size 10
    ind_in = clean["industry"] in s["icp_industries"]
    ind_pts = 10 if ind_in else 3
    if clean["size"] in s["icp_sizes"]:
        size_pts, size_note = 10, "size is in the target range"
    elif clean["size"] in SIZES:
        gap = min(abs(SIZES.index(clean["size"]) - SIZES.index(x)) for x in s["icp_sizes"])
        size_pts, size_note = (6, "size is one band from the target range") if gap == 1 else (2, "size is far from the target range")
    else:
        size_pts, size_note = 0, "size not given"
    breakdown["company"] = dict(points=ind_pts + size_pts, max=MAX_POINTS["company"],
                                reason=f"{clean['industry']} {'is a target industry' if ind_in else 'is not a target industry'}; {size_note}.")

    # 3. buying signals from context (max 30)
    pos = 0
    for key, pts, label, rx in POSITIVE_SIGNALS:
        if re.search(rx, ctx_l):
            pos += pts
            signals.append(dict(key=key, points=pts, label=label))
    pos = min(pos, INTENT_CAP)
    neg = 0
    for key, pts, label, rx in NEGATIVE_SIGNALS:
        if re.search(rx, ctx_l):
            neg += pts
            signals.append(dict(key=key, points=pts, label=label))
    neg = max(neg, -25)
    if not clean["context"]:
        flags.append("No context was provided, so buying signals could not be assessed.")
    pos_labels = [x["label"] for x in signals if x["points"] > 0]
    breakdown["intent"] = dict(points=pos, max=MAX_POINTS["intent"],
                               reason="; ".join(pos_labels) + "." if pos_labels else "No buying signals found in the context.")

    # 4. source (max 10)
    src_label, src_pts = SOURCES[clean["source"]]
    breakdown["source"] = dict(points=src_pts, max=MAX_POINTS["source"], reason=f"{src_label}.")

    # 5. recency (max 10)
    d = clean["last_contact_days"]
    if d is None:
        rec_pts, rec_note = 5, "No contact logged yet, scored as neutral"
    elif d <= 3:
        rec_pts, rec_note = 10, f"Last contact {d} day{'s' if d != 1 else ''} ago"
    elif d <= 7:
        rec_pts, rec_note = 8, f"Last contact {d} days ago"
    elif d <= 14:
        rec_pts, rec_note = 6, f"Last contact {d} days ago"
    elif d <= 30:
        rec_pts, rec_note = 3, f"Last contact {d} days ago"
    else:
        rec_pts, rec_note = 0, f"Last contact {d} days ago, the lead is going cold"
        flags.append("No contact for over 30 days: the lead is going cold.")
    breakdown["recency"] = dict(points=rec_pts, max=MAX_POINTS["recency"], reason=rec_note + ".")

    base = sum(b["points"] for b in breakdown.values())
    penalty = neg
    score = max(0, min(100, base + penalty))
    if penalty:
        neg_labels = [f"{x['label']} ({x['points']})" for x in signals if x["points"] < 0]
        breakdown["penalties"] = dict(points=penalty, max=0, reason="; ".join(neg_labels) + ".")

    # ---------- override rules (each one is recorded so the user can see it) ----------
    keys = {x["key"] for x in signals}
    if "dnc" in keys and score > 20:
        score = 20
        overrides.append("Score capped at 20: the lead asked not to be contacted.")
    if sen_key == "student" and score > 34:
        score = 34
        overrides.append("Score capped at 34: a student or intern is rarely the buyer.")
    if score >= s["hot_min"] and pos < 10:
        score = s["hot_min"] - 1
        overrides.append(f"Held below Hot ({s['hot_min']}): a Hot lead needs at least 10 points of buying signals, not just a good profile.")
    if score >= s["hot_min"] and d is not None and d > 45:
        score = s["hot_min"] - 1
        overrides.append("Held below Hot: no contact for over 45 days.")

    segment = ("hot" if score >= s["hot_min"] else "warm" if score >= s["warm_min"]
               else "nurture" if score >= s["nurture_min"] else "park")

    action = map_action(segment, keys, sen_key)
    return dict(score=score, base_score=base, penalty=penalty, segment=segment,
                segment_label=SEGMENT_LABELS[segment], segment_note=SEGMENT_NOTES[segment],
                breakdown=breakdown, signals=signals, flags=flags, overrides=overrides,
                seniority=sen_key, action=action,
                thresholds=dict(hot=s["hot_min"], warm=s["warm_min"], nurture=s["nurture_min"]))


# ---------- action mapping ----------
def map_action(segment, keys, seniority):
    if "dnc" in keys:
        return dict(title="Do not contact", detail="Remove the lead from all sequences and record the request.",
                    sla="Today", channel="None")
    if segment == "hot":
        if "demo" in keys:
            return dict(title="Call and book a demo", detail="They asked for a demo or pricing, so reply while the interest is fresh.",
                        sla="Within 4 business hours", channel="Phone, then email")
        return dict(title="Call, then send a tailored email", detail="Strong fit with real buying signals. Use the call to confirm need and timing.",
                    sla="Within 1 business day", channel="Phone, then email")
    if segment == "warm":
        if seniority in ("ic", "manager", "unknown") and "decision" not in keys:
            return dict(title="Ask for an introduction to the decision-maker",
                        detail="Interest is there, but this contact may not hold the budget. Ask who else should be involved.",
                        sla="This week", channel="Email")
        return dict(title="Send a discovery email and propose a 20-minute call",
                    detail="A good fit that needs one conversation to confirm need and timing.",
                    sla="Within 3 business days", channel="Email")
    if segment == "nurture":
        return dict(title="Add to the nurture sequence", detail="Fit or timing is not there yet. Share useful content and review again in 30 days.",
                    sla="Review in 30 days", channel="Email sequence")
    return dict(title="No rep time for now", detail="Keep on the newsletter list only and re-score if their situation changes.",
                sla="Re-score if status changes", channel="Newsletter")
