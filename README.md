# Market Maker Bot

A Python-based cryptocurrency market maker bot that provides liquidity to exchanges through automated order placement and management.

## Features

- **Real-time Orderbook Monitoring**: Uses WebSocket connections for live market data
- **Automated Order Management**: Places and manages buy/sell orders based on configurable parameters
- **Shadow Mode**: Test trading strategies without real money
- **Systematic Trading**: Predefined price levels for systematic market making
- **Risk Management**: Configurable position limits and balance thresholds
- **Fee Calculation**: Includes maker fees and per-trade close fees in profit calculations

## Files

- `mm.py` - Main market maker script with dynamic pricing
- `systematic_mm.py` - Systematic market maker with fixed price levels
- `test.py` - Test script for checking open orders
- `requirements.txt` - Python dependencies
- `env.example` - Example environment configuration

## Setup

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd Market-Maker
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On macOS/Linux
   # or
   .venv\Scripts\activate  # On Windows
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**:
   ```bash
   cp env.example .env
   # Edit .env with your API credentials and settings
   ```

5. **Run the bot**:
   ```bash
   # For dynamic market making
   python mm.py
   
   # For systematic market making
   python systematic_mm.py
   
   # To test connection
   python test.py
   ```

## Configuration

Copy `env.example` to `.env` and configure the following variables:

- `EXCHANGE_NAME`: Exchange name (e.g., 'dydx')
- `API_KEY`: Your API wallet address
- `PRIVATE_KEY`: Your API wallet private key
- `WEBSOCKET_URL`: WebSocket endpoint for real-time data
- `SYMBOL`: Trading pair (e.g., 'USDHL/USDC')
- `ORDER_SIZE`: Size of orders to place
- `BASE_SPREAD`: Base spread percentage
- `MIN_PROFIT`: Minimum profit threshold
- `TESTNET`: Set to 'true' for testing
- `SHADOW_MODE`: Set to 'true' for simulation mode

## Dependencies

- `ccxt` - Cryptocurrency exchange library
- `python-dotenv` - Environment variable management
- `websocket-client` - WebSocket connections
- `requests` - HTTP requests

## Security

⚠️ **Important**: Never commit your `.env` file or any files containing API keys or private keys. The `.gitignore` file is configured to exclude these sensitive files.

## License

[Add your license here]

## Disclaimer

This software is for educational purposes. Use at your own risk. Cryptocurrency trading involves substantial risk of loss. 