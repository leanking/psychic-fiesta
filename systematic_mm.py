import ccxt
import json
import time
import logging
import os
from dotenv import load_dotenv
from websocket import create_connection
import threading
import requests
import atexit

# Load environment variables from .env file
load_dotenv()

# Configure logging
log_level = os.getenv('LOG_LEVEL', 'INFO')
logging.basicConfig(level=getattr(logging, log_level), format='%(asctime)s - %(levelname)s - %(message)s')

# Exchange and API configuration
exchange_name = os.getenv('EXCHANGE_NAME')
api_key = os.getenv('API_KEY')  # API wallet address (for signing/orders)
private_key = os.getenv('PRIVATE_KEY')  # API wallet private key
websocket_url = os.getenv('WEBSOCKET_URL')
symbol = os.getenv('SYMBOL')  # e.g., 'USDHL/USDC' for spot order placement
order_size = float(os.getenv('ORDER_SIZE'))
base_spread = float(os.getenv('BASE_SPREAD'))
min_profit = float(os.getenv('MIN_PROFIT'))
testnet = os.getenv('TESTNET').lower() == 'true'
rate_limit = int(os.getenv('RATE_LIMIT'))
reconnect_delay = int(os.getenv('RECONNECT_DELAY'))
COIN = os.getenv('COIN')  # e.g., '@180' for WebSocket and direct API
max_position = float(os.getenv('MAX_POSITION'))
user_address = os.getenv('USER_ADDRESS')  # Main account address (for balances)

# Add SHADOW_MODE support
shadow_mode = os.getenv('SHADOW_MODE', 'false').lower() == 'true'

# For shadow mode: maintain simulated open orders and P&L
simulated_orders = []  # Each order: {'id', 'side', 'price', 'size', 'status'}
simulated_order_id = 1
simulated_pnl = 0.0
shadow_log_file = 'shadow_trades.log'

# Track inventory/position
shadow_position = 0.0  # For shadow mode

# Mapping from COIN to base asset
COIN_TO_BASE_ASSET = {
    '@180': 'USDHL',
    # Add more mappings here if you trade more pairs
}
base_asset = COIN_TO_BASE_ASSET.get(COIN)
quote_asset = 'USDC'
if base_asset is None:
    logging.error(f"COIN {COIN} not recognized. Please update COIN_TO_BASE_ASSET mapping.")
    base_asset = ''

# Add at the top, after other env vars
min_base_balance = float(os.getenv('MIN_BASE_BALANCE'))
max_base_balance = float(os.getenv('MAX_BASE_BALANCE'))
min_quote_balance = float(os.getenv('MIN_QUOTE_BALANCE'))
max_quote_balance = float(os.getenv('MAX_QUOTE_BALANCE'))
max_order_size = float(os.getenv('MAX_ORDER_SIZE'))

# Add maker fee rate as env variable
MAKER_FEE_RATE = float(os.getenv('MAKER_FEE_RATE', '0.000384'))  # Default 0.0384%
# Add per-trade close fee (hardcoded)
PER_TRADE_CLOSE_FEE = 0.02  # $0.02 per closed trade

# SYSTEMATIC TRADING PRICES - USDHL specific
SYSTEMATIC_BUY_PRICE = float(os.getenv('SYSTEMATIC_BUY_PRICE', '0.99929'))
SYSTEMATIC_SELL_PRICE = float(os.getenv('SYSTEMATIC_SELL_PRICE', '1.00009'))

print("API_Wallet:", api_key)
print("USER_ADDRESS:", user_address)

def log_shadow_trade(order, fill_price=None):
    with open(shadow_log_file, 'a') as f:
        log_entry = {
            'order': order,
            'fill_price': fill_price,
            'timestamp': time.time(),
        }
        f.write(json.dumps(log_entry) + '\n')

def simulate_order_fill(order, orderbook):
    global simulated_pnl
    bids = orderbook.get('bids', [])
    asks = orderbook.get('asks', [])
    filled = False
    fill_price = None
    # Improved fill logic: fill if order is at or inside the top of the book
    if order['side'] == 'buy':
        if (asks and float(asks[0]['px']) <= order['price']) or (bids and float(bids[0]['px']) >= order['price']):
            filled = True
            fill_price = float(asks[0]['px']) if asks and float(asks[0]['px']) <= order['price'] else float(bids[0]['px'])
    elif order['side'] == 'sell':
        if (bids and float(bids[0]['px']) >= order['price']) or (asks and float(asks[0]['px']) <= order['price']):
            filled = True
            fill_price = float(bids[0]['px']) if bids and float(bids[0]['px']) >= order['price'] else float(asks[0]['px'])
    if filled:
        order['status'] = 'filled'
        log_shadow_trade(order, fill_price)
        # Update P&L (now includes maker fees and per-trade close fee)
        if order['side'] == 'sell':
            for o in simulated_orders:
                if o['side'] == 'buy' and o['status'] == 'filled' and not o.get('pnl_counted'):
                    buy_fee = o['price'] * o['size'] * MAKER_FEE_RATE
                    sell_fee = order['size'] * MAKER_FEE_RATE * order['price']
                    pnl = (order['price'] - o['price']) * order['size'] - buy_fee - sell_fee - PER_TRADE_CLOSE_FEE
                    simulated_pnl += pnl
                    o['pnl_counted'] = True
                    break
        elif order['side'] == 'buy':
            for o in simulated_orders:
                if o['side'] == 'sell' and o['status'] == 'filled' and not o.get('pnl_counted'):
                    buy_fee = order['price'] * order['size'] * MAKER_FEE_RATE
                    sell_fee = o['size'] * MAKER_FEE_RATE * o['price']
                    pnl = (o['price'] - order['price']) * order['size'] - buy_fee - sell_fee - PER_TRADE_CLOSE_FEE
                    simulated_pnl += pnl
                    o['pnl_counted'] = True
                    break
    return filled

# Initialize exchange
exchange = getattr(ccxt, exchange_name)({
    'apiKey': api_key,              # API wallet address
    'privateKey': private_key,      # API wallet private key
    'walletAddress': api_key,       # API wallet address (again)
    'enableRateLimit': True,
    'rateLimit': rate_limit,
    'testnet': testnet,
})

# WebSocket for real-time orderbook updates
def on_message(ws, message):
    data = json.loads(message)
    # Extract levels from the correct location in the JSON
    levels = data.get('data', {}).get('levels', [[], []])
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []
    orderbook = {'bids': bids, 'asks': asks}
    update_orders(orderbook)

def on_error(ws, error):
    logging.error(f"WebSocket error: {error}")

def on_close(ws):
    logging.info("WebSocket closed")
    reconnect()

def on_open(ws):
    logging.info("WebSocket connected")
    subscribe_to_orderbook(ws)

def subscribe_to_orderbook(ws):
    # Subscribe to order book updates for a specific coin using the correct method and structure
    ws.send(json.dumps({
        "method": "subscribe",
        "subscription": {
            "type": "l2Book",
            "coin": COIN
        }
    }))

def reconnect():
    time.sleep(reconnect_delay)
    ws = create_connection(websocket_url)
    subscribe_to_orderbook(ws)
    while True:
        try:
            message = ws.recv()
            on_message(ws, message)
        except Exception as e:
            logging.error(f"WebSocket error: {e}")
            ws.close()
            break
    reconnect()

# --- SHADOW MODE STATE PERSISTENCE ---
shadow_state_file = 'shadow_state.json'

def save_shadow_state():
    if shadow_mode:
        state = {
            'simulated_orders': simulated_orders,
            'simulated_order_id': simulated_order_id,
            'simulated_pnl': simulated_pnl,
        }
        with open(shadow_state_file, 'w') as f:
            json.dump(state, f)

def load_shadow_state():
    global simulated_orders, simulated_order_id, simulated_pnl
    if shadow_mode and os.path.exists(shadow_state_file):
        with open(shadow_state_file, 'r') as f:
            state = json.load(f)
            simulated_orders.clear()
            simulated_orders.extend(state.get('simulated_orders', []))
            simulated_order_id = state.get('simulated_order_id', 1)
            simulated_pnl = state.get('simulated_pnl', 0.0)

if shadow_mode:
    load_shadow_state()
    atexit.register(save_shadow_state)

# Update and place orders - SYSTEMATIC VERSION
def update_orders(orderbook):
    global simulated_order_id, shadow_position
    try:
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if bids and asks:
            logging.debug(f"update_orders called: best_bid={bids[0]['px']}, best_ask={asks[0]['px']}")
        else:
            logging.debug(f"update_orders called: bids or asks missing. bids={bids}, asks={asks}")
        if not bids or not asks:
            return

        # Use systematic prices instead of calculating from orderbook
        buy_price = SYSTEMATIC_BUY_PRICE
        sell_price = SYSTEMATIC_SELL_PRICE

        # Get available balances for order sizing
        if not shadow_mode:
            base_balance, quote_balance = get_real_balances()
            logging.info(f"Available balances: base={base_balance} {base_asset}, quote={quote_balance} {quote_asset}")
            
            # Calculate order sizes based on available balance
            buy_size = quote_balance / buy_price  # How much USDHL we can buy with available USDC
            total_to_sell = base_balance  # How much USDHL we can sell
            
            # Cap at max_order_size if specified
            if max_order_size > 0:
                buy_size = min(buy_size, max_order_size)
                total_to_sell = min(total_to_sell, max_order_size)
        else:
            # In shadow mode, use max_order_size
            buy_size = max_order_size
            total_to_sell = max_order_size

        logging.info(f"Calculated order sizes: buy_size={buy_size}, sell_size={total_to_sell}")

        # Place buy order at systematic price - USE AVAILABLE BALANCE
        if buy_size > 0:
            if not shadow_mode:
                try:
                    open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
                except Exception as e:
                    logging.error(f"Error fetching open orders: {e}")
                    open_orders = []
                open_buy_orders = [o for o in open_orders if o['side'] == 'buy']
                
                # Cancel any existing buy orders at different prices
                for o in open_buy_orders:
                    if abs(float(o['price']) - buy_price) > 1e-8:
                        try:
                            exchange.cancel_order(o['id'], symbol)
                            logging.info(f"Cancelled existing buy order at {o['price']}")
                        except Exception as e:
                            logging.error(f"Error canceling buy order: {e}")
                
                # Place new buy order if no order exists at target price
                if not any(abs(float(o['price']) - buy_price) < 1e-8 for o in open_buy_orders):
                    try:
                        buy_order = exchange.create_order(
                            symbol, 'limit', 'buy', buy_size, buy_price, {'type': 'maker'}
                        )
                        logging.info(f"Placed systematic buy order: price={buy_price}, size={buy_size}")
                    except Exception as e:
                        logging.error(f"Error placing buy order: {e}")
            else:
                open_buy = next((o for o in simulated_orders if o['status'] == 'open' and o['side'] == 'buy'), None)
                need_new_buy = True
                if open_buy is not None:
                    open_buy_price = float(open_buy['price'])
                    open_buy_size = float(open_buy['size'])
                    if abs(open_buy_price - buy_price) < 1e-8 and abs(open_buy_size - buy_size) < 1e-8:
                        need_new_buy = False
                if need_new_buy:
                    if open_buy is not None:
                        open_buy['status'] = 'cancelled'
                        log_shadow_trade(open_buy)
                        logging.info(f"[SHADOW] Cancelled simulated buy order at {open_buy['price']}")
                    buy_order = {
                        'id': simulated_order_id,
                        'side': 'buy',
                        'price': buy_price,
                        'size': buy_size,
                        'status': 'open',
                    }
                    simulated_order_id += 1
                    simulated_orders.append(buy_order)
                    logging.info(f"[SHADOW] Placed simulated systematic buy order: price={buy_price}, size={buy_size}")
                    log_shadow_trade(buy_order)
                    save_shadow_state()

        # Place sell order at systematic price - USE AVAILABLE BALANCE
        if total_to_sell > 0:
            if not shadow_mode:
                try:
                    open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
                except Exception as e:
                    logging.error(f"Error fetching open orders: {e}")
                    open_orders = []
                open_sell_orders = [o for o in open_orders if o['side'] == 'sell']
                
                # Cancel any existing sell orders at different prices
                for o in open_sell_orders:
                    if abs(float(o['price']) - sell_price) > 1e-8:
                        try:
                            exchange.cancel_order(o['id'], symbol)
                            logging.info(f"Cancelled existing sell order at {o['price']}")
                        except Exception as e:
                            logging.error(f"Error canceling sell order: {e}")
                
                # Place new sell order if no order exists at target price
                if not any(abs(float(o['price']) - sell_price) < 1e-8 for o in open_sell_orders):
                    try:
                        sell_order = exchange.create_order(
                            symbol, 'limit', 'sell', total_to_sell, sell_price, {'type': 'maker'}
                        )
                        logging.info(f"Placed systematic sell order: price={sell_price}, size={total_to_sell}")
                    except Exception as e:
                        logging.error(f"Error placing sell order: {e}")
            else:
                open_sell = next((o for o in simulated_orders if o['status'] == 'open' and o['side'] == 'sell'), None)
                need_new_sell = True
                if open_sell is not None:
                    open_sell_price = float(open_sell['price'])
                    open_sell_size = float(open_sell['size'])
                    if abs(open_sell_price - sell_price) < 1e-8 and abs(open_sell_size - total_to_sell) < 1e-8:
                        need_new_sell = False
                if need_new_sell:
                    if open_sell is not None:
                        open_sell['status'] = 'cancelled'
                        log_shadow_trade(open_sell)
                        logging.info(f"[SHADOW] Cancelled simulated sell order at {open_sell['price']}")
                    sell_order = {
                        'id': simulated_order_id,
                        'side': 'sell',
                        'price': sell_price,
                        'size': total_to_sell,
                        'status': 'open',
                    }
                    simulated_order_id += 1
                    simulated_orders.append(sell_order)
                    logging.info(f"[SHADOW] Placed simulated systematic sell order: price={sell_price}, size={total_to_sell}")
                    log_shadow_trade(sell_order)
                    save_shadow_state()

        # For shadow mode, try to fill simulated orders
        if shadow_mode:
            for order in simulated_orders:
                if order['status'] == 'open':
                    if simulate_order_fill(order, orderbook):
                        logging.info(f"[SHADOW] Simulated order filled: {order}")
                        save_shadow_state()
            logging.info(f"[SHADOW] Simulated P&L: {simulated_pnl}")

    except Exception as e:
        logging.error(f"Error updating orders: {e}")

def get_spot_balances():
    """Fetch both base and quote spot available (free) balances from Hyperliquid (main account) using ccxt."""
    try:
        balance = exchange.fetch_balance(params={'type': 'spot', 'user': user_address})
        base = balance['free'].get(base_asset, 0.0)
        quote = balance['free'].get(quote_asset, 0.0)
        logging.info(f"Spot balances parsed: base={base} {base_asset}, quote={quote} {quote_asset}")
        return base, quote
    except Exception as e:
        logging.error(f"Error fetching spot balances: {e}")
        return 0.0, 0.0

def get_real_balances():
    return get_spot_balances()

def get_real_position():
    base, _ = get_spot_balances()
    return base

def get_shadow_position():
    # Net filled buys - sells
    pos = 0.0
    for order in simulated_orders:
        if order['status'] == 'filled':
            if order['side'] == 'buy':
                pos += order['size']
            elif order['side'] == 'sell':
                pos -= order['size']
    return pos

# Security measures
def secure_api_call():
    exchange.reconnect_delay = reconnect_delay

def log_filled_volume():
    while True:
        buy_volume = 0.0
        sell_volume = 0.0
        try:
            with open('shadow_trades.log', 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        order = entry.get('order', {})
                        if order.get('status') == 'filled':
                            if order.get('side') == 'buy':
                                buy_volume += order.get('size', 0.0)
                            elif order.get('side') == 'sell':
                                sell_volume += order.get('size', 0.0)
                    except Exception:
                        continue
            report = f"[VOLUME] Total filled buy volume: {buy_volume}, sell volume: {sell_volume}"
            print(report)
            with open('volume_report.log', 'a') as vfile:
                vfile.write(report + '\n')
        except Exception as e:
            err = f"[VOLUME] Error reading shadow_trades.log: {e}"
            print(err)
            with open('volume_report.log', 'a') as vfile:
                vfile.write(err + '\n')
        time.sleep(30)

if __name__ == "__main__":
    secure_api_call()
    # Log all open orders on startup
    try:
        open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
        if open_orders:
            logging.info(f"Open orders on startup:")
            for o in open_orders:
                logging.info(f"Order: id={o['id']}, side={o['side']}, price={o['price']}, amount={o['amount']}, status={o['status']}")
            # --- RECONCILIATION: Do not place new orders if open orders exist ---
            logging.warning("Open orders exist on startup. No new orders will be placed until these are resolved.")
            # Wait for user intervention or manual cancel/fill
            while True:
                time.sleep(10)
                open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
                if not open_orders:
                    logging.info("All open orders cleared. Proceeding with trading.")
                    break
        else:
            logging.info("No open orders on startup.")
    except Exception as e:
        logging.error(f"Error fetching open orders on startup: {e}")
    ws = create_connection(websocket_url)
    subscribe_to_orderbook(ws)
    if shadow_mode:
        t = threading.Thread(target=log_filled_volume, daemon=True)
        t.start()
    while True:
        try:
            message = ws.recv()
            on_message(ws, message)
        except Exception as e:
            logging.error(f"WebSocket error: {e}")
            ws.close()
            break
    reconnect()
