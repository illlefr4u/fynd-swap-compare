#!/usr/bin/env python3
"""Swap aggregator: picks best price from 1inch / Fynd, returns calldata for Rabby.

Endpoints:
  GET  /api/quote?src=&dst=&amount=&sender=  - best quote from both sources
  GET  /api/swap?src=&dst=&amount=&sender=&slippage=0.5  - calldata from best source
  GET  /api/gas  - current gas price
  GET  /  - frontend

Comparison log: data/swap_log.csv
"""

import json
import sys
import os
import time
import csv
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / '.env'
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

ONEINCH_API_KEY = os.environ.get('ONEINCH_API_KEY', '')
PORT = int(os.environ.get('PORT', '8899'))
INCH_API = 'https://api.1inch.dev'
FYND_URL = os.environ.get('FYND_URL', 'http://localhost:3000')
DEFAULT_SENDER = os.environ.get('DEFAULT_SENDER', '')
WETH = '0xC02aaA39b223FE8D0A0E5C4F27eAD9083C756Cc2'
ETH_NATIVE = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'

DATA_DIR = Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
LOG_FILE = DATA_DIR / 'swap_log.csv'

executor = ThreadPoolExecutor(max_workers=4)

TOKENS = {
    'USDC': {'addr': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 'dec': 6},
    'USDT': {'addr': '0xdAC17F958D2ee523a2206206994597C13D831ec7', 'dec': 6},
    'DAI':  {'addr': '0x6B175474E89094C44Da98b954EedeAC495271d0F', 'dec': 18},
    'WETH': {'addr': WETH, 'dec': 18},
    'WBTC': {'addr': '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599', 'dec': 8},
}


import re
_ADDR_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')

def _is_valid_address(addr):
    return bool(_ADDR_RE.match(addr))


def fetch_json(url, headers=None, data=None, timeout=15):
    try:
        h = headers or {}
        h.setdefault('User-Agent', 'SwapCompare/1.0')
        req = Request(url, headers=h)
        if data:
            req.data = json.dumps(data).encode()
            req.add_header('Content-Type', 'application/json')
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()
        try:
            return {'error': json.loads(body)}
        except Exception:
            return {'error': f'HTTP {e.code}: {body[:200]}'}
    except Exception as e:
        return {'error': str(e)}


def inch_headers():
    return {'Authorization': f'Bearer {ONEINCH_API_KEY}', 'Accept': 'application/json',
            'User-Agent': 'SwapCompare/1.0'}


def inch_addr(addr):
    """Convert WETH address to native ETH for 1inch."""
    return ETH_NATIVE if addr.lower() == WETH.lower() else addr


def get_gas():
    return fetch_json(f'{INCH_API}/gas-price/v1.6/1', headers=inch_headers())


BINANCE_SYMBOLS = {
    '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2': 'ETHUSDT',
    '0x2260fac5e5542a773aa44fbcfedf7c193bc2c599': 'BTCUSDT',
}
STABLECOINS = {
    '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',  # USDC
    '0xdac17f958d2ee523a2206206994597c13d831ec7',  # USDT
    '0x6b175474e89094c44da98b954eedeac495271d0f',  # DAI
}


def get_prices(token_addresses):
    """Get real-time USD prices from Binance (CEX, no delay)."""
    result = {}
    for addr in token_addresses:
        lower = addr.lower()
        if lower in STABLECOINS:
            result[lower] = 1.0
        elif lower in BINANCE_SYMBOLS:
            raw = fetch_json(
                f'https://api.binance.com/api/v3/ticker/price?symbol={BINANCE_SYMBOLS[lower]}')
            if isinstance(raw, dict) and 'price' in raw:
                result[lower] = float(raw['price'])
    return result


def get_inch_classic_quote(src, dst, amount):
    """Get 1inch classic quote with gas estimate (no balance check needed)."""
    url = (f'{INCH_API}/swap/v6.1/1/quote?'
           f'src={inch_addr(src)}&dst={inch_addr(dst)}&amount={amount}&includeGas=true')
    return fetch_json(url, headers=inch_headers())


def get_inch_classic_swap(src, dst, amount, sender, slippage=0.5):
    """Get 1inch classic swap calldata (for execution)."""
    url = (f'{INCH_API}/swap/v6.1/1/swap?'
           f'src={inch_addr(src)}&dst={inch_addr(dst)}&amount={amount}'
           f'&from={sender}&slippage={slippage}&disableEstimate=true')
    return fetch_json(url, headers=inch_headers())


def get_inch_fusion(src, dst, amount, sender):
    """Get 1inch fusion (gasless intent) quote."""
    url = (f'{INCH_API}/fusion/quoter/v2.0/1/quote/receive?'
           f'fromTokenAddress={src}&toTokenAddress={dst}'
           f'&amount={amount}&walletAddress={sender}&enableEstimate=true')
    return fetch_json(url, headers=inch_headers())


def get_fynd_quote(src, dst, amount, sender, gas_price='50000000'):
    data = {
        'orders': [{
            'token_in': src, 'token_out': dst,
            'amount': amount, 'side': 'sell', 'sender': sender
        }],
        'gas_price': gas_price
    }
    return fetch_json(f'{FYND_URL}/v1/quote', data=data)


def get_fynd_swap(src, dst, amount, sender, slippage, gas_price='50000000'):
    data = {
        'orders': [{
            'token_in': src, 'token_out': dst,
            'amount': amount, 'side': 'sell', 'sender': sender
        }],
        'gas_price': gas_price,
        'options': {
            'encoding_options': {
                'slippage': slippage / 100,  # pct -> fraction
                'transfer_type': 'transfer_from'
            }
        }
    }
    return fetch_json(f'{FYND_URL}/v1/quote', data=data)


def get_kyberswap_quote(src, dst, amount):
    url = (f'https://aggregator-api.kyberswap.com/ethereum/api/v1/routes?'
           f'tokenIn={src}&tokenOut={dst}&amountIn={amount}&gasInclude=true')
    return fetch_json(url)


def get_cowswap_quote(src, dst, amount, sender):
    data = {
        'sellToken': src, 'buyToken': dst,
        'sellAmountBeforeFee': amount,
        'from': sender, 'kind': 'sell',
    }
    return fetch_json('https://api.cow.fi/mainnet/api/v1/quote', data=data)


def get_enso_quote(src, dst, amount, sender):
    url = (f'https://api.enso.finance/api/v1/shortcuts/route?chainId=1'
           f'&fromAddress={sender}&tokenIn={src}&tokenOut={dst}&amountIn={amount}')
    return fetch_json(url)


def get_openocean_quote(src, dst, amount_human):
    """OpenOcean expects human-readable amount (e.g. 5000), not wei."""
    url = (f'https://open-api.openocean.finance/v4/1/quote?'
           f'inTokenAddress={src}&outTokenAddress={dst}'
           f'&amount={amount_human}&gasPrice=50000000')
    return fetch_json(url)


def token_decimals(addr):
    for info in TOKENS.values():
        if info['addr'].lower() == addr.lower():
            return info['dec']
    return 18


def token_symbol(addr):
    for sym, info in TOKENS.items():
        if info['addr'].lower() == addr.lower():
            return sym
    return addr[:10]


def log_comparison(src, dst, amount, classic_out, fusion_out, fynd_out, winner, diff_bps):
    if not LOG_FILE.exists():
        with open(LOG_FILE, 'w', newline='') as f:
            csv.writer(f).writerow([
                'timestamp', 'sell', 'buy', 'amount_raw',
                'inch_classic_out', 'inch_fusion_out', 'fynd_out', 'winner', 'diff_bps'
            ])
    with open(LOG_FILE, 'a', newline='') as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            token_symbol(src), token_symbol(dst), amount,
            classic_out, fusion_out, fynd_out, winner, f'{diff_bps:.2f}'
        ])


def compare_and_pick(src, dst, amount, sender, slippage=0.5):
    gas_data = get_gas()
    gas_price = '50000000'
    if isinstance(gas_data, dict) and 'medium' in gas_data:
        gas_price = gas_data['medium']['maxFeePerGas']

    # Convert amount to human-readable for APIs that need it
    src_dec = token_decimals(src)
    amount_human = str(int(amount) / (10 ** src_dec))

    # Fetch all sources in parallel
    classic_f = executor.submit(get_inch_classic_quote, src, dst, amount)
    fusion_f = executor.submit(get_inch_fusion, src, dst, amount, sender)
    fynd_f = executor.submit(get_fynd_quote, src, dst, amount, sender, gas_price)
    kyber_f = executor.submit(get_kyberswap_quote, src, dst, amount)
    cow_f = executor.submit(get_cowswap_quote, src, dst, amount, sender)
    enso_f = executor.submit(get_enso_quote, src, dst, amount, sender)
    ocean_f = executor.submit(get_openocean_quote, src, dst, amount_human)
    prices_f = executor.submit(get_prices, [src, dst])

    classic_raw = classic_f.result()
    fusion_raw = fusion_f.result()
    fynd_raw = fynd_f.result()
    kyber_raw = kyber_f.result()
    cow_raw = cow_f.result()
    enso_raw = enso_f.result()
    ocean_raw = ocean_f.result()
    prices_raw = prices_f.result()

    # Parse prices (API returns lowercase addresses)
    prices = {}
    if isinstance(prices_raw, dict) and 'error' not in prices_raw:
        for addr, price in prices_raw.items():
            prices[addr.lower()] = float(price)

    # Parse 1inch classic (from quote endpoint with includeGas)
    classic_out = 0
    classic_info = {}
    if isinstance(classic_raw, dict) and 'dstAmount' in classic_raw:
        classic_out = int(classic_raw['dstAmount'])
        classic_info = {
            'gas_estimate': str(classic_raw.get('gas', '0')),
            'gas_price': gas_price,
        }

    # Parse 1inch fusion (field = toTokenAmount, not dstTokenAmount)
    fusion_out = 0
    fusion_info = {}
    if isinstance(fusion_raw, dict) and 'toTokenAmount' in fusion_raw:
        fusion_out = int(fusion_raw['toTokenAmount'])
        fusion_info = {
            'gas_estimate': str(fusion_raw.get('gas', '0')),
            'gas_price': gas_price,
            'gasless': True,
        }

    # Parse Fynd (safe: check orders list before indexing)
    fynd_out = 0
    fynd_info = {}
    if isinstance(fynd_raw, dict):
        orders = fynd_raw.get('orders', [])
        if orders and isinstance(orders, list):
            order = orders[0]
            if order.get('status') == 'success':
                fynd_out = int(order.get('amount_out', '0'))
                fynd_info = {
                    'gas_estimate': str(order.get('gas_estimate', '0')),
                    'gas_price': str(order.get('gas_price', gas_price)),
                    'route': ' -> '.join(
                        s.get('protocol', '?')
                        for s in order.get('route', {}).get('swaps', [])),
                    'block': order.get('block', {}).get('number', 0),
                    'solve_time_ms': fynd_raw.get('solve_time_ms', 0),
                }

    # Parse KyberSwap
    kyber_out = 0
    kyber_info = {}
    if isinstance(kyber_raw, dict):
        rs = kyber_raw.get('data', {}).get('routeSummary', {})
        if rs and rs.get('amountOut'):
            kyber_out = int(rs['amountOut'])
            kyber_info = {
                'gas_estimate': str(rs.get('gas', '0')),
                'gas_price': gas_price,
            }

    # Parse CowSwap
    cow_out = 0
    cow_info = {}
    if isinstance(cow_raw, dict) and 'quote' in cow_raw:
        q = cow_raw['quote']
        cow_out = int(q.get('buyAmount', '0'))
        cow_info = {
            'gas_estimate': str(q.get('gasAmount', '0')),
            'gas_price': str(q.get('gasPrice', gas_price)),
            'fee_amount': q.get('feeAmount', '0'),
        }

    # Parse Enso
    enso_out = 0
    enso_info = {}
    if isinstance(enso_raw, dict) and 'amountOut' in enso_raw:
        enso_out = int(enso_raw['amountOut'])
        enso_info = {
            'gas_estimate': str(enso_raw.get('gas', '0')),
            'gas_price': gas_price,
        }

    # Parse OpenOcean
    ocean_out = 0
    ocean_info = {}
    if isinstance(ocean_raw, dict) and 'data' in ocean_raw:
        od = ocean_raw['data']
        if od and od.get('outAmount'):
            ocean_out = int(od['outAmount'])
            ocean_info = {
                'gas_estimate': str(od.get('estimatedGas', '0')),
                'gas_price': gas_price,
            }

    # On-chain fees per venue (deducted by smart contract at execution)
    # CowSwap/KyberSwap: 0 bps protocol fee on output (fee is in sell token for CoW)
    ON_CHAIN_FEE = {
        '1inch_classic': 0.003,
        '1inch_fusion': 0.003,
        'fynd': 0.001,
        'kyberswap': 0.0,
        'cowswap': 0.0,
        'enso': 0.0,
        'openocean': 0.0,
    }

    # Build candidates for execution comparison
    all_sources = {
        '1inch_classic': classic_out,
        '1inch_fusion': fusion_out,
        'fynd': fynd_out,
        'kyberswap': kyber_out,
        'cowswap': cow_out,
        'enso': enso_out,
        'openocean': ocean_out,
    }
    exec_candidates = {}
    for name, out in all_sources.items():
        if out > 0:
            exec_candidates[name] = out * (1 - ON_CHAIN_FEE.get(name, 0))

    if exec_candidates:
        winner = max(exec_candidates, key=exec_candidates.get)
        all_gross = [v for v in all_sources.values() if v > 0]
        best_out = max(all_gross) if all_gross else 0
        worst_exec = min(exec_candidates.values())
        best_exec = exec_candidates[winner]
        diff_bps = (best_exec - worst_exec) / worst_exec * 10000 if worst_exec > 0 else 0
    else:
        winner = 'none'
        best_out = 0
        diff_bps = 0

    log_comparison(src, dst, amount, str(classic_out), str(fusion_out),
                   str(fynd_out), winner, diff_bps)

    return {
        'winner': winner,
        'best_amount_out': str(best_out),
        'gas': gas_data,
        'gas_price': gas_price,
        'inch_classic': {
            'amount_out': str(classic_out),
            'receives_native_eth': dst.lower() == WETH.lower(),
            **classic_info,
        } if classic_out > 0 else None,
        'inch_classic_error': classic_raw.get('error') if classic_out == 0 else None,
        'inch_fusion': {
            'amount_out': str(fusion_out), **fusion_info
        } if fusion_out > 0 else None,
        'inch_fusion_error': fusion_raw.get('error') if fusion_out == 0 else None,
        'fynd': {
            'amount_out': str(fynd_out), **fynd_info
        } if fynd_out > 0 else None,
        'fynd_error': str(fynd_raw) if fynd_out == 0 else None,
        'kyberswap': {
            'amount_out': str(kyber_out), **kyber_info
        } if kyber_out > 0 else None,
        'kyberswap_error': kyber_raw.get('error') if kyber_out == 0 else None,
        'cowswap': {
            'amount_out': str(cow_out), **cow_info
        } if cow_out > 0 else None,
        'cowswap_error': cow_raw.get('error') if cow_out == 0 else None,
        'enso': {
            'amount_out': str(enso_out), **enso_info
        } if enso_out > 0 else None,
        'enso_error': enso_raw.get('error') if enso_out == 0 else None,
        'openocean': {
            'amount_out': str(ocean_out), **ocean_info
        } if ocean_out > 0 else None,
        'openocean_error': ocean_raw.get('error') if ocean_out == 0 else None,
        'diff_bps': round(diff_bps, 2),
        'prices': {
            'src': prices.get(src.lower()),
            'dst': prices.get(dst.lower()),
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(__file__), **kwargs)

    def end_headers(self):
        # Restrict CORS to exact localhost origins
        origin = self.headers.get('Origin', '')
        allowed = {f'http://localhost:{PORT}', f'http://127.0.0.1:{PORT}',
                    'http://localhost', 'http://127.0.0.1'}
        if origin in allowed or re.match(r'^http://(localhost|127\.0\.0\.1)(:\d+)?$', origin):
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/gas':
            self._json(get_gas())
        elif parsed.path == '/api/quote':
            self._handle_quote(parsed)
        elif parsed.path == '/api/swap':
            self._handle_swap(parsed)
        else:
            super().do_GET()

    def _p(self, parsed, key, default=''):
        return parse_qs(parsed.query).get(key, [default])[0]

    def _handle_quote(self, parsed):
        src = self._p(parsed, 'src')
        dst = self._p(parsed, 'dst')
        amount = self._p(parsed, 'amount')
        sender = self._p(parsed, 'sender', DEFAULT_SENDER)
        if not all([src, dst, amount]):
            return self._json({'error': 'missing src/dst/amount'}, 400)
        if not _is_valid_address(src) or not _is_valid_address(dst):
            return self._json({'error': 'invalid token address'}, 400)
        if sender and not _is_valid_address(sender):
            return self._json({'error': 'invalid sender address'}, 400)
        try:
            int(amount)
        except ValueError:
            return self._json({'error': 'amount must be integer (wei)'}, 400)
        result = compare_and_pick(src, dst, amount, sender)
        self._json(result)

    def _handle_swap(self, parsed):
        src = self._p(parsed, 'src')
        dst = self._p(parsed, 'dst')
        amount = self._p(parsed, 'amount')
        sender = self._p(parsed, 'sender', DEFAULT_SENDER)
        source = self._p(parsed, 'source', '')

        if not all([src, dst, amount]):
            return self._json({'error': 'missing src/dst/amount'}, 400)
        if not _is_valid_address(src) or not _is_valid_address(dst):
            return self._json({'error': 'invalid token address'}, 400)
        if sender and not _is_valid_address(sender):
            return self._json({'error': 'invalid sender address'}, 400)
        try:
            int(amount)
        except ValueError:
            return self._json({'error': 'amount must be integer (wei)'}, 400)

        try:
            slippage = float(self._p(parsed, 'slippage', '0.5'))
            if not (0.01 <= slippage <= 50):
                return self._json({'error': 'slippage must be 0.01-50'}, 400)
        except ValueError:
            return self._json({'error': 'invalid slippage value'}, 400)

        # Fusion is quote-only (no on-chain execution path available via API)
        if source == '1inch_fusion':
            return self._json({'source': '1inch_fusion',
                               'error': 'Fusion is gasless intent-based: execution requires '
                                        'EIP-712 signing flow (not yet implemented). '
                                        'Use 1inch Classic or Fynd instead.'})

        if not source:
            quote = compare_and_pick(src, dst, amount, sender)
            source = quote['winner']
            if source == '1inch_fusion':
                source = '1inch_classic'  # fallback: fusion can't execute

        gas_data = get_gas()
        gas_price = '50000000'
        if isinstance(gas_data, dict) and 'medium' in gas_data:
            gas_price = gas_data['medium']['maxFeePerGas']

        if source in ('1inch_classic', '1inch'):
            raw = get_inch_classic_swap(src, dst, amount, sender, slippage)
            if isinstance(raw, dict) and 'tx' in raw:
                tx = raw['tx']
                self._json({
                    'source': '1inch_classic',
                    'tx': {
                        'from': tx['from'],
                        'to': tx['to'],
                        'data': tx['data'],
                        'value': tx['value'],
                        'gas': tx.get('gas', '0'),
                    },
                    'amount_out': raw.get('dstAmount', '0'),
                })
            else:
                self._json({'source': '1inch_classic',
                            'error': raw.get('error', 'unknown') if isinstance(raw, dict) else str(raw)})
        elif source == 'fynd':
            raw = get_fynd_swap(src, dst, amount, sender, slippage, gas_price)
            if not isinstance(raw, dict) or 'orders' not in raw:
                return self._json({'source': 'fynd', 'error': str(raw)})
            orders = raw.get('orders', [])
            if not orders:
                return self._json({'source': 'fynd', 'error': 'empty orders response'})
            order = orders[0]
            if order.get('transaction'):
                tx = order['transaction']
                self._json({
                    'source': 'fynd',
                    'tx': {
                        'from': sender,
                        'to': tx['to'],
                        'data': tx['data'],
                        'value': tx.get('value', '0x0'),
                    },
                    'amount_out': order.get('amount_out', '0'),
                })
            else:
                self._json({
                    'source': 'fynd',
                    'amount_out': order.get('amount_out', '0'),
                    'error': 'no calldata returned (token approval needed?)',
                })
        else:
            self._json({'error': f'unknown source: {source}'}, 400)

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args):
        msg = args[0] if args else ''
        if '/api/gas' not in msg:
            super().log_message(fmt, *args)


if __name__ == '__main__':
    port = PORT
    if '--port' in sys.argv:
        port = int(sys.argv[sys.argv.index('--port') + 1])

    print(f'Swap Aggregator on http://localhost:{port}')
    print(f'1inch API: {"OK" if ONEINCH_API_KEY else "MISSING"}')
    print(f'Fynd: {FYND_URL}')
    print(f'Log: {LOG_FILE}')
    HTTPServer(('127.0.0.1', port), Handler).serve_forever()
