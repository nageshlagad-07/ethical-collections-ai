"""Streamlit dashboard. Run: streamlit run dashboard/app.py

Every number, chart and table is computed from the SQLite database (COLLECTIONS_DB, default data/collections.db).
Nothing is hardcoded. The dashboard opens the database READ-ONLY.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.express as px
import streamlit as st

from collections_ai import db, services
from collections_ai.config import REFERENCE_DATE, default_db_path
from collections_ai.contact import ContactContext, ContactRequest
from collections_ai.delinquency import Ledger
from collections_ai.detection import evaluate_against_ground_truth
from collections_ai.reconciliation import reconcile_payments
from collections_ai.rules import load_rules
from collections_ai.spec import CHANNELS, PURPOSES

st.set_page_config(page_title="Ethical Collections Intelligence", page_icon=":bar_chart:", layout="wide")
DB_PATH = os.environ.get("COLLECTIONS_DB", str(default_db_path()))
RULES = load_rules()
REF = REFERENCE_DATE.isoformat()
BUCKET_ORDER = ["CURRENT", "DPD_1_30", "DPD_31_60", "DPD_61_90", "DPD_90_PLUS", "PAID_OFF"]
CAT_ORDER = ["P1_HIGH", "P2_MEDIUM", "P3_LOW", "MONITOR_PROMISE", "HARDSHIP_REVIEW", "SUPPRESSED"]


@st.cache_resource
def conn_ro(path: str):
    return db.connect(path, readonly=True)


@st.cache_data(show_spinner=False)
def q(path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn_ro(path), params=list(params))


def Q(sql: str, params: tuple = ()) -> pd.DataFrame:
    return q(DB_PATH, sql, tuple(params))


def money(x) -> str:
    return f"INR {x:,.0f}"


def pct(x) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x * 100:.1f}%"


def search_table(df: pd.DataFrame, key: str, height: int = 380, cols=None):
    """Searchable table: free-text filter across all columns."""
    text = st.text_input("Search table", key=f"s_{key}", placeholder="type to filter rows (any column)")
    view = df
    if text:
        mask = df.astype(str).apply(lambda c: c.str.contains(text, case=False, na=False)).any(axis=1)
        view = df[mask]
    st.caption(f"{len(view):,} of {len(df):,} rows")
    st.dataframe(view[cols] if cols else view, width="stretch", height=height, hide_index=True)
    return view


# ----------------------------------------------------------------------------- pages
def page_overview():
    st.header("Executive Overview")
    m = services.portfolio_metrics(conn_ro(DB_PATH))
    c = st.columns(4)
    c[0].metric("Total loans", f"{m['total_loans']:,}", help=f"{m['total_installments']:,} instalments")
    c[1].metric("Outstanding balance", money(m["total_outstanding_balance"]), help="Contractual balance (total payable - allocated payments) at the reference date")
    c[2].metric("Total arrears", money(m["total_arrears"]))
    c[3].metric("Overdue accounts", f"{m['overdue_accounts']:,}", help="Loans with arrears > 0")
    c = st.columns(4)
    c[0].metric("Open collection cases", f"{m['open_cases']:,}")
    c[1].metric("Promise fulfilment rate", pct(m["promise_fulfillment_rate"]), help="FULFILLED / evaluated promises (PENDING excluded)")
    c[2].metric("Payment reconciliation rate", pct(m["payment_reconciliation_rate"]), help="RECONCILED / all payments")
    c[3].metric("Open compliance exceptions", f"{m['open_compliance_exceptions']:,}", help=f"Run {m['latest_exception_run']}")
    st.caption(f"Reference date {m['as_of_date']} | synthetic data | contact rules are illustrative placeholders, not legal advice")
    left, right = st.columns(2)
    d = Q("SELECT delinquency_bucket b, COUNT(*) loans, SUM(total_arrears) arrears FROM delinquency_snapshot WHERE snapshot_date=? GROUP BY 1", (REF,))
    d["b"] = pd.Categorical(d["b"], BUCKET_ORDER, ordered=True)
    left.plotly_chart(px.bar(d.sort_values("b"), x="b", y="loans", color="b", title="Delinquency distribution (loans by bucket)", labels={"b": "bucket"}), width="stretch")
    pr = Q("SELECT priority_category c, COUNT(*) cases FROM prioritization_result WHERE run_id=? GROUP BY 1", (m["latest_prioritization_run"] or "",))
    if len(pr):
        right.plotly_chart(px.bar(pr, x="c", y="cases", color="c", title=f"Priority distribution ({m['latest_prioritization_run']})", labels={"c": "category"}, category_orders={"c": CAT_ORDER}), width="stretch")
    else:
        right.info("No prioritization run yet. Run: python -m collections_ai.cli run-engines")
    tr = Q("SELECT snapshot_date d, SUM(total_arrears) arrears, SUM(CASE WHEN total_arrears>0 THEN 1 ELSE 0 END) overdue FROM delinquency_snapshot GROUP BY 1 ORDER BY 1")
    a, b = st.columns(2)
    a.plotly_chart(px.line(tr, x="d", y="arrears", markers=True, title="Total arrears at each snapshot date"), width="stretch")
    b.plotly_chart(px.line(tr, x="d", y="overdue", markers=True, title="Overdue accounts at each snapshot date"), width="stretch")


def page_portfolio():
    st.header("Loan Portfolio")
    df = Q("SELECT l.loan_id, l.customer_id, c.risk_category, l.product_type, l.principal_amount, l.interest_rate_pct, l.total_payable, "
           "l.origination_date, l.maturity_date, l.loan_status FROM loan l JOIN customer c USING(customer_id)")
    f = st.columns(4)
    status = f[0].multiselect("Loan status", sorted(df["loan_status"].unique()), default=[])
    prod = f[1].multiselect("Product", sorted(df["product_type"].unique()), default=[])
    risk = f[2].multiselect("Risk category", ["LOW", "MEDIUM", "HIGH"], default=[])
    rng = f[3].slider("Principal (INR)", int(df["principal_amount"].min()), int(df["principal_amount"].max()), (int(df["principal_amount"].min()), int(df["principal_amount"].max())), step=5000)
    v = df
    if status: v = v[v["loan_status"].isin(status)]
    if prod: v = v[v["product_type"].isin(prod)]
    if risk: v = v[v["risk_category"].isin(risk)]
    v = v[v["principal_amount"].between(*rng)]
    k = st.columns(3)
    k[0].metric("Loans in view", f"{len(v):,}")
    k[1].metric("Total principal", money(v["principal_amount"].sum()))
    k[2].metric("Avg interest rate", f"{v['interest_rate_pct'].mean():.1f}%" if len(v) else "n/a")
    a, b = st.columns(2)
    a.plotly_chart(px.histogram(v, x="loan_status", color="product_type", title="Loans by status and product"), width="stretch")
    b.plotly_chart(px.histogram(v, x="principal_amount", nbins=30, color="risk_category", title="Principal distribution by risk"), width="stretch")
    sv = search_table(v, "loans")
    st.subheader("Loan detail")
    lid = st.selectbox("Select loan", [""] + sv["loan_id"].head(500).tolist())
    if lid:
        st.write("**Instalments**")
        st.dataframe(Q("SELECT installment_id, due_date, amount_due, amount_paid, installment_status, fully_paid_date FROM installment WHERE loan_id=? ORDER BY installment_number", (lid,)), hide_index=True, width="stretch")
        st.write("**Payments and allocations**")
        st.dataframe(Q("SELECT p.payment_id, p.payment_date, p.amount, p.channel, p.reconciliation_status, a.installment_id, a.allocated_amount FROM payment p LEFT JOIN payment_allocation a USING(payment_id) WHERE p.loan_id=? ORDER BY p.payment_date", (lid,)), hide_index=True, width="stretch")
        st.write("**Delinquency snapshots**")
        st.dataframe(Q("SELECT snapshot_date, outstanding_balance, total_arrears, overdue_installments, days_past_due, delinquency_bucket, loan_status FROM delinquency_snapshot WHERE loan_id=? ORDER BY snapshot_date", (lid,)), hide_index=True, width="stretch")


def page_delinquency():
    st.header("Delinquency Analysis")
    dates = Q("SELECT DISTINCT snapshot_date FROM delinquency_snapshot ORDER BY 1")["snapshot_date"].tolist()
    sel = st.select_slider("Snapshot date", dates, value=dates[-1])
    d = Q("SELECT s.*, c.risk_category FROM delinquency_snapshot s JOIN loan l USING(loan_id) JOIN customer c ON c.customer_id=l.customer_id WHERE snapshot_date=?", (sel,))
    d["delinquency_bucket"] = pd.Categorical(d["delinquency_bucket"], BUCKET_ORDER, ordered=True)
    k = st.columns(4)
    k[0].metric("Loans measured", f"{len(d):,}")
    k[1].metric("Total arrears", money(d["total_arrears"].sum()))
    k[2].metric("Delinquent loans", f"{(d['total_arrears'] > 0).sum():,}")
    k[3].metric("90+ DPD loans", f"{(d['delinquency_bucket'] == 'DPD_90_PLUS').sum():,}")
    a, b = st.columns(2)
    g = d.groupby("delinquency_bucket", observed=True).agg(loans=("loan_id", "count"), arrears=("total_arrears", "sum")).reset_index()
    a.plotly_chart(px.bar(g, x="delinquency_bucket", y="arrears", title="Arrears by bucket"), width="stretch")
    od = d[d["days_past_due"] > 0]
    b.plotly_chart(px.histogram(od, x="days_past_due", nbins=30, color="risk_category", title="Days-past-due distribution (delinquent loans)"), width="stretch")
    rr = d[d["total_arrears"] > 0].groupby("risk_category")["loan_id"].count() / d.groupby("risk_category")["loan_id"].count()
    st.plotly_chart(px.bar(rr.reset_index(name="delinquency_rate"), x="risk_category", y="delinquency_rate", title="Delinquency rate by risk category"), width="stretch")
    if len(dates) > 1 and sel != dates[0]:
        prev = dates[dates.index(sel) - 1]
        p = Q("SELECT loan_id, delinquency_bucket pb FROM delinquency_snapshot WHERE snapshot_date=?", (prev,))
        m = d.merge(p, on="loan_id")
        if len(m):
            ct = pd.crosstab(m["pb"], m["delinquency_bucket"].astype(str)).reindex(index=BUCKET_ORDER, columns=BUCKET_ORDER, fill_value=0)
            st.subheader(f"Roll-rate matrix: bucket at {prev} (rows) -> bucket at {sel} (columns)")
            st.dataframe(ct, width="stretch")
    st.caption("DPD = days since the oldest unpaid overdue instalment; arrears = unpaid amount of instalments due before the snapshot date (see docs/BUSINESS_RULES.md).")


def page_prioritization():
    st.header("Collection Prioritization")
    run = services.latest_run(conn_ro(DB_PATH), "PRIORITIZATION")
    if not run:
        st.info("No prioritization run yet. Run: python -m collections_ai.cli run-engines")
        return
    st.caption(f"Run {run['run_id']} as of {run['as_of_date']} | rules {run['rules_version']} | rules-based, no black-box model")
    df = Q("SELECT r.*, c.loan_id, c.customer_id, c.case_status FROM prioritization_result r JOIN collection_case c USING(case_id) WHERE run_id=?", (run["run_id"],))
    f = st.columns(3)
    cats = f[0].multiselect("Category", CAT_ORDER, default=[])
    smin = f[1].slider("Minimum score", 0, 100, 0)
    action = f[2].multiselect("Recommended action", sorted(df["recommended_action"].unique()), default=[])
    v = df[df["priority_score"] >= smin]
    if cats: v = v[v["priority_category"].isin(cats)]
    if action: v = v[v["recommended_action"].isin(action)]
    a, b = st.columns(2)
    a.plotly_chart(px.histogram(df, x="priority_score", color="priority_category", nbins=25, title="Score distribution (all cases)", category_orders={"priority_category": CAT_ORDER}), width="stretch")
    ov = df[(df["priority_score"] >= 58) & df["priority_category"].isin(["SUPPRESSED", "HARDSHIP_REVIEW"])]
    b.metric("High-score cases overridden by legal/contact restrictions", f"{len(ov):,}", help="Score >= P2 threshold but category SUPPRESSED or HARDSHIP_REVIEW: restrictions beat the score")
    b.plotly_chart(px.pie(df, names="priority_category", title="Category mix", category_orders={"priority_category": CAT_ORDER}), width="stretch")
    sv = search_table(v.sort_values("priority_score", ascending=False), "pri", cols=["case_id", "loan_id", "priority_score", "priority_category", "days_past_due", "arrears", "recommended_action", "recommended_channel", "recommended_slot", "suppression_reasons", "reason_codes"] if "arrears" in v else None)
    cid = st.selectbox("Explain a case", [""] + sv["case_id"].head(300).tolist())
    if cid:
        r = df[df["case_id"] == cid].iloc[0]
        st.subheader(f"{cid} - {r['priority_category']} (score {r['priority_score']})")
        st.write(r["explanation"])
        fac = pd.DataFrame(json.loads(r["contributing_factors"]))
        st.plotly_chart(px.bar(fac, x="points", y="factor", orientation="h", title="Contributing factors (points)", hover_data=["explanation"]), width="stretch")
        st.dataframe(fac, hide_index=True, width="stretch")
        st.write(f"**Reason codes:** `{r['reason_codes']}`  |  **Suppression reasons:** `{r['suppression_reasons']}`")


def page_contact():
    st.header("Contact Management")
    log = Q("SELECT decision, reason_codes, purpose, requested_channel FROM contact_scheduling_log")
    k = st.columns(4)
    k[0].metric("Scheduling requests", f"{len(log):,}")
    k[1].metric("Permitted", f"{(log['decision'] == 'PERMITTED').sum():,}")
    k[2].metric("Rejected (with reasons)", f"{(log['decision'] == 'REJECTED').sum():,}")
    op = Q("SELECT COUNT(*) n FROM contact_preference WHERE status='ACTIVE' AND opt_out_all=1")["n"][0]
    k[3].metric("Customers opted out of all contact", f"{op:,}")
    rej = log[log["decision"] == "REJECTED"].assign(reason=lambda d: d["reason_codes"].str.split(",")).explode("reason")
    a, b = st.columns(2)
    if len(rej):
        a.plotly_chart(px.bar(rej["reason"].value_counts().reset_index(), x="count", y="reason", orientation="h", title="Why requests were rejected"), width="stretch")
    at = Q("SELECT channel, outcome, strftime('%Y-%m', scheduled_for) m FROM contact_attempt WHERE attempt_status='COMPLETED'")
    b.plotly_chart(px.histogram(at, x="channel", color="outcome", title="Completed attempts by channel and outcome"), width="stretch")
    st.plotly_chart(px.histogram(at, x="m", color="channel", title="Attempts per month"), width="stretch")
    st.subheader("Eligibility checker (dry run - nothing is stored)")
    cases = Q("SELECT case_id FROM collection_case WHERE case_status<>'RESOLVED' ORDER BY case_id")["case_id"].tolist()
    c = st.columns(4)
    cid = c[0].selectbox("Case", cases[:1000])
    ch = c[1].selectbox("Channel", CHANNELS)
    pu = c[2].selectbox("Purpose", PURPOSES)
    when = datetime.combine(c[3].date_input("Date", date(2026, 10, 23)), c[3].time_input("Time", time(10, 0)))
    if cid:
        case = Q("SELECT * FROM collection_case WHERE case_id=?", (cid,)).iloc[0]
        t = db.load_slice(conn_ro(DB_PATH), [case["loan_id"]])
        ctx = ContactContext(t["customer"], t["account"], t["loan"], t["contact_preference"], t["flag_event"], t["contact_attempt"],
                             Ledger(t["loan"], t["installment"], t["payment_allocation"]), RULES)
        dec = ctx.evaluate(ContactRequest(cid, case["customer_id"], case["loan_id"], ch, when, pu))
        (st.success if dec.permitted else st.error)("PERMITTED" if dec.permitted else "REJECTED: " + ", ".join(dec.reason_codes))
        for e in dec.explanations:
            st.write("- " + e)
    st.caption(RULES["disclaimer"])
    st.subheader("Recent scheduling decisions")
    search_table(Q("SELECT log_id, case_id, requested_channel, requested_for, purpose, decision, reason_codes, attempt_id FROM contact_scheduling_log ORDER BY requested_for DESC LIMIT 3000"), "sched")


def page_promises():
    st.header("Promise-to-Pay Monitoring")
    p = Q("SELECT * FROM promise_to_pay")
    ev = p[p["promise_status"] != "PENDING"]
    k = st.columns(4)
    k[0].metric("Promises", f"{len(p):,}")
    k[1].metric("Fulfilment rate", pct((ev["promise_status"] == "FULFILLED").mean() if len(ev) else None))
    k[2].metric("Partially fulfilled", f"{(p['promise_status'] == 'PARTIALLY_FULFILLED').sum():,}")
    k[3].metric("Broken (missed)", f"{(p['promise_status'] == 'BROKEN').sum():,}")
    a, b = st.columns(2)
    a.plotly_chart(px.pie(p, names="promise_status", title="Promise outcomes"), width="stretch")
    b.plotly_chart(px.scatter(ev, x="promised_amount", y="amount_received", color="promise_status", title="Promised vs received (evaluated promises)", log_x=True, log_y=True), width="stretch")
    p["month"] = p["created_date"].str[:7]
    st.plotly_chart(px.histogram(p, x="month", color="promise_status", title="Promises created per month"), width="stretch")
    ex = p[p["exception_approved_by"].notna()]
    st.info(f"{len(ex)} promise(s) exceed arrears under the documented exception workflow (approver + reason recorded).")
    f = st.multiselect("Status filter", sorted(p["promise_status"].unique()))
    search_table(p[p["promise_status"].isin(f)] if f else p, "promises", cols=["promise_id", "case_id", "created_date", "promised_date", "promised_amount", "arrears_at_creation", "amount_received", "promise_status", "exception_approved_by"])
    st.caption("Outcome = allocations dated after creation and up to promised_date + grace days (config/rules.json) vs promised amount.")


def page_reconciliation():
    st.header("Payment Reconciliation")
    pay = Q("SELECT reconciliation_status s, COUNT(*) n, SUM(amount) amt FROM payment GROUP BY 1")
    total = pay["n"].sum()
    rec = pay.loc[pay["s"] == "RECONCILED", "n"].sum()
    k = st.columns(3)
    k[0].metric("Payments", f"{total:,}")
    k[1].metric("Reconciliation rate", pct(rec / total if total else None))
    k[2].metric("Exceptions", f"{pay.loc[pay['s'] == 'EXCEPTION', 'n'].sum():,}")
    st.plotly_chart(px.bar(pay, x="s", y="n", color="s", title="Payments by reconciliation status"), width="stretch")
    m = Q("SELECT substr(payment_date,1,7) month, SUM(amount) collected, COUNT(*) payments FROM payment GROUP BY 1 ORDER BY 1")
    st.plotly_chart(px.bar(m, x="month", y="collected", title="Collections per month (INR)"), width="stretch")
    st.subheader("Live reconciliation exceptions (FIFO replay of source records)")

    @st.cache_data(show_spinner="Replaying allocations...")
    def _exc(path):
        t = {n: q(path, f"SELECT * FROM {n}") for n in ["installment", "payment", "payment_allocation"]}
        r = reconcile_payments(t["installment"], t["payment"], t["payment_allocation"])
        return r[r["reconciliation_status"] != "RECONCILED"]
    ex = _exc(DB_PATH)
    if len(ex):
        search_table(ex, "recon_exc")
    else:
        st.success("Every payment reconciles to the oldest-due-first allocation policy.")
    st.subheader("Allocation drill-down")
    pid = st.text_input("Payment id", "PMT-2026-000001")
    st.dataframe(Q("SELECT a.allocation_id, a.installment_id, i.installment_number, i.due_date, a.allocated_amount, a.allocation_rule FROM payment_allocation a JOIN installment i USING(installment_id) WHERE a.payment_id=?", (pid,)), hide_index=True, width="stretch")


def page_compliance():
    st.header("Compliance and Exception Monitoring")
    run = services.latest_run(conn_ro(DB_PATH), "EXCEPTION_DETECTION")
    if not run:
        st.info("No exception detection run yet. Run: python -m collections_ai.cli run-engines")
        return
    ex = Q("SELECT * FROM exception_record WHERE run_id=?", (run["run_id"],))
    st.caption(f"Run {run['run_id']} as of {run['as_of_date']}. Detectors use source records only; they never read ground-truth labels.")
    a, b = st.columns(2)
    a.plotly_chart(px.bar(ex["exception_type"].value_counts().reset_index(), x="count", y="exception_type", orientation="h", title="Exceptions by type"), width="stretch")
    b.plotly_chart(px.pie(ex, names="severity", title="By severity"), width="stretch")
    f = st.multiselect("Type", sorted(ex["exception_type"].unique()))
    v = search_table(ex[ex["exception_type"].isin(f)] if f else ex, "exc", cols=["exception_id", "exception_type", "severity", "entity_type", "entity_id", "case_id", "loan_id", "description", "expected_decision", "system_response", "status"])
    sel = st.selectbox("Case explanation", [""] + v["exception_id"].tolist()[:300])
    if sel:
        r = ex[ex["exception_id"] == sel].iloc[0]
        st.write(f"**{r['exception_type']}** on {r['entity_type']} `{r['entity_id']}`")
        st.write(r["description"]); st.write("**Expected decision:** " + r["expected_decision"]); st.write("**System response:** " + r["system_response"])
    gt = ROOT / "data" / "full" / "ground_truth.csv"
    if gt.exists():
        st.subheader("Detection quality vs separately stored ground truth (evaluation only)")
        ev = evaluate_against_ground_truth(ex.to_dict("records"), pd.read_csv(gt, dtype=str))
        st.dataframe(ev, hide_index=True, width="stretch")


def page_quality():
    st.header("Data Quality and Validation")
    run = services.latest_run(conn_ro(DB_PATH), "VALIDATION")
    if run:
        res = Q("SELECT check_name, category, severity, passed, violation_count, detail FROM validation_result WHERE run_id=?", (run["run_id"],))
        integ = res[res["category"] != "BUSINESS_RULE_EXCEPTION"]
        k = st.columns(3)
        k[0].metric("Integrity checks", f"{len(integ)}")
        k[1].metric("Passed", f"{int(integ['passed'].sum())}")
        k[2].metric("Failed", f"{int((1 - integ['passed']).sum())}")
        st.caption(f"Run {run['run_id']} ({run['started_at']})")
        cat = st.multiselect("Category", sorted(res["category"].unique()))
        search_table(res[res["category"].isin(cat)] if cat else res, "val")
    else:
        st.info("No validation run yet.")
    st.subheader("Table row counts (live)")
    names = ["customer", "account", "loan", "installment", "payment", "payment_allocation", "delinquency_snapshot", "contact_preference", "collection_case", "contact_attempt", "contact_scheduling_log", "promise_to_pay", "flag_event", "audit_event"]
    st.dataframe(pd.DataFrame({"table": names, "rows": [int(Q(f"SELECT COUNT(*) n FROM {n}")["n"][0]) for n in names]}), hide_index=True)
    rp = ROOT / "reports" / "reproducibility_report.json"
    if rp.exists():
        r = json.loads(rp.read_text())
        st.subheader("Reproducibility (same seed, two runs)")
        st.write({k: r[k] for k in ["seed", "n_loans", "row_counts_identical", "deterministic_ids_identical", "file_checksums_identical", "ground_truth_identical", "reproducible", "dataset_sha256_run1"]})


def page_audit():
    st.header("Audit Trail")
    types = Q("SELECT DISTINCT entity_type FROM audit_event ORDER BY 1")["entity_type"].tolist()
    f = st.columns(3)
    et = f[0].multiselect("Entity type", types)
    eid = f[1].text_input("Entity id (exact)")
    act = f[2].multiselect("Action", Q("SELECT DISTINCT action FROM audit_event ORDER BY 1")["action"].tolist())
    sql, params = "SELECT * FROM audit_event WHERE 1=1", []
    if et: sql += f" AND entity_type IN ({','.join('?' * len(et))})"; params += et
    if eid: sql += " AND entity_id=?"; params.append(eid)
    if act: sql += f" AND action IN ({','.join('?' * len(act))})"; params += act
    df = Q(sql + " ORDER BY event_ts DESC LIMIT 5000", tuple(params))
    st.plotly_chart(px.histogram(df, x="action", color="entity_type", title="Audit events by action (in view)"), width="stretch")
    search_table(df, "audit", height=450)
    st.caption("Audit events are append-only: state transitions never overwrite history.")


PAGES = {"1. Executive Overview": page_overview, "2. Loan Portfolio": page_portfolio, "3. Delinquency Analysis": page_delinquency,
         "4. Collection Prioritization": page_prioritization, "5. Contact Management": page_contact, "6. Promise-to-Pay Monitoring": page_promises,
         "7. Payment Reconciliation": page_reconciliation, "8. Compliance and Exceptions": page_compliance, "9. Data Quality and Validation": page_quality,
         "10. Audit Trail": page_audit}

st.sidebar.title("Ethical Collections")
st.sidebar.caption("Synthetic data | illustrative rules")
if not Path(DB_PATH).exists():
    st.error(f"Database not found at {DB_PATH}. Build it with:\n\n`python -m collections_ai.cli init-db --data data/full` then `python -m collections_ai.cli run-engines`")
    st.stop()
choice = st.sidebar.radio("Page", list(PAGES))
PAGES[choice]()
