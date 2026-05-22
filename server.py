"""
TechXR Feedback Dashboard Server
Fetches live Zoho CRM data and serves the dashboard on localhost.
"""

import os, json, math, requests
from flask import Flask, render_template_string, request, make_response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Zoho OAuth ──────────────────────────────────────────────────────────────

def get_access_token():
    resp = requests.post("https://accounts.zoho.in/oauth/v2/token", params={
        "grant_type":    "refresh_token",
        "client_id":     os.getenv("ZOHO_CLIENT_ID"),
        "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
        "refresh_token": os.getenv("ZOHO_REFRESH_TOKEN"),
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Zoho CRM fetch ──────────────────────────────────────────────────────────

def fetch_all_feedback(token):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    base    = "https://www.zohoapis.in/crm/v2/Feedback"
    fields  = ("Name,Owner,Rating,Feedback_Status,Remarks,"
               "What_most_you_like_about_the_app,Suggestion_for_improvement,"
               "Issue,Would_You_Refer_Our_Product_to_Someone,Created_Time")
    all_recs = []
    page = 1
    while True:
        r = requests.get(base, headers=headers, params={
            "fields": fields, "per_page": 200, "page": page
        })
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
    """Extract YYYY-MM-DD from Created_Time field."""
    ct = r.get("Created_Time", "") or ""
    return ct[:10] if len(ct) >= 10 else ""


def get_owner_name(r):
    owner = r.get("Owner") or {}
    name = owner.get("name") if isinstance(owner, dict) else "Unassigned"
    return name or "Unassigned"


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
                r_counts[v] += 1
                r_sum += v
                r_n += 1
        except:
            pass
    avg_r = round(r_sum / r_n, 1) if r_n else None
    max_r = max(r_counts.values()) or 1

    # Status
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
        if not vals:
            continue
        ref_answered += 1
        flat = ((" ".join(vals) if isinstance(vals, list) else str(vals))).lower()
        if any(x in flat for x in ["yes","definitely","absolutely"]):
            ref_yes += 1
        elif "no" in flat and "not sure" not in flat:
            ref_no += 1
        elif any(x in flat for x in ["maybe","not sure","possibly"]):
            ref_maybe += 1
        else:
            ref_yes += 1

    # Agent-wise
    agent_map = {}
    for r in recs:
        name = get_owner_name(r)
        if name not in agent_map:
            agent_map[name] = {"name": name, "total": 0, "satisfied": 0,
                               "not_sat": 0, "issue_r": 0, "in_followup": 0,
                               "new_f": 0, "critical": 0}
        a = agent_map[name]
        a["total"] += 1
        status = r.get("Feedback_Status", "")
        if status == "Satisfied":       a["satisfied"] += 1
        elif status == "Not Satisfied": a["not_sat"] += 1
        elif status == "Issue Raised":  a["issue_r"] += 1
        elif status == "In Followup":   a["in_followup"] += 1
        elif status == "New Feedback":  a["new_f"] += 1
        a["critical"] = a["not_sat"] + a["issue_r"]

    agent_list = sorted(agent_map.values(), key=lambda x: x["total"], reverse=True)

    # Critical recs
    crit_recs = [r for r in recs if r.get("Feedback_Status") in ("Not Satisfied", "Issue Raised")]

    # Suggestions — flatten into rows for table
    skip = {"cnr","busy","n/a","na","no","none"}
    sug_rows = []
    for r in recs:
        name  = r.get("Name") or "—"
        agent = get_owner_name(r)
        date  = get_record_date(r)
        if r.get("What_most_you_like_about_the_app"):
            sug_rows.append({"name": name, "agent": agent, "date": date,
                             "type": "Liked", "content": r["What_most_you_like_about_the_app"]})
        if r.get("Issue"):
            sug_rows.append({"name": name, "agent": agent, "date": date,
                             "type": "Issue", "content": r["Issue"]})
        if r.get("Suggestion_for_improvement") and \
           r["Suggestion_for_improvement"].strip().lower() not in ["na","n/a","no improvement","none",""]:
            sug_rows.append({"name": name, "agent": agent, "date": date,
                             "type": "Suggestion", "content": r["Suggestion_for_improvement"]})
        if r.get("Remarks") and len(r["Remarks"]) > 5 and \
           r["Remarks"].strip().lower() not in skip:
            sug_rows.append({"name": name, "agent": agent, "date": date,
                             "type": "Remark", "content": r["Remarks"]})

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
.wrap{max-width:1100px;margin:0 auto;padding:20px}
.header{background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;padding:24px 28px;margin-bottom:20px;color:#fff}
.header h1{font-size:24px;font-weight:800}
.header p{font-size:13px;opacity:.7;margin-top:4px}
.header-meta{font-size:11px;color:#c4b5fd;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
.section-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:#6b7280;margin:20px 0 10px;padding-left:2px}

/* Filter Bar */
.filter-bar{background:#fff;border-radius:10px;padding:14px 18px;margin-bottom:18px;box-shadow:0 1px 3px rgba(0,0,0,.06);display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.filter-group{display:flex;flex-direction:column;gap:4px}
.filter-group label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#6b7280}
.filter-group input,.filter-group select{border:1px solid #e5e7eb;border-radius:6px;padding:6px 10px;font-size:12px;color:#374151;background:#fafafa;outline:none}
.filter-group input:focus,.filter-group select:focus{border-color:#6366f1;background:#fff}
.filter-actions{display:flex;gap:8px;align-items:flex-end}
.btn{padding:7px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none}
.btn-primary{background:#4f46e5;color:#fff}
.btn-secondary{background:#f3f4f6;color:#374151}
.filter-badge{font-size:11px;color:#6366f1;font-weight:600;align-self:flex-end;padding-bottom:7px}

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
.bar-seg{height:10px}
.bar-rest{flex:1;background:#e5e7eb}
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
.scrollable-table{max-height:420px;overflow-y:auto;border-radius:8px;border:1px solid #f3f4f6}
.refer-card{display:flex;align-items:center;gap:24px;background:#fff;border-radius:10px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.refer-num{font-size:60px;font-weight:800;color:#7c3aed;line-height:1}
.refer-stats{font-size:13px;color:#6b7280;line-height:2.2}
.agent-bar{height:6px;border-radius:3px;background:#f3f4f6;overflow:hidden;margin-bottom:3px}
.agent-bar-fill{height:6px;border-radius:3px;background:#22c55e}
.perf{font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;display:inline-block}
.footer{text-align:center;margin-top:24px;padding:16px;color:#9ca3af;font-size:11px}
.refresh-btn{display:inline-block;margin-left:12px;background:#4f46e5;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;text-decoration:none}
.active-filter-tag{display:inline-block;background:#ede9fe;color:#6d28d9;font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;margin-left:6px}
@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}.pct-grid{grid-template-columns:repeat(3,1fr)}.filter-bar{flex-direction:column}}
</style>
</head>
<body>
<div class="wrap">

  <!-- HEADER -->
  <div class="header">
    <div class="header-meta">TechXR · Live Feedback Report</div>
    <h1>📊 Feedback Rating Dashboard</h1>
    <p>{{ date_str }} &nbsp;·&nbsp; {{ m.total }} records shown of {{ total_all }} total &nbsp;·&nbsp; <a href="/" class="refresh-btn">🔄 Refresh</a></p>
  </div>

  <!-- FILTER BAR -->
  <form method="GET" action="/">
    <div class="filter-bar">
      <div class="filter-group">
        <label>📅 Date From</label>
        <input type="date" name="date_from" value="{{ f_date_from }}">
      </div>
      <div class="filter-group">
        <label>📅 Date To</label>
        <input type="date" name="date_to" value="{{ f_date_to }}">
      </div>
      <div class="filter-group">
        <label>👤 Agent</label>
        <select name="agent">
          <option value="">All Agents</option>
          {% for ag in agents %}
          <option value="{{ ag }}" {% if f_agent == ag %}selected{% endif %}>{{ ag }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="filter-group">
        <label>🏷️ Status</label>
        <select name="status">
          <option value="">All Statuses</option>
          {% for st in statuses %}
          <option value="{{ st }}" {% if f_status == st %}selected{% endif %}>{{ st }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="filter-actions">
        <button type="submit" class="btn btn-primary">Apply Filters</button>
        <a href="/" class="btn btn-secondary">Clear</a>
      </div>
      {% if f_date_from or f_date_to or f_agent or f_status %}
      <div class="filter-badge">
        🔍 Filtered
        {% if f_agent %}<span class="active-filter-tag">{{ f_agent }}</span>{% endif %}
        {% if f_status %}<span class="active-filter-tag">{{ f_status }}</span>{% endif %}
        {% if f_date_from %}<span class="active-filter-tag">From {{ f_date_from }}</span>{% endif %}
        {% if f_date_to %}<span class="active-filter-tag">To {{ f_date_to }}</span>{% endif %}
      </div>
      {% endif %}
    </div>
  </form>

  <!-- OVERVIEW -->
  <div class="section-label">📈 Overview</div>
  <div class="cards">
    <div class="card" style="border-top:3px solid #8b5cf6">
      <div class="card-label">📋 Total Records</div>
      <div class="card-val">{{ m.total }}</div>
      <div class="card-sub">Matching records</div>
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
  <div class="section-label">⭐ Rating Dashboard &amp; 📞 Call Analysis</div>
  <div class="grid2">
    <div class="panel">
      <div class="panel-title">⭐ Rating Distribution</div>
      {% for i in [5,4,3,2,1] %}
      {% set colors = {5:'#22c55e',4:'#84cc16',3:'#f59e0b',2:'#f97316',1:'#ef4444'} %}
      <div class="rb-row">
        <div class="rb-label">{{ i }}★</div>
        <div class="rb-track"><div class="rb-fill" style="width:{{ m.r_pct[i] }}%;background:{{ colors[i] }};"></div></div>
        <div class="rb-count">{{ m.r_counts[i] }}</div>
      </div>
      {% endfor %}
      <div style="font-size:10px;color:#9ca3af;margin-top:6px;">{{ m.r_n }} of {{ m.total }} records rated</div>
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
      <div class="meta-line">New Feedback (pending): <strong>{{ m.new_f }}</strong> &nbsp;|&nbsp; In Follow-up: <strong>{{ m.in_followup }}</strong></div>
    </div>
  </div>

  <!-- AGENT-WISE REPORT -->
  <div class="section-label" style="border-left:3px solid #6366f1;padding-left:10px;">👤 Agent-Wise Report</div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th>Agent</th><th>Total</th><th>Satisfied</th><th>Critical</th>
          <th>Follow-up</th><th>New</th><th style="min-width:120px;">Performance</th>
        </tr></thead>
        <tbody>
        {% for a in m.agent_list %}
        {% set a_sat_pct = (a.satisfied / a.total * 100)|round|int if a.total else 0 %}
        {% set perf_color = '#059669' if a_sat_pct >= 60 else ('#d97706' if a_sat_pct >= 30 else '#dc2626') %}
        {% set perf_bg = '#f0fdf4' if a_sat_pct >= 60 else ('#fffbeb' if a_sat_pct >= 30 else '#fef2f2') %}
        <tr>
          <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ a.name }}</td>
          <td style="text-align:center;font-weight:700;">{{ a.total }}</td>
          <td style="text-align:center;"><span class="pill pill-green">{{ a.satisfied }}</span></td>
          <td style="text-align:center;"><span class="pill pill-red">{{ a.critical }}</span></td>
          <td style="text-align:center;"><span class="pill pill-blue">{{ a.in_followup }}</span></td>
          <td style="text-align:center;"><span class="pill pill-gray">{{ a.new_f }}</span></td>
          <td>
            <div class="agent-bar"><div class="agent-bar-fill" style="width:{{ a_sat_pct }}%;"></div></div>
            <span class="perf" style="color:{{ perf_color }};background:{{ perf_bg }};">{{ a_sat_pct }}% satisfied</span>
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- CALL FOR ACTION -->
  <div class="section-label" style="border-left:3px solid #dc2626;padding-left:10px;">
    🚨 Call for Action — Critical Leads
    <span class="pill pill-red" style="margin-left:8px;">{{ m.critical }}</span>
  </div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th>Lead Name</th><th>Agent</th><th>Date</th><th>Status</th><th>Issue Reported</th><th>Suggestion</th><th>Remarks</th>
        </tr></thead>
        <tbody>
        {% if m.crit_recs %}
          {% for r in m.crit_recs %}
          <tr>
            <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ r.Name or '—' }}</td>
            <td style="white-space:nowrap;color:#6b7280;">{{ r.Owner.name if r.Owner is mapping else '—' }}</td>
            <td style="white-space:nowrap;color:#9ca3af;font-size:11px;">{{ r.Created_Time[:10] if r.Created_Time else '—' }}</td>
            <td><span class="pill {{ 'pill-red' if r.Feedback_Status == 'Not Satisfied' else 'pill-orange' }}">{{ r.Feedback_Status }}</span></td>
            <td style="color:#374151;">{{ r.Issue or '—' }}</td>
            <td style="color:#374151;">{{ r.Suggestion_for_improvement or '—' }}</td>
            <td style="color:#6b7280;">{{ r.Remarks or '—' }}</td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="7" style="text-align:center;color:#9ca3af;padding:20px;">🎉 No critical issues found!</td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- SUGGESTIONS SUMMARY — scrollable table -->
  <div class="section-label">💬 Suggestions Summary
    <span style="font-size:10px;color:#9ca3af;font-weight:400;text-transform:none;letter-spacing:0;margin-left:6px;">{{ m.sug_rows|length }} entries</span>
  </div>
  <div class="panel" style="padding:0;">
    <div class="scrollable-table">
      <table>
        <thead><tr>
          <th style="min-width:120px;">Lead Name</th>
          <th style="min-width:110px;">Agent</th>
          <th style="min-width:90px;">Date</th>
          <th style="min-width:90px;">Type</th>
          <th>Content</th>
        </tr></thead>
        <tbody>
        {% if m.sug_rows %}
          {% for row in m.sug_rows %}
          {% if row.type == 'Liked' %}
            {% set pill_cls = 'pill-green' %}{% set icon = '👍' %}
          {% elif row.type == 'Issue' %}
            {% set pill_cls = 'pill-red' %}{% set icon = '⚠️' %}
          {% elif row.type == 'Suggestion' %}
            {% set pill_cls = 'pill-yellow' %}{% set icon = '💡' %}
          {% else %}
            {% set pill_cls = 'pill-purple' %}{% set icon = '💬' %}
          {% endif %}
          <tr>
            <td style="font-weight:600;color:#111827;white-space:nowrap;">{{ row.name }}</td>
            <td style="color:#6b7280;white-space:nowrap;">{{ row.agent }}</td>
            <td style="color:#9ca3af;font-size:11px;white-space:nowrap;">{{ row.date or '—' }}</td>
            <td><span class="pill {{ pill_cls }}">{{ icon }} {{ row.type }}</span></td>
            <td style="color:#374151;line-height:1.5;">{{ row.content }}</td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:20px;">No suggestions or remarks recorded yet.</td></tr>
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

  <div class="footer">
    TechXR Feedback Dashboard &nbsp;·&nbsp; Data from Zoho CRM &nbsp;·&nbsp; {{ m.total }} records shown
  </div>

</div>
</body>
</html>"""


# ── Route ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    from datetime import datetime
    try:
        token = get_access_token()
        recs  = fetch_all_feedback(token)
    except Exception as e:
        return f"<h2 style='color:red;font-family:sans-serif;padding:40px'>Error fetching data: {e}</h2>", 500

    # Unique filter options
    agents   = sorted(set(get_owner_name(r) for r in recs))
    statuses = sorted(set(r.get("Feedback_Status") or "Unknown" for r in recs))

    # Read filters from query params
    f_date_from = request.args.get("date_from", "").strip()
    f_date_to   = request.args.get("date_to", "").strip()
    f_agent     = request.args.get("agent", "").strip()
    f_status    = request.args.get("status", "").strip()

    # Apply filters
    filtered = recs
    if f_date_from:
        filtered = [r for r in filtered if get_record_date(r) >= f_date_from]
    if f_date_to:
        filtered = [r for r in filtered if get_record_date(r) <= f_date_to]
    if f_agent:
        filtered = [r for r in filtered if get_owner_name(r) == f_agent]
    if f_status:
        filtered = [r for r in filtered if r.get("Feedback_Status") == f_status]

    m        = compute_metrics(filtered)
    date_str = datetime.now().strftime("%A, %d %B %Y · %I:%M %p")

    resp = make_response(render_template_string(
        TEMPLATE, m=m, date_str=date_str,
        agents=agents, statuses=statuses,
        f_date_from=f_date_from, f_date_to=f_date_to,
        f_agent=f_agent, f_status=f_status,
        total_all=len(recs)
    ))
    # Prevent caching — always serve fresh data
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    print(f"\n✅  Dashboard running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
