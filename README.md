# 0DTE GEX Analyzer — SPY / QQQ → ES / NQ

A Python tool that scrapes **delayed CBOE options data**, filters for short-dated contracts (0DTE, 1DTE, or any combination), and produces a **two-panel chart** showing:

- **Left** — Dealer Gamma Exposure (GEX) by strike, with Put Wall / Call Wall and Expected Move range
- **Right** — OI-Weighted Implied Volatility concentration by strike

Strike prices are mapped to their equivalent **ES or NQ futures levels** on the right-hand axis.

---

## What is GEX?

Gamma Exposure (GEX) measures how much delta-hedging activity dealers must perform as price moves. The formula used:

```
GEX = spot² × gamma × open_interest × contract_size × 0.01
```

This gives **dollars of hedge flow per 1% move** in the underlying.

Sign convention (assumes dealers are long calls / short puts):
- **Calls → +GEX** — dealers buy dips and sell rips → price-pinning / mean-reverting
- **Puts → −GEX** — dealers sell dips and buy rips → trend-amplifying

**Put Wall** = strike with the largest negative GEX → intraday support zone  
**Call Wall** = strike with the largest positive GEX → intraday resistance zone

---

## What is the Expected Move (EM)?

The EM is the options market's consensus on the likely daily price range — equivalent to a ±1 standard deviation band. It is derived from the **ATM straddle price** (call mid + put mid), falling back to the IV formula if bid/ask data is unavailable:

```
EM (straddle) = ATM call mid + ATM put mid
EM (IV)       = spot × ATM_IV × √(1/252)
```

The EM range is shown as a shaded band on the chart. Price rarely closes outside this range on 0DTE without a macro catalyst, making the edges a useful outer boundary for intraday S/R analysis.

---

## What is OI-Weighted IV?

Rather than looking at GEX alone, the right panel shows **implied volatility weighted by open interest** per strike — split by calls (green, right) and puts (red, left):

```
OI-Weighted IV = Σ(OI × IV) / Σ(OI)   per strike
```

Strikes with a high OI×IV reading have both **large positioning** and **elevated implied uncertainty**, meaning the market is actively defending those levels. These tend to be stickier S/R levels than GEX walls alone.

---

## Reading the Two Panels Together

| Signal | Interpretation |
|--------|---------------|
| GEX wall + OI×IV spike | Highest-confidence level — dealers must hedge *and* market expects it to be contested |
| GEX wall, no OI×IV spike | Structural level but less actively defended intraday |
| OI×IV spike, no GEX wall | Options conviction without strong dealer hedging — watch but lower priority |
| Both walls inside EM range | Normal session — price likely oscillates between them |
| Wall outside EM range | Unlikely to be reached unless a catalyst drives a breakout |

---

## Features

- Downloads options chain JSON from CBOE (no API key required)
- Caches data locally to `data/` — pass `--refresh` to force a new download
- Flexible **DTE filter** — choose 0DTE only, 1DTE only, or combined (e.g. `0,1`)
- Falls back to nearest expiration automatically if no matching DTE found
- Strike prices decoded to full decimal precision from CBOE 8-digit encoding
- **Left panel** — GEX bars (±15% from spot) with:
  - Put Wall and Call Wall annotated with GEX magnitude
  - Expected Move shaded band (ATM straddle or IV-derived)
  - Spot price line and gamma regime label (pinning vs. amplifying)
  - Summary box: spot, walls, EM range, ATM IV, total GEX
- **Right panel** — OI-weighted IV by strike (±5% from spot, OI > 100 filter)
- Both panels share the same price Y-axis; futures equivalent shown on the far right

---

## Supported Tickers & Futures Mapping

| ETF | Futures | Default Scale |
|-----|---------|---------------|
| SPY | ES      | × 10.0        |
| QQQ | NQ      | × 40.0        |

> **Tip:** The scale ratio is applied to strike prices for display only — GEX magnitudes remain in ETF dollar terms. Adjust the `FUTURES_SCALE` dict at the top of the script to match the live ratio (e.g. if NQ = 21,000 and QQQ = 510, ratio ≈ 41.2).

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/gex-0dte.git
cd gex-0dte
pip install -r requirements.txt
```

---

## Usage

```bash
# Basic run (uses cached data if available)
python gex_0dte.py

# Force fresh download from CBOE
python gex_0dte.py --refresh
```

You will be prompted for a ticker and DTE selection:

```
Enter ticker (SPY or QQQ): SPY
DTE to include — e.g. 0  or  0,1  [default: 0]: 0
```

### Example terminal output

```
[fetch] Saved → data/SPY.json
[data] SPY spot = 562.14  |  48,320 total contracts
[filter] 0DTE (2025-03-13): 1,847 contracts
[GEX]  0DTE net GEX = -2.1340 $Bn
[GEX]  Call Wall  @ strike 565.00  (+0.842 Bn)
[GEX]  Put Wall   @ strike 558.00  (-1.203 Bn)
[EM]   ATM strike=562.00  IV=12.4%  EM=4.37 (ATM straddle)  → range [557.77, 566.51]
```

---

## Project Structure

```
gex-0dte/
├── gex_0dte.py          # Main script
├── requirements.txt     # Python dependencies
├── data/                # Cached CBOE JSON files (auto-created)
│   └── .gitkeep         # Keeps folder tracked by git
├── .gitignore
└── README.md
```

---

## Requirements

```
matplotlib>=3.5
pandas>=1.4
requests>=2.27
numpy>=1.22
```

Install with:

```bash
pip install -r requirements.txt
```

---

## Intraday / Real-Time Data

The CBOE endpoint provides **~15-minute delayed** data for free. Options for fresher data:

| Source | Latency | Cost | Notes |
|--------|---------|------|-------|
| CBOE delayed URL (this script) | ~15 min | Free | Pass `--refresh` each run to bypass cache |
| Tradier API | ~15 min | Free (sandbox) | Requires free account + token |
| IBKR TWS API | Real-time | Free (w/ account) | Best free option for live GEX |
| CBOE LiveVol | Real-time | Paid subscription | Full tick data + Greeks |

To poll quasi-live, run the script on a loop and always pass `--refresh` so the cache is bypassed each time.

---

## Disclaimer

This tool is for **educational and informational purposes only**. GEX levels are derived from delayed data and should not be used as the sole basis for trading decisions. Options data, dealer positioning assumptions, and GEX calculations involve simplifications that may not reflect actual market dynamics.

---

## License

MIT
