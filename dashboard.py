"""
Shotgun Event Analytics Dashboard
Run with: python -m streamlit run dashboard.py
"""

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

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

COLORS = px.colors.qualitative.Plotly
DEFAULT_CSV = Path("")

# ── API fetch logic ────────────────────────────────────────────────────────────

_TICKETS_URL = "https://api.shotgun.live/tickets"
_TIMEOUT     = 60
_MAX_RETRIES = 3


def _api_get(url: str, params: dict, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            if attempt == _MAX_RETRIES:
                raise requests.Timeout(
                    f"A API do Shotgun não respondeu após {_MAX_RETRIES} tentativas "
                    f"({_TIMEOUT}s cada). Tente novamente em alguns instantes."
                )
        except requests.HTTPError:
            raise


def _parse_after(next_url: str) -> str | None:
    values = parse_qs(urlparse(next_url).query).get("after", [])
    return values[0] if values else None


def fetch_tickets_from_api(token: str, organizer_id: str, progress=None) -> pd.DataFrame:
    params: dict = {"organizer_id": organizer_id}
    all_records: list[dict] = []
    cursor: str | None = None

    while True:
        if cursor:
            params["after"] = cursor
        data = _api_get(_TICKETS_URL, params, token)
        records = data.get("data", [])
        next_url = data.get("pagination", {}).get("next")
        all_records.extend(records)
        if progress:
            progress.text(f"Buscando... {len(all_records):,} ingressos encontrados")
        if not records or not next_url:
            break
        cursor = _parse_after(next_url)

    return pd.DataFrame(all_records)


# ── Data processing ────────────────────────────────────────────────────────────

def process(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["ordered_at", "event_start_time", "event_end_time",
                "ticket_scanned_at", "ticket_canceled_at", "event_published_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)

    if "contact_birthday" in df.columns:
        df["contact_birthday"] = pd.to_datetime(df["contact_birthday"], errors="coerce", utc=False)
        now = pd.Timestamp.now()
        df["age"] = (
            (now - df["contact_birthday"].dt.tz_localize(None)).dt.days / 365.25
        ).round(0).astype("Int64")

    for col in ["deal_price", "deal_user_service_fee", "deal_producer_cost"]:
        if col in df.columns:
            df[f"{col}_brl"] = pd.to_numeric(df[col], errors="coerce") / 100

    if "utm_source" in df.columns:
        df["utm_source"] = (
            df["utm_source"].fillna("direto").str.lower().str.strip()
            .str.replace(r"\.com$", "", regex=True)
            .replace({"": "direto", "direct": "direto"})
        )
    if "utm_medium" in df.columns:
        df["utm_medium"] = df["utm_medium"].fillna("desconhecido")

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


@st.cache_data
def load_csv(source) -> pd.DataFrame:
    return process(pd.read_csv(source))


# ── Sidebar esquerda — fonte de dados ─────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎟️ Shotgun Analytics")
    st.caption("feito por [ponkan](https://linktr.ee/ponkan_)")

    if "df" in st.session_state:
        if st.button("🚪 Limpar dados", use_container_width=True):
            del st.session_state["df"]
            st.session_state.pop("source_label", None)
            st.rerun()

    st.divider()

    st.markdown("### Buscar via API")
    st.caption("[Como descobrir os seus dados de API](https://support-pro.shotgun.live/hc/en-us/articles/33561354477970-Find-your-Organizer-id-and-API-token#h_01KJ7K6DYV1FWN0AD6NRV5W1XE)")
    api_token    = st.text_input("Token de API", type="password", placeholder="eyJhbGci...")
    organizer_id = st.text_input("ID do Organizador", placeholder="123456")

    if st.button("🔄 Buscar Dados", use_container_width=True, type="primary"):
        if not api_token or not organizer_id:
            st.error("Preencha o Token de API e o ID do Organizador.")
        else:
            prog = st.empty()
            try:
                with st.spinner("Conectando à API do Shotgun..."):
                    raw = fetch_tickets_from_api(api_token, organizer_id, progress=prog)
                st.session_state["df"] = process(raw)
                st.session_state["source_label"] = f"API ao vivo — {len(raw):,} ingressos"
                prog.empty()
                st.success(f"{len(raw):,} ingressos carregados com sucesso.")
            except requests.Timeout as e:
                st.error(f"⏱️ {e}")
            except requests.HTTPError as e:
                st.error(f"Erro na API {e.response.status_code}: {e.response.text[:200]}")
            except Exception as e:
                st.error(f"Erro: {e}")

    st.divider()

    st.markdown("### Ou envie um arquivo CSV")
    uploaded = st.file_uploader("CSV", type="csv", label_visibility="collapsed")
    if uploaded:
        st.session_state["df"] = load_csv(uploaded)
        st.session_state["source_label"] = f"CSV — {uploaded.name}"

    if "df" not in st.session_state:
        if DEFAULT_CSV.is_file():
            st.session_state["df"] = load_csv(str(DEFAULT_CSV))
            st.session_state["source_label"] = f"Arquivo local — {DEFAULT_CSV.name}"
        else:
            st.info("Insira seus dados de API ou envie um CSV para começar.")

    if "df" in st.session_state:
        st.caption(f"Fonte: {st.session_state.get('source_label', '')}")

    st.divider()
    st.caption("Gostou? Pix e sugestões para gustavobaida@gmail.com")


# ── Tela de boas-vindas ────────────────────────────────────────────────────────
if "df" not in st.session_state:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown(
            "<h1 style='text-align:center'>🎟️ Shotgun Analytics</h1>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<p style='text-align:center; color:#9a9ab0; margin-bottom:0.5rem'>"
            "Painel de análise de eventos conectado diretamente à API do Shotgun.</p>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<p style='text-align:center; color:#9a9ab0; margin-bottom:2rem'>"
            "feito por <a href='https://linktr.ee/ponkan_' target='_blank'>ponkan</a></p>",
            unsafe_allow_html=True,
        )
        st.markdown("""
**O que você pode analisar:**

- 📈 **Vendas** — evolução diária, horários de pico e comportamento de compra antes do evento
- 💰 **Receita** — por categoria de ingresso, método de pagamento e ao longo do tempo
- 📣 **Marketing** — quais canais (Instagram, Direct, Shotgun App...) geraram mais vendas
- 👥 **Público** — gênero, faixa etária, cidades e taxa de opt-in na newsletter
- 🔍 **Operações** — taxa de leitura de ingressos na entrada e cancelamentos por evento

---

**Como começar:**

1. Acesse o **Shotgun Smartboard** → Configurações → Integrações → APIs do Shotgun
2. Copie seu **Token de API** e **ID do Organizador**
3. Cole os dados na barra lateral e clique em **🔄 Buscar Dados**

Ou envie um arquivo `.csv` exportado anteriormente diretamente pela barra lateral.
""")
    st.stop()


# ── Layout principal: conteúdo + filtros à direita ────────────────────────────
df = st.session_state["df"]
col_main, col_filters = st.columns([5, 1])

# ── Filtros (coluna direita) ───────────────────────────────────────────────────
with col_filters:
    st.markdown("### Filtros")

    # Checklist de eventos
    st.markdown("**Eventos**")
    events_info = (
        df[["event_id", "event_name"]].drop_duplicates()
        .sort_values("event_name").reset_index(drop=True)
    )
    ca, cb = st.columns(2)
    if ca.button("✅ Todos", use_container_width=True):
        for eid in events_info["event_id"].tolist():
            st.session_state[f"evt_{eid}"] = True
        st.rerun()
    if cb.button("☐ Nenhum", use_container_width=True):
        for eid in events_info["event_id"].tolist():
            st.session_state[f"evt_{eid}"] = False
        st.rerun()

    box_h = min(260, len(events_info) * 28 + 16)
    sel_events = []
    with st.container(height=box_h):
        for _, row in events_info.iterrows():
            key = f"evt_{row['event_id']}"
            if key not in st.session_state:
                st.session_state[key] = True
            if st.checkbox(row["event_name"], key=key, help=f"ID: {row['event_id']}"):
                sel_events.append(row["event_name"])

    if not sel_events:
        sel_events = events_info["event_name"].tolist()

    # Período de compra
    st.markdown("**Período de compra**")
    if "order_date" in df.columns:
        min_d = df["order_date"].min()
        max_d = df["order_date"].max()
        if min_d < max_d:
            date_range = st.slider(
                "Período", min_value=min_d, max_value=max_d,
                value=(min_d, max_d), format="DD/MM/YY",
                label_visibility="collapsed",
            )
        else:
            date_range = (min_d, max_d)
    else:
        date_range = None

    # Status
    st.markdown("**Status do ingresso**")
    all_statuses = sorted(df["ticket_status"].dropna().unique())
    sel_statuses = st.multiselect(
        "Status", all_statuses,
        default=[s for s in all_statuses if s != "canceled"],
        label_visibility="collapsed",
    )

# ── Aplicar filtros ────────────────────────────────────────────────────────────
mask = df["event_name"].isin(sel_events) & df["ticket_status"].isin(sel_statuses)
if date_range and "order_date" in df.columns:
    mask &= df["order_date"].between(date_range[0], date_range[1])
dff    = df[mask].copy()
df_sel = df[df["event_name"].isin(sel_events)].copy()


# ── Conteúdo principal (coluna esquerda) ──────────────────────────────────────
with col_main:

    if dff.empty:
        st.warning("Nenhum ingresso encontrado para os filtros selecionados.")
        st.stop()

    # ── KPIs ──────────────────────────────────────────────────────────────────
    total_tickets    = len(dff)
    unique_attendees = dff["contact_id"].nunique()
    total_revenue    = dff["deal_price_brl"].sum() if "deal_price_brl" in dff else 0
    total_canceled   = (df_sel["ticket_status"] == "canceled").sum()
    cancel_rate      = total_canceled / len(df_sel) * 100 if len(df_sel) else 0
    scanned          = dff["ticket_scanned_at"].notna().sum()
    scan_rate        = scanned / total_tickets * 100 if total_tickets else 0
    newsletter_rate  = (
        dff["contact_newsletter_optin"].sum() / dff["contact_newsletter_optin"].notna().sum() * 100
        if "contact_newsletter_optin" in dff and dff["contact_newsletter_optin"].notna().sum() > 0
        else 0
    )

    st.markdown("## Visão Geral")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Ingressos Vendidos",    f"{total_tickets:,}")
    c2.metric("Participantes Únicos",  f"{unique_attendees:,}")
    c3.metric("Receita Total",         f"R${total_revenue:,.2f}")
    c4.metric("Taxa de Leitura",       f"{scan_rate:.1f}%")
    c5.metric("Taxa de Cancelamento",  f"{cancel_rate:.1f}%")
    c6.metric("Opt-in Newsletter",     f"{newsletter_rate:.1f}%")

    st.divider()

    # ── Abas ──────────────────────────────────────────────────────────────────
    tab_sales, tab_revenue, tab_marketing, tab_audience, tab_ops = st.tabs([
        "📈 Vendas", "💰 Receita", "📣 Marketing", "👥 Público", "🔍 Operações"
    ])

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 1 — VENDAS
    # ══════════════════════════════════════════════════════════════════════════
    with tab_sales:
        st.subheader("Vendas de Ingressos ao Longo do Tempo")

        if "order_date" in dff.columns:
            col_l, col_r = st.columns(2)
            daily = (
                dff.groupby(["order_date", "event_name"])
                .size().reset_index(name="ingressos")
            )
            daily["order_date"] = pd.to_datetime(daily["order_date"])
            daily = daily.sort_values("order_date")

            fig = px.bar(
                daily, x="order_date", y="ingressos", color="event_name",
                labels={"order_date": "Data", "ingressos": "Ingressos Vendidos", "event_name": "Evento"},
                title="Vendas Diárias por Evento",
                color_discrete_sequence=COLORS,
            )
            fig.update_layout(legend=dict(orientation="h", y=-0.2), bargap=0.15)
            col_l.plotly_chart(fig, use_container_width=True)

            cum = daily.copy()
            cum["acumulado"] = cum.groupby("event_name")["ingressos"].cumsum()
            fig2 = px.line(
                cum, x="order_date", y="acumulado", color="event_name",
                labels={"order_date": "Data", "acumulado": "Ingressos Acumulados", "event_name": "Evento"},
                title="Vendas Acumuladas de Ingressos",
                color_discrete_sequence=COLORS, markers=True,
            )
            fig2.update_layout(legend=dict(orientation="h", y=-0.2))
            col_r.plotly_chart(fig2, use_container_width=True)

        col_l2, col_r2 = st.columns(2)

        if "order_hour" in dff.columns:
            hourly = dff.groupby("order_hour").size().reset_index(name="ingressos")
            fig3 = px.bar(
                hourly, x="order_hour", y="ingressos",
                labels={"order_hour": "Hora do Dia", "ingressos": "Ingressos Vendidos"},
                title="Vendas por Hora do Dia",
                color_discrete_sequence=[COLORS[0]],
            )
            col_l2.plotly_chart(fig3, use_container_width=True)

        if "days_before_event" in dff.columns:
            dff_pre = dff[dff["days_before_event"] >= 0].copy()
            dff_pre["days_before_bucket"] = dff_pre["days_before_event"].clip(upper=30).astype(int)
            pre_counts = dff_pre.groupby("days_before_bucket").size().reset_index(name="ingressos")
            fig4 = px.bar(
                pre_counts.sort_values("days_before_bucket", ascending=False),
                x="days_before_bucket", y="ingressos",
                labels={"days_before_bucket": "Dias Antes do Evento", "ingressos": "Ingressos Vendidos"},
                title="Quando as Pessoas Compraram? (Dias Antes do Evento)",
                color_discrete_sequence=[COLORS[2]],
            )
            col_r2.plotly_chart(fig4, use_container_width=True)

        st.subheader("Resumo por Evento")
        summary = (
            df_sel.groupby("event_name").agg(
                total=("ticket_id", "count"),
                validos=("ticket_status", lambda x: (x == "valid").sum()),
                cancelados=("ticket_status", lambda x: (x == "canceled").sum()),
                lidos=("ticket_scanned_at", lambda x: x.notna().sum()),
                receita=("deal_price_brl", "sum"),
            ).reset_index()
        )
        summary["taxa_leitura"]      = (summary["lidos"] / summary["validos"] * 100).round(1).astype(str) + "%"
        summary["taxa_cancelamento"] = (summary["cancelados"] / summary["total"] * 100).round(1).astype(str) + "%"
        summary["receita"]           = summary["receita"].map("R${:,.2f}".format)
        summary.columns              = ["Evento", "Total", "Válidos", "Cancelados", "Lidos", "Receita", "Taxa de Leitura", "Taxa de Cancelamento"]
        st.dataframe(summary, use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 2 — RECEITA
    # ══════════════════════════════════════════════════════════════════════════
    with tab_revenue:
        if "deal_price_brl" not in dff.columns:
            st.info("Dados de preço não disponíveis.")
        else:
            col_l, col_r = st.columns(2)

            vol_ev = (
                dff.groupby(["event_name", "deal_title"])
                .agg(ingressos=("ticket_id", "count"), receita=("deal_price_brl", "sum"))
                .reset_index()
            )
            fig2 = px.bar(
                vol_ev, x="event_name", y="ingressos", color="deal_title",
                barmode="stack",
                labels={"event_name": "Evento", "ingressos": "Ingressos", "deal_title": "Categoria"},
                title="Volume de Ingressos por Evento e Categoria",
                color_discrete_sequence=COLORS,
            )
            fig2.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.3))
            col_l.plotly_chart(fig2, use_container_width=True)

            rev_tier = (
                dff.groupby("deal_title")
                .agg(ingressos=("ticket_id", "count"), receita=("deal_price_brl", "sum"))
                .reset_index().sort_values("receita", ascending=False)
            )
            fig = px.bar(
                rev_tier, x="deal_title", y="receita",
                labels={"deal_title": "Categoria de Ingresso", "receita": "Receita (BRL)"},
                title="Receita por Categoria de Ingresso",
                color="deal_title", color_discrete_sequence=COLORS, text="receita",
            )
            fig.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
            fig.update_layout(showlegend=False, xaxis_tickangle=-20)
            col_r.plotly_chart(fig, use_container_width=True)

            col_l2, col_r2 = st.columns(2)

            if "payment_method" in dff.columns:
                pay = (
                    dff[dff["deal_price_brl"] > 0]
                    .groupby("payment_method")
                    .agg(ingressos=("ticket_id", "count"), receita=("deal_price_brl", "sum"))
                    .reset_index()
                )
                pay["payment_method"] = pay["payment_method"].replace({"": "outro"}).fillna("outro")
                fig3 = px.bar(
                    pay, x="payment_method", y="receita",
                    labels={"payment_method": "Método de Pagamento", "receita": "Receita (BRL)"},
                    title="Receita por Método de Pagamento",
                    color="payment_method", color_discrete_sequence=COLORS, text="receita",
                )
                fig3.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
                fig3.update_layout(showlegend=False)
                col_l2.plotly_chart(fig3, use_container_width=True)

            dff["tipo_ingresso"] = dff["deal_price_brl"].apply(lambda x: "Gratuito" if x == 0 else "Pago")
            fp = dff["tipo_ingresso"].value_counts().reset_index()
            fp.columns = ["tipo", "quantidade"]
            fig4 = px.pie(
                fp, names="tipo", values="quantidade",
                title="Ingressos Gratuitos vs Pagos",
                color_discrete_sequence=COLORS, hole=0.4,
            )
            fig4.update_traces(textinfo="percent+value")
            col_r2.plotly_chart(fig4, use_container_width=True)

            if "order_date" in dff.columns:
                rev_daily = (
                    dff.groupby(["order_date", "event_name"])["deal_price_brl"]
                    .sum().reset_index()
                )
                rev_daily["order_date"] = pd.to_datetime(rev_daily["order_date"])
                fig5 = px.area(
                    rev_daily.sort_values("order_date"),
                    x="order_date", y="deal_price_brl", color="event_name",
                    labels={"order_date": "Data", "deal_price_brl": "Receita (BRL)", "event_name": "Evento"},
                    title="Receita Diária ao Longo do Tempo",
                    color_discrete_sequence=COLORS,
                )
                fig5.update_layout(legend=dict(orientation="h", y=-0.2))
                st.plotly_chart(fig5, use_container_width=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 3 — MARKETING
    # ══════════════════════════════════════════════════════════════════════════
    with tab_marketing:
        col_l, col_r = st.columns(2)

        if "utm_source" in dff.columns:
            src = dff["utm_source"].value_counts().reset_index()
            src.columns = ["canal", "ingressos"]
            fig = px.bar(
                src, x="ingressos", y="canal", orientation="h",
                labels={"canal": "Canal", "ingressos": "Ingressos"},
                title="Ingressos por Canal de Aquisição",
                color="canal", color_discrete_sequence=COLORS, text="ingressos",
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
            col_l.plotly_chart(fig, use_container_width=True)

        if "utm_medium" in dff.columns:
            med = dff["utm_medium"].value_counts().reset_index()
            med.columns = ["meio", "ingressos"]
            fig2 = px.pie(
                med, names="meio", values="ingressos",
                title="Compras: App vs Website",
                color_discrete_sequence=COLORS, hole=0.4,
            )
            fig2.update_traces(textinfo="percent+label")
            col_r.plotly_chart(fig2, use_container_width=True)

        if "utm_source" in dff.columns and "utm_medium" in dff.columns:
            pivot = (
                dff.groupby(["utm_source", "utm_medium"])
                .size().reset_index(name="ingressos")
                .pivot(index="utm_source", columns="utm_medium", values="ingressos")
                .fillna(0)
            )
            fig3 = px.imshow(
                pivot,
                labels=dict(x="Meio", y="Canal", color="Ingressos"),
                title="Mapa de Calor: Canal × Meio",
                color_continuous_scale="Blues", text_auto=True,
            )
            st.plotly_chart(fig3, use_container_width=True)

        if len(sel_events) > 1 and "utm_source" in dff.columns:
            src_ev = dff.groupby(["event_name", "utm_source"]).size().reset_index(name="ingressos")
            fig4 = px.bar(
                src_ev, x="event_name", y="ingressos", color="utm_source",
                barmode="group",
                labels={"event_name": "Evento", "ingressos": "Ingressos", "utm_source": "Canal"},
                title="Performance por Canal e Evento",
                color_discrete_sequence=COLORS,
            )
            fig4.update_layout(legend=dict(orientation="h", y=-0.25), xaxis_tickangle=-20)
            st.plotly_chart(fig4, use_container_width=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 4 — PÚBLICO
    # ══════════════════════════════════════════════════════════════════════════
    with tab_audience:
        col_l, col_r = st.columns(2)

        if "contact_gender" in dff.columns:
            gender = (
                dff.drop_duplicates("contact_id")["contact_gender"]
                .replace({"-": None, "": None}).dropna()
                .value_counts().reset_index()
            )
            gender.columns = ["genero", "quantidade"]
            fig = px.pie(
                gender, names="genero", values="quantidade",
                title="Gênero do Público",
                color_discrete_sequence=COLORS, hole=0.4,
            )
            fig.update_traces(textinfo="percent+label")
            col_l.plotly_chart(fig, use_container_width=True)

        if "age" in dff.columns:
            ages = dff.drop_duplicates("contact_id")["age"].dropna()
            ages = ages[(ages >= 16) & (ages <= 80)]
            fig2 = px.histogram(
                ages, x="age", nbins=30,
                labels={"age": "Idade", "count": "Participantes"},
                title="Distribuição de Idade dos Participantes",
                color_discrete_sequence=["#636EFA"],
            )
            fig2.update_layout(bargap=0.05)
            col_r.plotly_chart(fig2, use_container_width=True)

        col_l2, col_r2 = st.columns(2)

        if "contact_locality" in dff.columns:
            cities = (
                dff.drop_duplicates("contact_id")["contact_locality"]
                .replace({"-": None, "01008-000": None, "": None}).dropna()
                .value_counts().head(15).reset_index()
            )
            cities.columns = ["cidade", "participantes"]
            fig3 = px.bar(
                cities, x="participantes", y="cidade", orientation="h",
                labels={"cidade": "Cidade", "participantes": "Participantes"},
                title="Top 15 Cidades",
                color_discrete_sequence=[COLORS[0]], text="participantes",
            )
            fig3.update_traces(textposition="outside")
            fig3.update_layout(yaxis={"categoryorder": "total ascending"})
            col_l2.plotly_chart(fig3, use_container_width=True)

        if "contact_newsletter_optin" in dff.columns:
            optin = (
                dff.drop_duplicates("contact_id")["contact_newsletter_optin"]
                .map({True: "Inscrito", False: "Não Inscrito"})
                .dropna().value_counts().reset_index()
            )
            optin.columns = ["status", "quantidade"]
            fig4 = px.pie(
                optin, names="status", values="quantidade",
                title="Taxa de Opt-in Newsletter",
                color_discrete_sequence=COLORS, hole=0.4,
            )
            fig4.update_traces(textinfo="percent+value")
            col_r2.plotly_chart(fig4, use_container_width=True)

        if len(sel_events) > 1:
            st.subheader("Fidelidade do Público")
            events_per_contact = dff.groupby("contact_id")["event_id"].nunique().reset_index()
            events_per_contact.columns = ["contact_id", "eventos_frequentados"]
            loyalty = events_per_contact["eventos_frequentados"].value_counts().reset_index()
            loyalty.columns = ["eventos_frequentados", "participantes"]
            loyalty["label"] = loyalty["eventos_frequentados"].apply(
                lambda x: f"{x} evento{'s' if x > 1 else ''}"
            )
            fig5 = px.bar(
                loyalty.sort_values("eventos_frequentados"),
                x="label", y="participantes",
                labels={"label": "Eventos Frequentados", "participantes": "Participantes"},
                title="Participantes Recorrentes",
                color_discrete_sequence=[COLORS[2]], text="participantes",
            )
            fig5.update_traces(textposition="outside")
            st.plotly_chart(fig5, use_container_width=True)

        if "age" in dff.columns and "contact_gender" in dff.columns:
            age_gen = (
                dff.drop_duplicates("contact_id")[["age", "contact_gender"]]
                .replace({"-": None, "": None}).dropna()
            )
            age_gen = age_gen[(age_gen["age"] >= 16) & (age_gen["age"] <= 80)]
            fig6 = px.histogram(
                age_gen, x="age", color="contact_gender", nbins=25,
                barmode="overlay", opacity=0.75,
                labels={"age": "Idade", "contact_gender": "Gênero"},
                title="Distribuição de Idade por Gênero",
                color_discrete_sequence=COLORS,
            )
            st.plotly_chart(fig6, use_container_width=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 5 — OPERAÇÕES
    # ══════════════════════════════════════════════════════════════════════════
    with tab_ops:
        col_l, col_r = st.columns(2)

        scan_ev = (
            dff.groupby("event_name").agg(
                total=("ticket_id", "count"),
                lidos=("ticket_scanned_at", lambda x: x.notna().sum()),
            ).reset_index()
        )
        scan_ev["nao_lidos"] = scan_ev["total"] - scan_ev["lidos"]

        fig = go.Figure()
        fig.add_trace(go.Bar(
            name="Lidos", x=scan_ev["event_name"], y=scan_ev["lidos"],
            marker_color="#00CC96", text=scan_ev["lidos"], textposition="inside",
        ))
        fig.add_trace(go.Bar(
            name="Não Lidos", x=scan_ev["event_name"], y=scan_ev["nao_lidos"],
            marker_color="#EF553B", text=scan_ev["nao_lidos"], textposition="inside",
        ))
        fig.update_layout(
            barmode="stack", title="Taxa de Leitura por Evento",
            xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.25),
        )
        col_l.plotly_chart(fig, use_container_width=True)

        cancel_ev = (
            df_sel.groupby("event_name").agg(
                total=("ticket_id", "count"),
                cancelados=("ticket_status", lambda x: (x == "canceled").sum()),
            ).reset_index()
        )
        cancel_ev["taxa_cancelamento"] = cancel_ev["cancelados"] / cancel_ev["total"] * 100
        fig2 = px.bar(
            cancel_ev, x="event_name", y="taxa_cancelamento",
            labels={"event_name": "Evento", "taxa_cancelamento": "Taxa de Cancelamento (%)"},
            title="Taxa de Cancelamento por Evento",
            color_discrete_sequence=[COLORS[1]],
            text=cancel_ev["taxa_cancelamento"].round(1).astype(str) + "%",
        )
        fig2.update_traces(textposition="outside")
        fig2.update_layout(xaxis_tickangle=-20)
        col_r.plotly_chart(fig2, use_container_width=True)

        status_counts = df_sel["ticket_status"].value_counts().reset_index()
        status_counts.columns = ["status", "quantidade"]
        fig3 = px.pie(
            status_counts, names="status", values="quantidade",
            title="Distribuição por Status de Ingresso",
            color_discrete_sequence=COLORS, hole=0.4,
        )
        fig3.update_traces(textinfo="percent+label+value")
        col_l.plotly_chart(fig3, use_container_width=True)

        tier_ev = dff.groupby(["event_name", "deal_title"]).size().reset_index(name="ingressos")
        fig4 = px.bar(
            tier_ev, x="event_name", y="ingressos", color="deal_title",
            barmode="stack",
            labels={"event_name": "Evento", "ingressos": "Ingressos", "deal_title": "Categoria"},
            title="Mix de Categorias por Evento",
            color_discrete_sequence=COLORS,
        )
        fig4.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.3))
        col_r.plotly_chart(fig4, use_container_width=True)
