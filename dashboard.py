"""
Shotgun Event Analytics Dashboard
Run with: streamlit run dashboard.py

Install deps: pip install streamlit pandas plotly
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path

st.set_page_config(
    page_title="Shotgun Analytics",
    page_icon="🎟️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    div[data-testid="metric-container"] {
        background: #0e1117;
        border: 1px solid #2a2a3a;
        border-radius: 10px;
        padding: 16px 20px;
    }
    div[data-testid="metric-container"] label { color: #9a9ab0 !important; font-size: 13px; }
    div[data-testid="metric-container"] [data-testid="stMetricValue"] { font-size: 26px; }
</style>
""", unsafe_allow_html=True)

DEFAULT_CSV = Path(r"C:\Users\gbaida\Documents\Claude testes\shotgun_tickets_20260504_224306.csv")

COLORS = px.colors.qualitative.Vivid


@st.cache_data
def load_data(source) -> pd.DataFrame:
    df = pd.read_csv(source)

    for col in ["ordered_at", "event_start_time", "event_end_time",
                "ticket_scanned_at", "ticket_canceled_at", "event_published_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    if "contact_birthday" in df.columns:
        df["contact_birthday"] = pd.to_datetime(df["contact_birthday"], errors="coerce", utc=False)
        now = pd.Timestamp.now()
        df["age"] = ((now - df["contact_birthday"].dt.tz_localize(None)).dt.days / 365.25).round(0).astype("Int64")

    # Prices stored in centavos → BRL
    for col in ["deal_price", "deal_user_service_fee", "deal_producer_cost"]:
        if col in df.columns:
            df[f"{col}_brl"] = pd.to_numeric(df[col], errors="coerce") / 100

    # Normalize UTM source
    if "utm_source" in df.columns:
        df["utm_source"] = (
            df["utm_source"].fillna("direct").str.lower().str.strip()
            .str.replace(r"\.com$", "", regex=True)
        )
        df["utm_source"] = df["utm_source"].replace({"": "direct"})

    if "utm_medium" in df.columns:
        df["utm_medium"] = df["utm_medium"].fillna("unknown")

    if "contact_newsletter_optin" in df.columns:
        df["contact_newsletter_optin"] = df["contact_newsletter_optin"].map(
            {"True": True, "False": False, True: True, False: False}
        )

    if "ordered_at" in df.columns and "event_start_time" in df.columns:
        df["days_before_event"] = (
            df["event_start_time"] - df["ordered_at"]
        ).dt.total_seconds() / 86400

    if "ordered_at" in df.columns:
        df["order_date"] = df["ordered_at"].dt.date
        df["order_hour"] = df["ordered_at"].dt.hour
        df["order_dow"]  = df["ordered_at"].dt.day_name()

    return df


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎟️ Shotgun Analytics")
    st.divider()

    uploaded = st.file_uploader("Upload CSV export", type="csv")
    if uploaded:
        df = load_data(uploaded)
    elif DEFAULT_CSV.exists():
        df = load_data(str(DEFAULT_CSV))
        st.caption(f"File: `{DEFAULT_CSV.name}`")
    else:
        st.error("No CSV found. Upload one above.")
        st.stop()

    st.divider()
    events = sorted(df["event_name"].dropna().unique())
    sel_events = st.multiselect("Filter events", events, default=list(events))

    all_statuses = sorted(df["ticket_status"].dropna().unique())
    sel_statuses = st.multiselect(
        "Ticket status", all_statuses,
        default=[s for s in all_statuses if s != "canceled"]
    )

# Filtered frame (for most charts, excluding canceled)
dff = df[df["event_name"].isin(sel_events) & df["ticket_status"].isin(sel_statuses)].copy()
# Full frame for selected events (including canceled, for cancel-rate calcs)
df_sel = df[df["event_name"].isin(sel_events)].copy()

if dff.empty:
    st.warning("No tickets match the current filters.")
    st.stop()

# ── KPI Row ────────────────────────────────────────────────────────────────────
total_tickets     = len(dff)
unique_attendees  = dff["contact_id"].nunique()
total_revenue     = dff["deal_price_brl"].sum() if "deal_price_brl" in dff else 0
total_canceled    = (df_sel["ticket_status"] == "canceled").sum()
cancel_rate       = total_canceled / len(df_sel) * 100 if len(df_sel) else 0
scanned           = dff["ticket_scanned_at"].notna().sum()
scan_rate         = scanned / total_tickets * 100 if total_tickets else 0
newsletter_rate   = (
    dff["contact_newsletter_optin"].sum() / dff["contact_newsletter_optin"].notna().sum() * 100
    if "contact_newsletter_optin" in dff and dff["contact_newsletter_optin"].notna().sum() > 0
    else 0
)

st.markdown("## Overview")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Tickets Sold",     f"{total_tickets:,}")
c2.metric("Unique Attendees", f"{unique_attendees:,}")
c3.metric("Total Revenue",    f"R${total_revenue:,.2f}")
c4.metric("Scan Rate",        f"{scan_rate:.1f}%")
c5.metric("Cancellation Rate",f"{cancel_rate:.1f}%")
c6.metric("Newsletter Opt-in",f"{newsletter_rate:.1f}%")

st.divider()

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_sales, tab_revenue, tab_marketing, tab_audience, tab_ops = st.tabs([
    "📈 Sales", "💰 Revenue", "📣 Marketing", "👥 Audience", "🔍 Operations"
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — SALES
# ══════════════════════════════════════════════════════════════════════════════
with tab_sales:
    st.subheader("Ticket Sales Over Time")

    if "order_date" in dff.columns:
        col_l, col_r = st.columns(2)

        # Daily sales per event
        daily = (
            dff.groupby(["order_date", "event_name"])
            .size().reset_index(name="tickets")
        )
        daily["order_date"] = pd.to_datetime(daily["order_date"])
        daily = daily.sort_values("order_date")

        fig = px.bar(
            daily, x="order_date", y="tickets", color="event_name",
            labels={"order_date": "Date", "tickets": "Tickets Sold", "event_name": "Event"},
            title="Daily Ticket Sales by Event",
            color_discrete_sequence=COLORS,
        )
        fig.update_layout(legend=dict(orientation="h", y=-0.2), bargap=0.15)
        col_l.plotly_chart(fig, use_container_width=True)

        # Cumulative sales
        cum = daily.copy()
        cum["cumulative"] = cum.groupby("event_name")["tickets"].cumsum()
        fig2 = px.line(
            cum, x="order_date", y="cumulative", color="event_name",
            labels={"order_date": "Date", "cumulative": "Cumulative Tickets", "event_name": "Event"},
            title="Cumulative Ticket Sales",
            color_discrete_sequence=COLORS,
            markers=True,
        )
        fig2.update_layout(legend=dict(orientation="h", y=-0.2))
        col_r.plotly_chart(fig2, use_container_width=True)

    col_l2, col_r2 = st.columns(2)

    # Sales by hour of day
    if "order_hour" in dff.columns:
        hourly = dff.groupby("order_hour").size().reset_index(name="tickets")
        fig3 = px.bar(
            hourly, x="order_hour", y="tickets",
            labels={"order_hour": "Hour of Day", "tickets": "Tickets Sold"},
            title="Sales by Hour of Day",
            color="tickets", color_continuous_scale="Blues",
        )
        fig3.update_coloraxes(showscale=False)
        col_l2.plotly_chart(fig3, use_container_width=True)

    # Days before event
    if "days_before_event" in dff.columns:
        dff_pre = dff[dff["days_before_event"] >= 0].copy()
        dff_pre["days_before_bucket"] = dff_pre["days_before_event"].clip(upper=30).astype(int)
        pre_counts = dff_pre.groupby("days_before_bucket").size().reset_index(name="tickets")
        fig4 = px.bar(
            pre_counts.sort_values("days_before_bucket", ascending=False),
            x="days_before_bucket", y="tickets",
            labels={"days_before_bucket": "Days Before Event", "tickets": "Tickets Sold"},
            title="When Did People Buy? (Days Before Event)",
            color="tickets", color_continuous_scale="Teal",
        )
        fig4.update_coloraxes(showscale=False)
        col_r2.plotly_chart(fig4, use_container_width=True)

    # Tickets by event summary table
    st.subheader("Sales by Event")
    summary = (
        df_sel.groupby("event_name").agg(
            total=("ticket_id", "count"),
            valid=("ticket_status", lambda x: (x == "valid").sum()),
            canceled=("ticket_status", lambda x: (x == "canceled").sum()),
            scanned=("ticket_scanned_at", lambda x: x.notna().sum()),
            revenue=("deal_price_brl", "sum"),
        ).reset_index()
    )
    summary["scan_rate"]   = (summary["scanned"] / summary["valid"] * 100).round(1).astype(str) + "%"
    summary["cancel_rate"] = (summary["canceled"] / summary["total"] * 100).round(1).astype(str) + "%"
    summary["revenue"]     = summary["revenue"].map("R${:,.2f}".format)
    summary.columns        = ["Event", "Total", "Valid", "Canceled", "Scanned", "Revenue", "Scan Rate", "Cancel Rate"]
    st.dataframe(summary, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — REVENUE
# ══════════════════════════════════════════════════════════════════════════════
with tab_revenue:
    if "deal_price_brl" not in dff.columns:
        st.info("No price data available.")
    else:
        col_l, col_r = st.columns(2)

        # Revenue by ticket tier
        rev_tier = (
            dff.groupby("deal_title")
            .agg(tickets=("ticket_id", "count"), revenue=("deal_price_brl", "sum"))
            .reset_index().sort_values("revenue", ascending=False)
        )
        fig = px.bar(
            rev_tier, x="deal_title", y="revenue",
            labels={"deal_title": "Ticket Tier", "revenue": "Revenue (BRL)"},
            title="Revenue by Ticket Tier",
            color="deal_title", color_discrete_sequence=COLORS,
            text="revenue",
        )
        fig.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
        fig.update_layout(showlegend=False, xaxis_tickangle=-20)
        col_l.plotly_chart(fig, use_container_width=True)

        # Tickets sold by tier
        fig2 = px.pie(
            rev_tier, names="deal_title", values="tickets",
            title="Ticket Volume by Tier",
            color_discrete_sequence=COLORS, hole=0.4,
        )
        fig2.update_traces(textinfo="percent+label")
        col_r.plotly_chart(fig2, use_container_width=True)

        col_l2, col_r2 = st.columns(2)

        # Payment method breakdown
        if "payment_method" in dff.columns:
            pay = (
                dff[dff["deal_price_brl"] > 0]
                .groupby("payment_method")
                .agg(tickets=("ticket_id", "count"), revenue=("deal_price_brl", "sum"))
                .reset_index()
            )
            pay["payment_method"] = pay["payment_method"].replace({"": "other"}).fillna("other")
            fig3 = px.bar(
                pay, x="payment_method", y="revenue",
                labels={"payment_method": "Payment Method", "revenue": "Revenue (BRL)"},
                title="Revenue by Payment Method",
                color="payment_method", color_discrete_sequence=COLORS,
                text="revenue",
            )
            fig3.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
            fig3.update_layout(showlegend=False)
            col_l2.plotly_chart(fig3, use_container_width=True)

        # Free vs paid
        dff["ticket_type"] = dff["deal_price_brl"].apply(
            lambda x: "Free" if x == 0 else "Paid"
        )
        fp = dff["ticket_type"].value_counts().reset_index()
        fp.columns = ["type", "count"]
        fig4 = px.pie(
            fp, names="type", values="count",
            title="Free vs Paid Tickets",
            color_discrete_map={"Free": "#636EFA", "Paid": "#EF553B"},
            hole=0.4,
        )
        fig4.update_traces(textinfo="percent+value")
        col_r2.plotly_chart(fig4, use_container_width=True)

        # Revenue over time
        if "order_date" in dff.columns:
            rev_daily = (
                dff.groupby(["order_date", "event_name"])["deal_price_brl"]
                .sum().reset_index()
            )
            rev_daily["order_date"] = pd.to_datetime(rev_daily["order_date"])
            fig5 = px.area(
                rev_daily.sort_values("order_date"),
                x="order_date", y="deal_price_brl", color="event_name",
                labels={"order_date": "Date", "deal_price_brl": "Revenue (BRL)", "event_name": "Event"},
                title="Daily Revenue Over Time",
                color_discrete_sequence=COLORS,
            )
            fig5.update_layout(legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(fig5, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — MARKETING
# ══════════════════════════════════════════════════════════════════════════════
with tab_marketing:
    col_l, col_r = st.columns(2)

    # UTM Source
    if "utm_source" in dff.columns:
        src = dff["utm_source"].value_counts().reset_index()
        src.columns = ["source", "tickets"]
        fig = px.bar(
            src, x="tickets", y="source", orientation="h",
            labels={"source": "Source", "tickets": "Tickets"},
            title="Tickets by Acquisition Source",
            color="source", color_discrete_sequence=COLORS,
            text="tickets",
        )
        fig.update_traces(textposition="outside")
        fig.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
        col_l.plotly_chart(fig, use_container_width=True)

    # UTM Medium
    if "utm_medium" in dff.columns:
        med = dff["utm_medium"].value_counts().reset_index()
        med.columns = ["medium", "tickets"]
        fig2 = px.pie(
            med, names="medium", values="tickets",
            title="App vs Website Purchases",
            color_discrete_sequence=COLORS, hole=0.4,
        )
        fig2.update_traces(textinfo="percent+label")
        col_r.plotly_chart(fig2, use_container_width=True)

    # Source × Medium heatmap
    if "utm_source" in dff.columns and "utm_medium" in dff.columns:
        pivot = (
            dff.groupby(["utm_source", "utm_medium"])
            .size().reset_index(name="tickets")
            .pivot(index="utm_source", columns="utm_medium", values="tickets")
            .fillna(0)
        )
        fig3 = px.imshow(
            pivot,
            labels=dict(x="Medium", y="Source", color="Tickets"),
            title="Source × Medium Heatmap",
            color_continuous_scale="Blues",
            text_auto=True,
        )
        st.plotly_chart(fig3, use_container_width=True)

    # Source performance per event
    if len(sel_events) > 1 and "utm_source" in dff.columns:
        src_ev = (
            dff.groupby(["event_name", "utm_source"])
            .size().reset_index(name="tickets")
        )
        fig4 = px.bar(
            src_ev, x="event_name", y="tickets", color="utm_source",
            barmode="group",
            labels={"event_name": "Event", "tickets": "Tickets", "utm_source": "Source"},
            title="Channel Performance per Event",
            color_discrete_sequence=COLORS,
        )
        fig4.update_layout(legend=dict(orientation="h", y=-0.25), xaxis_tickangle=-20)
        st.plotly_chart(fig4, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AUDIENCE
# ══════════════════════════════════════════════════════════════════════════════
with tab_audience:
    col_l, col_r = st.columns(2)

    # Gender
    if "contact_gender" in dff.columns:
        gender = (
            dff.drop_duplicates("contact_id")["contact_gender"]
            .replace({"-": None, "": None}).dropna()
            .value_counts().reset_index()
        )
        gender.columns = ["gender", "count"]
        fig = px.pie(
            gender, names="gender", values="count",
            title="Audience Gender",
            color_discrete_sequence=COLORS, hole=0.4,
        )
        fig.update_traces(textinfo="percent+label")
        col_l.plotly_chart(fig, use_container_width=True)

    # Age distribution
    if "age" in dff.columns:
        ages = dff.drop_duplicates("contact_id")["age"].dropna()
        ages = ages[(ages >= 16) & (ages <= 80)]
        fig2 = px.histogram(
            ages, x="age", nbins=30,
            labels={"age": "Age", "count": "Attendees"},
            title="Age Distribution of Attendees",
            color_discrete_sequence=["#636EFA"],
        )
        fig2.update_layout(bargap=0.05)
        col_r.plotly_chart(fig2, use_container_width=True)

    col_l2, col_r2 = st.columns(2)

    # Top cities
    if "contact_locality" in dff.columns:
        cities = (
            dff.drop_duplicates("contact_id")["contact_locality"]
            .replace({"-": None, "01008-000": None, "": None}).dropna()
            .value_counts().head(15).reset_index()
        )
        cities.columns = ["city", "attendees"]
        fig3 = px.bar(
            cities, x="attendees", y="city", orientation="h",
            labels={"city": "City", "attendees": "Attendees"},
            title="Top 15 Cities",
            color="attendees", color_continuous_scale="Blues",
            text="attendees",
        )
        fig3.update_traces(textposition="outside")
        fig3.update_coloraxes(showscale=False)
        fig3.update_layout(yaxis={"categoryorder": "total ascending"})
        col_l2.plotly_chart(fig3, use_container_width=True)

    # Newsletter opt-in
    if "contact_newsletter_optin" in dff.columns:
        optin = (
            dff.drop_duplicates("contact_id")["contact_newsletter_optin"]
            .map({True: "Opted In", False: "Opted Out"})
            .dropna().value_counts().reset_index()
        )
        optin.columns = ["status", "count"]
        fig4 = px.pie(
            optin, names="status", values="count",
            title="Newsletter Opt-in Rate",
            color_discrete_map={"Opted In": "#00CC96", "Opted Out": "#EF553B"},
            hole=0.4,
        )
        fig4.update_traces(textinfo="percent+value")
        col_r2.plotly_chart(fig4, use_container_width=True)

    # Returning vs new attendees (appeared in >1 event)
    if len(sel_events) > 1:
        st.subheader("Audience Loyalty")
        events_per_contact = (
            dff.groupby("contact_id")["event_id"].nunique().reset_index()
        )
        events_per_contact.columns = ["contact_id", "events_attended"]
        loyalty = events_per_contact["events_attended"].value_counts().reset_index()
        loyalty.columns = ["events_attended", "attendees"]
        loyalty["label"] = loyalty["events_attended"].apply(
            lambda x: f"{x} event{'s' if x > 1 else ''}"
        )
        fig5 = px.bar(
            loyalty.sort_values("events_attended"),
            x="label", y="attendees",
            labels={"label": "Events Attended", "attendees": "Attendees"},
            title="Returning Attendees (Events Attended)",
            color="attendees", color_continuous_scale="Greens",
            text="attendees",
        )
        fig5.update_traces(textposition="outside")
        fig5.update_coloraxes(showscale=False)
        st.plotly_chart(fig5, use_container_width=True)

    # Age × Gender breakdown
    if "age" in dff.columns and "contact_gender" in dff.columns:
        age_gen = (
            dff.drop_duplicates("contact_id")[["age", "contact_gender"]]
            .replace({"-": None, "": None}).dropna()
        )
        age_gen = age_gen[(age_gen["age"] >= 16) & (age_gen["age"] <= 80)]
        fig6 = px.histogram(
            age_gen, x="age", color="contact_gender", nbins=25,
            barmode="overlay", opacity=0.75,
            labels={"age": "Age", "contact_gender": "Gender"},
            title="Age Distribution by Gender",
            color_discrete_sequence=COLORS,
        )
        st.plotly_chart(fig6, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_ops:
    col_l, col_r = st.columns(2)

    # Scan rate per event
    scan_ev = (
        dff.groupby("event_name").agg(
            total=("ticket_id", "count"),
            scanned=("ticket_scanned_at", lambda x: x.notna().sum()),
        ).reset_index()
    )
    scan_ev["scan_rate"] = scan_ev["scanned"] / scan_ev["total"] * 100
    scan_ev["not_scanned"] = scan_ev["total"] - scan_ev["scanned"]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Scanned", x=scan_ev["event_name"], y=scan_ev["scanned"],
        marker_color="#00CC96", text=scan_ev["scanned"], textposition="inside",
    ))
    fig.add_trace(go.Bar(
        name="Not Scanned", x=scan_ev["event_name"], y=scan_ev["not_scanned"],
        marker_color="#EF553B", text=scan_ev["not_scanned"], textposition="inside",
    ))
    fig.update_layout(
        barmode="stack", title="Scan Rate per Event",
        xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.25),
    )
    col_l.plotly_chart(fig, use_container_width=True)

    # Cancellations per event
    cancel_ev = (
        df_sel.groupby("event_name").agg(
            total=("ticket_id", "count"),
            canceled=("ticket_status", lambda x: (x == "canceled").sum()),
        ).reset_index()
    )
    cancel_ev["cancel_rate"] = cancel_ev["canceled"] / cancel_ev["total"] * 100
    fig2 = px.bar(
        cancel_ev, x="event_name", y="cancel_rate",
        labels={"event_name": "Event", "cancel_rate": "Cancellation Rate (%)"},
        title="Cancellation Rate per Event",
        color="cancel_rate", color_continuous_scale="Reds",
        text=cancel_ev["cancel_rate"].round(1).astype(str) + "%",
    )
    fig2.update_traces(textposition="outside")
    fig2.update_coloraxes(showscale=False)
    fig2.update_layout(xaxis_tickangle=-20)
    col_r.plotly_chart(fig2, use_container_width=True)

    # Ticket status breakdown
    status_counts = df_sel["ticket_status"].value_counts().reset_index()
    status_counts.columns = ["status", "count"]
    fig3 = px.pie(
        status_counts, names="status", values="count",
        title="Ticket Status Breakdown (All Selected Events)",
        color_discrete_sequence=COLORS, hole=0.4,
    )
    fig3.update_traces(textinfo="percent+label+value")
    col_l.plotly_chart(fig3, use_container_width=True)

    # Ticket tier per event
    tier_ev = (
        dff.groupby(["event_name", "deal_title"])
        .size().reset_index(name="tickets")
    )
    fig4 = px.bar(
        tier_ev, x="event_name", y="tickets", color="deal_title",
        barmode="stack",
        labels={"event_name": "Event", "tickets": "Tickets", "deal_title": "Ticket Tier"},
        title="Ticket Tier Mix per Event",
        color_discrete_sequence=COLORS,
    )
    fig4.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.3))
    col_r.plotly_chart(fig4, use_container_width=True)
