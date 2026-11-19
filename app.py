import os
import threading
import time
from datetime import datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import factsheet_export as fx
from asset_store import AssetStore
from factsheet_export import (OVERLAY_PROXIES, RF, normalize_index, render_factsheet_section)
from factsheet_export import line_meta as _line_meta_hard
from portfolio_builder import (add_asset_ui, init_alloc_state, render_alloc_editor,
                               sync_alloc)

st.set_page_config(page_title="Portfolio vs Benchmark", page_icon="📊", layout="wide")

# =====================================================================
# CHARTE GRAPHIQUE (Sobriété institutionnelle — palette factsheet PDF)
# =====================================================================
NAVY, STEEL, MIST = "#0D2B55", "#2E6DA4", "#8FA3B8"
NEG = "#B23A48"
SEQ = [NAVY, STEEL, MIST, "#5B7FA6", "#1B4F8A"]

st.markdown("""
<style>
:root{--navy:#0D2B55;--steel:#2E6DA4;--mist:#8FA3B8;--bg:#F8FAFC;--line:#E2E8F0;--muted:#64748B;}
.stApp{background:var(--bg);}

/* Laisse l'espace nécessaire sous le bandeau Streamlit Cloud (Fork / GitHub) */
header[data-testid="stHeader"]{background:transparent;}
.block-container{padding-top:3.6rem;padding-bottom:3rem;max-width:1440px;}
#MainMenu, footer{visibility:hidden;}

/* Typographie éditoriale sans artifice */
h1,h2,h3,h4,h5{color:var(--navy);letter-spacing:-0.015em;}
h3{border-bottom:1px solid var(--line);padding-bottom:0.4rem;margin-top:1.4rem;font-size:1.15rem;}
h5{text-transform:uppercase;font-size:0.75rem;letter-spacing:0.06em;color:var(--muted);margin:0.1rem 0 0.75rem;font-weight:600;}

/* En-tête sobre avec filet bas */
.app-header{border-bottom:1px solid var(--line);padding-bottom:0.85rem;margin-bottom:0.5rem;}
.app-header h1{font-size:1.75rem;font-weight:700;margin:0;padding:0;line-height:1.2;color:var(--navy);}
.app-header p{margin:0.3rem 0 0;color:var(--muted);font-size:0.88rem;}

/* Cartes métriques épurées (encadrement fin 1px, sans barre latérale colorée) */
[data-testid="stMetric"]{
  background:#FFFFFF;
  border:1px solid var(--line);
  border-radius:6px;
  padding:0.9rem 1.1rem;
  box-shadow:0 1px 2px rgba(15, 23, 42, 0.03);
}
[data-testid="stMetricLabel"] p{
  color:var(--muted);
  font-size:0.72rem;
  text-transform:uppercase;
  letter-spacing:0.05em;
  font-weight:600;
}
[data-testid="stMetricValue"]{
  color:var(--navy);
  font-weight:700;
  font-variant-numeric:tabular-nums;
}

/* Onglets */
.stTabs [data-baseweb="tab-list"]{gap:0.4rem;border-bottom:1px solid var(--line);}
.stTabs [data-baseweb="tab"]{height:2.75rem;padding:0 1rem;font-weight:600;font-size:0.9rem;color:var(--muted);}
.stTabs [aria-selected="true"]{color:var(--navy);}
.stTabs [data-baseweb="tab-highlight"]{background:var(--navy);height:2px;}
.stTabs [data-baseweb="tab-border"]{background:transparent;}

/* Sidebar */
[data-testid="stSidebar"]{background:#FFFFFF;border-right:1px solid var(--line);}
[data-testid="stSidebar"] h2,[data-testid="stSidebar"] h3{border:none;font-size:0.95rem;}

/* Boutons */
.stButton>button,.stDownloadButton>button{
  border-radius:6px;
  border:1px solid #CBD5E1;
  color:var(--navy);
  font-weight:600;
  font-size:0.85rem;
  background:#FFFFFF;
  transition:all 0.15s ease;
}
.stButton>button:hover,.stDownloadButton>button:hover{
  background:var(--navy);
  color:#FFFFFF;
  border-color:var(--navy);
}
.stButton>button[kind="primary"]{background:var(--navy);color:#FFFFFF;border-color:var(--navy);}

/* Conteneurs et tableaux */
[data-testid="stVerticalBlockBorderWrapper"]{background:#FFFFFF;border-color:var(--line);border-radius:8px;}
[data-testid="stDataFrame"]{border:1px solid var(--line);border-radius:6px;}

/* Jauge d'allocation */
.alloc-gauge{display:flex;height:10px;border-radius:5px;overflow:hidden;background:#E2E8F0;margin:0.2rem 0 0.9rem;}
.alloc-gauge div:last-child{background:#CBD5E1;}
</style>
""", unsafe_allow_html=True)


def style_fig(fig, height=420, **kw):
    """Habillage Plotly commun : fond transparent, grille discrète, légende sous le graphique."""
    fig.update_layout(template="plotly_white", height=height, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(family="Inter, Segoe UI, sans-serif", color=NAVY, size=12),
                      margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", yanchor="top", y=-0.12, x=0), **kw)
    fig.update_xaxes(gridcolor="#E2E8F0", zeroline=False)
    fig.update_yaxes(gridcolor="#E2E8F0", zeroline=False)
    return fig

# =====================
# Paramètres
# =====================
ALLOCATION_DEFAULT = {
    "TTE.PA": 0.05,
    "MC.PA": 0.05,
    "INGA.AS": 0.05,
    "SAP.DE": 0.04,
    "ACLN.SW": 0.05,
    "THEON.AS": 0.04,
    "BOI.PA": 0.05,
    "EOAN.DE": 0.05,
    "GOOGL": 0.03,
    "META": 0.02,
    "HWM": 0.03,
    "AMZN": 0.03,
    "0P0000ZWX4.F": 0.08,
    "0P0001861S.F": 0.08,
    "0P00000M6C.F": 0.09,
    "0P00008ESK.F": 0.08,
    "0P0000A6ZG.F": 0.05,
    "0P0000WHLW.F": 0.08,
}

BENCHMARK_WEIGHTS = {"IEV": 0.35, "SPY": 0.20, "TLT": 0.25, "VNQ": 0.10, "EEM": 0.05}

TICKER_NAMES_HARD = {
    "TTE.PA": "TotalEnergies", "MC.PA": "LVMH", "INGA.AS": "ING Groep",
    "SAP.DE": "SAP", "ACLN.SW": "ACLN", "THEON.AS": "Theon Intl",
    "BOI.PA": "Boiron", "EOAN.DE": "E.ON", "GOOGL": "Alphabet",
    "META": "Meta", "HWM": "Howmet", "AMZN": "Amazon",
    "0P0000ZWX4.F": "Helium Fund Perf A EUR",
    "0P0001861S.F": "Eleva Abs Ret Eurp S EUR",
    "0P00000M6C.F": "R-co Conviction Credit Euro",
    "0P00008ESK.F": "AXAIMFIIS US SD HY EUR H",
    "0P0000A6ZG.F": "Immobilier 21 AC",
    "0P0000WHLW.F": "GemEquity R",
}

USD_DEFAULT = ["GOOGL", "META", "HWM", "AMZN"]   # repli si la devise est inconnue

FALLBACK_TICKERS = {
    "TTE.PA": ["FP.PA"],
}

for _t, _m in OVERLAY_PROXIES.items():
    if _m["fallbacks"]:
        FALLBACK_TICKERS.setdefault(_t, list(_m["fallbacks"]))

OV_DEFAULTS = dict(ov_eu=0.0, ov_us=0.0, ov_bond=0.0, ov_re=0.0, ov_split=50, ov_hedge=100,
                   ov_fund="prorata")
FUNDING_OPTIONS = {"Au prorata des lignes non touchées": "prorata", "Via les liquidités": "cash"}

MIN_OBS = 30          # nombre minimal de cours pour qu'une ligne soit utilisable
STALE_DAYS = 10       # au-delà, un cours est jugé figé
LATE_START_DAYS = 30  # historique démarrant bien après la date de début

PRICE_DB_PATH = os.environ.get("PRICE_DB_PATH", "price_cache.duckdb")
PRICE_TTL_HOURS = float(os.environ.get("PRICE_TTL_HOURS", "6"))

REBAL_OPTIONS = {"Quotidien (théorique)": "D", "Mensuel": "M", "Trimestriel": "Q"}


def pct(x):
    """Rendement simple sans remplissage implicite (compatible pandas 2.x et 3.x)."""
    return x / x.shift(1) - 1


# =====================
# Moteur de portefeuille : rebalancement, dérive des poids, frais
# =====================
def rebalance_flags(index, freq):
    """Vrai à la dernière séance de chaque période ("M" mois, "Q" trimestre) ou chaque jour ("D")."""
    n = len(index)
    if freq == "D":
        return np.ones(n, dtype=bool)
    per = pd.DatetimeIndex(index).to_period("M" if freq == "M" else "Q")
    flags = np.zeros(n, dtype=bool)
    if n > 1:
        flags[:-1] = np.asarray(per[:-1] != per[1:])
    return flags


def simulate_portfolio(returns, target_w, freq="M", cost_bps=0.0):
    """Simule la valeur du portefeuille avec dérive des poids entre deux rebalancements."""
    cols = list(returns.columns)
    w = target_w.reindex(cols).fillna(0.0).to_numpy(dtype=float)
    w_full = np.append(w, max(0.0, 1.0 - w.sum()))          # dernière colonne = cash
    rets = np.hstack([returns.fillna(0.0).to_numpy(dtype=float), np.zeros((len(returns), 1))])
    flags = rebalance_flags(returns.index, freq)

    hold = w_full.copy()
    nav = np.empty(len(returns))
    traded_pct, cost_total, n_rebal = [], 0.0, 0
    for t in range(len(returns)):
        hold = hold * (1.0 + rets[t])
        if flags[t]:
            nav_pre = hold.sum()
            traded = np.abs(w_full * nav_pre - hold)[:-1].sum()
            cost = traded * cost_bps / 1e4
            hold = w_full * (nav_pre - cost)
            cost_total += cost
            traded_pct.append(traded / nav_pre)
            n_rebal += 1
        nav[t] = hold.sum()

    years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1e-9)
    return dict(
        nav=pd.Series(nav, index=returns.index),
        weights_end=pd.Series(hold[:-1] / hold.sum(), index=cols),
        n_rebal=n_rebal,
        turnover_ann=float(np.sum(traded_pct) / years) if traded_pct else 0.0,
        cost_total=float(cost_total),
    )


# =====================
# Overlay tactique (TAA) : poids tactiques, rendements couverts, expositions
# =====================
def proxy_shares(eu_ticker, us_ticker, us_share):
    """Répartition d'une poche de proxys entre Europe et US ; repli sur le proxy disponible."""
    if eu_ticker and us_ticker:
        out = {eu_ticker: 1.0 - us_share, us_ticker: us_share}
        return {t: v for t, v in out.items() if v > 1e-12}
    if eu_ticker:
        return {eu_ticker: 1.0}
    if us_ticker:
        return {us_ticker: 1.0}
    return {}


def build_tactical_weights(base_w, tilts, groups, proxy_alloc, funding="prorata"):
    """Applique les écarts tactiques aux poids de base (sans levier, sans poids négatif)."""
    proxies = sorted({t for a in proxy_alloc.values() for t in a})
    idx = list(base_w.index) + [t for t in proxies if t not in base_w.index]
    w = base_w.reindex(idx).fillna(0.0).astype(float)
    d = pd.Series(0.0, index=idx)
    sleeve_lines = {k: [] for k in ("eq_eu", "eq_us", "bond", "re")}

    for k in ("eq_eu", "eq_us", "re"):
        x, lines = float(tilts.get(k, 0.0)), [t for t in groups.get(k, []) if t in w.index]
        sleeve_lines[k] += lines
        if x < 0 or k != "re":
            tot = float(w[lines].sum()) if lines else 0.0
            if tot > 0 and x != 0:
                x = max(x, -tot)
                d[lines] += w[lines] / tot * x
    for k in ("bond", "re"):
        x = float(tilts.get(k, 0.0))
        if x > 0:
            for t, sh in proxy_alloc.get(k, {}).items():
                d[t] += x * sh
                sleeve_lines[k].append(t)

    touched = {t for k, v in sleeve_lines.items() if float(tilts.get(k, 0.0)) != 0 for t in v}
    free = [t for t in base_w.index if d[t] == 0 and w[t] > 0 and t not in touched]
    pos, neg = float(d[d > 0].sum()), float(d[d < 0].sum())
    capacity = float(w[free].sum()) if funding == "prorata" else max(0.0, 1.0 - float(w.sum()))
    k_scale = 1.0
    if pos > 0 and pos + neg > capacity + 1e-12:
        k_scale = max(0.0, (capacity - neg) / pos)
        d[d > 0] *= k_scale
    net = float(d.sum())
    w_new = w + d
    if funding == "prorata" and free:
        sf = float(w[free].sum())
        if sf > 0:
            w_new[free] = w[free] * (1.0 - net / sf)
    w_new = w_new.clip(lower=0.0)
    eff = {k: float(d[sorted(set(v))].sum()) if v else 0.0 for k, v in sleeve_lines.items()}
    return dict(weights=w_new, cash=float(max(0.0, 1.0 - w_new.sum())), eff=eff,
                scaled=bool(k_scale < 1.0 - 1e-9))


def strategy_returns(ret_eur, fx_ret, usd_cols, hedge):
    """Rendements EUR après couverture de change du ratio `hedge` (0 à 1) sur les colonnes USD."""
    r = ret_eur.copy()
    cols = [c for c in usd_cols if c in r.columns]
    if hedge > 0 and cols and fx_ret is not None:
        adj = (hedge * fx_ret / (1.0 + fx_ret)).reindex(r.index).fillna(0.0)
        r[cols] = r[cols].add(adj, axis=0)
    return r


def exposure_table(w, cash, meta_fn, hedge=0.0):
    """Expositions (en % de l'actif net) par poche, plus USD brut et USD net de couverture."""
    out = dict.fromkeys(("Actions Europe", "Actions Amérique du Nord", "Actions autres zones",
                         "Obligations & crédit", "Immobilier", "Alternatif", "Liquidités",
                         "Exposition USD brute", "Exposition USD nette"), 0.0)
    for t, x in w.items():
        m = meta_fn(t)
        if m["cls"] == "Actions":
            key = {"Europe": "Actions Europe",
                   "Amerique du Nord": "Actions Amérique du Nord"}.get(m["region"], "Actions autres zones")
        else:
            key = {"Credit & Obligations": "Obligations & crédit", "Immobilier": "Immobilier",
                   "Alternatif": "Alternatif"}.get(m["cls"], "Alternatif")
        out[key] += x * 100
        if m["ccy"] == "USD":
            out["Exposition USD brute"] += x * 100
    out["Liquidités"] = cash * 100
    out["Exposition USD nette"] = out["Exposition USD brute"] * (1.0 - hedge)
    return out


# =====================
# Cache de cours DuckDB
# =====================
class PriceStore:
    """Cache local des séries de cours dans DuckDB (fichier ou mémoire)."""

    OVERLAP_DAYS = 10
    ADJ_TOLERANCE = 5e-4
    FAIL_TTL_MIN = 30

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self._failures = {}
        try:
            self.con = duckdb.connect(path)
            self.path = path
        except Exception:
            self.con = duckdb.connect(":memory:")
            self.path = ":memory:"
        with self._lock:
            self.con.execute(
                "CREATE TABLE IF NOT EXISTS prices ("
                "symbol VARCHAR, dt DATE, close DOUBLE, PRIMARY KEY (symbol, dt))")
            self.con.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "symbol VARCHAR PRIMARY KEY, source VARCHAR, start_covered DATE, last_fetch TIMESTAMP)")

    def _meta(self, key):
        with self._lock:
            row = self.con.execute(
                "SELECT m.source, m.start_covered, m.last_fetch, "
                "(SELECT max(dt) FROM prices p WHERE p.symbol = m.symbol) "
                "FROM meta m WHERE m.symbol = ?", [key]).fetchone()
        if row is None or row[3] is None:
            return None
        return dict(source=row[0], start_covered=pd.Timestamp(row[1]),
                    last_fetch=row[2], last_date=pd.Timestamp(row[3]))

    def _store(self, key, s, source, start_covered, replace_all):
        df = pd.DataFrame({"symbol": key, "dt": pd.DatetimeIndex(s.index),
                           "close": s.astype(float).to_numpy()})
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._lock:
            self.con.execute("BEGIN")
            try:
                if replace_all:
                    self.con.execute("DELETE FROM prices WHERE symbol = ?", [key])
                self.con.register("_tmp_prices", df)
                self.con.execute("INSERT OR REPLACE INTO prices "
                                 "SELECT symbol, CAST(dt AS DATE), close FROM _tmp_prices")
                self.con.unregister("_tmp_prices")
                self.con.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?)",
                    [key, source, pd.Timestamp(start_covered).date(), now])
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    def _touch(self, key):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._lock:
            self.con.execute("UPDATE meta SET last_fetch = ? WHERE symbol = ?", [now, key])

    def read(self, keys, start_iso):
        keys = list(keys)
        if not keys:
            return {}
        marks = ",".join("?" * len(keys))
        with self._lock:
            df = self.con.execute(
                f"SELECT symbol, dt, close FROM prices WHERE symbol IN ({marks}) AND dt >= ? "
                "ORDER BY symbol, dt", keys + [pd.Timestamp(start_iso).date()]).df()
        out = {}
        for sym, g in df.groupby("symbol"):
            out[sym] = normalize_index(pd.Series(g["close"].to_numpy(), index=pd.DatetimeIndex(g["dt"])))
        return out

    def stats(self):
        with self._lock:
            n_sym, n_rows = self.con.execute(
                "SELECT count(DISTINCT symbol), count(*) FROM prices").fetchone()
        return n_sym, n_rows

    def sync(self, key, start_iso, fetch, candidates, ttl_hours=6.0, force=False):
        start = pd.Timestamp(start_iso)
        meta = self._meta(key)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        covered = meta is not None and meta["start_covered"] <= start
        fresh = (meta is not None and meta["last_fetch"] is not None
                 and now - meta["last_fetch"] < timedelta(hours=ttl_hours))
        if covered and fresh and not force:
            return dict(status="hit", source=meta["source"])
        last_fail = self._failures.get(key)
        if last_fail and not force and now - last_fail[0] < timedelta(minutes=self.FAIL_TTL_MIN):
            return last_fail[1]

        cands = list(dict.fromkeys(([meta["source"]] if meta and meta["source"] else []) + list(candidates)))

        if covered:
            inc_start = (meta["last_date"] - pd.Timedelta(days=self.OVERLAP_DAYS)).strftime("%Y-%m-%d")
            for sym in cands:
                s = fetch(sym, inc_start)
                if s is None or len(s) == 0:
                    continue
                old = self.read([key], inc_start).get(key)
                common = s.index.intersection(old.index) if old is not None else []
                if len(common) == 0:
                    break
                gap = ((s.loc[common] / old.loc[common]) - 1).abs().max()
                if gap > self.ADJ_TOLERANCE:
                    break
                self._store(key, s, sym, meta["start_covered"], replace_all=False)
                return dict(status="incremental", source=sym)

        for sym in cands:
            s = fetch(sym, start_iso)
            if s is not None and len(s) > 0:
                self._store(key, s, sym, start, replace_all=True)
                return dict(status="full", source=sym)

        result = (dict(status="stale", source=meta["source"]) if meta is not None
                  else dict(status="failed", source=None))
        self._failures[key] = (now, result)
        return result


@st.cache_resource
def get_store(path):
    return PriceStore(path)


# =====================================================================
# HEADER (titre à gauche, export factsheet à droite : rempli en fin de script)
# =====================================================================
h_left, h_right = st.columns([5, 2], vertical_alignment="center")
h_left.markdown(
    '<div class="app-header"><h1>Portfolio vs Benchmark</h1>'
    '<p>Gestion multi-actifs · Overlay tactique · Couverture USD/EUR · Reporting PDF</p></div>',
    unsafe_allow_html=True)
export_slot = h_right.container()

# =====================================================================
# SIDEBAR ALLÉGÉE : paramètres globaux de simulation uniquement
# =====================================================================
st.sidebar.header("⚙️ Simulation")
start = st.sidebar.date_input("Start date", datetime(2020, 1, 1))
start_iso = pd.Timestamp(start).strftime("%Y-%m-%d")
rebal_label = st.sidebar.selectbox("Fréquence de rebalancement", list(REBAL_OPTIONS), index=1)
rebal_code = REBAL_OPTIONS[rebal_label]
cost_bps = st.sidebar.number_input(
    "Frais de transaction (bps par mouvement)", min_value=0.0, max_value=200.0, value=10.0, step=1.0,
    help="Appliqués au montant acheté ou vendu sur chaque ligne lors d'un rebalancement. "
         "Le premier investissement n'est pas facturé.")
force_refresh = st.sidebar.button("🔄 Forcer le rafraîchissement des cours", use_container_width=True)

for _k, _v in OV_DEFAULTS.items():
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("ov_fund_label", list(FUNDING_OPTIONS)[0])


def _reset_overlay():
    for k, v in OV_DEFAULTS.items():
        st.session_state[k] = v
    st.session_state["ov_fund_label"] = list(FUNDING_OPTIONS)[0]


store = get_store(PRICE_DB_PATH)


# =====================
# Chargement des prix (DuckDB + téléchargement uniquement si nécessaire)
# =====================
def _extract_close(df):
    """Extrait une série de cours exploitable d'un DataFrame yfinance, ou None."""
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    for col in ("Adj Close", "Close"):
        if col in df.columns:
            s = df[col]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            s = pd.to_numeric(s, errors="coerce").dropna()
            s = s[s > 0]
            if len(s):
                return normalize_index(s)
    return None


def _try_symbol(symbol, start_iso, retries=2):
    """Plusieurs méthodes yfinance, plusieurs tentatives. Retourne une Series ou None."""
    methods = (
        lambda: yf.download(symbol, start=start_iso, auto_adjust=False,
                            progress=False, threads=False),
        lambda: yf.Ticker(symbol).history(start=start_iso, auto_adjust=False),
    )
    for attempt in range(retries):
        for method in methods:
            try:
                s = _extract_close(method())
                if s is not None:
                    return s
            except Exception:
                continue
        time.sleep(0.4 * (attempt + 1))
    return None


def sync_symbol(ticker):
    return store.sync(ticker, start_iso, _try_symbol, [ticker] + FALLBACK_TICKERS.get(ticker, []),
                      ttl_hours=PRICE_TTL_HOURS, force=force_refresh)


def get_series(ticker):
    """(Series ou None, info de synchronisation) pour un symbole isolé."""
    info = sync_symbol(ticker)
    s = store.read([ticker], start_iso).get(ticker)
    return s, info


def load_prices(tickers):
    """Retourne (DataFrame des cours valides, rapport par ticker)."""
    infos = {t: sync_symbol(t) for t in tickers}
    data = store.read(tickers, start_iso)
    series, report = {}, {}
    for t in tickers:
        s, info = data.get(t), infos[t]
        cache = info["status"]
        if s is None or info["status"] == "failed":
            report[t] = dict(status="failed", source="-", first=None, last=None, cache=cache,
                             note="Aucune donnée renvoyée par Yahoo Finance")
        elif len(s) < MIN_OBS:
            report[t] = dict(status="failed", source=info["source"], first=s.index[0], last=s.index[-1],
                             cache=cache, note=f"Historique trop court ({len(s)} cours)")
        else:
            note, status = "", "ok"
            if cache == "stale":
                status, note = "stale", "Yahoo indisponible : dernier cache utilisé"
            series[t] = s
            report[t] = dict(status=status, source=info["source"], first=s.index[0], last=s.index[-1],
                             cache=cache, note=note)
    if not series:
        return pd.DataFrame(), report
    return pd.concat(series, axis=1).sort_index(), report


def load_fx():
    """EUR/USD (USD pour 1 EUR). Repli sur EUR=X (EUR pour 1 USD) inversé."""
    s, _ = get_series("EURUSD=X")
    if s is not None and len(s) >= MIN_OBS:
        return s
    inv, _ = get_series("EUR=X")
    if inv is not None and len(inv) >= MIN_OBS:
        return 1.0 / inv
    return None


def load_benchmark_prices():
    """Cours des composantes du benchmark. Retourne (DataFrame, poids effectifs, échecs)."""
    series, failed = {}, []
    for tk in BENCHMARK_WEIGHTS:
        s, _ = get_series(tk)
        if s is None or len(s) < MIN_OBS:
            failed.append(tk)
        else:
            series[tk] = s
    if not series:
        return pd.DataFrame(), {}, failed
    px = pd.concat(series, axis=1).sort_index()
    w = pd.Series({k: BENCHMARK_WEIGHTS[k] for k in px.columns})
    w = w * (sum(BENCHMARK_WEIGHTS.values()) / w.sum())
    return px, w.to_dict(), failed


# =====================
# Portfolio Builder : actifs (DuckDB), métadonnées dynamiques, allocation en session
# =====================
@st.cache_resource
def get_asset_store(_ps):
    """AssetStore partage la connexion et le verrou du PriceStore (même fichier DuckDB)."""
    return AssetStore(con=_ps.con, lock=_ps._lock)


asset_store = get_asset_store(store)
ASSETS = asset_store.all_assets()
ticker_names = {**TICKER_NAMES_HARD, **{s: a["name"] for s, a in ASSETS.items() if a["name"]}}
TICKER_NAMES_ALL = {**ticker_names, **{t: m["name"] for t, m in OVERLAY_PROXIES.items()}}
init_alloc_state({t: round(w * 100, 2) for t, w in ALLOCATION_DEFAULT.items()}, TICKER_NAMES_HARD)

_EU_SFX = {".PA", ".DE", ".AS", ".MI", ".MC", ".BR", ".LS", ".HE", ".F", ".SW", ".L", ".VI", ".CO", ".ST", ".OL"}
_CCY_SFX = {".SW": "CHF", ".L": "GBP", ".CO": "DKK", ".ST": "SEK", ".OL": "NOK"}
_KIND = {"Equity": "Direct", "ETF": "Overlay", "Fund": "Fonds", "Bond": "Direct"}
_CLS = {"Equity": "Actions", "ETF": "Actions", "Fund": "Alternatif", "Bond": "Credit & Obligations"}


def _sfx(t):
    return t[t.rfind("."):].upper() if "." in t else ""


def _region(t):
    s = _sfx(t)
    return "Amerique du Nord" if s in ("", ".TO", ".V") else "Europe" if s in _EU_SFX else "Marches emergents"


def _ccy(t):
    s = _sfx(t)
    return "USD" if s in ("", ".TO", ".V") else _CCY_SFX.get(s, "EUR" if s in _EU_SFX else "USD")


def line_meta(t: str) -> dict:
    """Table assets d'abord, repli sur factsheet_export.line_meta (lignes historiques en dur)."""
    a = ASSETS.get(t)
    if not a:
        return _line_meta_hard(t)
    ac = a["asset_class"]
    sec = a["sector"] or {"Fund": "Fonds", "ETF": "ETF"}.get(ac, "Autre")
    cls = _CLS.get(ac, "Alternatif")
    if ac == "Fund" and sec.lower().startswith(("immo", "real estate")):
        cls = "Immobilier"
    return dict(cls=cls, region=a["region"] or _region(t), sector=sec,
                ccy=a["currency"] or _ccy(t), kind=_KIND.get(ac, "Direct"))


fx.line_meta = line_meta


def check_symbol(sym: str) -> tuple[bool, str]:
    try:
        s, _ = get_series(sym)
    except Exception as e:
        return False, f"erreur de chargement ({e})"
    if s is None:
        return False, "aucune donnée de cours"
    if len(s) < MIN_OBS:
        return False, f"historique insuffisant ({len(s)}/{MIN_OBS} obs.)"
    return True, ""


def _meta_name(s):
    return {"name": ticker_names.get(s) or st.session_state["alloc_names"].get(s, s)}


def _load_profile():
    n = st.session_state.get("prof_sel")
    a = asset_store.load_portfolio(n) if n and n != "—" else None
    if a is not None:
        for k in [k for k in st.session_state if k.startswith("w_")]:
            del st.session_state[k]
        st.session_state["alloc"] = {s: float(w) for s, w in a.items()}
        st.session_state["prof_name"] = n
    st.session_state["prof_sel"] = "—"


def _save_profile():
    sync_alloc()
    n = st.session_state.get("prof_name", "").strip()
    if not n or not st.session_state["alloc"]:
        st.session_state["_flash"] = ("error", "Nom et allocation requis.")
        return
    asset_store.save_portfolio(n, st.session_state["alloc"])
    st.session_state["_flash"] = ("success", f"Profil « {n} » sauvegardé.")


# =====================================================================
# SQUELETTE : zone d'alertes + onglets
# =====================================================================
alerts = st.container()
tab_perf, tab_build, tab_taa, tab_data = st.tabs(
    ["📊 Performance & Risque", "🧱 Portfolio Builder (Allocation)",
     "🎯 Overlay Tactique (TAA) & Change", "🔍 Qualité des données & Coûts"])

# ---- Onglet 2 : Portfolio Builder
with tab_build:
    col_b_l, col_b_r = st.columns([1, 1.25], gap="large")
    with col_b_l:
        with st.container(border=True):
            st.markdown("##### Ajouter un actif")
            add_asset_ui(asset_store, check_symbol)
        with st.container(border=True):
            st.markdown("##### Profils sauvegardés")
            st.selectbox("Charger un profil", ["—"] + asset_store.list_portfolios(), key="prof_sel",
                         on_change=_load_profile)
            st.text_input("Nom du profil", key="prof_name")
            st.button("Sauvegarder le profil courant", on_click=_save_profile, use_container_width=True)
    with col_b_r:
        with st.container(border=True):
            st.markdown("##### Allocation cible")
            render_alloc_editor(_meta_name)

sync_alloc()

# ---- Onglet 3 : contrôles de l'overlay
with tab_taa:
    col_t_l, col_t_r = st.columns([1, 2], gap="large")
with col_t_l:
    with st.container(border=True):
        st.markdown("##### Écarts tactiques (pts d'actif net)")
        ov_eu = st.slider("Actions Europe (pts)", -20.0, 20.0, step=0.5, key="ov_eu",
                          help="Sur / sous-pondération des actions directes européennes de la poche.")
        ov_us = st.slider("Actions US (pts)", -20.0, 20.0, step=0.5, key="ov_us",
                          help="Sur / sous-pondération des actions directes d'Amérique du Nord.")
        ov_bond = st.slider("Obligations d'État (pts)", 0.0, 30.0, step=0.5, key="ov_bond",
                            help="Achat de proxys liquides : ETF Euro Govt (EUNH) et US Treasury 20+ ans (TLT).")
        ov_re = st.slider("Immobilier (pts)", -10.0, 20.0, step=0.5, key="ov_re",
                          help="Négatif : réduit le fonds immobilier existant. Positif : ETF IPRP / VNQ.")
    with st.container(border=True):
        st.markdown("##### Proxys & financement")
        ov_split = st.slider("Part US des proxys (%)", 0, 100, step=5, key="ov_split",
                             help="Répartition Europe / US des achats obligataires et immobiliers.")
        ov_fund_label = st.radio("Financement des écarts", list(FUNDING_OPTIONS), key="ov_fund_label")
        ov_fund = FUNDING_OPTIONS[ov_fund_label]
    with st.container(border=True):
        st.markdown("##### Change USD/EUR")
        ov_hedge = st.slider("Ratio de couverture USD/EUR (%)", 0, 100, step=5, key="ov_hedge",
                             help="Appliqué à toutes les lignes en USD (actions US, TLT, VNQ).")
    st.button("↺ Revenir au portefeuille de base", on_click=_reset_overlay, use_container_width=True)


# =====================
# Chargement & Calculs financiers
# =====================
allocation = {s: w / 100 for s, w in st.session_state["alloc"].items() if w > 0}
if not allocation:
    alerts.warning("Allocation vide : ajoutez des actifs dans l'onglet Portfolio Builder.")
    st.stop()
if sum(allocation.values()) > 1 + 1e-9:
    alerts.warning("⚠️ Somme des poids > 100 % : poids normalisés à 100 % (pas de levier).")
    _tot = sum(allocation.values())
    allocation = {s: w / _tot for s, w in allocation.items()}
tickers = list(allocation)
USD_TICKERS = [t for t in tickers if line_meta(t)["ccy"] == "USD" or (t not in ASSETS and t in USD_DEFAULT)]

with st.spinner("Chargement des cours (cache DuckDB, téléchargement si nécessaire)..."):
    prices, report = load_prices(tickers)
    fx_series = load_fx() if not prices.empty else None
    bench_px, bench_weights_eff, bench_failed = load_benchmark_prices()

n_sym, n_rows = store.stats()

if prices.empty:
    alerts.error("Aucune donnée de prix n'a pu être téléchargée. Vérifiez votre connexion ou les tickers.")
    st.stop()
if bench_px.empty:
    alerts.error("Aucune donnée de benchmark n'a pu être téléchargée.")
    st.stop()

if fx_series is None:
    alerts.warning("⚠️ Taux EUR/USD indisponible : les lignes cotées en USD sont exclues du portefeuille.")
    for t in USD_TICKERS:
        if t in prices.columns:
            prices = prices.drop(columns=t)
            report[t].update(status="failed", note="Taux EUR/USD indisponible")

calendar = prices.index.union(bench_px.index)
if fx_series is not None:
    calendar = calendar.union(fx_series.index)
prices = prices.reindex(calendar).ffill()
bench_px = bench_px.reindex(calendar).ffill()
if fx_series is not None:
    fx_aligned = fx_series.reindex(calendar).ffill().bfill()
first_day = max(prices.dropna(how="all").index[0], bench_px.dropna(how="all").index[0])
prices, bench_px = prices.loc[first_day:], bench_px.loc[first_day:]
if fx_series is not None:
    fx_aligned = fx_aligned.loc[first_day:]
if len(prices) < MIN_OBS:
    alerts.error(f"Historique commun insuffisant ({len(prices)} dates). Reculez la date de début.")
    st.stop()

last_global = prices.index[-1]
for t in prices.columns:
    r = report[t]
    if r["status"] != "ok":
        continue
    if (last_global - r["last"]).days > STALE_DAYS:
        r.update(status="stale",
                 note=f"Cours figé depuis le {r['last']:%d/%m/%Y} (dernier cours reporté)")
    elif (r["first"] - pd.Timestamp(start_iso)).days > LATE_START_DAYS:
        r.update(status="partial",
                 note=f"Historique depuis le {r['first']:%d/%m/%Y} (rendement 0 % avant)")

excluded = [t for t in tickers if t not in prices.columns]
kept = [t for t in tickers if t in prices.columns]
total_target = sum(allocation.values())
w_kept = pd.Series({t: allocation[t] for t in kept})
weights = w_kept * (total_target / w_kept.sum())
allocation_eff = weights.to_dict()
excluded_names = [ticker_names.get(t, t) for t in excluded]

if excluded:
    lost = sum(allocation[t] for t in excluded) * 100
    alerts.warning(
        f"⚠️ {len(excluded)} ligne(s) exclue(s) faute de données exploitables : "
        f"{', '.join(excluded_names)} ({lost:.1f}% du poids cible). "
        "Leur poids est réparti au prorata sur les autres lignes ; "
        "la mention figure dans la factsheet."
    )
if bench_failed:
    alerts.warning("⚠️ Composantes du benchmark indisponibles et exclues : "
                   f"{', '.join(bench_failed)}. Les autres poids sont réajustés au prorata.")
issues = [t for t in kept if report[t]["status"] in ("stale", "partial")]
issues_msg = (
    "ℹ️ Lignes à surveiller : "
    + " ; ".join(f"{ticker_names.get(t, t)} ({report[t]['note']})" for t in issues)
    if issues else None
)

# =====================
# Overlay tactique + NAV portefeuille
# =====================
usd_ok = [t for t in USD_TICKERS if t in prices.columns]
prices_eur = prices.copy()
for t in usd_ok:
    prices_eur[t] = prices[t] / fx_aligned
prices_eur = prices_eur.ffill()

tilts_req = dict(eq_eu=ov_eu / 100, eq_us=ov_us / 100, bond=ov_bond / 100, re=ov_re / 100)

proxy_px, proxy_notes = {}, []
for t, m in OVERLAY_PROXIES.items():
    if tilts_req[m["sleeve"]] <= 0:
        continue
    if m["ccy"] == "USD" and fx_series is None:
        proxy_notes.append(f"{m['name']} : taux EUR/USD indisponible")
        continue
    s_t, _ = get_series(t)
    if s_t is None or len(s_t) < MIN_OBS:
        proxy_notes.append(f"{m['name']} ({t}) : aucune donnée exploitable")
    else:
        proxy_px[t] = s_t

proxies_eur = pd.DataFrame(index=prices.index)
for t, s_t in proxy_px.items():
    if t in prices_eur.columns:
        continue
    px_t = s_t.reindex(prices.index.union(s_t.index)).ffill().reindex(prices.index)
    proxies_eur[t] = px_t / fx_aligned if OVERLAY_PROXIES[t]["ccy"] == "USD" else px_t
prices_eur_all = pd.concat([prices_eur, proxies_eur], axis=1)


def _proxy_alloc(sleeve):
    av = {m["region"]: t for t, m in OVERLAY_PROXIES.items() if m["sleeve"] == sleeve and t in proxy_px}
    return proxy_shares(av.get("Europe"), av.get("Amerique du Nord"), ov_split / 100)


proxy_alloc = {k: _proxy_alloc(k) for k in ("bond", "re")}
meta_base = {t: line_meta(t) for t in weights.index}
groups = dict(
    eq_eu=[t for t, m in meta_base.items() if m["kind"] == "Direct" and m["region"] == "Europe"],
    eq_us=[t for t, m in meta_base.items() if m["kind"] == "Direct" and m["region"] == "Amerique du Nord"],
    re=[t for t, m in meta_base.items() if m["cls"] == "Immobilier" and m["kind"] == "Fonds"],
)
tact = build_tactical_weights(weights, tilts_req, groups, proxy_alloc, ov_fund)
w_strat = tact["weights"]
w_strat = w_strat[(w_strat > 1e-12) | w_strat.index.isin(weights.index)]
overlay_active = any(abs(v) > 1e-9 for v in tact["eff"].values())

ret_eur = pct(prices_eur_all).fillna(0)
fx_ret = pct(fx_aligned).fillna(0) if fx_series is not None else None
usd_cols = [t for t in ret_eur.columns
            if t in usd_ok or OVERLAY_PROXIES.get(t, {}).get("ccy") == "USD"]
ret_strat = strategy_returns(ret_eur, fx_ret, usd_cols, ov_hedge / 100)

sim_s = simulate_portfolio(ret_strat[w_strat.index], w_strat, rebal_code, cost_bps)
sim_s_gross = simulate_portfolio(ret_strat[w_strat.index], w_strat, rebal_code, 0.0)
sim_b = simulate_portfolio(ret_eur[weights.index], weights, rebal_code, cost_bps)

portfolio_index = normalize_index(sim_s["nav"])
portfolio_index_base = normalize_index(sim_b["nav"])
weights_now = sim_s["weights_end"]
cost_drag = (sim_s["nav"].iloc[-1] / sim_s_gross["nav"].iloc[-1] - 1) * 100

icons = {"ok": "✅ Disponible", "partial": "⚠️ Historique partiel",
         "stale": "⚠️ Données anciennes", "failed": "❌ Exclu"}
cache_lbl = {"hit": "cache", "incremental": "mise à jour", "full": "téléchargé",
             "stale": "cache (hors ligne)", "failed": "-"}
df_weights = pd.DataFrame({
    "Actif": [ticker_names.get(t, t) for t in tickers],
    "Ticker": tickers,
    "Source": [report[t]["source"] for t in tickers],
    "Cache": [cache_lbl[report[t]["cache"]] for t in tickers],
    "Statut": [icons[report[t]["status"]] for t in tickers],
    "Poids cible": [f"{allocation[t] * 100:.1f}%" for t in tickers],
    "Poids rebalancé": [f"{weights[t] * 100:.1f}%" if t in weights.index else "0.0%" for t in tickers],
    "Poids tactique": [f"{w_strat[t] * 100:.1f}%" if t in w_strat.index else "0.0%" for t in tickers],
    "Poids actuel (dérive)": [f"{weights_now[t] * 100:.1f}%" if t in weights_now.index else "0.0%"
                              for t in tickers],
    "Détail": [report[t]["note"] for t in tickers],
})

bench_ret_df = pct(bench_px).fillna(0)
bench_index = normalize_index(
    simulate_portfolio(bench_ret_df, pd.Series(bench_weights_eff), "D", 0.0)["nav"])


# =====================
# FONCTIONS DE CALCUL
# =====================
def calculate_performance(index_series, days):
    if len(index_series) < 2:
        return 0.0
    start_date = index_series.index[-1] - timedelta(days=days)
    if start_date < index_series.index[0]:
        start_date = index_series.index[0]
    start_value = index_series[index_series.index >= start_date].iloc[0]
    end_value = index_series.iloc[-1]
    return (end_value / start_value - 1) * 100


def calculate_volatility(returns_series, periods=252):
    return returns_series.std() * np.sqrt(periods) * 100


def calculate_sharpe_ratio(returns_series, risk_free_rate=RF, periods=252):
    excess_returns = returns_series - (risk_free_rate / periods)
    return (excess_returns.mean() * periods) / (returns_series.std() * np.sqrt(periods))


def calculate_sortino_ratio(returns_series, risk_free_rate=RF, periods=252):
    excess_returns = returns_series - (risk_free_rate / periods)
    downside_returns = returns_series[returns_series < 0]
    downside_std = downside_returns.std() * np.sqrt(periods)
    if downside_std == 0:
        return 0
    return (excess_returns.mean() * periods) / downside_std


def calculate_max_drawdown(index_series):
    cummax = index_series.cummax()
    drawdown = (index_series - cummax) / cummax
    return drawdown.min() * 100


def calculate_calmar_ratio(index_series, returns_series, periods=252):
    annual_return = (index_series.iloc[-1] / index_series.iloc[0] - 1) * (periods / len(index_series))
    max_dd = abs(calculate_max_drawdown(index_series)) / 100
    if max_dd == 0:
        return 0
    return annual_return / max_dd


def calculate_beta(portfolio_returns, benchmark_returns):
    covariance = np.cov(portfolio_returns, benchmark_returns)[0][1]
    variance = np.var(benchmark_returns)
    return covariance / variance if variance != 0 else 0


def calculate_alpha(portfolio_return, benchmark_return, beta, risk_free_rate=RF):
    return portfolio_return - (risk_free_rate + beta * (benchmark_return - risk_free_rate))


port_ret = pct(portfolio_index).dropna()
port_ret_base = pct(portfolio_index_base).dropna()
bench_ret = pct(bench_index).dropna()

ytd_days = (datetime.now() - datetime(datetime.now().year, 1, 1)).days
perf_1y_port = calculate_performance(portfolio_index, 365)
perf_1y_bench = calculate_performance(bench_index, 365)
perf_3y_port = calculate_performance(portfolio_index, 3 * 365)
perf_3y_bench = calculate_performance(bench_index, 3 * 365)
perf_ytd_port = calculate_performance(portfolio_index, ytd_days)
perf_ytd_bench = calculate_performance(bench_index, ytd_days)

vol_port = calculate_volatility(port_ret)
vol_bench = calculate_volatility(bench_ret)
sharpe_port = calculate_sharpe_ratio(port_ret)
sharpe_bench = calculate_sharpe_ratio(bench_ret)
sortino_port = calculate_sortino_ratio(port_ret)
sortino_bench = calculate_sortino_ratio(bench_ret)
mdd_port = calculate_max_drawdown(portfolio_index)
mdd_bench = calculate_max_drawdown(bench_index)

common_dates = port_ret.index.intersection(bench_ret.index)
beta = calculate_beta(port_ret[common_dates].values, bench_ret[common_dates].values)
alpha = calculate_alpha(perf_1y_port / 100, perf_1y_bench / 100, beta) * 100

calmar_port = calculate_calmar_ratio(portfolio_index, port_ret)
calmar_bench = calculate_calmar_ratio(bench_index, bench_ret)


def metrics_row(idx, ret):
    return {
        "Perf depuis le début (%)": (idx.iloc[-1] / idx.iloc[0] - 1) * 100,
        "Perf 1 an (%)": calculate_performance(idx, 365),
        "Volatilité (%)": calculate_volatility(ret),
        "Sharpe": calculate_sharpe_ratio(ret),
        "Max Drawdown (%)": calculate_max_drawdown(idx),
        "Sortino": calculate_sortino_ratio(ret),
        "Calmar": calculate_calmar_ratio(idx, ret),
    }


# =====================================================================
# ONGLET 1 : PERFORMANCE & RISQUE
# =====================================================================
with tab_perf:
    d_perf = perf_1y_port - perf_1y_bench
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Performance 1 an", f"{perf_1y_port:.2f}%", delta=f"{d_perf:+.2f} pts vs benchmark")
    k2.metric("Volatilité annualisée", f"{vol_port:.2f}%", delta=f"{vol_port - vol_bench:+.2f} pts vs benchmark",
              delta_color="inverse")
    k3.metric("Ratio de Sharpe", f"{sharpe_port:.2f}", delta=f"{sharpe_port - sharpe_bench:+.2f} vs benchmark")
    k4.metric("Max Drawdown", f"{mdd_port:.2f}%", delta=f"{mdd_port - mdd_bench:+.2f} pts vs benchmark")

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=portfolio_index.index, y=portfolio_index,
                             name="Portefeuille tactique" if overlay_active or ov_hedge else "Portefeuille",
                             line=dict(width=3, color=NAVY)))
    fig.add_trace(go.Scatter(x=bench_index.index, y=bench_index, name="Benchmark composite",
                             line=dict(width=3, color=MIST)))
    fig.add_trace(go.Scatter(x=portfolio_index_base.index, y=portfolio_index_base,
                             name="Portefeuille de base (sans overlay, non couvert)",
                             line=dict(width=2, dash="dot", color=STEEL)))
    style_fig(fig, 560, hovermode="x unified",
              title=f"Performance cumulée (rebalancement : {rebal_label.lower()}, "
                    f"frais : {cost_bps:g} bps, couverture USD : {ov_hedge}%)")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Comparatif des métriques")
    df_cmp = pd.DataFrame({"Portefeuille tactique": metrics_row(portfolio_index, port_ret),
                           "Portefeuille de base": metrics_row(portfolio_index_base, port_ret_base),
                           "Benchmark": metrics_row(bench_index, bench_ret)})
    st.dataframe(df_cmp.round(2), use_container_width=True)

    st.subheader("Top 5 des lignes sur le dernier mois")
    month_start = prices_eur.index[-1] - timedelta(days=30)
    prices_month = prices_eur[prices_eur.index >= month_start].ffill()
    if len(prices_month) >= 2:
        monthly_perf = ((prices_month.iloc[-1] / prices_month.iloc[0] - 1) * 100).dropna()
        monthly_perf = monthly_perf.sort_values(ascending=False)
        top5 = monthly_perf.head(5)
        col_bar, col_line = st.columns(2)
        top5_labels = [ticker_names.get(t, t) for t in top5.index]
        with col_bar:
            fig_bar = go.Figure(go.Bar(
                x=top5_labels, y=top5.values,
                marker_color=[STEEL if v >= 0 else NEG for v in top5.values],
                text=[f"{v:.2f}%" for v in top5.values], textposition="outside"))
            style_fig(fig_bar, 400, title="Performance mensuelle (%)", yaxis_title="%", showlegend=False)
            st.plotly_chart(fig_bar, use_container_width=True)
        with col_line:
            fig_line = go.Figure()
            for i, ticker in enumerate(top5.index):
                series = prices_month[ticker].dropna()
                if len(series) >= 2:
                    normalized = (series / series.iloc[0]) * 100
                    fig_line.add_trace(go.Scatter(x=normalized.index, y=normalized,
                                                  name=ticker_names.get(ticker, ticker), mode="lines",
                                                  line=dict(width=2, color=SEQ[i % len(SEQ)])))
            style_fig(fig_line, 400, title="Évolution normalisée sur le mois (base 100)", yaxis_title="Base 100")
            st.plotly_chart(fig_line, use_container_width=True)
    st.caption(f"Taux sans risque : {RF * 100:.1f}% (identique dans l'application et dans la factsheet). "
               "Mise à jour automatique toutes les heures.")

# =====================================================================
# ONGLET 3 (colonne droite) : expositions + alertes de financement
# =====================================================================
with col_t_r:
    labels = dict(eq_eu="Actions Europe", eq_us="Actions US", bond="Obligations d'État", re="Immobilier")
    if overlay_active:
        st.success("Écarts effectifs : " + " | ".join(
            f"{labels[k]} {tact['eff'][k] * 100:+.1f} pt" for k in labels) +
            f" | Couverture USD {ov_hedge}%")
    else:
        st.info(f"Aucun écart tactique actif (couverture USD : {ov_hedge}%).")
    if tact["scaled"]:
        st.warning("⚠️ Financement insuffisant : les surpondérations ont été réduites (pas de levier). "
                   "Choisissez « Via les liquidités » ou diminuez les écarts.")
    clipped = [labels[k] for k in labels if abs(tact["eff"][k] - tilts_req[k]) > 1e-9 and not tact["scaled"]]
    if clipped:
        st.info("ℹ️ Écart limité par la taille de la poche existante ou l'absence de proxy : "
                + ", ".join(clipped))
    if proxy_notes:
        st.warning("⚠️ Proxys indisponibles : " + " ; ".join(proxy_notes) +
                   ". Vérifiez OVERLAY_PROXIES dans factsheet_export.py.")

    expo_b = exposure_table(weights, max(0.0, 1.0 - weights.sum()), line_meta, 0.0)
    expo_s = exposure_table(w_strat, tact["cash"], line_meta, ov_hedge / 100)
    df_exp = pd.DataFrame({"Base": expo_b, "Tactique": expo_s})
    df_exp["Écart (pts)"] = df_exp["Tactique"] - df_exp["Base"]

    fig_exp = go.Figure()
    fig_exp.add_trace(go.Bar(x=df_exp.index, y=df_exp["Base"], name="Base", marker_color=MIST))
    fig_exp.add_trace(go.Bar(x=df_exp.index, y=df_exp["Tactique"], name="Tactique", marker_color=NAVY))
    style_fig(fig_exp, 380, barmode="group", yaxis_title="% actif net", title="Expositions : base vs tactique")
    st.plotly_chart(fig_exp, use_container_width=True)
    st.dataframe(df_exp.round(1), use_container_width=True)

# =====================================================================
# ONGLET 4 : QUALITÉ DES DONNÉES & COÛTS
# =====================================================================
with tab_data:
    st.subheader("Suivi des tickers et du cache DuckDB")
    st.dataframe(df_weights, use_container_width=True, hide_index=True)
    st.caption(f"Total investi : {weights.sum() * 100:.1f}% "
               f"(cible {total_target * 100:.1f}%, le solde est conservé en liquidités). "
               f"🗄️ Cache DuckDB : {n_sym} séries, {n_rows:,} cours ({store.path})".replace(",", " "))

    st.subheader("Dérive des poids")
    drift = ((weights_now.reindex(w_strat.index).fillna(0.0) - w_strat) * 100).sort_values()
    fig_drift = go.Figure(go.Bar(
        x=drift.values, y=[ticker_names.get(t, t) for t in drift.index], orientation="h",
        marker_color=[STEEL if v >= 0 else NEG for v in drift.values],
        text=[f"{v:+.2f}" for v in drift.values], textposition="outside"))
    style_fig(fig_drift, max(320, 24 * len(drift) + 120),
              title="Poids actuel (dérive) − poids tactique cible, en points", showlegend=False)
    st.plotly_chart(fig_drift, use_container_width=True)

    st.subheader("Rebalancement et frais (stratégie)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Fréquence", rebal_label)
    c2.metric("Rebalancements", f"{sim_s['n_rebal']}")
    c3.metric("Rotation annualisée", f"{sim_s['turnover_ann'] * 100:.1f}%",
              help="Montants achetés + vendus rapportés à la valeur du portefeuille, annualisés.")
    c4.metric("Impact cumulé des frais", f"{cost_drag:.2f}%",
              help="Valeur nette de frais par rapport à la même stratégie sans frais.")

# =====================================================================
# EXPORT FACTSHEET : injecté dans l'en-tête (popover en haut à droite)
# =====================================================================
with export_slot:
    with st.popover("📄 Factsheet PDF (FR / EN)", use_container_width=True):
        render_factsheet_section(
            portfolio_index, bench_index, port_ret,
            w_strat.to_dict(), TICKER_NAMES_ALL, weights_now, prices_eur_all,
            bench_weights=bench_weights_eff,
            excluded=excluded_names,
            rebal=dict(freq=rebal_code, bps=cost_bps, n=sim_s["n_rebal"],
                       turnover=sim_s["turnover_ann"] * 100, drag=cost_drag),
            overlay=dict(active=overlay_active, hedge=float(ov_hedge),
                         tilts={k: round(v * 100, 2) for k, v in tact["eff"].items()}),
        )
