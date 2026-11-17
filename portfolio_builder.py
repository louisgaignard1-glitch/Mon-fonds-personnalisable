"""portfolio_builder.py : recherche d'actifs, édition de l'allocation et ajout d'un actif (Streamlit)."""
import requests
import streamlit as st

_AC = {"EQUITY": "Equity", "ETF": "ETF", "MUTUALFUND": "Fund", "BOND": "Bond", "CRYPTOCURRENCY": "Crypto"}
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_AC_OPTS = ["Equity", "ETF", "Fund", "Bond", "Crypto", "Autre"]
_CCY_OPTS = ["EUR", "USD", "CHF", "GBP", "Autre"]


# ---------------------------------------------------------------- calcul
def normalize_weights(alloc: dict[str, float], total: float = 1.0) -> dict[str, float]:
    s = sum(alloc.values()) if alloc else 0
    if total <= 0 or s <= 0:
        return {}
    return {k: v / s * total for k, v in alloc.items()}


# ---------------------------------------------------------------- recherche
@st.cache_data(ttl=86400, show_spinner=False)
def _yahoo_raw(query: str, limit: int) -> list[dict]:
    """Lève en cas d'erreur : une erreur n'est donc jamais mise en cache 24 h."""
    r = requests.get("https://query1.finance.yahoo.com/v1/finance/search",
                     params={"q": query, "quotesCount": limit, "newsCount": 0},
                     headers=_UA, timeout=5)
    r.raise_for_status()
    return [{"symbol": q["symbol"].upper(),
             "name": q.get("shortname") or q.get("longname") or q["symbol"],
             "asset_class": _AC.get(q.get("quoteType")),
             "region": None, "currency": None, "isin": q.get("isin")}
            for q in r.json().get("quotes", []) if q.get("symbol")][:limit]


def search_yahoo(query: str, limit: int = 8) -> list[dict]:
    try:
        return _yahoo_raw(query, limit)
    except Exception:
        return []


def combine_search(store, query: str, limit: int = 12) -> list[dict]:
    out, seen = [], set()
    for r in store.search_local(query) + search_yahoo(query):
        s = r["symbol"].upper()
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out[:limit]


# ---------------------------------------------------------------- éditeur d'allocation
def init_alloc_state(default_alloc: dict[str, float], names: dict[str, str]) -> None:
    st.session_state.setdefault("alloc", dict(default_alloc))
    st.session_state.setdefault("alloc_names", dict(names))


def _purge_sliders() -> None:
    for k in [k for k in st.session_state if k.startswith("w_")]:
        del st.session_state[k]


def sync_alloc() -> None:
    """Recopie les valeurs des sliders (w_*) dans st.session_state['alloc']."""
    a = st.session_state["alloc"]
    for s in a:
        if (k := f"w_{s}") in st.session_state:
            a[s] = float(st.session_state[k])


def _normalize() -> None:
    sync_alloc()
    n = normalize_weights(st.session_state["alloc"], 100.0)
    if n:
        st.session_state["alloc"] = {s: round(v, 2) for s, v in n.items()}
        for s, v in st.session_state["alloc"].items():
            st.session_state[f"w_{s}"] = v      # réaligne les sliders (callback : avant instanciation)


def _delete(s: str) -> None:
    st.session_state["alloc"].pop(s, None)
    st.session_state.pop(f"w_{s}", None)


def _clear(step: str) -> None:
    if step == "ask":
        st.session_state["confirm_clear"] = True
    elif step == "ok":
        _purge_sliders()
        st.session_state["alloc"] = {}
        st.session_state["confirm_clear"] = False
    else:
        st.session_state["confirm_clear"] = False


def render_alloc_editor(meta_fn) -> dict[str, float]:
    sync_alloc()
    alloc = st.session_state["alloc"]
    names = st.session_state.get("alloc_names", {})
    tot = sum(alloc.values())
    m1, m2 = st.columns(2)
    m1.metric("Somme des poids", f"{tot:.1f} %", delta=f"{tot - 100:+.1f} % vs 100 %", delta_color="off")
    m2.metric("Liquidités", f"{max(0.0, 100 - tot):.1f} %")

    b1, b2 = st.columns(2)
    b1.button("Normaliser à 100 %", on_click=_normalize, disabled=tot <= 0)
    if st.session_state.get("confirm_clear"):
        c1, c2 = b2.columns(2)
        c1.button("Confirmer", on_click=_clear, args=("ok",), type="primary")
        c2.button("Annuler", on_click=_clear, args=("no",))
    else:
        b2.button("Vider", on_click=_clear, args=("ask",), disabled=not alloc)

    for s, w in sorted(alloc.items(), key=lambda kv: -kv[1]):
        k = f"w_{s}"
        st.session_state.setdefault(k, float(w))
        c = st.columns([4, 5, 1])
        c[0].markdown(f"**{(meta_fn(s) or {}).get('name') or names.get(s) or s}**  \n`{s}`")
        alloc[s] = float(c[1].slider("w", 0.0, 100.0, step=0.5, key=k, label_visibility="collapsed"))
        c[2].button("🗑", key=f"del_{s}", on_click=_delete, args=(s,))
    return dict(alloc)


# ---------------------------------------------------------------- ajout d'un actif
def _add(store, chk, symbol, name, **kw):
    s = symbol.strip().upper()
    if not s:
        return "error", "Ticker requis."
    if s in st.session_state["alloc"]:
        return "warning", f"{s} est déjà dans le portefeuille."
    ok, why = chk(s)
    if not ok:
        return "error", f"{s} : {why}"
    name = name.strip() or s
    store.upsert_asset(s, name, **kw)
    st.session_state["alloc"][s] = 5.0
    st.session_state.setdefault("alloc_names", {})[s] = name
    st.session_state[f"w_{s}"] = 5.0
    return "success", f"{s} ajouté au portefeuille."


def _on_search_add(store, chk):
    res, i = st.session_state.get("search_results", []), st.session_state.get("sel")
    if i is None or not 0 <= i < len(res):
        return
    r = res[i]
    lvl, msg = _add(store, chk, r["symbol"], r["name"], asset_class=r.get("asset_class"),
                    region=r.get("region"), currency=r.get("currency"), isin=r.get("isin"))
    st.session_state["_flash"] = (lvl, msg)
    if lvl == "success":
        st.session_state["q"] = ""
        st.session_state["search_results"] = []
        st.session_state.pop("sel", None)


def _on_manual_add(store, chk):
    g = st.session_state.get
    lvl, msg = _add(store, chk, g("m_t", ""), g("m_n", ""),
                    asset_class=None if g("m_c") == "Autre" else g("m_c"),
                    currency=None if g("m_d") == "Autre" else g("m_d"),
                    sector=(g("m_s", "").strip() or None),
                    isin=(g("m_i", "").strip().upper() or None))
    st.session_state["_flash"] = (lvl, msg)
    if lvl == "success":
        st.session_state.update(m_t="", m_n="", m_c="Equity", m_d="EUR", m_s="", m_i="")


def add_asset_ui(store, check_symbol) -> None:
    lvl, msg = st.session_state.pop("_flash", (None, None))
    if lvl:
        getattr(st, lvl)(msg)

    q = st.text_input("Rechercher un actif (nom, ticker, ISIN)", key="q").strip()
    if len(q) < 2:
        st.caption("Saisir au moins 2 caractères.")
        st.session_state["search_results"] = []
    else:
        st.session_state["search_results"] = combine_search(store, q)
    res = st.session_state["search_results"]

    if res:
        st.radio("Résultats", range(len(res)), index=None, key="sel",
                 format_func=lambda i: f"{res[i]['name']} — {res[i]['symbol']} "
                                       f"({res[i].get('asset_class') or 'n/d'})")
    elif len(q) >= 2:
        st.caption("Aucun résultat.")

    st.button("Ajouter au portefeuille", disabled=st.session_state.get("sel") is None or not res,
              on_click=_on_search_add, args=(store, check_symbol))

    with st.expander("Ajout manuel"):
        with st.form("manual_add", border=False):
            st.text_input("Ticker", key="m_t")
            st.text_input("Nom", key="m_n")
            st.selectbox("Classe d'actifs", _AC_OPTS, key="m_c")
            st.selectbox("Devise", _CCY_OPTS, key="m_d")
            st.text_input("Secteur (optionnel)", key="m_s")
            st.text_input("ISIN (optionnel)", key="m_i")
            st.form_submit_button("Ajouter", on_click=_on_manual_add, args=(store, check_symbol))
