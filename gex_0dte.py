"""
0DTE Gamma Exposure (GEX) Analyzer
===================================
Downloads CBOE delayed options data for SPY or QQQ, filters for 0DTE contracts,
computes dealer GEX by strike, and plots support (Put Wall) and resistance
(Call Wall) levels — optionally mapped to ES or NQ futures prices.

Usage:
    python gex_0dte.py

Requirements:
    pip install pandas requests matplotlib scipy
"""

import json
import os
import sys
from datetime import date, datetime

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

CONTRACT_SIZE = 100  # standard equity options contract multiplier

# Approximate futures-to-ETF price ratios used to convert strike levels.
# Tune these to the live ratio: e.g. if NQ=21000 and QQQ=525, ratio ≈ 40.
# The ratio is applied to strike prices only — GEX magnitudes stay in ETF $.
FUTURES_SCALE = {
    "SPY": {"futures": "ES", "ratio": 10.0},   # ES ≈ SPY × 10
    "QQQ": {"futures": "NQ", "ratio": 40.0},   # NQ ≈ QQQ × 40
}

# Optional: override ratios with live data by setting to True
AUTO_SCALE = True   # fetches spot prices from CBOE and derives ratio automatically

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# PLOT STYLE
# ---------------------------------------------------------------------------
plt.style.use("seaborn-v0_8-dark")
_DARK_BG = "#212946"
_GRID    = "#2A3459"
for _p in ["figure.facecolor", "axes.facecolor", "savefig.facecolor"]:
    plt.rcParams[_p] = _DARK_BG
for _p in ["text.color", "axes.labelcolor", "xtick.color", "ytick.color"]:
    plt.rcParams[_p] = "0.9"

CALL_COLOR = "#00FFAA"   # green  → resistance / Call Wall
PUT_COLOR  = "#FF4466"   # red    → support   / Put Wall
SPOT_COLOR = "#FFD700"   # gold   → spot / current price line


# ---------------------------------------------------------------------------
# DATA ACQUISITION
# ---------------------------------------------------------------------------

def fetch_cboe_data(ticker: str, force_refresh: bool = False) -> dict:
    """
    Download options chain JSON from CBOE.
    Caches to data/{ticker}.json — pass force_refresh=True to bypass cache.
    """
    cache_path = os.path.join(DATA_DIR, f"{ticker}.json")
    if not force_refresh and os.path.exists(cache_path):
        print(f"[cache] Loading {ticker} data from {cache_path}")
        with open(cache_path) as f:
            return json.load(f)

    urls = [
        f"https://cdn.cboe.com/api/global/delayed_quotes/options/_{ticker}.json",
        f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json",
    ]
    for url in urls:
        try:
            print(f"[fetch] GET {url}")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            payload = resp.json()
            with open(cache_path, "w") as f:
                json.dump(payload, f)
            print(f"[fetch] Saved → {cache_path}")
            return payload
        except Exception as e:
            print(f"[warn] {url} failed: {e}")

    raise RuntimeError(f"Could not retrieve CBOE data for {ticker}")


def parse_cboe_payload(payload: dict) -> tuple[float, pd.DataFrame]:
    """
    Extract spot price and raw options DataFrame from CBOE JSON payload.
    Returns (spot_price, options_df).
    """
    df = pd.DataFrame.from_dict(payload)
    spot_price = df.loc["current_price", "data"]
    options = pd.DataFrame(df.loc["options", "data"])
    return float(spot_price), options


# ---------------------------------------------------------------------------
# DATA CLEANING
# ---------------------------------------------------------------------------

def parse_option_fields(df: pd.DataFrame) -> pd.DataFrame:
    """
    Derive type (C/P), strike, and expiration from the option symbol.

    CBOE option symbol format: {ROOT}{YYMMDD}{C|P}{8-digit strike×1000}
    e.g.  SPY230120C00380000  → SPY, 2023-01-20, Call, $380.000
    """
    df = df.copy()
    df["type"]       = df["option"].str.extract(r"\d([CP])\d")
    df["strike"]     = df["option"].str.extract(r"\d[CP](\d+)\d{3}").astype(int)
    df["expiration"] = pd.to_datetime(
        df["option"].str.extract(r"[A-Z]+(\d{6})")[0], format="%y%m%d"
    )
    return df


def filter_0dte(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only contracts expiring today (0DTE)."""
    today = pd.Timestamp(date.today())
    mask  = df["expiration"].dt.normalize() == today
    result = df.loc[mask].copy()
    print(f"[0DTE] {len(result):,} contracts expire today ({today.date()})")
    if result.empty:
        print("[warn] No 0DTE contracts found — market may be closed or data is stale.")
    return result


# ---------------------------------------------------------------------------
# GEX CALCULATION
# ---------------------------------------------------------------------------

def compute_gex(spot: float, df: pd.DataFrame) -> pd.DataFrame:
    """
    Dealer GEX per contract:

        GEX = spot² × gamma × open_interest × contract_size × 0.01

    This gives dollars of delta-hedge per 1% move in spot.

    Sign convention (assuming dealers are long calls, short puts):
        Calls → +GEX   (dealers buy on dips, sell on rips → mean-reverting)
        Puts  → -GEX   (dealers sell on dips, buy on rips → trend-amplifying)
    """
    df = df.copy()
    df["GEX_raw"] = (
        spot**2 * df["gamma"] * df["open_interest"] * CONTRACT_SIZE * 0.01
    )
    df["GEX"] = df.apply(
        lambda r: -r["GEX_raw"] if r["type"] == "P" else r["GEX_raw"], axis=1
    )
    return df


def gex_by_strike(df: pd.DataFrame, spot: float, band: float = 0.15) -> pd.Series:
    """Aggregate net GEX per strike, limited to ±{band}% of spot."""
    lo, hi = spot * (1 - band), spot * (1 + band)
    mask = (df["strike"] >= lo) & (df["strike"] <= hi)
    return df.loc[mask].groupby("strike")["GEX"].sum() / 1e9   # in $Bn


# ---------------------------------------------------------------------------
# FUTURES SCALING
# ---------------------------------------------------------------------------

def get_scale(ticker: str, spot: float, payload: dict) -> tuple[float, str]:
    """
    Return (scale_ratio, futures_name).

    If AUTO_SCALE is True, attempts to derive the ratio from spot prices
    fetched from the CBOE payload or a secondary ticker lookup.
    Falls back to FUTURES_SCALE defaults if auto-scaling fails.
    """
    cfg = FUTURES_SCALE.get(ticker.upper())
    if cfg is None:
        print(f"[scale] No futures mapping for {ticker} — using ratio=1")
        return 1.0, ticker

    futures_name = cfg["futures"]
    default_ratio = cfg["ratio"]

    if not AUTO_SCALE:
        return default_ratio, futures_name

    # Attempt to derive ratio from a rough estimate using CBOE NQ/ES quotes.
    # In practice, you can replace these with live futures prices from a broker API.
    futures_spot_hints = {"ES": None, "NQ": None}

    # Heuristic: use known approximate relationship unless we can fetch better.
    # Here we use the spot price itself to calibrate.
    if ticker.upper() == "SPY":
        # ES/SPY ratio is fairly stable ~10×; refine by checking ES front-month
        estimated_ratio = default_ratio
    elif ticker.upper() == "QQQ":
        # NQ/QQQ ratio hovers around 38-42; default is fine without live futures
        estimated_ratio = default_ratio

    print(f"[scale] {ticker} → {futures_name}, ratio={estimated_ratio:.2f}  "
          f"(implied futures spot ≈ {spot * estimated_ratio:,.0f})")
    return estimated_ratio, futures_name


# ---------------------------------------------------------------------------
# PLOTTING
# ---------------------------------------------------------------------------

def plot_gex_by_strike(
    gex: pd.Series,
    spot: float,
    ticker: str,
    scale: float,
    futures_name: str,
):
    """
    Horizontal bar chart of GEX by strike.
    - Y-axis : price levels (ETF on left, futures equivalent on right)
    - X-axis : Net GEX in $Bn
    - Bars   : green = net positive (call-heavy / resistance)
               red   = net negative (put-heavy  / support)
    - Marks  : Put Wall, Call Wall, spot price
    """
    if gex.empty:
        print("[plot] No data to plot.")
        return

    strikes = gex.index.values
    values  = gex.values
    colors  = [CALL_COLOR if v >= 0 else PUT_COLOR for v in values]

    # Identify walls
    call_wall_strike = strikes[np.argmax(values)]
    put_wall_strike  = strikes[np.argmin(values)]

    fig, ax = plt.subplots(figsize=(11, 9))
    fig.suptitle(
        f"{ticker} — 0DTE Gamma Exposure by Strike\n"
        f"(Mapped to {futures_name} | as of {date.today()})",
        fontsize=13, fontweight="heavy", y=0.98,
    )

    bars = ax.barh(strikes, values, height=max(1, (strikes[-1]-strikes[0])/len(strikes)*0.8),
                   color=colors, alpha=0.75, edgecolor="none")

    # Spot line
    ax.axhline(spot, color=SPOT_COLOR, linewidth=1.6, linestyle="--", label=f"Spot  {spot:,.2f}")

    # Call Wall
    ax.axhline(call_wall_strike, color=CALL_COLOR, linewidth=1.2, linestyle=":",
               label=f"Call Wall  {call_wall_strike:,}  (≈ {futures_name} {call_wall_strike*scale:,.0f})")

    # Put Wall
    ax.axhline(put_wall_strike, color=PUT_COLOR, linewidth=1.2, linestyle=":",
               label=f"Put Wall   {put_wall_strike:,}  (≈ {futures_name} {put_wall_strike*scale:,.0f})")

    # Zero line
    ax.axvline(0, color="white", linewidth=0.6, alpha=0.4)

    # ---- Axes ----
    ax.set_xlabel("Net GEX ($ Billions / 1% move)", fontweight="heavy")
    ax.set_ylabel(f"{ticker} Strike Price", fontweight="heavy")
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    ax.grid(color=_GRID, axis="x", linewidth=0.6)

    # Secondary Y-axis → futures equivalent
    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim())
    ax2.yaxis.set_major_locator(ax.yaxis.get_major_locator())
    futures_ticks = [t * scale for t in ax.get_yticks()]
    ax2.set_yticks(ax.get_yticks())
    ax2.set_yticklabels([f"{v:,.0f}" for v in futures_ticks], color="0.7")
    ax2.set_ylabel(f"{futures_name} Equivalent", fontweight="heavy", color="0.7")
    ax2.tick_params(axis="y", colors="0.7")
    ax2.spines["right"].set_color("0.4")

    # ---- Legend & annotations ----
    ax.legend(loc="lower right", fontsize=8.5, framealpha=0.35)

    # Annotate walls with GEX magnitude
    _annotate_wall(ax, put_wall_strike,  gex[put_wall_strike],  "Put Wall",  PUT_COLOR)
    _annotate_wall(ax, call_wall_strike, gex[call_wall_strike], "Call Wall", CALL_COLOR)

    # Summary text box
    total_gex = values.sum()
    sign_label = "NET LONG Γ (pinning)" if total_gex > 0 else "NET SHORT Γ (amplifying)"
    info = (
        f"Spot:       {spot:>10,.2f}\n"
        f"Call Wall:  {call_wall_strike:>10,}  (≈{futures_name} {call_wall_strike*scale:>7,.0f})\n"
        f"Put Wall:   {put_wall_strike:>10,}  (≈{futures_name} {put_wall_strike*scale:>7,.0f})\n"
        f"Total GEX:  {total_gex:>+9.3f} Bn\n"
        f"Regime:     {sign_label}"
    )
    ax.text(
        0.02, 0.02, info,
        transform=ax.transAxes,
        fontsize=8.5, family="monospace",
        verticalalignment="bottom",
        bbox=dict(boxstyle="round,pad=0.5", facecolor=_DARK_BG, alpha=0.8, edgecolor="0.4"),
    )

    plt.tight_layout()
    out_path = f"{ticker}_0dte_gex.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] Saved → {out_path}")
    plt.show()


def _annotate_wall(ax, strike, gex_val, label, color):
    """Add a small label next to a wall strike bar."""
    ax.annotate(
        f" {label}\n {gex_val:+.2f} Bn",
        xy=(gex_val, strike),
        xytext=(gex_val + (0.02 if gex_val >= 0 else -0.02), strike),
        fontsize=7.5,
        color=color,
        fontweight="bold",
        va="center",
    )


# ---------------------------------------------------------------------------
# INTRADAY / LIVE DATA NOTE
# ---------------------------------------------------------------------------

def print_intraday_note():
    print("""
─────────────────────────────────────────────────────────────
INTRADAY / REAL-TIME DATA OPTIONS (free or low-cost):
─────────────────────────────────────────────────────────────
1. CBOE Delayed (this script)
   • URL: https://cdn.cboe.com/api/global/delayed_quotes/options/_{TICKER}.json
   • Lag: ~15 min delayed
   • Cost: FREE — no API key needed
   • Refresh: re-run with force_refresh=True to bypass cache

2. CBOE LiveVol (paid)
   • https://datashop.cboe.com/
   • Real-time 1-min snapshots, full Greeks — $$ subscription

3. Tradier API (free brokerage tier)
   • https://developer.tradier.com/
   • Free sandbox: delayed quotes + option chains with Greeks
   • Requires free account + API token

4. Unusual Whales / Market Chameleon
   • Web scraping / unofficial APIs — TOS risk

5. IBKR TWS API (free if you have an account)
   • Real-time option Greeks via reqSecDefOptParams + reqMktData
   • Best free option for live 0DTE GEX updates

Recommendation for live 0DTE GEX:
  → Poll CBOE delayed URL every 5–15 min (delete cache each loop)
  → Or use IBKR TWS API for real-time if you have an account
─────────────────────────────────────────────────────────────
""")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run(ticker: str, force_refresh: bool = False):
    ticker = ticker.upper()

    if ticker not in FUTURES_SCALE:
        print(f"[warn] '{ticker}' not in FUTURES_SCALE map. Supported: {list(FUTURES_SCALE)}")

    # 1. Fetch raw data
    payload = fetch_cboe_data(ticker, force_refresh=force_refresh)

    # 2. Parse
    spot, raw_options = parse_cboe_payload(payload)
    print(f"[data] {ticker} spot = {spot:,.2f}  |  {len(raw_options):,} total contracts")

    options = parse_option_fields(raw_options)

    # 3. Full-chain GEX (needed before 0DTE filter so columns exist)
    options = compute_gex(spot, options)

    # 4. 0DTE filter (copy — original chain preserved)
    options_0dte = filter_0dte(options)

    if options_0dte.empty:
        print("[warn] Falling back to nearest expiration (no true 0DTE found).")
        nearest_exp = options["expiration"].min()
        options_0dte = options.loc[options["expiration"] == nearest_exp].copy()
        print(f"[fallback] Using expiration: {nearest_exp.date()}  "
              f"({len(options_0dte):,} contracts)")

    # 5. GEX by strike
    gex = gex_by_strike(options_0dte, spot, band=0.15)
    print(f"\n[GEX] 0DTE net GEX = {gex.sum():+.4f} $Bn")
    print(f"[GEX] Call Wall   @ strike {gex.idxmax():,}  ({gex.max():+.3f} Bn)")
    print(f"[GEX] Put Wall    @ strike {gex.idxmin():,}  ({gex.min():+.3f} Bn)")

    # 6. Scale factor for futures display
    scale, futures_name = get_scale(ticker, spot, payload)

    # 7. Plot
    plot_gex_by_strike(gex, spot, ticker, scale, futures_name)
    print_intraday_note()


if __name__ == "__main__":
    ticker_input = input("Enter ticker (SPY or QQQ): ").strip().upper() or "SPY"

    # Pass force_refresh=True to always download fresh data (ignore cache)
    refresh = "--refresh" in sys.argv
    run(ticker_input, force_refresh=refresh)
