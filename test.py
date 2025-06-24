import ccxt
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

exchange_name = os.getenv('EXCHANGE_NAME')
api_key = os.getenv('API_KEY')
private_key = os.getenv('PRIVATE_KEY')
symbol = os.getenv('SYMBOL')
user_address = os.getenv('USER_ADDRESS')
rate_limit = int(os.getenv('RATE_LIMIT', '1000'))
testnet = os.getenv('TESTNET', 'false').lower() == 'true'

exchange = getattr(ccxt, exchange_name)({
    'apiKey': api_key,
    'privateKey': private_key,
    'walletAddress': api_key,
    'enableRateLimit': True,
    'rateLimit': rate_limit,
    'testnet': testnet,
})

params = {
    'user': user_address,
    # 'method': 'frontendOpenOrders',  # Optional, default is 'frontendOpenOrders'
}

try:
    open_orders = exchange.fetch_open_orders(symbol, params=params)
    print(f"Open orders for {symbol}:")
    for order in open_orders:
        print(order)
    if not open_orders:
        print("No open orders.")
except Exception as e:
    print(f"Error fetching open orders: {e}")




