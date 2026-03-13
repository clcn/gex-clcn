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
    df["strike"]     = df["option"].str.extract(r"\d[CP](\d{8})").astype(int) / 1000
    df["expiration"] = pd.to_datetime(
        df["option"].str.extract(r"[A-Z]+(\d{6})")[0], format="%y%m%d"
    )
    return df


def filter_by_dte(df: pd.DataFrame, dte_list: list[int] = [0]) -> pd.DataFrame:
    """
    Keep only contracts whose expiration is within the specified DTE values.
    e.g. dte_list=[0, 1] includes both 0DTE (today) and 1DTE (tomorrow).
    """
    today = pd.Timestamp(date.today()).normalize()
    target_dates = {today + pd.Timedelta(days=d) for d in dte_list}
    mask = df["expiration"].dt.normalize().isin(target_dates)
    result = df.loc[mask].copy()

    # Report counts per expiration for transparency
    for d in sorted(dte_list):
        exp_date = today + pd.Timedelta(days=d)
        count = mask[df["expiration"].dt.normalize() == exp_date].sum()
        label = f"{d}DTE ({exp_date.date()})"
        print(f"[filter] {label}: {count:,} contracts")

    if result.empty:
        print("[warn] No contracts found for DTE filter — market may be closed or data is stale.")
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
# EXPECTED MOVE
# ---------------------------------------------------------------------------

def compute_expected_move(spot: float, df: pd.DataFrame) -> dict:
    """
    Derive the market-implied daily Expected Move (±1 SD) two ways:

    1. ATM Straddle price  — most direct: EM = ATM call mid + ATM put mid
    2. IV-derived          — EM = spot × ATM_IV × √(1/252)

    Returns a dict with both estimates plus the ATM strike and IV used.
    Filters to OI > 50 to avoid using thinly-quoted contracts.
    """
    # Midpoint price proxy (use 'last' if bid/ask unavailable)
    for col in ["bid", "ask"]:
        if col not in df.columns:
            df = df.copy()
            df[col] = np.nan

    df = df.copy()
    df["mid"] = np.where(
        df["bid"].notna() & df["ask"].notna() & (df["bid"] > 0),
        (df["bid"] + df["ask"]) / 2,
        df["last"],
    )

    # ATM = strike closest to spot, with meaningful OI
    liquid = df[df["open_interest"] > 50].copy()
    if liquid.empty:
        liquid = df.copy()

    liquid["dist"] = (liquid["strike"] - spot).abs()
    atm_strike = liquid.loc[liquid["dist"].idxmin(), "strike"]

    atm = liquid[liquid["strike"] == atm_strike]
    atm_call = atm[atm["type"] == "C"]
    atm_put  = atm[atm["type"] == "P"]

    # Straddle-derived EM
    call_mid = atm_call["mid"].values[0] if len(atm_call) else np.nan
    put_mid  = atm_put["mid"].values[0]  if len(atm_put)  else np.nan
    em_straddle = call_mid + put_mid if not (np.isnan(call_mid) or np.isnan(put_mid)) else np.nan

    # IV-derived EM  (use average of ATM call/put IV)
    call_iv = atm_call["iv"].values[0] if (len(atm_call) and "iv" in atm_call.columns) else np.nan
    put_iv  = atm_put["iv"].values[0]  if (len(atm_put)  and "iv" in atm_put.columns)  else np.nan
    atm_iv  = np.nanmean([call_iv, put_iv])
    em_iv   = spot * atm_iv * np.sqrt(1 / 252) if not np.isnan(atm_iv) else np.nan

    # Prefer straddle price; fall back to IV-derived
    em = em_straddle if not np.isnan(em_straddle) else em_iv
    method = "ATM straddle" if not np.isnan(em_straddle) else "IV-derived"

    result = {
        "em":         em,
        "em_high":    spot + em  if em else np.nan,
        "em_low":     spot - em  if em else np.nan,
        "em_iv":      em_iv,
        "em_straddle": em_straddle,
        "atm_strike": atm_strike,
        "atm_iv":     atm_iv,
        "method":     method,
    }

    print(f"[EM]  ATM strike={atm_strike:.2f}  IV={atm_iv:.1%}  "
          f"EM={em:.2f} ({method})  → range [{spot-em:.2f}, {spot+em:.2f}]")
    return result


# ---------------------------------------------------------------------------
# OI-WEIGHTED IV BY STRIKE
# ---------------------------------------------------------------------------

def oi_iv_by_strike(
    df: pd.DataFrame,
    spot: float,
    band: float = 0.05,
    min_oi: int = 100,
) -> pd.DataFrame:
    """
    Compute OI-weighted implied volatility per strike, split by calls and puts.
    Limited to ±{band}% from spot and OI > {min_oi} to filter illiquid strikes.

    Returns a DataFrame indexed by strike with columns [call_oiiv, put_oiiv].
    """
    if "iv" not in df.columns:
        print("[warn] IV column not found — skipping OI×IV calculation.")
        return pd.DataFrame()

    lo, hi = spot * (1 - band), spot * (1 + band)
    mask = (
        (df["strike"] >= lo) &
        (df["strike"] <= hi) &
        (df["open_interest"] >= min_oi) &
        df["iv"].notna() &
        (df["iv"] > 0)
    )
    filtered = df.loc[mask].copy()

    if filtered.empty:
        print("[warn] No liquid strikes found for OI×IV calculation.")
        return pd.DataFrame()

    def weighted_iv(g):
        total_oi = g["open_interest"].sum()
        return (g["iv"] * g["open_interest"]).sum() / total_oi if total_oi > 0 else np.nan

    calls = filtered[filtered["type"] == "C"].groupby("strike").apply(weighted_iv).rename("call_oiiv")
    puts  = filtered[filtered["type"] == "P"].groupby("strike").apply(weighted_iv).rename("put_oiiv")

    result = pd.concat([calls, puts], axis=1).sort_index()
    return result


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
    dte_label: str = "0DTE",
    em: dict = None,
    oiiv: pd.DataFrame = None,
):
    """
    Two-panel horizontal chart sharing the price Y-axis.

    Left panel (wider)  — GEX bars + EM bands + spot/wall lines
    Right panel         — OI-weighted IV by strike (calls / puts)
    """
    if gex.empty:
        print("[plot] No data to plot.")
        return

    has_oiiv = oiiv is not None and not oiiv.empty

    # ---- Figure layout ----
    width_ratios = [3, 1.2] if has_oiiv else [1]
    ncols = 2 if has_oiiv else 1
    fig, axes = plt.subplots(
        1, ncols, figsize=(14 if has_oiiv else 11, 10),
        sharey=True,
        gridspec_kw={"width_ratios": width_ratios, "wspace": 0.06},
    )
    ax = axes[0] if has_oiiv else axes
    ax_iv = axes[1] if has_oiiv else None

    fig.suptitle(
        f"{ticker} — {dte_label} Gamma Exposure & IV Concentration by Strike\n"
        f"(Mapped to {futures_name} | as of {date.today()})",
        fontsize=13, fontweight="heavy", y=0.99,
    )

    # ================================================================
    # LEFT PANEL — GEX
    # ================================================================
    strikes = gex.index.values
    values  = gex.values
    colors  = [CALL_COLOR if v >= 0 else PUT_COLOR for v in values]
    bar_h   = max(0.5, (strikes[-1] - strikes[0]) / len(strikes) * 0.8)

    call_wall_strike = strikes[np.argmax(values)]
    put_wall_strike  = strikes[np.argmin(values)]

    ax.barh(strikes, values, height=bar_h, color=colors, alpha=0.75, edgecolor="none")

    # EM shaded band
    EM_COLOR = "#8888FF"
    if em and not np.isnan(em.get("em", np.nan)):
        ax.axhspan(em["em_low"], em["em_high"],
                   color=EM_COLOR, alpha=0.10, label=f"EM range ±{em['em']:.2f}")
        ax.axhline(em["em_high"], color=EM_COLOR, linewidth=0.9,
                   linestyle=(0, (4, 4)), alpha=0.7,
                   label=f"EM High  {em['em_high']:,.2f}  (≈{futures_name} {em['em_high']*scale:,.0f})")
        ax.axhline(em["em_low"],  color=EM_COLOR, linewidth=0.9,
                   linestyle=(0, (4, 4)), alpha=0.7,
                   label=f"EM Low   {em['em_low']:,.2f}  (≈{futures_name} {em['em_low']*scale:,.0f})")

    # Spot
    ax.axhline(spot, color=SPOT_COLOR, linewidth=1.6, linestyle="--",
               label=f"Spot  {spot:,.2f}")

    # Walls
    ax.axhline(call_wall_strike, color=CALL_COLOR, linewidth=1.2, linestyle=":",
               label=f"Call Wall  {call_wall_strike:,.2f}  (≈{futures_name} {call_wall_strike*scale:,.0f})")
    ax.axhline(put_wall_strike, color=PUT_COLOR, linewidth=1.2, linestyle=":",
               label=f"Put Wall   {put_wall_strike:,.2f}  (≈{futures_name} {put_wall_strike*scale:,.0f})")

    ax.axvline(0, color="white", linewidth=0.6, alpha=0.4)

    ax.set_xlabel("Net GEX ($ Billions / 1% move)", fontweight="heavy")
    ax.set_ylabel(f"{ticker} Strike Price", fontweight="heavy")
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.2f}"))
    ax.grid(color=_GRID, axis="x", linewidth=0.6)
    ax.legend(loc="lower right", fontsize=7.8, framealpha=0.35)

    _annotate_wall(ax, put_wall_strike,  gex[put_wall_strike],  "Put Wall",  PUT_COLOR)
    _annotate_wall(ax, call_wall_strike, gex[call_wall_strike], "Call Wall", CALL_COLOR)

    # Summary text box
    total_gex  = values.sum()
    sign_label = "NET LONG Γ (pinning)" if total_gex > 0 else "NET SHORT Γ (amplifying)"
    em_line = (
        f"EM ±{em['em']:,.2f}  [{em['em_low']:,.2f}–{em['em_high']:,.2f}]\n"
        f"ATM IV:     {em['atm_iv']:.1%}  ({em['method']})\n"
    ) if (em and not np.isnan(em.get("em", np.nan))) else ""
    info = (
        f"Scope:      {dte_label}\n"
        f"Spot:       {spot:,.2f}\n"
        f"Call Wall:  {call_wall_strike:,.2f}  (≈{futures_name} {call_wall_strike*scale:,.0f})\n"
        f"Put Wall:   {put_wall_strike:,.2f}  (≈{futures_name} {put_wall_strike*scale:,.0f})\n"
        + em_line +
        f"Total GEX:  {total_gex:+.3f} Bn\n"
        f"Regime:     {sign_label}"
    )
    ax.text(
        0.02, 0.02, info,
        transform=ax.transAxes, fontsize=8, family="monospace",
        verticalalignment="bottom",
        bbox=dict(boxstyle="round,pad=0.5", facecolor=_DARK_BG, alpha=0.85, edgecolor="0.4"),
    )

    # Secondary Y-axis (futures) on left panel
    ax2 = ax.twinx() if not has_oiiv else None
    if ax2:
        ax2.set_ylim(ax.get_ylim())
        ax2.set_yticks(ax.get_yticks())
        ax2.set_yticklabels([f"{t*scale:,.0f}" for t in ax.get_yticks()], color="0.7")
        ax2.set_ylabel(f"{futures_name} Equivalent", fontweight="heavy", color="0.7")
        ax2.tick_params(axis="y", colors="0.7")
        ax2.spines["right"].set_color("0.4")

    # ================================================================
    # RIGHT PANEL — OI-Weighted IV
    # ================================================================
    if has_oiiv and ax_iv is not None:
        iv_strikes = oiiv.index.values

        if "call_oiiv" in oiiv.columns:
            ax_iv.barh(iv_strikes, oiiv["call_oiiv"] * 100,
                       height=bar_h, color=CALL_COLOR, alpha=0.65,
                       label="Call OI×IV", edgecolor="none")
        if "put_oiiv" in oiiv.columns:
            ax_iv.barh(iv_strikes, -oiiv["put_oiiv"] * 100,
                       height=bar_h, color=PUT_COLOR, alpha=0.65,
                       label="Put OI×IV (−)", edgecolor="none")

        # Mirror spot and wall lines
        ax_iv.axhline(spot,             color=SPOT_COLOR,  linewidth=1.4, linestyle="--", alpha=0.6)
        ax_iv.axhline(call_wall_strike, color=CALL_COLOR,  linewidth=0.9, linestyle=":",  alpha=0.5)
        ax_iv.axhline(put_wall_strike,  color=PUT_COLOR,   linewidth=0.9, linestyle=":",  alpha=0.5)
        if em and not np.isnan(em.get("em", np.nan)):
            ax_iv.axhspan(em["em_low"], em["em_high"], color=EM_COLOR, alpha=0.07)

        ax_iv.axvline(0, color="white", linewidth=0.5, alpha=0.35)
        ax_iv.set_xlabel("OI-Weighted IV (%)\n← Puts | Calls →", fontweight="heavy", fontsize=8)
        ax_iv.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{abs(x):.0f}%"))
        ax_iv.grid(color=_GRID, axis="x", linewidth=0.5)
        ax_iv.tick_params(axis="y", left=False, labelleft=False)
        ax_iv.legend(loc="lower right", fontsize=7.5, framealpha=0.35)

        # Futures Y-axis on far right
        ax_iv2 = ax_iv.twinx()
        ax_iv2.set_ylim(ax.get_ylim())
        ax_iv2.set_yticks(ax.get_yticks())
        ax_iv2.set_yticklabels([f"{t*scale:,.0f}" for t in ax.get_yticks()], color="0.7")
        ax_iv2.set_ylabel(f"{futures_name} Equivalent", fontweight="heavy", color="0.7")
        ax_iv2.tick_params(axis="y", colors="0.7")
        ax_iv2.spines["right"].set_color("0.4")

    out_path = f"{ticker}_{dte_label.replace(' + ', '_')}_gex.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] Saved → {out_path}")
    plt.show()


def _annotate_wall(ax, strike, gex_val, label, color):
    """Add a small label next to a wall strike bar."""
    ax.annotate(
        f" {label}\n {gex_val:+.2f} Bn",
        xy=(gex_val, strike),
        xytext=(gex_val + (0.02 if gex_val >= 0 else -0.02), strike),
        fontsize=7.5, color=color, fontweight="bold", va="center",
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

def run(ticker: str, force_refresh: bool = False, dte_list: list[int] = [0]):
    ticker = ticker.upper()

    if ticker not in FUTURES_SCALE:
        print(f"[warn] '{ticker}' not in FUTURES_SCALE map. Supported: {list(FUTURES_SCALE)}")

    # 1. Fetch raw data
    payload = fetch_cboe_data(ticker, force_refresh=force_refresh)

    # 2. Parse
    spot, raw_options = parse_cboe_payload(payload)
    print(f"[data] {ticker} spot = {spot:,.2f}  |  {len(raw_options):,} total contracts")

    options = parse_option_fields(raw_options)

    # 3. Full-chain GEX (needed before DTE filter so columns exist)
    options = compute_gex(spot, options)

    # 4. DTE filter (copy — original chain preserved)
    dte_label = " + ".join(f"{d}DTE" for d in sorted(dte_list))
    options_filtered = filter_by_dte(options, dte_list=dte_list)

    if options_filtered.empty:
        print("[warn] Falling back to nearest expiration (no matching DTE found).")
        nearest_exp = options["expiration"].min()
        options_filtered = options.loc[options["expiration"] == nearest_exp].copy()
        print(f"[fallback] Using expiration: {nearest_exp.date()}  "
              f"({len(options_filtered):,} contracts)")

    # 5. GEX by strike
    gex = gex_by_strike(options_filtered, spot, band=0.15)
    print(f"\n[GEX] {dte_label} net GEX = {gex.sum():+.4f} $Bn")
    print(f"[GEX] Call Wall   @ strike {gex.idxmax():,.2f}  ({gex.max():+.3f} Bn)")
    print(f"[GEX] Put Wall    @ strike {gex.idxmin():,.2f}  ({gex.min():+.3f} Bn)")

    # 6. Expected Move
    em = compute_expected_move(spot, options_filtered)

    # 7. OI-weighted IV by strike (±5% band, liquid strikes only)
    oiiv = oi_iv_by_strike(options_filtered, spot, band=0.05, min_oi=100)

    # 8. Scale factor for futures display
    scale, futures_name = get_scale(ticker, spot, payload)

    # 9. Plot
    plot_gex_by_strike(gex, spot, ticker, scale, futures_name,
                       dte_label=dte_label, em=em, oiiv=oiiv)
    print_intraday_note()


if __name__ == "__main__":
    ticker_input = input("Enter ticker (SPY or QQQ): ").strip().upper() or "SPY"

    dte_input = input("DTE to include — e.g. 0  or  0,1  [default: 0]: ").strip() or "0"
    try:
        dte_list = [int(x.strip()) for x in dte_input.split(",")]
    except ValueError:
        print("[warn] Invalid DTE input, defaulting to 0DTE only.")
        dte_list = [0]

    refresh = "--refresh" in sys.argv
    run(ticker_input, force_refresh=refresh, dte_list=dte_list)
