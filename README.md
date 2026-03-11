# 0DTE GEX Analyzer — SPY / QQQ → ES / NQ

A Python tool that scrapes **delayed CBOE options data**, filters for **0DTE contracts only**, computes **dealer Gamma Exposure (GEX)** by strike, and identifies the **Put Wall** (support) and **Call Wall** (resistance) — with strike prices mapped to their equivalent **ES or NQ futures levels**.

---

## What is GEX?

Gamma Exposure (GEX) measures how much delta-hedging activity dealers must perform as price moves. The formula used here:

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

## Features

- Downloads options chain JSON from CBOE (no API key required)
- Caches data locally to `data/` — pass `--refresh` to force a new download
- Filters for **0DTE contracts** only (falls back to nearest expiration if no 0DTE found)
- Computes net GEX per strike, limited to ±15% from spot
- **Horizontal bar chart** with:
  - ETF strike price on the left Y-axis
  - Futures equivalent (ES or NQ) on the right Y-axis
  - Put Wall and Call Wall annotated
  - Net GEX regime label (pinning vs. amplifying)
- Prints a summary of free/low-cost intraday data sources

---

## Supported Tickers & Futures Mapping

| ETF | Futures | Default Scale |
|-----|---------|---------------|
| SPY | ES      | × 10.0        |
| QQQ | NQ      | × 40.0        |

> **Tip:** The scale ratio is applied to strike prices for display only — GEX magnitudes remain in ETF dollar terms. Adjust the `FUTURES_SCALE` dict in the script to match the live ratio (e.g. if NQ = 21,000 and QQQ = 510, ratio ≈ 41.2).

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

You will be prompted to enter a ticker:

```
Enter ticker (SPY or QQQ): SPY
```

### Example output

```
[fetch] Saved → data/SPY.json
[data] SPY spot = 562.14  |  48,320 total contracts
[0DTE] 1,847 contracts expire today (2025-03-11)
[GEX] 0DTE net GEX = -2.1340 $Bn
[GEX] Call Wall  @ strike 565  (+0.842 Bn)
[GEX] Put Wall   @ strike 558  (-1.203 Bn)
```

---

## Project Structure

```
gex-0dte/
├── gex_0dte.py          # Main script
├── requirements.txt     # Python dependencies
├── data/                # Cached CBOE JSON files (auto-created)
│   └── SPY.json         # Example cached payload
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
| CBOE delayed URL (this script) | ~15 min | Free | Poll every 5–15 min, delete cache each loop |
| Tradier API | ~15 min | Free (sandbox) | Requires free account + token |
| IBKR TWS API | Real-time | Free (w/ account) | Best free option for live GEX |
| CBOE LiveVol | Real-time | Paid subscription | Full tick data + Greeks |

To loop the script for quasi-live updates, delete `data/{TICKER}.json` before each run or always pass `--refresh`.

---

## Disclaimer

This tool is for **educational and informational purposes only**. GEX levels are derived from delayed data and should not be used as the sole basis for trading decisions. Options data, dealer positioning assumptions, and GEX calculations involve simplifications that may not reflect actual market dynamics.

---

## License

MIT
