"""
TechXR Feedback Dashboard Server
Fetches live Zoho CRM data and serves the dashboard.
"""

import os, requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, make_response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Zoho OAuth (cached — token valid for 1 hr, refresh after 50 min) ────────

_token_cache = {"token": None, "expires_at": 0}

def _fetch_new_token():
    """Fetch a fresh access token from Zoho and cache it."""
    client_id     = os.getenv("ZOHO_CLIENT_ID", "")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET", "")
    refresh_token = os.getenv("ZOHO_REFRESH_TOKEN", "")

    # Check env vars are set
    missing = [k for k, v in [
        ("ZOHO_CLIENT_ID", client_id),
        ("ZOHO_CLIENT_SECRET", client_secret),
        ("ZOHO_REFRESH_TOKEN", refresh_token)
    ] if not v.strip()]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    resp = requests.post("https://accounts.zoho.in/oauth/v2/token", data={
        "grant_type":    "refresh_token",
        "client_id":     client_id.strip(),
        "client_secret": client_secret.strip(),
        "refresh_token": refresh_token.strip(),
    })

    if not resp.ok:
        # Show Zoho's actual error message
        try:
            zoho_err = resp.json()
        except Exception:
            zoho_err = resp.text
        raise ValueError(f"Zoho OAuth failed ({resp.status_code}): {zoho_err}")

    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"No access_token in Zoho response: {data}")

    token = data["access_token"]
    _token_cache["token"]      = token
    _token_cache["expires_at"] = datetime.now().timestamp() + 50 * 60
    return token

def get_access_token():
    """Return cached token if valid, else fetch a new one."""
    now = datetime.now().timestamp()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    return _fetch_new_token()

def get_access_token_with_retry(headers):
    """Call Zoho API with auto token refresh if 401 is returned."""
    token = get_access_token()
    headers["Authorization"] = f"Zoho-oauthtoken {token}"
    return headers

def zoho_get(url, headers, params):
    """GET request with automatic token expiry retry."""
    r = requests.get(url, headers=headers, params=params)
    if r.status_code == 401:
        # Token expired mid-session — force refresh and retry once
        _token_cache["token"] = None
        token = _fetch_new_token()
        headers["Authorization"] = f"Zoho-oauthtoken {token}"
        r = requests.get(url, headers=headers, params=params)
    return r


# ── Zoho CRM fetch ──────────────────────────────────────────────────────────

def fetch_all_feedback():
    headers = {"Authorization": f"Zoho-oauthtoken {get_access_token()}"}
    base    = "https://www.zohoapis.in/crm/v2/Feedback"
    fields  = ("Name,Owner,Rating,Feedback_Status,Remarks,"
               "What_most_you_like_about_the_app,Suggestion_for_improvement,"
               "Issue,Would_You_Refer_Our_Product_to_Someone,Created_Time,"
               "Product_Name")
    all_recs = []
    page = 1
    while True:
        r = zoho_get(base, headers, {"fields": fields, "per_page": 200, "page": page})
        if r.status_code != 200:
            break
        data = r.json()
        recs = data.get("data", [])
        all_recs.extend(recs)
        if not data.get("info", {}).get("more_records"):
            break
        page += 1
    return all_recs


def get_record_date(r):
    ct = r.get("Created_Time", "") or ""
    return ct[:10] if len(ct) >= 10 else ""

def get_owner_name(r):
    owner = r.get("Owner") or {}
    name  = owner.get("name") if isinstance(owner, dict) else "Unassigned"
    return name or "Unassigned"

def get_product(r):
    p = r.get("Product_Name") or ""
    if isinstance(p, dict):
        return p.get("name", "") or "—"
    return str(p).strip() or "—"


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(recs):
    total = len(recs)

    # Rating
    r_counts = {1:0, 2:0, 3:0, 4:0, 5:0}
    r_sum = r_n = 0
    for r in recs:
        try:
            v = int(r.get("Rating") or 0)
            if 1 <= v <= 5:
                r_counts[v] += 1; r_sum += v; r_n += 1
        except: pass
    avg_r = round(r_sum / r_n, 1) if r_n else None
    max_r = max(r_counts.values()) or 1

    # Status counts
    s_counts = {}
    for r in recs:
        s = r.get("Feedback_Status") or "Unknown"
        s_counts[s] = s_counts.get(s, 0) + 1

    satisfied   = s_counts.get("Satisfied", 0)
    not_sat     = s_counts.get("Not Satisfied", 0)
    issue_r     = s_counts.get("Issue Raised", 0)
    in_followup = s_counts.get("In Followup", 0)
    new_f       = s_counts.get("New Feedback", 0)
    critical    = not_sat + issue_r

    def pct(n): return round(n / total * 100) if total else 0

    # Referral
    ref_yes = ref_no = ref_maybe = ref_answered = 0
    for r in recs:
        vals = r.get("Would_You_Refer_Our_Product_to_Someone")
        if not vals: continue
        ref_answered += 1
        flat = (" ".join(vals) if isinstance(vals, list) else str(vals)).lower()
        if any(x in flat for x in ["yes","definitely","absolutely"]): ref_yes += 1
        elif "no" in flat and "not sure" not in flat: ref_no += 1
        elif any(x in flat for x in ["maybe","not sure","possibly"]): ref_maybe += 1
        else: ref_yes += 1

    # Agent-wise
    agent_map = {}
    for r in recs:
        name = get_owner_name(r)
        if name not in agent_map:
            agent_map[name] = {"name": name, "total": 0, "satisfied": 0,
                               "not_sat": 0, "issue_r": 0, "in_followup": 0,
                               "new_f": 0, "critical": 0}
        a = agent_map[name]; a["total"] += 1
        st = r.get("Feedback_Status", "")
        if st == "Satisfied":       a["satisfied"] += 1
        elif st == "Not Satisfied": a["not_sat"] += 1
        elif st == "Issue Raised":  a["issue_r"] += 1
        elif st == "In Followup":   a["in_followup"] += 1
        elif st == "New Feedback":  a["new_f"] += 1
        a["critical"] = a["not_sat"] + a["issue_r"]
    agent_list = sorted(agent_map.values(), key=lambda x: x["total"], reverse=True)

    # Product-wise
    product_map = {}
    for r in recs:
        prod = get_product(r)
        if prod not in product_map:
            product_map[prod] = {"name": prod, "total": 0, "satisfied": 0,
                                 "not_sat": 0, "issue_r": 0, "in_followup": 0,
                                 "new_f": 0, "critical": 0, "r_sum": 0, "r_n": 0}
        p = product_map[prod]; p["total"] += 1
        st = r.get("Feedback_Status", "")
        if st == "Satisfied":       p["satisfied"] += 1
        elif st == "Not Satisfied": p["not_sat"] += 1
        elif st == "Issue Raised":  p["issue_r"] += 1
        elif st == "In Followup":   p["in_followup"] += 1
        elif st == "New Feedback":  p["new_f"] += 1
        p["critical"] = p["not_sat"] + p["issue_r"]
        try:
            v = int(r.get("Rating") or 0)
            if 1 <= v <= 5: p["r_sum"] += v; p["r_n"] += 1
        except: pass
    for p in product_map.values():
        p["avg_r"] = round(p["r_sum"] / p["r_n"], 1) if p["r_n"] else None
        p["sat_pct"] = round(p["satisfied"] / p["total"] * 100) if p["total"] else 0
    product_list = sorted(product_map.values(), key=lambda x: x["total"], reverse=True)

    # Critical records
    crit_recs = [r for r in recs if r.get("Feedback_Status") in ("Not Satisfied", "Issue Raised")]

    # Suggestions flat rows
    skip = {"cnr","busy","n/a","na","no","none",""}
    sug_rows = []
    for r in recs:
        name  = r.get("Name") or "—"
        agent = get_owner_name(r)
        prod  = get_product(r)
        date  = get_record_date(r)
        if r.get("What_most_you_like_about_the_app"):
            sug_rows.append({"name":name,"agent":agent,"product":prod,"date":date,
                             "type":"Liked","content":r["What_most_you_like_about_the_app"]})
        if r.get("Issue"):
            sug_rows.append({"name":name,"agent":agent,"product":prod,"date":date,
                             "type":"Issue","content":r["Issue"]})
        if r.get("Suggestion_for_improvement") and \
           r["Suggestion_for_improvement"].strip().lower() not in ["na","n/a","no improvement","none",""]:
            sug_rows.append({"name":name,"agent":agent,"product":prod,"date":date,
                             "type":"Suggestion","content":r["Suggestion_for_improvement"]})
        if r.get("Remarks") and len(r["Remarks"]) > 5 and \
           r["Remarks"].strip().lower() not in skip:
            sug_rows.append({"name":name,"agent":agent,"product":prod,"date":date,
                             "type":"Remark","content":r["Remarks"]})

    return {
        "total": total,
        "r_counts": r_counts, "avg_r": avg_r, "r_n": r_n, "max_r": max_r,
        "r_pct": {i: round(r_counts[i]/max_r*100) for i in range(1,6)},
        "satisfied": satisfied, "not_sat": not_sat, "issue_r": issue_r,
        "in_followup": in_followup, "new_f": new_f, "critical": critical,
        "sat_pct": pct(satisfied), "crit_pct": pct(critical),
        "followup_pct": pct(in_followup),
        "ref_yes": ref_yes, "ref_no": ref_no, "ref_maybe": ref_maybe,
        "ref_answered": ref_answered,
        "agent_list": agent_list,
        "product_list": product_list,
        "crit_recs": crit_recs,
        "sug_rows": sug_rows,
        "s_counts": s_counts,
    }


# ── HTML Template ─────────────────────────────────────────────────────────────

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TechXR Feedback Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#1a1a2e}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
.header{background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;padding:24px 28px;margin-bottom:16px;color:#fff}
.header h1{font-size:24px;font-weight:800}
.header p{font-size:13px;opacity:.7;margin-top:4px}
.header-meta{font-size:11px;color:#c4b5fd;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
.section-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:#6b7280;margin:20px 0 10px;padding-left:2px}

/* Quick date buttons */
.quick-filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.qbtn{padding:5px 12px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;border:1.5px solid #e5e7eb;background:#fff;color:#6b7280;transition:all .15s}
.qbtn:hover{border-color:#6366f1;color:#6366f1}
.qbtn.active{background:#4f46e5;color:#fff;border-color:#4f46e5}

/* Filter bar */
.filter-bar{background:#fff;border-radius:10px;padding:14px 18px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.filter-row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.filter-group{display:flex;flex-direction:column;gap:4px}
.filter-group label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#6b7280}
.filter-group input,.filter-group select{border:1px solid #e5e7eb;border-radius:6px;padding:6px 10px;font-size:12px;color:#374151;background:#fafafa;outline:none;height:32px}
.filter-group input:focus,.filter-group select:focus{border-color:#6366f1;background:#fff}
.filter-actions{display:flex;gap:8px;align-items:flex-end}
.btn{padding:6px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none;height:32px}
.btn-primary{background:#4f46e5;color:#fff}
.btn-secondary{background:#f3f4f6;color:#374151;text-decoration:none;display:inline-flex;align-items:center}
.active-tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;padding-top:10px;border-top:1px solid #f3f4f6}
.active-tag{display:inline-block;background:#ede9fe;color:#6d28d9;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px}

.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:4px}
.card{background:#fff;border-radius:10px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-label{font-size:10px;font-weight:700;text-transform:uppercase;color:#6b7280;margin-bottom:8px}
.card-val{font-size:34px;font-weight:800;line-height:1}
.card-sub{font-size:11px;color:#9ca3af;margin-top:4px}
.purple{color:#7c3aed}.green{color:#059669}.red{color:#dc2626}.blue{color:#3b82f6}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.panel{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.panel-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#6b7280;margin-bottom:12px}
.rb-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.rb-label{width:24px;font-size:11px;font-weight:600;text-align:right;color:#374151}
.rb-track{flex:1;background:#f3f4f6;border-radius:4px;height:12px;overflow:hidden}
.rb-fill{height:12px;border-radius:4px}
.rb-count{width:24px;font-size:11px;color:#6b7280;text-align:right}
.pct-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.pct-cell{padding:10px 8px;border-radius:8px;text-align:center}
.pct-num{font-size:22px;font-weight:800}
.pct-lbl{font-size:9px;font-weight:700;margin-top:2px;text-transform:uppercase}
.pct-cnt{font-size:9px;margin-top:1px;opacity:.8}
.g{background:#d1fae5;color:#065f46}
.r{background:#fee2e2;color:#991b1b}
.b{background:#dbeafe;color:#1e40af}
.stacked-bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin-bottom:8px}
.bar-seg{height:10px}.bar-rest{flex:1;background:#e5e7eb}
.meta-line{font-size:11px;color:#6b7280}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:9px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#9ca3af;border-bottom:2px solid #f3f4f6;background:#fafafa;position:sticky;top:0;z-index:1}
td{padding:8px 10px;border-bottom:1px solid #f9fafb;font-size:12px;vertical-align:top}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.pill-red{background:#fee2e2;color:#dc2626}
.pill-orange{background:#ffedd5;color:#c2410c}
.pill-green{background:#d1fae5;color:#065f46}
.pill-blue{background:#dbeafe;color:#1e40af}
.pill-gray{background:#f3f4f6;color:#374151}
.pill-yellow{background:#fef9c3;color:#854d0e}
.pill-purple{background:#ede9fe;color:#6d28d9}
.scrollable-table{max-height:400px;overflow-y:auto;border-radius:8px;border:1px solid #f3f4f6}
.agent-bar{height:6px;border-radius:3px;background:#f3f4f6;overflow:hidden;margin-bottom:3px}
.agent-bar-fill{height:6px;border-radius:3px}
.perf{font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;display:inline-block}
.refer-card{display:flex;align-items:center;gap:24px;background:#fff;border-radius:10px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.refer-num{font-size:60px;font-weight:800;color:#7c3aed;line-height:1}
.refer-stats{font-size:13px;color:#6b7280;line-height:2.2}
.footer{text-align:center;margin-top:24px;padding:16px;color:#9ca3af;font-size:11px}
.refresh-btn{display:inline-block;margin-left:12px;background:rgba(255,255,255,.2);color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;text-decoration:none}
.refresh-btn:hover{background:rgba(255,255,255,.3)}
@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}.filter-row{flex-direction:column}}
</style>
</head>
<body>
<div class="wrap">

  <!-- HEADER -->
  <div class="header">
    <div class="header-meta">TechXR · Live Feedback Report</div>
    <h1>📊 Feedback Rating Dashboard</h1>
    <p>{{ date_str }} &nbsp;·&nbsp; {{ m.total }} records shown of {{ total_all }} total &nbsp;·&nbsp;
      <a href="/?date_from={{ default_from }}&date_to={{ default_to }}" class="refresh-btn">🔄 Refresh</a>
    </p>
  </div>

  <!-- FILTERS -->
  <form method="GET" action="/" id="filterForm">
    <div class="filter-bar">

      <!-- Quick date buttons -->
      <div class="quick-filters">
        <span style="font-size:10px;font-weight:700;color:#9ca3af;align-self:center;margin-right:4px;">QUICK:</span>
        <button type="button" class="qbtn {% if active_preset=='today' %}active{% endif %}"
          onclick="setPreset('today')">Today</button>
        <button type="button" class="qbtn {% if active_preset=='yesterday' %}active{% endif %}"
          onclick="setPreset('yesterday')">Yesterday</button>
        <button type="button" class="qbtn {% if active_preset=='7d' %}active{% endif %}"
          onclick="setPreset('7d')">Last 7 Days</button>
        <button type="button" class="qbtn {% if active_preset=='15d' %}active{% endif %}"
          onclick="setPreset('15d')">Last 15 Days</button>
        <button type="button" class="qbtn {% if active_preset=='30d' %}active{% endif %}"
          onclick="setPreset('30d')">Last 30 Days</button>
        <button type="button" class="qbtn {% if active_preset=='90d' %}active{% endif %}"
          onclick="setPreset('90d')">Last 90 Days</button>
        <button type="button" class="qbtn {% if active_preset=='all' %}active{% endif %}"
          onclick="setPreset('all')">All Time</button>
      </div>

      <!-- Filter inputs -->
      <div class="filter-row">
        <div class="filter-group">
          <label>📅 Date From</label>
          <input type="date" name="date_from" id="date_from" value="{{ f_date_from }}" onchange="autoSubmit()">
        </div>
        <div class="filter-group">
          <label>📅 Date To</label>
          <input type="date" name="date_to" id="date_to" value="{{ f_date_to }}" onchange="autoSubmit()">
        </div>
        <div class="filter-group">
          <label>👤 Agent</label>
          <select name="agent" onchange="autoSubmit()">
            <option value="">All Agents</option>
            {% for ag in agents %}
            <option value="{{ ag }}" {% if f_agent==ag %}selected{% endif %}>{{ ag }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="filter-group">
          <label>📦 Product</label>
          <select name="product" onchange="autoSubmit()">
            <option value="">All Products</option>
            {% for pr in products %}
            <option value="{{ pr }}" {% if f_product==pr %}selected{% endif %}>{{ pr }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="filter-group">
          <label>🏷️ Status</label>
          <select name="status" onchange="autoSubmit()">
            <option value="">All Statuses</option>
            {% for st in statuses %}
            <option value="{{ st }}" {% if f_status==st %}selected{% endif %}>{{ st }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="filter-group">
          <label>💬 Suggestion Type</label>
          <select name="sug_type" onchange="autoSubmit()">
            <option value="">All Types</option>
            <option value="Liked"      {% if f_sug_type=='Liked' %}selected{% endif %}>👍 Liked</option>
            <option value="Issue"      {% if f_sug_type=='Issue' %}selected{% endif %}>⚠️ Issue</option>
            <option value="Suggestion" {% if f_sug_type=='Suggestion' %}selected{% endif %}>💡 Suggestion</option>
            <option value="Remark"     {% if f_sug_type=='Remark' %}selected{% endif %}>💬 Remark</option>
          </select>
        </div>
        <div class="filter-actions">
          <a href="/?date_from={{ default_from }}&date_to={{ default_to }}" class="btn btn-secondary">Reset</a>
        </div>
      </div>

      <!-- Active filter tags -->
      {% if f_agent or f_product or f_status or f_sug_type %}
      <div class="active-tags">
        <span style="font-size:10px;color:#9ca3af;align-self:center;">Active:</span>
        {% if f_agent %}<span class="active-tag">👤 {{ f_agent }}</span>{% endif %}
        {% if f_product %}<span class="active-tag">📦 {{ f_product }}</span>{% endif %}
        {% if f_status %}<span class="active-tag">🏷️ {{ f_status }}</span>{% endif %}
        {% if f_sug_type %}<span class="active-tag">💬 {{ f_sug_type }}</span>{% endif %}
        <span class="active-tag" style="background:#dbeafe;color:#1e40af;">
          📅 {{ f_date_from or '—' }} → {{ f_date_to or '—' }}
        </span>
      </div>
      {% endif %}
    </div>
    <!-- Hidden preset field -->
    <input type="hidden" name="preset" id="preset" value="{{ active_preset }}">
  </form>

  <!-- OVERVIEW CARDS -->
  <div class="section-label">📈 Overview</div>
  <div class="cards">
    <div class="card" style="border-top:3px solid #8b5cf6">
      <div class="card-label">📋 Total Records</div>
      <div class="card-val">{{ m.total }}</div>
      <div class="card-sub">of {{ total_all }} total</div>
    </div>
    <div class="card" style="border-top:3px solid #7c3aed">
      <div class="card-label">⭐ Avg Rating</div>
      <div class="card-val purple">{{ m.avg_r or 'N/A' }}</div>
      <div class="card-sub">{{ m.r_n }} of {{ m.total }} rated</div>
    </div>
    <div class="card" style="border-top:3px solid #059669">
      <div class="card-label">✅ Satisfied</div>
      <div class="card-val green">{{ m.satisfied }}</div>
      <div class="card-sub">{{ m.sat_pct }}% of records</div>
    </div>
    <div class="card" style="border-top:3px solid #dc2626">
      <div class="card-label">🚨 Critical</div>
      <div class="card-val red">{{ m.critical }}</div>
      <div class="card-sub">Not satisfied + Issues</div>
    </div>
  </div>

  <!-- RATING + CALL ANALYSIS -->
  <div class="section-label">⭐ Rating Distribution &amp; 📞 Call Analysis</div>
  <div class="grid2">
    <div class="panel">
      <div class="panel-title">⭐ Rating Distribution</div>
      {% for i in [5,4,3,2,1] %}
      {% set colors={5:'#22c55e',4:'#84cc16',3:'#f59e0b',2:'#f97316',1:'#ef4444'} %}
      <div class="rb-row">
        <div class="rb-label">{{ i }}★</div>
        <div class="rb-track"><div class="rb-fill" style="width:{{ m.r_pct[i] }}%;background:{{ colors[i] }};"></div></div>
        <div class="rb-count">{{ m.r_counts[i] }}</div>
      </div>
      {% endfor %}
      <div style="font-size:10px;color:#9ca3af;margin-top:6px;">{{ m.r_n }} of {{ m.total }} rated</div>
    </div>
    <div class="panel">
      <div class="panel-title">📞 Call Analysis — % by Outcome</div>
      <div class="pct-grid">
        <div class="pct-cell g">
          <div class="pct-num">{{ m.sat_pct }}%</div>
          <div class="pct-lbl">✅ Satisfied</div>
          <div class="pct-cnt">{{ m.satisfied }} leads</div>
        </div>
        <div class="pct-cell r">
          <div class="pct-num">{{ m.crit_pct }}%</div>
          <div class="pct-lbl">🚨 Problems</div>
          <div class="pct-cnt">{{ m.critical }} leads</div>
        </div>
        <div class="pct-cell b">
          <div class="pct-num">{{ m.followup_pct }}%</div>
          <div class="pct-lbl">🔄 Follow-up</div>
          <div class="pct-cnt">{{ m.in_followup }} leads</div>
        </div>
      </div>
      <div class="stacked-bar">
        <div class="bar-seg" style="width:{{ m.sat_pct }}%;background:#22c55e;"></div>
        <div class="bar-seg" style="width:{{ m.crit_pct }}%;background:#ef4444;"></div>
        <div class="bar-seg" style="width:{{ m.followup_pct }}%;background:#3b82f6;"></div>
        <div class="bar-rest"></div>
      </div>
      <div class="meta-line">New Feedback: <strong>{{ m.new_f }}</strong> &nbsp;|&nbsp; In Follow-up: <strong>{{ m.in_followup }}</strong></div>
    </div>
  </div>

  <!-- AGENT-WISE REPORT -->
  <div class="section-label" style="border-left:3px solid #6366f1;padding-left:10px;">👤 Agent-Wise Report</div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th>Agent</th><th>Total</th><th>Satisfied</th><th>Critical</th>
          <th>Follow-up</th><th>New</th><th style="min-width:130px;">Performance</th>
        </tr></thead>
        <tbody>
        {% for a in m.agent_list %}
        {% set sp=(a.satisfied/a.total*100)|round|int if a.total else 0 %}
        {% set pc='#059669' if sp>=60 else ('#d97706' if sp>=30 else '#dc2626') %}
        {% set pb='#f0fdf4' if sp>=60 else ('#fffbeb' if sp>=30 else '#fef2f2') %}
        {% set fc='#22c55e' if sp>=60 else ('#f59e0b' if sp>=30 else '#ef4444') %}
        <tr>
          <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ a.name }}</td>
          <td style="text-align:center;font-weight:700;">{{ a.total }}</td>
          <td style="text-align:center;"><span class="pill pill-green">{{ a.satisfied }}</span></td>
          <td style="text-align:center;"><span class="pill pill-red">{{ a.critical }}</span></td>
          <td style="text-align:center;"><span class="pill pill-blue">{{ a.in_followup }}</span></td>
          <td style="text-align:center;"><span class="pill pill-gray">{{ a.new_f }}</span></td>
          <td>
            <div class="agent-bar"><div class="agent-bar-fill" style="width:{{ sp }}%;background:{{ fc }};"></div></div>
            <span class="perf" style="color:{{ pc }};background:{{ pb }};">{{ sp }}% satisfied</span>
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- PRODUCT-WISE SATISFACTION -->
  <div class="section-label" style="border-left:3px solid #0ea5e9;padding-left:10px;">📦 Product-Wise Satisfaction</div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th>Product</th><th>Total</th><th>Avg ⭐</th><th>Satisfied</th>
          <th>Critical</th><th>Follow-up</th><th>New</th><th style="min-width:130px;">Satisfaction</th>
        </tr></thead>
        <tbody>
        {% if m.product_list %}
          {% for p in m.product_list %}
          {% set sp=p.sat_pct %}
          {% set pc='#059669' if sp>=60 else ('#d97706' if sp>=30 else '#dc2626') %}
          {% set pb='#f0fdf4' if sp>=60 else ('#fffbeb' if sp>=30 else '#fef2f2') %}
          {% set fc='#22c55e' if sp>=60 else ('#f59e0b' if sp>=30 else '#ef4444') %}
          <tr>
            <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ p.name }}</td>
            <td style="text-align:center;font-weight:700;">{{ p.total }}</td>
            <td style="text-align:center;color:#7c3aed;font-weight:700;">{{ p.avg_r or '—' }}</td>
            <td style="text-align:center;"><span class="pill pill-green">{{ p.satisfied }}</span></td>
            <td style="text-align:center;"><span class="pill pill-red">{{ p.critical }}</span></td>
            <td style="text-align:center;"><span class="pill pill-blue">{{ p.in_followup }}</span></td>
            <td style="text-align:center;"><span class="pill pill-gray">{{ p.new_f }}</span></td>
            <td>
              <div class="agent-bar"><div class="agent-bar-fill" style="width:{{ sp }}%;background:{{ fc }};"></div></div>
              <span class="perf" style="color:{{ pc }};background:{{ pb }};">{{ sp }}% satisfied</span>
            </td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:20px;">No product data available. Check if "Product_Name" field exists in Zoho CRM.</td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- CRITICAL LEADS -->
  <div class="section-label" style="border-left:3px solid #dc2626;padding-left:10px;">
    🚨 Critical Leads <span class="pill pill-red" style="margin-left:8px;">{{ m.critical }}</span>
  </div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th>Lead</th><th>Agent</th><th>Product</th><th>Date</th>
          <th>Status</th><th>Issue</th><th>Suggestion</th><th>Remarks</th>
        </tr></thead>
        <tbody>
        {% if m.crit_recs %}
          {% for r in m.crit_recs %}
          <tr>
            <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ r.Name or '—' }}</td>
            <td style="white-space:nowrap;color:#6b7280;">{{ r.Owner.name if r.Owner is mapping else '—' }}</td>
            <td style="white-space:nowrap;color:#0ea5e9;font-size:11px;">{{ r.Product_Name or '—' }}</td>
            <td style="white-space:nowrap;color:#9ca3af;font-size:11px;">{{ r.Created_Time[:10] if r.Created_Time else '—' }}</td>
            <td><span class="pill {{ 'pill-red' if r.Feedback_Status=='Not Satisfied' else 'pill-orange' }}">{{ r.Feedback_Status }}</span></td>
            <td style="color:#374151;">{{ r.Issue or '—' }}</td>
            <td style="color:#374151;">{{ r.Suggestion_for_improvement or '—' }}</td>
            <td style="color:#6b7280;">{{ r.Remarks or '—' }}</td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:20px;">🎉 No critical issues!</td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- SUGGESTIONS SUMMARY -->
  <div class="section-label">💬 Suggestions Summary
    <span style="font-size:10px;color:#9ca3af;font-weight:400;text-transform:none;margin-left:6px;">{{ m.sug_rows|length }} entries</span>
  </div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th style="min-width:120px;">Lead</th>
          <th style="min-width:110px;">Agent</th>
          <th style="min-width:110px;">Product</th>
          <th style="min-width:90px;">Date</th>
          <th style="min-width:90px;">Type</th>
          <th>Content</th>
        </tr></thead>
        <tbody>
        {% if m.sug_rows %}
          {% for row in m.sug_rows %}
          {% if row.type=='Liked' %}{% set pc='pill-green' %}{% set ic='👍' %}
          {% elif row.type=='Issue' %}{% set pc='pill-red' %}{% set ic='⚠️' %}
          {% elif row.type=='Suggestion' %}{% set pc='pill-yellow' %}{% set ic='💡' %}
          {% else %}{% set pc='pill-purple' %}{% set ic='💬' %}{% endif %}
          <tr>
            <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ row.name }}</td>
            <td style="color:#6b7280;white-space:nowrap;">{{ row.agent }}</td>
            <td style="color:#0ea5e9;font-size:11px;white-space:nowrap;">{{ row.product }}</td>
            <td style="color:#9ca3af;font-size:11px;white-space:nowrap;">{{ row.date or '—' }}</td>
            <td><span class="pill {{ pc }}">{{ ic }} {{ row.type }}</span></td>
            <td style="color:#374151;line-height:1.5;">{{ row.content }}</td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="6" style="text-align:center;color:#9ca3af;padding:20px;">No suggestions recorded.</td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- READY TO REFER -->
  <div class="section-label">🤝 Ready to Refer</div>
  <div class="refer-card">
    <div class="refer-num">{{ m.ref_yes }}</div>
    <div class="refer-stats">
      Would refer: <strong>{{ m.ref_yes }}</strong><br>
      Would not refer: <strong>{{ m.ref_no }}</strong><br>
      Maybe / Not sure: <strong>{{ m.ref_maybe }}</strong><br>
      <span style="color:#9ca3af;font-size:11px;">{{ m.ref_answered }} of {{ m.total }} answered</span>
    </div>
  </div>

  <div class="footer">TechXR Feedback Dashboard · Zoho CRM · {{ m.total }} records shown</div>
</div>

<script>
function autoSubmit(){ document.getElementById('filterForm').submit(); }

// Calculate date string in YYYY-MM-DD
function fmtDate(d){ return d.toISOString().split('T')[0]; }
function daysAgo(n){ var d=new Date(); d.setDate(d.getDate()-n); return fmtDate(d); }

function setPreset(p){
  var today = fmtDate(new Date());
  var from = '', to = today;
  if(p==='today')     { from=today; }
  else if(p==='yesterday'){ var y=new Date(); y.setDate(y.getDate()-1); from=fmtDate(y); to=fmtDate(y); }
  else if(p==='7d')   { from=daysAgo(6); }
  else if(p==='15d')  { from=daysAgo(14); }
  else if(p==='30d')  { from=daysAgo(29); }
  else if(p==='90d')  { from=daysAgo(89); }
  else if(p==='all')  { from=''; to=''; }
  document.getElementById('date_from').value = from;
  document.getElementById('date_to').value   = to;
  document.getElementById('preset').value    = p;
  document.getElementById('filterForm').submit();
}
</script>
</body>
</html>"""


# ── Debug Route ──────────────────────────────────────────────────────────────

@app.route("/debug")
def debug():
    cid  = os.getenv("ZOHO_CLIENT_ID", "")
    csec = os.getenv("ZOHO_CLIENT_SECRET", "")
    rtok = os.getenv("ZOHO_REFRESH_TOKEN", "")

    lines = ["<pre style='font-family:monospace;padding:30px;font-size:13px;'>"]
    lines.append("<b>── Env Vars ──</b>")
    lines.append(f"ZOHO_CLIENT_ID     : {'✅ SET (' + cid[:8] + '...)' if cid.strip() else '❌ MISSING'}")
    lines.append(f"ZOHO_CLIENT_SECRET : {'✅ SET (' + csec[:6] + '...)' if csec.strip() else '❌ MISSING'}")
    lines.append(f"ZOHO_REFRESH_TOKEN : {'✅ SET (' + rtok[:8] + '...)' if rtok.strip() else '❌ MISSING'}")
    lines.append("")
    lines.append("<b>── Token Test ──</b>")
    try:
        _token_cache["token"] = None  # force fresh fetch
        token = _fetch_new_token()
        lines.append(f"OAuth Token : ✅ SUCCESS (token starts: {token[:12]}...)")
    except Exception as e:
        lines.append(f"OAuth Token : ❌ FAILED\nError: {e}")
    lines.append("</pre>")
    return "\n".join(lines)


# ── Route ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    today        = datetime.now()
    default_from = (today - timedelta(days=29)).strftime("%Y-%m-%d")
    default_to   = today.strftime("%Y-%m-%d")

    try:
        recs = fetch_all_feedback()
    except Exception as e:
        return f"<h2 style='color:red;font-family:sans-serif;padding:40px'>Error: {e}</h2>", 500

    # Unique filter options (from all records)
    agents   = sorted(set(get_owner_name(r) for r in recs))
    products = sorted(set(get_product(r) for r in recs if get_product(r) != "—"))
    statuses = sorted(set(r.get("Feedback_Status") or "Unknown" for r in recs))

    # Read filters — default to last 30 days if nothing is set
    f_date_from = request.args.get("date_from", "").strip()
    f_date_to   = request.args.get("date_to",   "").strip()
    f_agent     = request.args.get("agent",     "").strip()
    f_product   = request.args.get("product",   "").strip()
    f_status    = request.args.get("status",    "").strip()
    f_sug_type  = request.args.get("sug_type",  "").strip()
    active_preset = request.args.get("preset",  "").strip()

    # If no date filter at all (first load) → default to last 30 days
    if not f_date_from and not f_date_to and "date_from" not in request.args:
        f_date_from   = default_from
        f_date_to     = default_to
        active_preset = "30d"

    # Apply filters to records
    filtered = recs
    if f_date_from:
        filtered = [r for r in filtered if get_record_date(r) >= f_date_from]
    if f_date_to:
        filtered = [r for r in filtered if get_record_date(r) <= f_date_to]
    if f_agent:
        filtered = [r for r in filtered if get_owner_name(r) == f_agent]
    if f_product:
        filtered = [r for r in filtered if get_product(r) == f_product]
    if f_status:
        filtered = [r for r in filtered if r.get("Feedback_Status") == f_status]

    m = compute_metrics(filtered)

    # Filter suggestion rows by type
    if f_sug_type:
        m["sug_rows"] = [s for s in m["sug_rows"] if s["type"] == f_sug_type]

    date_str = today.strftime("%A, %d %B %Y · %I:%M %p")

    resp = make_response(render_template_string(
        TEMPLATE, m=m, date_str=date_str,
        agents=agents, products=products, statuses=statuses,
        f_date_from=f_date_from, f_date_to=f_date_to,
        f_agent=f_agent, f_product=f_product,
        f_status=f_status, f_sug_type=f_sug_type,
        active_preset=active_preset,
        default_from=default_from, default_to=default_to,
        total_all=len(recs)
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    print(f"\n✅  Dashboard running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
