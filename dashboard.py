"""
Shotgun Event Analytics Dashboard
Run with: python -m streamlit run dashboard.py
"""

import base64 as _b64
import hashlib as _hl
import json
import os as _os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ── Supabase (opcional) ───────────────────────────────────────────────────────
_SB_AVAIL = False
try:
    from supabase import create_client as _sb_create_client
    _SB_AVAIL = True
except ImportError:
    pass

# Estas variáveis são inicializadas antes do sidebar (veja bloco _SB_INIT abaixo)
_SB_MODE: bool = False
_sb        = None   # supabase.Client
_sb_user   = None   # supabase.User


def _pkce_pair() -> tuple[str, str]:
    """Gera (code_verifier, code_challenge) para PKCE OAuth."""
    verifier  = _b64.urlsafe_b64encode(_os.urandom(32)).decode().rstrip("=")
    challenge = _b64.urlsafe_b64encode(
        _hl.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge

st.set_page_config(
    page_title="Clubber Analytics",
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

_DOW_ORDER  = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DOW_MAP    = dict(zip(_DOW_ORDER, ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]))

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


# ── JSON serialization helper (used when persisting to Supabase) ───────────────
import math as _math

def _sanitize_for_json(v):
    """Convert non-JSON-serializable pandas/numpy scalars to Python-native types."""
    if v is None:
        return None
    try:
        if pd.isnull(v):          # catches NaT, NaN, None
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and (_math.isnan(v) or _math.isinf(v)):
        return None
    if hasattr(v, "isoformat"):   # Timestamp / datetime / date → ISO string
        return v.isoformat()
    if hasattr(v, "item"):        # numpy int64 / float64 → Python native
        return v.item()
    return v


def _sanitize_record(record: dict) -> dict:
    return {k: _sanitize_for_json(val) for k, val in record.items()}


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


# ── Porta (vendas externas: dinheiro / PagBank) ───────────────────────────────

PORTA_PATH = Path(__file__).parent / "porta_entries.json"


def load_porta() -> list[dict]:
    """Carrega entradas Porta — do Supabase (modo multi-user) ou do JSON local."""
    if _SB_MODE and _sb_user:
        try:
            resp = _sb.table("porta_entries").select("*").eq("user_id", _sb_user.id).order("added_at").execute()
            return [
                {
                    "event_name":  r["event_name"],
                    "tickets":     r["tickets"],
                    "revenue_brl": float(r["revenue_brl"]),
                    "source":      r["source"],
                    "added_at":    r.get("added_at", ""),
                    **({"prices":  r["prices"]}      if r.get("prices")     else {}),
                    **({"date":    r["entry_date"]}  if r.get("entry_date") else {}),
                }
                for r in (resp.data or [])
            ]
        except Exception:
            pass  # fallback to local JSON on error
    if not PORTA_PATH.is_file():
        return []
    try:
        return json.loads(PORTA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_porta(entries: list[dict]) -> None:
    """Salva entradas Porta. Em modo Supabase, `save_porta([])` limpa o registro do usuário."""
    if _SB_MODE and _sb_user:
        if not entries:
            try:
                _sb.table("porta_entries").delete().eq("user_id", _sb_user.id).execute()
            except Exception as e:
                st.error(f"Erro ao limpar Porta no banco: {e}")
        # Upsert de lista não-vazia não é chamado diretamente (append_porta_entry insere linha a linha)
        return
    PORTA_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def append_porta_entry(
    event_name: str,
    tickets: int,
    revenue_brl: float,
    source: str,
    prices: list[float] | None = None,
    entry_date=None,
) -> None:
    """Adiciona uma entrada Porta — no Supabase ou no JSON local."""
    if _SB_MODE and _sb_user:
        try:
            row: dict = {
                "user_id":     _sb_user.id,
                "event_name":  event_name,
                "tickets":     int(tickets),
                "revenue_brl": float(revenue_brl),
                "source":      source,
                "added_at":    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if prices:
                row["prices"] = [float(p) for p in prices]
            if entry_date is not None:
                row["entry_date"] = entry_date.isoformat() if hasattr(entry_date, "isoformat") else str(entry_date)
            _sb.table("porta_entries").insert(row).execute()
        except Exception as e:
            st.error(f"Erro ao salvar entrada Porta no banco: {e}")
        return
    # Fallback: JSON local
    entries = load_porta()
    record: dict = {
        "event_name": event_name,
        "tickets": int(tickets),
        "revenue_brl": float(revenue_brl),
        "source": source,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    }
    if prices:
        record["prices"] = [float(p) for p in prices]
    if entry_date is not None:
        record["date"] = entry_date.isoformat() if hasattr(entry_date, "isoformat") else str(entry_date)
    entries.append(record)
    save_porta(entries)


def _read_pagbank(file) -> pd.DataFrame:
    file.seek(0)
    raw = pd.read_csv(file, skiprows=8)
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw[raw["Tipo"].astype(str).str.strip() == "Vendas"].copy()
    raw["Entradas"] = pd.to_numeric(raw["Entradas"], errors="coerce")
    raw = raw[raw["Entradas"].notna() & (raw["Entradas"] > 0)]
    raw["Data"] = pd.to_datetime(raw["Data"], format="%d/%m/%Y", errors="coerce").dt.date
    raw = raw[raw["Data"].notna()]
    return raw


def parse_pagbank_csv(file) -> tuple[int, float, list[float]]:
    sales = _read_pagbank(file)
    prices = sales["Entradas"].astype(float).tolist()
    return len(prices), float(sum(prices)), prices


def parse_pagbank_csv_by_date(file) -> dict:
    sales = _read_pagbank(file)
    out: dict = {}
    for d, group in sales.groupby("Data"):
        prices = group["Entradas"].astype(float).tolist()
        out[d] = (len(prices), float(sum(prices)), prices)
    return out


def porta_totals_by_event(entries: list[dict]) -> pd.DataFrame:
    if not entries:
        return pd.DataFrame(columns=["event_name", "porta_tickets", "porta_revenue"])
    df = pd.DataFrame(entries)
    return (
        df.groupby("event_name", as_index=False)
        .agg(porta_tickets=("tickets", "sum"), porta_revenue=("revenue_brl", "sum"))
    )


def expand_porta_to_rows(entries: list[dict], shotgun_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Expand Porta aggregate entries into one row per ticket, matching shotgun_df schema."""
    if not entries:
        return pd.DataFrame()

    # Map event_name → event_start_time (date) for fallback order_date on manual entries
    event_dates: dict = {}
    if shotgun_df is not None and "event_name" in shotgun_df and "event_start_time" in shotgun_df:
        for ev, grp in shotgun_df.groupby("event_name"):
            d = grp["event_start_time"].dropna()
            if not d.empty:
                event_dates[ev] = d.min().date()

    rows: list[dict] = []
    for entry in entries:
        evt    = entry["event_name"]
        n      = int(entry.get("tickets", 0))
        rev    = float(entry.get("revenue_brl", 0.0))
        prices = entry.get("prices")
        # Resolve order_date: explicit "date" → event_start_time → today
        if entry.get("date"):
            try:
                order_d = datetime.fromisoformat(entry["date"]).date()
            except Exception:
                order_d = None
        else:
            order_d = None
        if order_d is None:
            order_d = event_dates.get(evt) or datetime.now().date()

        if prices and len(prices) == n:
            ticket_prices = [float(p) for p in prices]
        else:
            avg = (rev / n) if n else 0.0
            ticket_prices = [avg] * n

        for p in ticket_prices:
            rows.append({
                "event_name":     evt,
                "deal_price_brl": p,
                "ticket_status":  "valid",
                "source":         "Porta",
                "order_date":     order_d,
            })

    return pd.DataFrame(rows)


# ── Supabase: inicialização, OAuth callback, restauração de sessão ─────────────
# (_SB_INIT — roda a cada rerun antes do sidebar)

def _sb_is_configured() -> bool:
    if not _SB_AVAIL:
        return False
    try:
        return bool(st.secrets.get("supabase", {}).get("url"))
    except Exception:
        return False


def _render_login_page() -> None:
    """Página standalone de login — Google OAuth ou visitante."""
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown(
            "<h1 style='text-align:center'>🎟️ Clubber Analytics</h1>",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<p style='text-align:center;color:#9a9ab0'>Painel de análise de eventos.</p>",
            unsafe_allow_html=True,
        )
        st.divider()

        # ── Google OAuth (PKCE flow) ──────────────────────────────────────────
        try:
            _base_redir = st.secrets.get("supabase", {}).get("redirect_url", "http://localhost:8501").rstrip("/")

            # 1. Pede URL ao supabase-py — ele gera o code_challenge internamente
            _oa = _sb.auth.sign_in_with_oauth({
                "provider": "google",
                "options": {"redirect_to": f"{_base_redir}/"},
            })

            # 2. Extrai o verifier que o supabase-py guardou no storage interno
            #    (chave padrão: "{storage_key}-code-verifier")
            _ver = None
            try:
                _sk  = _sb.auth._storage_key
                _ver = _sb.auth._storage.get_item(f"{_sk}-code-verifier")
            except Exception:
                pass

            # 3. Reconstrói a URL embutindo o verifier no redirect_to para que
            #    volte como ?sb_ver= após o OAuth — sem depender de session_state.
            if _ver:
                _parsed = urlparse(_oa.url)
                _qp = dict(parse_qsl(_parsed.query))
                _qp["redirect_to"] = f"{_base_redir}/?sb_ver={_ver}"
                _oauth_url = _parsed._replace(query=urlencode(_qp)).geturl()
            else:
                _oauth_url = _oa.url   # fallback sem PKCE

            st.link_button("🔑 Entrar com Google", url=_oauth_url, use_container_width=True, type="primary")
            st.caption("Seus dados ficam salvos e sincronizados entre sessões.")
        except Exception as e:
            st.error(f"Não foi possível iniciar autenticação Google: {e}")

        st.divider()

        # ── Visitante ─────────────────────────────────────────────────────────
        if st.button("👤 Continuar como visitante", use_container_width=True, key="li_guest"):
            st.session_state["sb_guest"] = True
            st.rerun()
        st.caption("Modo visitante: dados não são salvos entre sessões.")


if _sb_is_configured():
    # Cria client sem sessão
    _sb = _sb_create_client(st.secrets["supabase"]["url"], st.secrets["supabase"]["key"])
    _SB_MODE = True

    # ── PKCE callback: ?code= + ?sb_ver= chegam juntos na query string ────────
    # (o verifier foi embutido no redirect_to URL, então volta com o code)
    _pkce_code = st.query_params.get("code")
    _pkce_ver  = st.query_params.get("sb_ver")
    if _pkce_code and _pkce_ver:
        try:
            _pkce_resp = _sb.auth.exchange_code_for_session(
                {"auth_code": _pkce_code, "code_verifier": _pkce_ver}
            )
            st.session_state["sb_access_token"]  = _pkce_resp.session.access_token
            st.session_state["sb_refresh_token"] = _pkce_resp.session.refresh_token
            st.query_params.clear()
            st.rerun()
        except Exception as _pkce_err:
            st.error(f"Erro ao completar login com Google: {_pkce_err}")
            st.query_params.clear()
            st.stop()

    # Erros OAuth retornados pelo Supabase/Google (?error=...)
    _oauth_err = st.query_params.get("error")
    if _oauth_err:
        st.error(
            f"Erro no login com Google: "
            f"{st.query_params.get('error_description', _oauth_err)}"
        )
        st.query_params.clear()

    # Restaura sessão do session_state
    _at = st.session_state.get("sb_access_token")
    _rt = st.session_state.get("sb_refresh_token", "")
    if _at:
        try:
            _sb.auth.set_session(_at, _rt)
            _sb_user = _sb.auth.get_user().user
        except Exception:
            st.session_state.pop("sb_access_token", None)
            st.session_state.pop("sb_refresh_token", None)
            _sb_user = None

    # ── Auth gate ──────────────────────────────────────────────────────────────
    if _sb_user is None and not st.session_state.get("sb_guest"):
        _render_login_page()
        st.stop()

    # ── Migração única: porta_entries.json → DB ────────────────────────────────
    _migrated_flag = PORTA_PATH.with_name("porta_entries.imported.json")
    if PORTA_PATH.is_file() and not _migrated_flag.is_file():
        try:
            _local_entries = json.loads(PORTA_PATH.read_text(encoding="utf-8"))
            if _local_entries:
                _chk = _sb.table("porta_entries").select("id").eq("user_id", _sb_user.id).limit(1).execute()
                if not _chk.data:
                    for _e in _local_entries:
                        _row: dict = {
                            "user_id":     _sb_user.id,
                            "event_name":  _e["event_name"],
                            "tickets":     int(_e["tickets"]),
                            "revenue_brl": float(_e["revenue_brl"]),
                            "source":      _e.get("source", "manual"),
                            "added_at":    _e.get("added_at", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
                        }
                        if _e.get("prices"):
                            _row["prices"] = _e["prices"]
                        if _e.get("date"):
                            _row["entry_date"] = _e["date"]
                        _sb.table("porta_entries").insert(_row).execute()
                    PORTA_PATH.rename(_migrated_flag)
                    st.toast(f"Importadas {len(_local_entries)} entradas Porta para sua conta. ✓")
        except Exception as _me:
            st.warning(f"Migração de dados locais falhou (não crítico): {_me}")

    # ── Carrega ingressos Shotgun do banco (se df ainda não estiver em memória) ─
    if "df" not in st.session_state:
        try:
            _db_resp = _sb.table("shotgun_tickets").select("raw").eq("user_id", _sb_user.id).execute()
            if _db_resp.data:
                _db_recs = [row["raw"] for row in _db_resp.data]
                st.session_state["df"] = process(pd.DataFrame(_db_recs))
                st.session_state["source_label"] = f"Base de dados — {len(_db_recs):,} ingressos"
        except Exception as _dbe:
            pass  # silencioso; usuário pode buscar via API


# ── Sidebar esquerda — fonte de dados ─────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎟️ Clubber Analytics")
    st.caption("feito por [ponkan](https://linktr.ee/ponkan_)")

    if _SB_MODE:
        if _sb_user:
            st.caption(f"👤 {_sb_user.email}")
        else:
            st.caption("👤 Visitante")
        if st.button("Sair", use_container_width=True, key="sb_signout"):
            try:
                if _sb_user:
                    _sb.auth.sign_out()
            except Exception:
                pass
            for _k in ["sb_access_token", "sb_refresh_token", "df", "source_label",
                        "_consolidated_processed", "sb_guest"]:
                st.session_state.pop(_k, None)
            for _k in [k for k in st.session_state if k.startswith("evt_")]:
                del st.session_state[_k]
            st.rerun()

    if "df" in st.session_state:
        if st.button("🚪 Limpar dados", use_container_width=True):
            del st.session_state["df"]
            st.session_state.pop("source_label", None)
            if _SB_MODE and _sb_user:
                try:
                    _sb.table("shotgun_tickets").delete().eq("user_id", _sb_user.id).execute()
                except Exception:
                    pass
            st.rerun()

    st.divider()

    st.markdown("### Buscar via API")
    st.caption("[Como descobrir os seus dados de API](https://support-pro.shotgun.live/hc/en-us/articles/33561354477970-Find-your-Organizer-id-and-API-token#h_01KJ7K6DYV1FWN0AD6NRV5W1XE)")
    organizer_id = st.text_input("ID do Organizador", placeholder="123456")
    api_token    = st.text_input("Token de API", type="password", placeholder="eyJhbGci...")

    if st.button("🔄 Buscar Dados", use_container_width=True, type="primary"):
        if not api_token or not organizer_id:
            st.error("Preencha o Token de API e o ID do Organizador.")
        else:
            prog = st.empty()
            try:
                with st.spinner("Conectando à API do Shotgun..."):
                    raw = fetch_tickets_from_api(api_token, organizer_id, progress=prog)
                prog.empty()

                if _SB_MODE and _sb_user and not raw.empty:
                    try:
                        with st.spinner("Verificando ingressos já salvos..."):
                            # ── Step 1: fetch existing ticket_ids from DB (IDs only, fast) ──
                            _exist_resp = _sb.table("shotgun_tickets") \
                                .select("ticket_id") \
                                .eq("user_id", _sb_user.id) \
                                .execute()
                            _existing_ids = {r["ticket_id"] for r in (_exist_resp.data or [])}

                        # ── Step 2: split API result into new vs already-stored ──────────
                        _all_recs = raw.to_dict("records")
                        _new_recs = [
                            r for i, r in enumerate(_all_recs)
                            if str(r.get("ticket_id", i)) not in _existing_ids
                        ]
                        _n_existing = len(_existing_ids)
                        _n_new      = len(_new_recs)

                        # ── Step 3: upsert ONLY genuinely new tickets ────────────────────
                        if _new_recs:
                            with st.spinner(f"Salvando {_n_new:,} novos ingressos..."):
                                _upsert_rows = [
                                    {
                                        "user_id":   _sb_user.id,
                                        "ticket_id": str(r.get("ticket_id", i)),
                                        "raw":       _sanitize_record(r),
                                    }
                                    for i, r in enumerate(_new_recs)
                                ]
                                _sb.table("shotgun_tickets") \
                                    .upsert(_upsert_rows, on_conflict="user_id,ticket_id") \
                                    .execute()

                        # ── Step 4: rebuild df = existing DB rows + new API rows ─────────
                        # Reload full dataset from DB so df is always the single source of truth.
                        with st.spinner("Carregando dados atualizados..."):
                            _full_resp = _sb.table("shotgun_tickets") \
                                .select("raw") \
                                .eq("user_id", _sb_user.id) \
                                .execute()
                            _full_recs = [row["raw"] for row in (_full_resp.data or [])]
                            st.session_state["df"] = process(pd.DataFrame(_full_recs))

                        total_in_db = _n_existing + _n_new
                        st.session_state["source_label"] = f"Base de dados — {total_in_db:,} ingressos"

                        if _n_new:
                            st.success(
                                f"✓ {_n_new:,} novos ingressos adicionados. "
                                f"{_n_existing:,} já estavam salvos. "
                                f"Total: {total_in_db:,}."
                            )
                        else:
                            st.info(
                                f"Nenhum ingresso novo encontrado. "
                                f"{_n_existing:,} ingressos já estavam salvos."
                            )

                    except Exception as _ue:
                        # Fallback: at least show API data in session, even if DB failed
                        st.session_state["df"] = process(raw)
                        st.session_state["source_label"] = f"API ao vivo — {len(raw):,} ingressos"
                        st.warning(f"Dados carregados, mas não foi possível sincronizar com o banco: {_ue}")

                else:
                    # Guest / no-DB mode: just use the API response directly
                    st.session_state["df"] = process(raw)
                    st.session_state["source_label"] = f"API ao vivo — {len(raw):,} ingressos"
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

    with st.expander("🚪 Adicionar Ingressos Porta"):
        if "df" not in st.session_state:
            st.caption("Carregue os dados do Shotgun primeiro.")
        else:
            _events = sorted(st.session_state["df"]["event_name"].dropna().unique().tolist())
            mode = st.radio(
                "Modo de entrada", ["Manual", "CSV PagBank"],
                horizontal=True, key="porta_mode",
            )

            if mode == "Manual":
                evt = st.selectbox(
                    "Evento", _events, key="porta_evt_manual",
                    index=None, placeholder="Selecione...",
                )
                n = st.number_input("Número de ingressos", min_value=1, step=1, key="porta_n")
                v = st.number_input(
                    "Valor total (R$)", min_value=0.0, step=0.01,
                    format="%.2f", key="porta_v",
                )
                if st.button(
                    "Adicionar", use_container_width=True, type="primary",
                    disabled=evt is None, key="porta_add_manual",
                ):
                    append_porta_entry(evt, int(n), float(v), "manual")
                    st.success(f"Adicionado: {int(n)} ingressos / R${float(v):,.2f} para {evt}")
                    st.rerun()

            else:
                scope = st.radio(
                    "Escopo", ["Evento Único", "Vários Eventos"],
                    horizontal=True, key="porta_scope",
                )
                up = st.file_uploader(
                    "Extrato PagBank (.csv)", type="csv", key="porta_csv",
                )
                if up is not None:
                    if scope == "Evento Único":
                        evt = st.selectbox(
                            "Evento", _events, key="porta_evt_csv",
                            index=None, placeholder="Selecione...",
                        )
                        try:
                            n_csv, v_csv, prices_csv = parse_pagbank_csv(up)
                            st.caption(f"Detectado: {n_csv} vendas / R${v_csv:,.2f}")
                            if st.button(
                                "Adicionar", use_container_width=True, type="primary",
                                disabled=evt is None, key="porta_add_csv_single",
                            ):
                                append_porta_entry(
                                    evt, n_csv, v_csv, "pagbank_csv",
                                    prices=prices_csv,
                                )
                                st.success(f"Adicionado: {n_csv} ingressos / R${v_csv:,.2f} para {evt}")
                                st.rerun()
                        except Exception as e:
                            st.error(f"Erro ao ler CSV PagBank: {e}")
                    else:
                        try:
                            by_date = parse_pagbank_csv_by_date(up)
                            if not by_date:
                                st.warning("Nenhuma venda detectada no CSV.")
                            else:
                                st.caption(
                                    f"Detectadas {len(by_date)} datas com vendas. "
                                    "Atribua um evento a cada data:"
                                )
                                _IGNORE = "(ignorar)"
                                assignments: dict = {}
                                for d, (n_d, v_d, _p) in sorted(by_date.items()):
                                    assignments[d] = st.selectbox(
                                        f"{d.strftime('%d/%m/%Y')} — {n_d} vendas / R${v_d:,.2f}",
                                        [_IGNORE] + _events,
                                        key=f"porta_date_{d.isoformat()}",
                                    )
                                if st.button(
                                    "Adicionar", use_container_width=True, type="primary",
                                    key="porta_add_csv_multi",
                                ):
                                    added = 0
                                    for d, evt_pick in assignments.items():
                                        if evt_pick != _IGNORE:
                                            n_d, v_d, prices_d = by_date[d]
                                            append_porta_entry(
                                                evt_pick, n_d, v_d, "pagbank_csv",
                                                prices=prices_d, entry_date=d,
                                            )
                                            added += 1
                                    if added:
                                        st.success(f"{added} entradas adicionadas.")
                                        st.rerun()
                                    else:
                                        st.info("Nenhuma data foi atribuída a um evento.")
                        except Exception as e:
                            st.error(f"Erro ao ler CSV PagBank: {e}")

        _porta_existing = load_porta()
        if _porta_existing:
            st.caption(f"{len(_porta_existing)} entradas Porta salvas.")
            if st.button("🗑️ Limpar Porta", use_container_width=True, key="porta_clear"):
                save_porta([])
                st.rerun()

    st.divider()
    with st.expander("📥 Carregar dados consolidados"):
        st.caption(
            "Restaura um CSV exportado em **Baixar dados consolidados** "
            "(Shotgun + Porta unificados)."
        )
        consolidated = st.file_uploader(
            "CSV consolidado", type="csv", key="consolidated_upload",
            label_visibility="collapsed",
        )
        if consolidated is not None:
            _file_token = f"{consolidated.name}:{consolidated.size}"
            if st.session_state.get("_consolidated_processed") != _file_token:
                try:
                    full_df = pd.read_csv(consolidated)
                    if "source" not in full_df.columns:
                        st.error("Este CSV não parece ser consolidado (faltam a coluna 'source').")
                    else:
                        sg_part    = full_df[full_df["source"] == "Shotgun"].drop(columns=["source"]).copy()
                        porta_part = full_df[full_df["source"] == "Porta"].copy()

                        new_entries: list[dict] = []
                        for evt, grp in porta_part.groupby("event_name"):
                            prices = [float(p) for p in grp["deal_price_brl"].tolist()]
                            new_entries.append({
                                "event_name":  evt,
                                "tickets":     len(prices),
                                "revenue_brl": float(sum(prices)),
                                "prices":      prices,
                                "source":      "consolidated_upload",
                                "added_at":    datetime.now().isoformat(timespec="seconds"),
                            })

                        st.session_state["df"] = process(sg_part)
                        st.session_state["source_label"] = (
                            f"Consolidado — {consolidated.name} "
                            f"({len(sg_part):,} Shotgun + {len(porta_part):,} Porta)"
                        )
                        save_porta(new_entries)
                        st.session_state["_consolidated_processed"] = _file_token
                        st.success(
                            f"Restaurado: {len(sg_part):,} ingressos Shotgun e "
                            f"{len(new_entries)} entradas Porta."
                        )
                        st.rerun()
                except Exception as e:
                    st.error(f"Erro ao ler o CSV consolidado: {e}")
            else:
                st.caption(f"✓ Já carregado: {consolidated.name}")

    st.divider()
    st.caption(
        "Gostou? Sugira novas funcionalidades "
        "[aqui](https://docs.google.com/spreadsheets/d/1zu40CkqlMhMVPAPITPiAqoNPBP_eSGptYlPYvtAJPHs/edit?usp=sharing)"
    )
    st.caption("Gostou muito? PIX para gustavobaida@gmail.com")


# ── Tela de boas-vindas ────────────────────────────────────────────────────────
if "df" not in st.session_state:
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown(
            "<h1 style='text-align:center'>🎟️ Clubber Analytics</h1>",
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

- 📈 **Vendas** — evolução diária, dias de venda e comportamento de compra antes do evento
- 💰 **Receita** — por categoria de ingresso, método de pagamento e ao longo do tempo
- 📣 **Marketing** — quais canais (Instagram, Direct, Shotgun App...) geraram mais vendas
- 👥 **Público** — gênero, faixa etária, cidades e taxa de opt-in na newsletter
- 🔍 **Operações** — taxa de comparecimento e cancelamentos por evento

---

**Como começar:**

1. Acesse o **Shotgun Smartboard** → Configurações → Integrações → APIs do Shotgun
2. Copie seu **Token de API** e **ID do Organizador**
3. Cole os dados na barra lateral e clique em **🔄 Buscar Dados**

Ou envie um arquivo `.csv` exportado anteriormente diretamente pela barra lateral.
""")
    st.stop()


# ── Layout principal: conteúdo + filtros à direita ────────────────────────────
df_shotgun = st.session_state["df"].copy()
df_shotgun["source"] = "Shotgun"

_porta_entries = load_porta()
df_porta_rows  = expand_porta_to_rows(_porta_entries, df_shotgun)
has_porta_data = not df_porta_rows.empty

if has_porta_data:
    df = pd.concat([df_shotgun, df_porta_rows], ignore_index=True)
else:
    df = df_shotgun

col_main, col_filters = st.columns([5, 1])

# ── Filtros (coluna direita) ───────────────────────────────────────────────────
with col_filters:
    st.markdown("### Filtros")

    # Checklist de eventos (a partir do Shotgun — Porta usa os mesmos nomes)
    st.markdown("**Eventos**")
    events_info = (
        df_shotgun[["event_id", "event_name"]].drop_duplicates()
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

    # Canal (Shotgun / Porta) — só aparece quando há dados de Porta
    if has_porta_data:
        st.markdown("**Canal**")
        sel_channels = st.multiselect(
            "Canal", ["Shotgun", "Porta"],
            default=["Shotgun", "Porta"],
            label_visibility="collapsed",
        )
        if not sel_channels:
            sel_channels = ["Shotgun", "Porta"]
    else:
        sel_channels = ["Shotgun"]

    # Período de compra
    st.markdown("**Período de compra**")
    valid_dates = df["order_date"].dropna() if "order_date" in df.columns else pd.Series([], dtype=object)
    if not valid_dates.empty:
        min_d = valid_dates.min()
        max_d = valid_dates.max()
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
mask = (
    df["event_name"].isin(sel_events)
    & df["ticket_status"].isin(sel_statuses)
    & df["source"].isin(sel_channels)
)
if date_range and "order_date" in df.columns:
    mask &= df["order_date"].between(date_range[0], date_range[1])
dff    = df[mask].copy()
df_sel = df[df["event_name"].isin(sel_events)].copy()
# Subconjunto apenas Shotgun — usado para KPIs/abas que dependem de campos
# que Porta não possui (utm, contact, scan, cancelamento).
dff_shotgun = dff[dff["source"] == "Shotgun"].copy()

# Botão de download (anexado à coluna de filtros, abaixo de tudo)
with col_filters:
    st.divider()
    st.download_button(
        "📥 Baixar dados consolidados",
        data=dff.to_csv(index=False).encode("utf-8"),
        file_name=f"shotgun_porta_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        use_container_width=True,
    )


# ── Conteúdo principal (coluna esquerda) ──────────────────────────────────────
with col_main:

    if dff.empty:
        st.warning("Nenhum ingresso encontrado para os filtros selecionados.")
        st.stop()

    # ── KPIs ──────────────────────────────────────────────────────────────────
    # Cross-channel: somam Shotgun + Porta (quando ambos selecionados)
    total_tickets    = len(dff)
    unique_attendees = dff["contact_id"].nunique() if "contact_id" in dff else 0
    total_revenue    = dff["deal_price_brl"].sum() if "deal_price_brl" in dff else 0

    # Shotgun-only: campos que Porta não tem (scan, cancel, newsletter)
    df_sel_sg = df_sel[df_sel["source"] == "Shotgun"]
    total_canceled  = (df_sel_sg["ticket_status"] == "canceled").sum()
    cancel_rate     = total_canceled / len(df_sel_sg) * 100 if len(df_sel_sg) else 0
    sg_total        = len(dff_shotgun)
    scanned         = dff_shotgun["ticket_scanned_at"].notna().sum() if "ticket_scanned_at" in dff_shotgun else 0
    scan_rate       = scanned / sg_total * 100 if sg_total else 0
    newsletter_rate = (
        dff_shotgun["contact_newsletter_optin"].sum() / dff_shotgun["contact_newsletter_optin"].notna().sum() * 100
        if "contact_newsletter_optin" in dff_shotgun and dff_shotgun["contact_newsletter_optin"].notna().sum() > 0
        else 0
    )

    st.markdown("## Visão Geral")
    c1, c2, c3 = st.columns(3)
    c4, c5, c6 = st.columns(3)
    c1.metric("Ingressos Vendidos",     f"{total_tickets:,}")
    c2.metric("Participantes Únicos",   f"{unique_attendees:,}")
    c3.metric("Receita Total",          f"R${total_revenue:,.2f}")
    c4.metric("Taxa de Comparecimento", f"{scan_rate:.1f}%")
    c5.metric("Taxa de Cancelamento",   f"{cancel_rate:.1f}%")
    c6.metric("Opt-in Newsletter",      f"{newsletter_rate:.1f}%")

    st.divider()

    # ── Abas ──────────────────────────────────────────────────────────────────
    porta_df = porta_totals_by_event(_porta_entries)
    porta_df = porta_df[porta_df["event_name"].isin(sel_events)].copy()

    shotgun_in = "Shotgun" in sel_channels
    porta_in   = "Porta" in sel_channels
    # Aba Porta (comparação) só faz sentido com ambos os canais selecionados
    show_porta = porta_in and shotgun_in and not porta_df.empty

    _tab_labels = ["📊 Comparar", "📈 Vendas", "💰 Receita", "📣 Marketing", "👥 Público", "🔍 Operações"]
    if show_porta:
        _tab_labels.append("🚪 Porta")
    _tabs = st.tabs(_tab_labels)
    tab_compare, tab_sales, tab_revenue, tab_marketing, tab_audience, tab_ops = _tabs[:6]
    tab_porta = _tabs[6] if show_porta else None

    # Alias para restaurar `dff` após abas que precisam usar somente Shotgun
    _dff_all = dff

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 1 — VENDAS
    # ══════════════════════════════════════════════════════════════════════════
    with tab_sales:
        st.subheader("Vendas de Ingressos ao Longo do Tempo")

        if "order_date" in dff.columns:
            col_l, col_r = st.columns(2)
            multi = len(sel_events) > 1

            daily = (
                dff.groupby(["order_date", "event_name"])
                .size().reset_index(name="ingressos")
            )
            daily["order_date"] = pd.to_datetime(daily["order_date"])
            daily = daily.sort_values("order_date")

            if multi:
                first_sale = daily.groupby("event_name")["order_date"].min().rename("first_sale")
                daily = daily.merge(first_sale, on="event_name")
                daily["dias_desde_inicio"] = (daily["order_date"] - daily["first_sale"]).dt.days

                fig = px.bar(
                    daily, x="dias_desde_inicio", y="ingressos", color="event_name",
                    labels={"dias_desde_inicio": "Dias desde a 1ª Venda", "ingressos": "Ingressos Vendidos", "event_name": "Evento"},
                    title="Vendas Diárias por Evento (desde a 1ª venda)",
                    color_discrete_sequence=COLORS,
                )
                fig.update_layout(legend=dict(orientation="h", y=-0.2), bargap=0.15)
                col_l.plotly_chart(fig, use_container_width=True)

                cum = daily.copy()
                cum["acumulado"] = cum.groupby("event_name")["ingressos"].cumsum()
                fig2 = px.line(
                    cum, x="dias_desde_inicio", y="acumulado", color="event_name",
                    labels={"dias_desde_inicio": "Dias desde a 1ª Venda", "acumulado": "Ingressos Acumulados", "event_name": "Evento"},
                    title="Vendas Acumuladas de Ingressos (desde a 1ª venda)",
                    color_discrete_sequence=COLORS, markers=True,
                )
            else:
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

        if "order_dow" in dff.columns:
            dow = dff.groupby("order_dow").size().reset_index(name="ingressos")
            dow["order_dow"] = pd.Categorical(dow["order_dow"], categories=_DOW_ORDER, ordered=True)
            dow = dow.sort_values("order_dow")
            dow["dia"] = dow["order_dow"].map(_DOW_MAP)
            fig3 = px.bar(
                dow, x="dia", y="ingressos",
                labels={"dia": "Dia da Semana", "ingressos": "Ingressos Vendidos"},
                title="Vendas por Dia da Semana",
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
            fig4.update_xaxes(autorange="reversed")
            col_r2.plotly_chart(fig4, use_container_width=True)

        if "order_hour" in dff.columns and "order_dow" in dff.columns:
            heat = dff.groupby(["order_dow", "order_hour"]).size().reset_index(name="ingressos")
            heat["order_dow"] = pd.Categorical(heat["order_dow"], categories=_DOW_ORDER, ordered=True)
            heat = heat.sort_values("order_dow")
            pivot_heat = heat.pivot(index="order_dow", columns="order_hour", values="ingressos").fillna(0)
            pivot_heat.index = [_DOW_MAP[d] for d in pivot_heat.index]
            fig_heat = px.imshow(
                pivot_heat,
                labels=dict(x="Hora do Dia", y="Dia da Semana", color="Ingressos"),
                title="Mapa de Calor: Dia da Semana × Hora",
                color_continuous_scale="Blues", text_auto=True,
                aspect="auto",
            )
            st.plotly_chart(fig_heat, use_container_width=True)

        st.subheader("Resumo por Evento")

        event_dates = df_sel.groupby("event_name").agg(
            primeira_venda=("order_date", "min"),
            data_evento=("event_start_time", "min"),
        ).reset_index()
        if "event_start_time" in df_sel.columns:
            event_dates["dias_de_vendas"] = (
                event_dates["data_evento"].dt.tz_convert(None).dt.normalize()
                - pd.to_datetime(event_dates["primeira_venda"])
            ).dt.days
        else:
            event_dates["dias_de_vendas"] = None

        summary = (
            df_sel.groupby("event_name").agg(
                total=("ticket_id", "count"),
                validos=("ticket_status", lambda x: int((x == "valid").sum())),
                cancelados=("ticket_status", lambda x: int((x == "canceled").sum())),
                lidos=("ticket_scanned_at", "count"),
                receita=("deal_price_brl", "sum"),
            ).reset_index()
        )
        summary["lidos"] = summary["lidos"].astype(int)
        summary = summary.merge(event_dates[["event_name", "dias_de_vendas"]], on="event_name", how="left")
        summary["taxa_presenca"]     = (summary["lidos"] / summary["validos"] * 100).round(1).astype(str) + "%"
        summary["taxa_cancelamento"] = (summary["cancelados"] / summary["total"] * 100).round(1).astype(str) + "%"
        summary["receita"]           = summary["receita"].map("R${:,.2f}".format)
        summary = summary[["event_name", "total", "validos", "cancelados", "lidos", "receita", "dias_de_vendas", "taxa_presenca", "taxa_cancelamento"]]
        summary.columns = ["Evento", "Total", "Válidos", "Cancelados", "Comparecimento", "Receita", "Dias de Venda", "Taxa de Comparecimento", "Taxa de Cancelamento"]
        st.dataframe(summary, use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 2 — RECEITA
    # ══════════════════════════════════════════════════════════════════════════
    with tab_revenue:
        if "deal_price_brl" not in dff.columns:
            st.info("Dados de preço não disponíveis.")
        else:
            col_l, col_r = st.columns(2)

            if len(sel_events) > 1:
                vol_ev = (
                    dff.groupby("event_name")
                    .agg(receita=("deal_price_brl", "sum"))
                    .reset_index().sort_values("receita", ascending=False)
                )
                fig2 = px.bar(
                    vol_ev, x="event_name", y="receita",
                    labels={"event_name": "Evento", "receita": "Receita (BRL)"},
                    title="Receita por Evento",
                    color="event_name", color_discrete_sequence=COLORS,
                    text="receita",
                )
                fig2.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
                fig2.update_layout(showlegend=False, xaxis_tickangle=-20)
                col_l.plotly_chart(fig2, use_container_width=True)

            dff["deal_title_display"] = dff["deal_title"].where(dff["deal_price_brl"] > 0, "Gratuito")
            rev_tier = (
                dff.groupby("deal_title_display")
                .agg(ingressos=("ticket_id", "count"), receita=("deal_price_brl", "sum"))
                .reset_index().sort_values("receita", ascending=False)
            )
            fig = px.bar(
                rev_tier, x="deal_title_display", y="receita",
                labels={"deal_title_display": "Categoria de Ingresso", "receita": "Receita (BRL)"},
                title="Receita por Categoria de Ingresso",
                color="deal_title_display", color_discrete_sequence=COLORS, text="receita",
            )
            fig.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
            fig.update_layout(showlegend=False, xaxis_tickangle=-20)
            col_r.plotly_chart(fig, use_container_width=True)

            free_tickets = dff[dff["deal_price_brl"] == 0]
            if not free_tickets.empty:
                free_counts = free_tickets.groupby("deal_title").size().reset_index(name="ingressos")
                fig_free = px.bar(
                    free_counts.sort_values("ingressos", ascending=False),
                    x="deal_title", y="ingressos",
                    labels={"deal_title": "Tipo de Ingresso Gratuito", "ingressos": "Ingressos"},
                    title="Tipos de Ingresso Gratuito",
                    color="deal_title", color_discrete_sequence=COLORS, text="ingressos",
                )
                fig_free.update_traces(textposition="outside")
                fig_free.update_layout(showlegend=False, xaxis_tickangle=-20)
                st.plotly_chart(fig_free, use_container_width=True)

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

            if "order_date" in dff.columns and len(sel_events) == 1:
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
        dff = dff_shotgun
        if dff.empty:
            st.info("Sem dados Shotgun para os filtros selecionados (Canal Shotgun pode estar desligado).")
        col_l, col_r = st.columns(2)

        if "utm_source" in dff.columns:
            src = dff["utm_source"].value_counts().reset_index()
            src.columns = ["canal", "ingressos"]
            fig = px.bar(
                src, x="ingressos", y="canal", orientation="h",
                labels={"canal": "Canal de Aquisição", "ingressos": "Ingressos"},
                title="Ingressos por Canal de Aquisição",
                color="canal", color_discrete_sequence=COLORS, text="ingressos",
            )
            fig.update_traces(textposition="outside")
            fig.update_layout(showlegend=False, yaxis={"categoryorder": "total ascending"})
            col_l.plotly_chart(fig, use_container_width=True)

        if "utm_source" in dff.columns and "utm_medium" in dff.columns:
            src_med = (
                dff.groupby(["utm_source", "utm_medium"])
                .size().reset_index(name="ingressos")
            )
            fig_stk = px.bar(
                src_med, x="utm_source", y="ingressos", color="utm_medium",
                barmode="stack",
                labels={"utm_source": "Canal de Aquisição", "ingressos": "Ingressos", "utm_medium": "Meio"},
                title="Canal × Meio (App vs Web)",
                color_discrete_sequence=COLORS,
            )
            fig_stk.update_layout(
                xaxis={"categoryorder": "total descending"},
                legend=dict(orientation="h", y=-0.25),
            )
            col_r.plotly_chart(fig_stk, use_container_width=True)

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
        dff = dff_shotgun
        if dff.empty:
            st.info("Sem dados Shotgun para os filtros selecionados (Canal Shotgun pode estar desligado).")
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

        if len(sel_events) > 1:
            st.subheader("Top 10 — Participantes Mais Fiéis")
            name_col = next((c for c in ["contact_name", "contact_email"] if c in dff.columns), None)
            top = (
                dff.groupby("contact_id")["event_id"].nunique()
                .reset_index(name="eventos")
                .sort_values("eventos", ascending=False)
                .head(10)
            )
            if name_col:
                top = top.merge(
                    dff[["contact_id", name_col]].drop_duplicates("contact_id"),
                    on="contact_id", how="left",
                )
                display_col = name_col
            else:
                display_col = "contact_id"
            fig_top = px.bar(
                top, x="eventos", y=display_col, orientation="h",
                labels={display_col: "Participante", "eventos": "Eventos Frequentados"},
                title="Top 10 Participantes por Eventos Frequentados",
                color_discrete_sequence=[COLORS[3]], text="eventos",
            )
            fig_top.update_traces(textposition="outside")
            fig_top.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig_top, use_container_width=True)

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
        dff = dff_shotgun
        if dff.empty:
            st.info("Sem dados Shotgun para os filtros selecionados (Canal Shotgun pode estar desligado).")
        col_l, col_r = st.columns(2)

        dff["_ticket_type"] = (
            dff["deal_price_brl"].apply(lambda x: "Gratuito" if x == 0 else "Pago")
            if "deal_price_brl" in dff.columns
            else "Pago"
        )

        scan_ev = dff.groupby(["event_name", "_ticket_type"]).agg(
            total=("ticket_id", "count"),
            presentes=("ticket_scanned_at", "count"),
        ).reset_index()
        scan_ev["total"]     = scan_ev["total"].astype(int)
        scan_ev["presentes"] = scan_ev["presentes"].astype(int)
        scan_ev["ausentes"]  = scan_ev["total"] - scan_ev["presentes"]

        events_order = (
            scan_ev.groupby("event_name")["total"].sum()
            .sort_values(ascending=False).index.tolist()
        )

        _SCAN_COLORS = {
            ("Pago",     "Presente"): "#00CC96",
            ("Pago",     "Ausente"):  "#EF553B",
            ("Gratuito", "Presente"): "#72EFDD",
            ("Gratuito", "Ausente"):  "#FF9F7F",
        }

        fig = go.Figure()
        for ttype in ["Pago", "Gratuito"]:
            sub = scan_ev[scan_ev["_ticket_type"] == ttype]
            if sub.empty:
                continue
            for status, col_name in [("Presente", "presentes"), ("Ausente", "ausentes")]:
                fig.add_trace(go.Bar(
                    name=f"{ttype} – {status}",
                    x=sub["event_name"],
                    y=sub[col_name],
                    marker_color=_SCAN_COLORS[(ttype, status)],
                    text=sub[col_name],
                    textposition="inside",
                    offsetgroup=ttype,
                ))
        fig.update_layout(
            barmode="stack", title="Comparecimento por Evento",
            xaxis={"categoryorder": "array", "categoryarray": events_order, "tickangle": -20},
            legend=dict(orientation="h", y=-0.3),
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
        fig3 = px.bar(
            status_counts, x="status", y="quantidade",
            labels={"status": "Status", "quantidade": "Ingressos"},
            title="Distribuição por Status de Ingresso",
            color="status", color_discrete_sequence=COLORS, text="quantidade",
        )
        fig3.update_traces(textposition="outside")
        fig3.update_layout(showlegend=False)
        col_l.plotly_chart(fig3, use_container_width=True)

        if "deal_price_brl" in dff.columns:
            dff["deal_category"] = dff["deal_title"].where(dff["deal_price_brl"] > 0, "Gratuito")
        else:
            dff["deal_category"] = dff["deal_title"]
        tier_ev = dff.groupby(["event_name", "deal_category"]).size().reset_index(name="ingressos")
        fig4 = px.bar(
            tier_ev, x="event_name", y="ingressos", color="deal_category",
            barmode="stack",
            labels={"event_name": "Evento", "ingressos": "Ingressos", "deal_category": "Categoria"},
            title="Mix de Categorias por Evento",
            color_discrete_sequence=COLORS,
        )
        fig4.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.3))
        col_r.plotly_chart(fig4, use_container_width=True)

    # Restaura dff completo (Shotgun + Porta) para abas de comparação
    dff = _dff_all

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 6 — COMPARAR EVENTO
    # ══════════════════════════════════════════════════════════════════════════
    with tab_compare:
        if len(sel_events) < 2:
            st.info("Selecione pelo menos 2 eventos no filtro lateral para usar a comparação.")
        else:
            _HL   = "#FF7F0E"
            _MUTE = "#AAAAAA"

            ref_event = st.selectbox(
                "Evento de referência",
                sorted(sel_events),
                key="compare_ref_event",
            )

            dff_ref   = dff[dff["event_name"] == ref_event]
            other_evs = [e for e in sel_events if e != ref_event]

            # ── KPIs: selecionado vs média dos outros ─────────────────────────
            def _ev_kpis(d):
                total   = len(d)
                receita = d["deal_price_brl"].sum() if "deal_price_brl" in d.columns else 0
                pago    = (d["deal_price_brl"] > 0).mean() * 100 if "deal_price_brl" in d.columns else 0
                scan    = d["ticket_scanned_at"].notna().mean() * 100
                return total, receita, pago, scan

            ref_k  = _ev_kpis(dff_ref)
            avg_k  = tuple(
                sum(_ev_kpis(dff[dff["event_name"] == e])[i] for e in other_evs) / len(other_evs)
                for i in range(4)
            )

            st.markdown(f"### ⭐ {ref_event}")
            ck1, ck2, ck3, ck4 = st.columns(4)
            ck1.metric("Ingressos Vendidos",    f"{ref_k[0]:,}",          f"{ref_k[0]-avg_k[0]:+.0f} vs média")
            ck2.metric("Receita Total",          f"R${ref_k[1]:,.2f}",     f"R${ref_k[1]-avg_k[1]:+,.2f} vs média")
            ck3.metric("% Ingressos Pagos",      f"{ref_k[2]:.1f}%",       f"{ref_k[2]-avg_k[2]:+.1f}pp vs média")
            ck4.metric("Taxa de Comparecimento", f"{ref_k[3]:.1f}%",       f"{ref_k[3]-avg_k[3]:+.1f}pp vs média")

            st.divider()

            # ── Receita e Ingressos por evento (cor do destaque) ──────────────
            ev_sum   = dff.groupby("event_name").agg(
                receita=("deal_price_brl", "sum"),
                ingressos=("ticket_id", "count"),
            ).reset_index()
            disc_map = {e: (_HL if e == ref_event else _MUTE) for e in sel_events}

            col_l, col_r = st.columns(2)

            fig_rev = px.bar(
                ev_sum.sort_values("receita", ascending=False),
                x="event_name", y="receita", color="event_name",
                color_discrete_map=disc_map, text="receita",
                labels={"event_name": "Evento", "receita": "Receita (BRL)"},
                title="Receita por Evento",
            )
            fig_rev.update_traces(texttemplate="R$%{text:,.0f}", textposition="outside")
            fig_rev.update_layout(showlegend=False, xaxis_tickangle=-20)
            col_l.plotly_chart(fig_rev, use_container_width=True)

            fig_tix = px.bar(
                ev_sum.sort_values("ingressos", ascending=False),
                x="event_name", y="ingressos", color="event_name",
                color_discrete_map=disc_map, text="ingressos",
                labels={"event_name": "Evento", "ingressos": "Ingressos Vendidos"},
                title="Ingressos Vendidos por Evento",
            )
            fig_tix.update_traces(textposition="outside")
            fig_tix.update_layout(showlegend=False, xaxis_tickangle=-20)
            col_r.plotly_chart(fig_tix, use_container_width=True)

            # ── Pago vs Gratuito (stacked, borda laranja no selecionado) ──────
            col_l2, col_r2 = st.columns(2)

            # First-timers vs returning across all events
            contacts_by_ev = dff.groupby("event_name")["contact_id"].apply(set)
            loyalty_rows = []
            for ev in sel_events:
                ev_c    = contacts_by_ev.get(ev, set())
                other_c = set().union(*[contacts_by_ev.get(e, set()) for e in sel_events if e != ev])
                loyalty_rows.append({"event_name": ev, "tipo": "Primeira Vez", "participantes": len(ev_c - other_c)})
                loyalty_rows.append({"event_name": ev, "tipo": "Recorrente",   "participantes": len(ev_c & other_c)})
            loyalty_df = pd.DataFrame(loyalty_rows)

            ev_ord_l  = ev_sum.sort_values("ingressos", ascending=False)["event_name"].tolist()
            ref_pos_l = ev_ord_l.index(ref_event)
            fig_loyal = px.bar(
                loyalty_df, x="event_name", y="participantes", color="tipo",
                barmode="stack",
                category_orders={"event_name": ev_ord_l},
                color_discrete_map={"Primeira Vez": COLORS[4], "Recorrente": COLORS[5]},
                labels={"event_name": "Evento", "participantes": "Participantes", "tipo": ""},
                title="Primeira Vez vs Recorrente por Evento",
            )
            fig_loyal.add_shape(
                type="rect", xref="x", yref="paper",
                x0=ref_pos_l - 0.45, x1=ref_pos_l + 0.45, y0=0, y1=1,
                line=dict(color=_HL, width=3), fillcolor="rgba(0,0,0,0)",
            )
            fig_loyal.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.25))
            col_l2.plotly_chart(fig_loyal, use_container_width=True)

            if "deal_price_brl" in dff.columns:
                dff["_ctype"] = dff["deal_price_brl"].apply(lambda x: "Gratuito" if x == 0 else "Pago")
                type_grp = dff.groupby(["event_name", "_ctype"]).agg(
                    ingressos=("ticket_id", "count"),
                ).reset_index()

                ev_ord_t  = ev_sum.sort_values("ingressos", ascending=False)["event_name"].tolist()
                ref_pos_t = ev_ord_t.index(ref_event)
                fig_tt = px.bar(
                    type_grp, x="event_name", y="ingressos", color="_ctype",
                    barmode="stack",
                    category_orders={"event_name": ev_ord_t},
                    color_discrete_map={"Pago": COLORS[0], "Gratuito": COLORS[2]},
                    labels={"event_name": "Evento", "ingressos": "Ingressos", "_ctype": "Tipo"},
                    title="Ingressos: Pago vs Gratuito",
                )
                fig_tt.add_shape(
                    type="rect", xref="x", yref="paper",
                    x0=ref_pos_t - 0.45, x1=ref_pos_t + 0.45, y0=0, y1=1,
                    line=dict(color=_HL, width=3), fillcolor="rgba(0,0,0,0)",
                )
                fig_tt.update_layout(xaxis_tickangle=-20, legend=dict(orientation="h", y=-0.25))
                col_r2.plotly_chart(fig_tt, use_container_width=True)

            # ── Vendas ao longo do tempo ───────────────────────────────────────
            if "order_date" in dff.columns:
                st.divider()

                daily = dff.groupby(["order_date", "event_name"]).size().reset_index(name="ingressos")
                daily["order_date"] = pd.to_datetime(daily["order_date"])
                fs = daily.groupby("event_name")["order_date"].min().rename("first_sale")
                daily = daily.merge(fs, on="event_name")
                daily["dia"] = (daily["order_date"] - daily["first_sale"]).dt.days
                daily["acumulado"] = daily.groupby("event_name")["ingressos"].cumsum()

                ref_d = daily[daily["event_name"] == ref_event]
                avg_d = (
                    daily[daily["event_name"] != ref_event]
                    .groupby("dia")[["ingressos", "acumulado"]].mean().reset_index()
                )

                col_l3, col_r3 = st.columns(2)

                fig_day = go.Figure()
                for ev in other_evs:
                    ev_d = daily[daily["event_name"] == ev]
                    fig_day.add_trace(go.Scatter(
                        x=ev_d["dia"], y=ev_d["ingressos"], mode="lines",
                        line=dict(color=_MUTE, width=1), opacity=0.4,
                        showlegend=False, name=ev,
                    ))
                fig_day.add_trace(go.Scatter(
                    x=avg_d["dia"], y=avg_d["ingressos"], mode="lines",
                    line=dict(color=_MUTE, width=2, dash="dash"),
                    name="Média dos outros",
                ))
                fig_day.add_trace(go.Scatter(
                    x=ref_d["dia"], y=ref_d["ingressos"], mode="lines+markers",
                    line=dict(color=_HL, width=3), name=ref_event,
                ))
                fig_day.update_layout(
                    title="Vendas Diárias: Selecionado vs Outros",
                    xaxis_title="Dias desde a 1ª Venda",
                    yaxis_title="Ingressos",
                    legend=dict(orientation="h", y=-0.25),
                )
                col_l3.plotly_chart(fig_day, use_container_width=True)

                fig_cum = go.Figure()
                for ev in other_evs:
                    ev_d = daily[daily["event_name"] == ev]
                    fig_cum.add_trace(go.Scatter(
                        x=ev_d["dia"], y=ev_d["acumulado"], mode="lines",
                        line=dict(color=_MUTE, width=1), opacity=0.4,
                        showlegend=False, name=ev,
                    ))
                fig_cum.add_trace(go.Scatter(
                    x=avg_d["dia"], y=avg_d["acumulado"], mode="lines",
                    line=dict(color=_MUTE, width=2, dash="dash"),
                    name="Média dos outros",
                ))
                fig_cum.add_trace(go.Scatter(
                    x=ref_d["dia"], y=ref_d["acumulado"], mode="lines+markers",
                    line=dict(color=_HL, width=3), name=ref_event,
                ))
                fig_cum.update_layout(
                    title="Vendas Acumuladas: Selecionado vs Outros",
                    xaxis_title="Dias desde a 1ª Venda",
                    yaxis_title="Ingressos Acumulados",
                    legend=dict(orientation="h", y=-0.25),
                )
                col_r3.plotly_chart(fig_cum, use_container_width=True)

            # ── Receita ao longo do tempo ──────────────────────────────────────
            if "deal_price_brl" in dff.columns and "order_date" in dff.columns:
                daily_rev = (
                    dff.groupby(["order_date", "event_name"])["deal_price_brl"]
                    .sum().reset_index(name="receita")
                )
                daily_rev["order_date"] = pd.to_datetime(daily_rev["order_date"])
                fs_r = daily_rev.groupby("event_name")["order_date"].min().rename("first_sale")
                daily_rev = daily_rev.merge(fs_r, on="event_name")
                daily_rev["dia"] = (daily_rev["order_date"] - daily_rev["first_sale"]).dt.days
                daily_rev["receita_acum"] = daily_rev.groupby("event_name")["receita"].cumsum()

                ref_r = daily_rev[daily_rev["event_name"] == ref_event]
                avg_r = (
                    daily_rev[daily_rev["event_name"] != ref_event]
                    .groupby("dia")[["receita", "receita_acum"]].mean().reset_index()
                )

                col_l4, col_r4 = st.columns(2)

                fig_rday = go.Figure()
                for ev in other_evs:
                    ev_r = daily_rev[daily_rev["event_name"] == ev]
                    fig_rday.add_trace(go.Scatter(
                        x=ev_r["dia"], y=ev_r["receita"], mode="lines",
                        line=dict(color=_MUTE, width=1), opacity=0.4,
                        showlegend=False, name=ev,
                    ))
                fig_rday.add_trace(go.Scatter(
                    x=avg_r["dia"], y=avg_r["receita"], mode="lines",
                    line=dict(color=_MUTE, width=2, dash="dash"),
                    name="Média dos outros",
                ))
                fig_rday.add_trace(go.Scatter(
                    x=ref_r["dia"], y=ref_r["receita"], mode="lines+markers",
                    line=dict(color=_HL, width=3), name=ref_event,
                ))
                fig_rday.update_layout(
                    title="Receita Diária: Selecionado vs Outros",
                    xaxis_title="Dias desde a 1ª Venda",
                    yaxis_title="Receita (BRL)",
                    legend=dict(orientation="h", y=-0.25),
                )
                col_l4.plotly_chart(fig_rday, use_container_width=True)

                fig_racum = go.Figure()
                for ev in other_evs:
                    ev_r = daily_rev[daily_rev["event_name"] == ev]
                    fig_racum.add_trace(go.Scatter(
                        x=ev_r["dia"], y=ev_r["receita_acum"], mode="lines",
                        line=dict(color=_MUTE, width=1), opacity=0.4,
                        showlegend=False, name=ev,
                    ))
                fig_racum.add_trace(go.Scatter(
                    x=avg_r["dia"], y=avg_r["receita_acum"], mode="lines",
                    line=dict(color=_MUTE, width=2, dash="dash"),
                    name="Média dos outros",
                ))
                fig_racum.add_trace(go.Scatter(
                    x=ref_r["dia"], y=ref_r["receita_acum"], mode="lines+markers",
                    line=dict(color=_HL, width=3), name=ref_event,
                ))
                fig_racum.update_layout(
                    title="Receita Acumulada: Selecionado vs Outros",
                    xaxis_title="Dias desde a 1ª Venda",
                    yaxis_title="Receita Acumulada (BRL)",
                    legend=dict(orientation="h", y=-0.25),
                )
                col_r4.plotly_chart(fig_racum, use_container_width=True)

    # ══════════════════════════════════════════════════════════════════════════
    # ABA 7 — PORTA (somente quando há dados de Porta)
    # ══════════════════════════════════════════════════════════════════════════
    if tab_porta is not None:
        with tab_porta:
            shotgun_by_event = (
                dff_shotgun.groupby("event_name")
                .agg(
                    shotgun_tickets=("event_name", "size"),
                    shotgun_revenue=("deal_price_brl", "sum"),
                )
                .reset_index()
            )
            comp = shotgun_by_event.merge(porta_df, on="event_name", how="outer").fillna(0)

            total_shotgun_rev = float(comp["shotgun_revenue"].sum())
            total_porta_rev   = float(comp["porta_revenue"].sum())
            total_shotgun_tk  = int(comp["shotgun_tickets"].sum())
            total_porta_tk    = int(comp["porta_tickets"].sum())

            st.subheader("Totais — Shotgun vs Porta")
            col_l, col_r = st.columns(2)

            fig_rev_total = px.bar(
                pd.DataFrame({
                    "Canal": ["Shotgun", "Porta"],
                    "Receita (R$)": [total_shotgun_rev, total_porta_rev],
                }),
                x="Canal", y="Receita (R$)", text_auto=".2f",
                color="Canal",
                color_discrete_map={"Shotgun": COLORS[0], "Porta": COLORS[1]},
            )
            fig_rev_total.update_layout(
                title="Receita Total", showlegend=False,
            )
            col_l.plotly_chart(fig_rev_total, use_container_width=True)

            fig_tk_total = px.bar(
                pd.DataFrame({
                    "Canal": ["Shotgun", "Porta"],
                    "Ingressos": [total_shotgun_tk, total_porta_tk],
                }),
                x="Canal", y="Ingressos", text_auto=True,
                color="Canal",
                color_discrete_map={"Shotgun": COLORS[0], "Porta": COLORS[1]},
            )
            fig_tk_total.update_layout(
                title="Ingressos Vendidos", showlegend=False,
            )
            col_r.plotly_chart(fig_tk_total, use_container_width=True)

            st.divider()
            st.subheader("Receita por Evento (Shotgun + Porta)")
            comp_long_rev = comp.melt(
                id_vars="event_name",
                value_vars=["shotgun_revenue", "porta_revenue"],
                var_name="Canal", value_name="Receita (R$)",
            )
            comp_long_rev["Canal"] = comp_long_rev["Canal"].map({
                "shotgun_revenue": "Shotgun", "porta_revenue": "Porta",
            })
            fig_rev_evt = px.bar(
                comp_long_rev,
                x="event_name", y="Receita (R$)", color="Canal",
                color_discrete_map={"Shotgun": COLORS[0], "Porta": COLORS[1]},
                barmode="stack",
            )
            fig_rev_evt.update_layout(
                xaxis_title="Evento", xaxis_tickangle=-20,
                legend=dict(orientation="h", y=-0.25),
            )
            st.plotly_chart(fig_rev_evt, use_container_width=True)

            st.subheader("Ingressos por Evento (Shotgun + Porta)")
            comp_long_tk = comp.melt(
                id_vars="event_name",
                value_vars=["shotgun_tickets", "porta_tickets"],
                var_name="Canal", value_name="Ingressos",
            )
            comp_long_tk["Canal"] = comp_long_tk["Canal"].map({
                "shotgun_tickets": "Shotgun", "porta_tickets": "Porta",
            })
            fig_tk_evt = px.bar(
                comp_long_tk,
                x="event_name", y="Ingressos", color="Canal",
                color_discrete_map={"Shotgun": COLORS[0], "Porta": COLORS[1]},
                barmode="stack",
            )
            fig_tk_evt.update_layout(
                xaxis_title="Evento", xaxis_tickangle=-20,
                legend=dict(orientation="h", y=-0.25),
            )
            st.plotly_chart(fig_tk_evt, use_container_width=True)
