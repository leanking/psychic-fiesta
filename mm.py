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

# Add logic mode and zone boundaries from environment
LOGIC_MODE = os.getenv('LOGIC_MODE', 'spread').lower()  # 'spread' or 'zone'
BUY_ZONE_LOW = float(os.getenv('BUY_ZONE_LOW', '0.0'))
BUY_ZONE_HIGH = float(os.getenv('BUY_ZONE_HIGH', '0.0'))
SELL_ZONE_LOW = float(os.getenv('SELL_ZONE_LOW', '0.0'))
SELL_ZONE_HIGH = float(os.getenv('SELL_ZONE_HIGH', '0.0'))
logging.info(f"Logic mode loaded: {LOGIC_MODE.upper()}")

print("API_Wallet:", api_key)
print("USER_ADDRESS:", user_address)

MIN_NOTIONAL = 10.0  # Minimum notional value for any order (in quote currency, e.g., $10)

# Add at the top of the file, after other globals
last_used_available_quote = None
last_used_available_base = None
STICKY_THRESHOLD = 0.001  # 0.1% change required to update order sizes
last_used_buy_prices = None
last_used_sell_prices = None

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

# Utility to calculate expected profit after all fees

def expected_profit_after_fees(buy_price, sell_price, size):
    buy_fee = buy_price * size * MAKER_FEE_RATE
    sell_fee = sell_price * size * MAKER_FEE_RATE
    profit = (sell_price - buy_price) * size - buy_fee - sell_fee - PER_TRADE_CLOSE_FEE
    return profit

# Utility to calculate break-even sell price given buy price, size, and fees

def breakeven_sell_price(buy_price, size):
    f = MAKER_FEE_RATE
    F = PER_TRADE_CLOSE_FEE
    Q = size
    # sell_price = (buy_price * (1 + f) + F / Q) / (1 - f)
    return (buy_price * (1 + f) + F / Q) / (1 - f)

def breakeven_buy_price(sell_price, size):
    f = MAKER_FEE_RATE
    F = PER_TRADE_CLOSE_FEE
    Q = size
    # buy_price = (sell_price * (1 - f) - F / Q) / (1 + f)
    return (sell_price * (1 - f) - F / Q) / (1 + f)

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

# Update and place orders
def update_orders(orderbook):
    global simulated_order_id, shadow_position
    global last_used_available_quote, last_used_available_base
    global last_used_buy_prices, last_used_sell_prices
    try:
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if bids and asks:
            logging.debug(f"update_orders called: best_bid={bids[0]['px']}, best_ask={asks[0]['px']}")
        else:
            logging.debug(f"update_orders called: bids or asks missing. bids={bids}, asks={asks}")
        if not bids or not asks:
            return

        best_bid = float(bids[0]['px'])
        best_ask = float(asks[0]['px'])
        buy_price = best_bid - 0.00001
        sell_price = best_ask + 0.00001

        # --- LOGIC MODE BRANCH ---
        if LOGIC_MODE == 'zone':
            in_buy_zone = BUY_ZONE_LOW <= best_ask <= BUY_ZONE_HIGH
            in_sell_zone = SELL_ZONE_LOW <= best_bid <= SELL_ZONE_HIGH
            if not in_buy_zone and not in_sell_zone:
                logging.info(f"No action: best_ask {best_ask} not in buy zone and best_bid {best_bid} not in sell zone.")
                return
            can_place_buy = in_buy_zone
            can_place_sell = in_sell_zone
        else:
            # --- SPREAD MODE: New multi-order logic ---
            if shadow_mode:
                logging.info("[SHADOW] Skipping multi-order logic in shadow mode.")
                return
            base_balance, quote_balance = get_real_balances()
            available_quote = max(0.0, quote_balance - min_quote_balance)
            available_base = max(0.0, base_balance - min_base_balance)
            # Sticky logic: Only update if available balance changes by more than threshold
            update_buys = True
            update_sells = True
            if last_used_available_quote is not None:
                if last_used_available_quote > 0:
                    rel_change = abs(available_quote - last_used_available_quote) / last_used_available_quote
                else:
                    rel_change = 1.0 if available_quote > 0 else 0.0
                if rel_change < STICKY_THRESHOLD:
                    update_buys = False
            if last_used_available_base is not None:
                if last_used_available_base > 0:
                    rel_change = abs(available_base - last_used_available_base) / last_used_available_base
                else:
                    rel_change = 1.0 if available_base > 0 else 0.0
                if rel_change < STICKY_THRESHOLD:
                    update_sells = False
            # --- Sticky logic for prices ---
            # Calculate target buy and sell prices as before
            min_size = 1.0  # for profit calc
            breakeven_sell = breakeven_sell_price(buy_price, min_size)
            target_sell_price = breakeven_sell + (min_profit / min_size)
            breakeven_buy = breakeven_buy_price(sell_price, min_size)
            target_buy_price = breakeven_buy - (min_profit / min_size)
            buy_outer = min(buy_price, target_buy_price)
            buy_tick = 0.00001 if buy_outer < 1.0 else 0.0001
            buy_prices = [buy_outer - i * buy_tick for i in range(3)]
            if best_ask >= 1.0:
                sell_prices = [1.0001, 1.0000, 0.9999]
                sell_tick = 0.0001
            else:
                sell_tick = 0.00001
                sell_prices = [best_ask + 3*sell_tick, best_ask + 2*sell_tick, best_ask + 1*sell_tick]
            # Only update if price changes by more than one tick
            update_buy_prices = [True] * len(buy_prices)
            update_sell_prices = [True] * len(sell_prices)
            if last_used_buy_prices is not None and len(last_used_buy_prices) == len(buy_prices):
                for i in range(len(buy_prices)):
                    if abs(buy_prices[i] - last_used_buy_prices[i]) <= buy_tick:
                        update_buy_prices[i] = False
                        logging.info(f"Sticky logic: Buy price {buy_prices[i]} change <= tick. Skipping update for this price.")
            if last_used_sell_prices is not None and len(last_used_sell_prices) == len(sell_prices):
                for i in range(len(sell_prices)):
                    if abs(sell_prices[i] - last_used_sell_prices[i]) <= sell_tick:
                        update_sell_prices[i] = False
                        logging.info(f"Sticky logic: Sell price {sell_prices[i]} change <= tick. Skipping update for this price.")
            # If all are False and balances are sticky, skip entirely
            if (not update_buys or not any(update_buy_prices)) and (not update_sells or not any(update_sell_prices)):
                logging.info(f"Sticky logic: No significant price or balance change. Skipping order update.")
                return
            # Update last used prices
            last_used_buy_prices = buy_prices.copy()
            last_used_sell_prices = sell_prices.copy()
            if update_buys:
                last_used_available_quote = available_quote
            if update_sells:
                last_used_available_base = available_base
            # BUY ORDERS
            # Outermost buy price from spread logic (current buy_price logic)
            min_size = 1.0  # for profit calc
            breakeven_sell = breakeven_sell_price(buy_price, min_size)
            target_sell_price = breakeven_sell + (min_profit / min_size)
            breakeven_buy = breakeven_buy_price(sell_price, min_size)
            target_buy_price = breakeven_buy - (min_profit / min_size)
            buy_outer = min(buy_price, target_buy_price)
            # Tick size for buy
            buy_tick = 0.00001 if buy_outer < 1.0 else 0.0001
            buy_prices = [buy_outer - i * buy_tick for i in range(3)]
            # SELL ORDERS
            if best_ask >= 1.0:
                sell_prices = [1.0001, 1.0000, 0.9999]
                sell_tick = 0.0001
            else:
                sell_tick = 0.00001
                sell_prices = [best_ask + 3*sell_tick, best_ask + 2*sell_tick, best_ask + 1*sell_tick]
            # Sizing weights
            def get_weights(n):
                if n == 3:
                    return [0.4, 0.35, 0.25]
                elif n == 2:
                    return [0.6, 0.4]
                else:
                    return [1.0]
            # BUY SIZES
            max_buy_size = available_quote / buy_outer if buy_outer > 0 else 0.0
            buy_weights = get_weights(3)
            buy_sizes = [max_buy_size * w for w in buy_weights]
            # Check notional for each, fallback to 2 or 1 if needed
            buy_n = 3
            for n in [3,2,1]:
                weights = get_weights(n)
                sizes = [max_buy_size * w for w in weights]
                notionals = [sizes[i] * buy_prices[i] for i in range(n)]
                if all(nl >= MIN_NOTIONAL for nl in notionals):
                    buy_n = n
                    buy_sizes = sizes[:n]
                    buy_prices = buy_prices[:n]
                    break
            # SELL SIZES
            max_sell_size = available_base
            sell_weights = get_weights(3)
            sell_sizes = [max_sell_size * w for w in sell_weights]
            sell_n = 3
            for n in [3,2,1]:
                weights = get_weights(n)
                sizes = [max_sell_size * w for w in weights]
                notionals = [sizes[i] * sell_prices[i] for i in range(n)]
                if all(nl >= MIN_NOTIONAL for nl in notionals):
                    sell_n = n
                    sell_sizes = sizes[:n]
                    sell_prices = sell_prices[:n]
                    break
            # Cancel/replace logic
            try:
                open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
            except Exception as e:
                logging.error(f"Error fetching open orders: {e}")
                open_orders = []
            # Helper to quantize size to 5 decimals
            def quantize_size(size):
                return round(size, 5)
            # Cancel all open buy orders at our target prices
            open_buy_orders = [o for o in open_orders if o['side'] == 'buy']
            for o in open_buy_orders:
                if not any(abs(float(o['price']) - p) < 1e-8 for p in buy_prices):
                    try:
                        exchange.cancel_order(o['id'], symbol)
                        logging.info(f"Cancelled buy order at {o['price']}")
                    except Exception as e:
                        logging.error(f"Error canceling buy order: {e}")
            # Cancel all open sell orders at our target prices
            open_sell_orders = [o for o in open_orders if o['side'] == 'sell']
            for o in open_sell_orders:
                if not any(abs(float(o['price']) - p) < 1e-8 for p in sell_prices):
                    try:
                        exchange.cancel_order(o['id'], symbol)
                        logging.info(f"Cancelled sell order at {o['price']}")
                    except Exception as e:
                        logging.error(f"Error canceling sell order: {e}")
            # Place buy orders only if not already present at price and size (quantized)
            for i in range(buy_n):
                size = quantize_size(buy_sizes[i])
                price = buy_prices[i]
                notional = size * price
                # Check for existing order at this price and size (quantized, tolerance 1e-5)
                exists = False
                for o in open_buy_orders:
                    price_match = abs(float(o['price']) - price) < 1e-8
                    size_match = abs(float(o['amount']) - size) < 1e-5
                    if price_match and size_match:
                        exists = True
                        break
                if size > 0 and notional >= MIN_NOTIONAL:
                    if exists:
                        for o in open_buy_orders:
                            if abs(float(o['price']) - price) < 1e-8:
                                size_diff = float(o['amount']) - size
                                logging.info(f"Skipped placing buy order at {price} size {size}: identical order already exists (size diff: {size_diff:+.8f}).")
                                break
                    else:
                        try:
                            buy_order = exchange.create_order(
                                symbol, 'limit', 'buy', size, price, {'type': 'maker'}
                            )
                            logging.info(f"Placed buy order: price={price}, size={size}")
                        except Exception as e:
                            logging.error(f"Error placing buy order: {e}")
            # Place sell orders only if not already present at price and size (quantized)
            for i in range(sell_n):
                size = quantize_size(sell_sizes[i])
                price = sell_prices[i]
                notional = size * price
                exists = False
                for o in open_sell_orders:
                    price_match = abs(float(o['price']) - price) < 1e-8
                    size_match = abs(float(o['amount']) - size) < 1e-5
                    if price_match and size_match:
                        exists = True
                        break
                if size > 0 and notional >= MIN_NOTIONAL:
                    if exists:
                        for o in open_sell_orders:
                            if abs(float(o['price']) - price) < 1e-8:
                                size_diff = float(o['amount']) - size
                                logging.info(f"Skipped placing sell order at {price} size {size}: identical order already exists (size diff: {size_diff:+.8f}).")
                                break
                    else:
                        try:
                            sell_order = exchange.create_order(
                                symbol, 'limit', 'sell', size, price, {'type': 'maker'}
                            )
                            logging.info(f"Placed sell order: price={price}, size={size}")
                        except Exception as e:
                            logging.error(f"Error placing sell order: {e}")
            return

        if shadow_mode:
            position = get_shadow_position()
        else:
            position = get_real_position()
        logging.info(f"Current position: {position}")

        if not shadow_mode:
            base_balance, quote_balance = get_real_balances()
            logging.info(f"Base balance: {base_balance}, Quote balance: {quote_balance}")
        else:
            base_balance, quote_balance = 0.0, 0.0  # Not used in shadow mode

        if not shadow_mode:
            buy_size = min(max_order_size, max(0.0, (quote_balance - min_quote_balance) / buy_price))
            total_to_sell = max(0.0, base_balance - min_base_balance)
        else:
            buy_size = max_order_size
            total_to_sell = max_order_size

        # Log available balances for order sizing
        if not shadow_mode:
            logging.info(f"Available quote for buy: {quote_balance}, calculated buy_size: {buy_size}")
            logging.info(f"Available base for sell: {base_balance}, calculated total_to_sell: {total_to_sell}")
        else:
            logging.info(f"[SHADOW] Simulated buy_size: {buy_size}, simulated total_to_sell: {total_to_sell}")

        can_place_buy = can_place_buy and (position + buy_size <= max_position and
                         (not shadow_mode and quote_balance > min_quote_balance and base_balance < max_base_balance) or
                         (shadow_mode))
        can_place_sell = can_place_sell and (position - total_to_sell >= -max_position and
                          (not shadow_mode and base_balance > min_base_balance and quote_balance < max_quote_balance) or
                          (shadow_mode))

        # Place buy order
        if can_place_buy and buy_size > 0:
            buy_notional = buy_size * buy_price
            if buy_notional < MIN_NOTIONAL:
                logging.info(f"Buy order not placed: notional ${buy_notional:.2f} is below minimum ${MIN_NOTIONAL}.")
            else:
                if not shadow_mode:
                    try:
                        open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
                    except Exception as e:
                        logging.error(f"Error fetching open orders: {e}")
                        open_orders = []
                    open_buy_orders = [o for o in open_orders if o['side'] == 'buy']
                    committed_quote = sum(float(o['price']) * float(o['amount']) for o in open_buy_orders)
                    available_quote = quote_balance - committed_quote
                    logging.info(f"Available quote after committed: {available_quote}")
                    if available_quote < buy_size * buy_price:
                        logging.warning(f"Insufficient quote balance to place buy order: required={buy_size * buy_price}, available={available_quote}")
                    else:
                        for o in open_buy_orders:
                            if abs(float(o['price']) - buy_price) > 1e-8:
                                try:
                                    exchange.cancel_order(o['id'], symbol)
                                    logging.info(f"Cancelled duplicate buy order at {o['price']}")
                                except Exception as e:
                                    logging.error(f"Error canceling buy order: {e}")
                        if not any(abs(float(o['price']) - buy_price) < 1e-8 for o in open_buy_orders):
                            try:
                                buy_order = exchange.create_order(
                                    symbol, 'limit', 'buy', buy_size, buy_price, {'type': 'maker'}
                                )
                                logging.info(f"Placed buy order: price={buy_price}, size={buy_size}")
                            except Exception as e:
                                logging.error(f"Error placing buy order: {e}")
                else:
                    open_buy = next((o for o in simulated_orders if o['status'] == 'open' and o['side'] == 'buy'), None)
                    open_sell = next((o for o in simulated_orders if o['status'] == 'open' and o['side'] == 'sell'), None)
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
                        logging.info(f"[SHADOW] Placed simulated buy order: price={buy_price}, size={buy_size}")
                        log_shadow_trade(buy_order)
                        save_shadow_state()
        else:
            logging.info("Buy order not placed: inventory or balance constraints.")

        # Place sell order(s) with dynamic sizing and thin liquidity consideration
        if can_place_sell and total_to_sell > 0:
            sell_notional = total_to_sell * sell_price
            if sell_notional < MIN_NOTIONAL:
                logging.info(f"Sell order not placed: notional ${sell_notional:.2f} is below minimum ${MIN_NOTIONAL}.")
            else:
                if not shadow_mode:
                    try:
                        open_orders = exchange.fetch_open_orders(symbol, params={'user': user_address})
                    except Exception as e:
                        logging.error(f"Error fetching open orders: {e}")
                        open_orders = []
                    open_sell_orders = [o for o in open_orders if o['side'] == 'sell']
                    committed_base = sum(float(o['amount']) for o in open_sell_orders)
                    available_base = base_balance - committed_base
                    logging.info(f"Available base after committed: {available_base}")
                    orders_placed = 0
                    remaining_to_sell = available_base
                    while remaining_to_sell > 0 and orders_placed < 3:
                        this_order_size = min(max_order_size, remaining_to_sell)
                        if this_order_size <= 0:
                            break
                        if not any(abs(float(o['price']) - sell_price) < 1e-8 and abs(float(o['amount']) - this_order_size) < 1e-8 for o in open_sell_orders):
                            try:
                                sell_order = exchange.create_order(
                                    symbol, 'limit', 'sell', this_order_size, sell_price, {'type': 'maker'}
                                )
                                logging.info(f"Placed sell order: price={sell_price}, size={this_order_size}")
                                orders_placed += 1
                            except Exception as e:
                                logging.error(f"Error placing sell order: {e}")
                        remaining_to_sell -= this_order_size
                    if orders_placed == 0:
                        logging.info("Sell order not placed: already at target price/size or inventory constraints.")
                else:
                    open_buy = next((o for o in simulated_orders if o['status'] == 'open' and o['side'] == 'buy'), None)
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
                        logging.info(f"[SHADOW] Placed simulated sell order: price={sell_price}, size={total_to_sell}")
                        log_shadow_trade(sell_order)
                        save_shadow_state()
        else:
            logging.info("Sell order not placed: inventory or balance constraints.")

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