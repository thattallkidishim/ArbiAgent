import os
import logging
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- Logging setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ARBISCAN_API_KEY = os.getenv("ARBISCAN_API_KEY", "")
ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"

if not BOT_TOKEN:
    raise EnvironmentError("BOT_TOKEN environment variable is not set.")

WELCOME = (
    "Hey, welcome.\n\n"
    "Send any Arbitrum wallet address and I will take a quick look at it.\n"
    "Activity, balance, token holdings, and what it might say about the person behind it.\n\n"
    "No noise. Just a clean read.\n\n"
    "Commands:\n"
    "/start - Show this message\n"
    "/help - How to use this bot"
)

HELP = (
    "How to use:\n\n"
    "Just paste any Ethereum or Arbitrum wallet address (starts with 0x, 42 characters).\n\n"
    "I will check:\n"
    "- ETH balance\n"
    "- Recent transactions\n"
    "- Token holdings (ERC-20)\n"
    "- First and last activity\n"
    "- Wallet age\n"
    "- Activity insight\n"
    "- Overall score\n\n"
    "Example:\n"
    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
)


# --- Data fetchers ---

def get_eth_balance(address: str) -> float:
    try:
        data = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1
        }
        res = requests.post(ARBITRUM_RPC, json=data, timeout=10)
        res.raise_for_status()
        result = res.json().get("result")
        if result is None:
            return 0.0
        return int(result, 16) / 1e18
    except Exception as e:
        logger.error("Error fetching balance for %s: %s", address, e)
        return 0.0


def get_transactions(address: str, limit: int = 20) -> list:
    try:
        url = (
            "https://api.arbiscan.io/api"
            "?module=account&action=txlist"
            "&address=" + address +
            "&page=1&offset=" + str(limit) + "&sort=desc"
            "&apikey=" + ARBISCAN_API_KEY
        )
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("status") == "1":
            return data.get("result", [])
        return []
    except Exception as e:
        logger.error("Error fetching transactions for %s: %s", address, e)
        return []


def get_token_holdings(address: str) -> list:
    try:
        url = (
            "https://api.arbiscan.io/api"
            "?module=account&action=tokentx"
            "&address=" + address +
            "&page=1&offset=50&sort=desc"
            "&apikey=" + ARBISCAN_API_KEY
        )
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("status") == "1":
            txs = data.get("result", [])
            seen = {}
            for tx in txs:
                symbol = tx.get("tokenSymbol", "?")
                name = tx.get("tokenName", "?")
                if symbol not in seen:
                    seen[symbol] = name
            return list(seen.items())[:5]
        return []
    except Exception as e:
        logger.error("Error fetching token holdings for %s: %s", address, e)
        return []


def get_wallet_age(txs: list) -> str:
    try:
        if not txs:
            return "unknown"
        oldest = min(txs, key=lambda t: int(t.get("timeStamp", 0)))
        ts = int(oldest.get("timeStamp", 0))
        if ts == 0:
            return "unknown"
        dt = datetime.utcfromtimestamp(ts)
        now = datetime.utcnow()
        days = (now - dt).days
        if days < 30:
            return str(days) + " days"
        elif days < 365:
            return str(days // 30) + " months"
        else:
            years = days // 365
            months = (days % 365) // 30
            return str(years) + "y " + str(months) + "m"
    except Exception:
        return "unknown"


def get_last_active(txs: list) -> str:
    try:
        if not txs:
            return "never"
        latest = max(txs, key=lambda t: int(t.get("timeStamp", 0)))
        ts = int(latest.get("timeStamp", 0))
        if ts == 0:
            return "unknown"
        dt = datetime.utcfromtimestamp(ts)
        now = datetime.utcnow()
        days = (now - dt).days
        if days == 0:
            return "today"
        elif days == 1:
            return "yesterday"
        elif days < 30:
            return str(days) + " days ago"
        elif days < 365:
            return str(days // 30) + " months ago"
        else:
            return str(days // 365) + " years ago"
    except Exception:
        return "unknown"


def count_failed_txs(txs: list) -> int:
    return sum(1 for tx in txs if tx.get("isError") == "1")


# --- Analysis ---

def generate_insight(tx_count: int, balance: float) -> str:
    if tx_count > 20:
        note = (
            "This wallet is very active on-chain. "
            "Frequent interactions usually point to someone deeply engaged in DeFi or trading."
        )
    elif tx_count > 5:
        note = (
            "This wallet has a steady level of activity. "
            "Not aggressive, but clearly not idle either."
        )
    else:
        note = (
            "This wallet seems relatively quiet. "
            "It may be holding assets rather than actively using them."
        )

    if balance > 1:
        balance_note = (
            "It holds a noticeable ETH balance, "
            "which can suggest confidence or longer-term positioning."
        )
    elif balance > 0:
        balance_note = (
            "The balance is small, "
            "so activity here may be more experimental than strategic."
        )
    else:
        balance_note = "The wallet is currently empty on ETH."

    return note + "\n\n" + balance_note


def score_wallet(tx_count: int, balance: float, failed: int, age: str) -> float:
    balance_score = min(balance * 0.5, 3.0)
    tx_score = min(tx_count * 0.4, 5.0)
    penalty = min(failed * 0.3, 2.0)
    age_bonus = 0.5 if "y" in age else 0.0
    return min(10.0, round(tx_score + balance_score + age_bonus - penalty, 1))


def get_risk_label(tx_count: int, balance: float, failed: int) -> str:
    if failed > 3:
        return "elevated (many failed txs)"
    if tx_count > 10 and balance < 0.1:
        return "moderate (high activity, low balance)"
    if tx_count == 0:
        return "low (no activity)"
    return "normal"


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.strip()
    address = raw.lower()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "That does not look like a valid wallet address.\n"
            "Send a proper 0x address (42 characters total)."
        )
        return

    if not all(c in "0123456789abcdef" for c in address[2:]):
        await update.message.reply_text(
            "That address has invalid characters.\n"
            "Only 0-9 and a-f are allowed after 0x."
        )
        return

    await update.message.reply_text("Checking that wallet... \U0001f50d")

    try:
        balance = get_eth_balance(address)
        txs = get_transactions(address, limit=20)
        tokens = get_token_holdings(address)
        tx_count = len(txs)
        failed = count_failed_txs(txs)
        age = get_wallet_age(txs)
        last_active = get_last_active(txs)
        insight = generate_insight(tx_count, balance)
        score = score_wallet(tx_count, balance, failed, age)
        risk = get_risk_label(tx_count, balance, failed)

        # Recent tx lines
        if txs:
            tx_lines = ""
            for tx in txs[:3]:
                tx_hash = tx.get("hash", "")
                short = tx_hash[:10] + "..." if tx_hash else "unknown"
                status = "failed" if tx.get("isError") == "1" else "ok"
                tx_lines += "- " + short + " [" + status + "]\n"
        else:
            tx_lines = "- no transactions found\n"

        # Token lines
        if tokens:
            token_lines = ""
            for symbol, name in tokens:
                token_lines += "- " + symbol + " (" + name + ")\n"
        else:
            token_lines = "- none detected\n"

        response = (
            "Wallet Analysis\n"
            "--------------------\n\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n"
            "Balance: " + str(round(balance, 4)) + " ETH\n"
            "Transactions: " + str(tx_count) + " recent\n"
            "Failed txs: " + str(failed) + "\n"
            "Wallet age: " + age + "\n"
            "Last active: " + last_active + "\n\n"
            "Token Activity:\n" + token_lines + "\n"
            "Recent Transactions:\n" + tx_lines + "\n"
            "Insight:\n" + insight + "\n\n"
            "Risk level: " + risk + "\n"
            "Score: " + str(score) + " / 10\n\n"
            "Send another address to keep going \U0001f44d"
        )

        await update.message.reply_text(response)

    except Exception as e:
        logger.error("Unhandled error in handle_message for %s: %s", address, e)
        await update.message.reply_text(
            "Something went wrong while checking that wallet. Try again in a bit."
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("ArbiAgent is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
