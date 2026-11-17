"""asset_store.py : persistance DuckDB des actifs et des profils de portefeuille."""
import json
import threading
import unicodedata
from typing import Protocol

import duckdb

DDL_ASSETS = """CREATE TABLE IF NOT EXISTS assets (
  symbol VARCHAR PRIMARY KEY,
  name VARCHAR NOT NULL,
  asset_class VARCHAR,
  region VARCHAR,
  sector VARCHAR,
  currency VARCHAR,
  isin VARCHAR,
  active BOOLEAN DEFAULT TRUE,
  added_at TIMESTAMP DEFAULT now())"""
DDL_PORTFOLIOS = """CREATE TABLE IF NOT EXISTS portfolios (
  name VARCHAR PRIMARY KEY,
  allocation_json VARCHAR NOT NULL,
  updated_at TIMESTAMP DEFAULT now())"""

# Migration initiale : volontairement vide. Les 18 lignes historiques restent servies par
# factsheet_export.line_meta (repli en dur) : la factsheet reste ainsi strictement identique.
SEED: dict[str, dict] = {}

_COLS = ("symbol", "name", "asset_class", "region", "sector", "currency", "isin")


def _norm(s) -> str:
    """Minuscules, sans accents, sans espaces de bord."""
    nfd = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in nfd if not unicodedata.combining(c)).casefold().strip()


class PortfolioBackend(Protocol):
    """Interface de persistance des profils : un autre backend s'y branche sans toucher l'UI."""

    def save_portfolio(self, name: str, alloc: dict[str, float]) -> None: ...
    def load_portfolio(self, name: str) -> dict[str, float] | None: ...
    def delete_portfolio(self, name: str) -> None: ...
    def list_portfolios(self) -> list[str]: ...


class AssetStore:
    """Implémentation DuckDB. Accepte une connexion et un verrou existants (partage avec PriceStore)."""

    def __init__(self, path: str = ":memory:", seed: dict | None = None, con=None, lock=None):
        self._lock = lock or threading.RLock()
        self._con = con if con is not None else duckdb.connect(path)
        self._seed = SEED if seed is None else seed
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self._lock:
            self._con.execute(DDL_ASSETS)
            self._con.execute(DDL_PORTFOLIOS)
            if self._seed and self._con.execute("SELECT count(*) FROM assets").fetchone()[0] == 0:
                self._con.executemany(
                    "INSERT INTO assets (symbol,name,asset_class,region,sector,currency,isin) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [(s, d["name"], d.get("asset_class"), d.get("region"), d.get("sector"),
                      d.get("currency"), d.get("isin")) for s, d in self._seed.items()])

    def upsert_asset(self, symbol: str, name: str, **kw) -> None:
        row = (symbol.strip().upper(), name, kw.get("asset_class"), kw.get("region"), kw.get("sector"),
               kw.get("currency"), kw.get("isin"), kw.get("active", True))
        keep = ",".join(f"{c}=coalesce(excluded.{c}, assets.{c})"
                        for c in ("asset_class", "region", "sector", "currency", "isin"))
        with self._lock:
            self._con.execute(
                "INSERT INTO assets (symbol,name,asset_class,region,sector,currency,isin,active) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (symbol) DO UPDATE SET "
                f"name=excluded.name,{keep},active=excluded.active", row)

    def all_assets(self) -> dict[str, dict]:
        with self._lock:
            cur = self._con.execute(
                "SELECT symbol,name,asset_class,region,sector,currency,isin FROM assets WHERE active")
            return {r[0]: dict(zip(_COLS, r)) for r in cur.fetchall()}

    def search_local(self, query: str, limit: int = 20) -> list[dict]:
        n = _norm(query)
        if len(n) < 2:
            return []
        with self._lock:
            cur = self._con.execute(
                "SELECT symbol,name,asset_class,region,sector,currency,isin FROM assets "
                "WHERE active AND (strip_accents(symbol) ILIKE ? OR strip_accents(name) ILIKE ? "
                "OR strip_accents(coalesce(isin,'')) ILIKE ?)", [f"%{n}%"] * 3)
            rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]
        ranked = []
        for r in rows:
            sym, nm, isin = _norm(r["symbol"]), _norm(r["name"]), _norm(r["isin"])
            if sym == n:
                rank = 0
            elif sym.startswith(n) or nm.startswith(n):
                rank = 1
            elif n in nm or n in isin:
                rank = 2
            else:
                continue
            ranked.append((rank, nm, r))
        ranked.sort(key=lambda t: (t[0], t[1]))
        return [r for _, _, r in ranked[:limit]]

    def save_portfolio(self, name: str, alloc: dict[str, float]) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO portfolios (name,allocation_json) VALUES (?,?) ON CONFLICT (name) "
                "DO UPDATE SET allocation_json=excluded.allocation_json, updated_at=now()",
                [name, json.dumps(alloc)])

    def load_portfolio(self, name: str) -> dict[str, float] | None:
        with self._lock:
            r = self._con.execute("SELECT allocation_json FROM portfolios WHERE name=?", [name]).fetchone()
        return json.loads(r[0]) if r else None

    def delete_portfolio(self, name: str) -> None:
        with self._lock:
            self._con.execute("DELETE FROM portfolios WHERE name=?", [name])

    def list_portfolios(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._con.execute("SELECT name FROM portfolios ORDER BY name").fetchall()]
