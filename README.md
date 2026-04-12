# swap-compare

Local swap aggregator that picks the best execution price between [1inch](https://1inch.io) and [Fynd](https://github.com/propeller-heads/fynd) on Ethereum.

## How it works

1. You enter token pair + amount
2. Server fetches quotes from both 1inch and Fynd in parallel
3. Shows both quotes with full price breakdown (fees, gas, route)
4. Picks the best one, sends calldata to Rabby for signing

Every comparison is logged to `data/swap_log.csv` for historical analysis.

## Prerequisites

- Python 3.10+
- [Fynd](https://github.com/propeller-heads/fynd) running locally (`fynd serve`)
- 1inch Developer Portal API key
- [Rabby](https://rabby.io) browser extension (for swap execution)
- `TYCHO_API_KEY` for Fynd (get from [@fynd_portal_bot](https://t.me/fynd_portal_bot))

## Setup

```bash
cp .env.example .env
# Edit .env with your API keys

# Start Fynd (in a separate terminal)
TYCHO_API_KEY=your-key RUST_LOG=fynd=info fynd serve

# Start the aggregator
python server.py
# Open http://localhost:8899
```

## Fee structure

| Source | Infrastructure fee | Gas |
|--------|-------------------|-----|
| 1inch (free tier) | 30 bps non-stables, 10 bps stables | Classic: user pays. Fusion: gasless |
| Fynd | 10 bps (router fee) | User pays |

## API

```
GET /api/quote?src=0x...&dst=0x...&amount=1000000&sender=0x...
GET /api/swap?src=0x...&dst=0x...&amount=1000000&sender=0x...&slippage=0.5
GET /api/gas
```
