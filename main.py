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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ARBISCAN_API_KEY = os.getenv("ARBISCAN_API_KEY", "")
ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"

if not BOT_TOKEN:
    raise EnvironmentError("BOT_TOKEN environment variable is not set.")
if not GROQ_API_KEY:
    raise EnvironmentError("GROQ_API_KEY environment variable is not set.")

WELCOME = (
    "Hey, welcome.\n\n"
    "I am ArbiAgent -- an onchain analyst living inside Arbitrum.\n\n"
    "Drop any wallet address and I will read it for you. "
    "Activity, balance, token history, wallet age, risk feel, and a real take on what is going on.\n\n"
    "No fluff. Just signal.\n\n"
    "/help to see how it works."
)

HELP = (
    "How to use ArbiAgent:\n\n"
    "Paste any Ethereum or Arbitrum wallet address (starts with 0x, 42 characters).\n\n"
    "I will pull:\n"
    "- ETH balance\n"
    "- Recent transactions + failed tx count\n"
    "- Token history (ERC-20 interactions)\n"
    "- Wallet age and last activity\n"
    "- An AI-generated read on the wallet\n\n"
    "Then I give you my honest take -- what kind of wallet this is, "
    "what the behavior suggests, and what to watch out for.\n\n"
    "Example address:\n"
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


def get_token_interactions(address: str) -> list:
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
            return list(seen.items())[:6]
        return []
    except Exception as e:
        logger.error("Error fetching token interactions for %s: %s", address, e)
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
        days = (datetime.utcnow() - dt).days
        if days < 30:
            return str(days) + " days"
        elif days < 365:
            return str(days // 30) + " months"
        else:
            return str(days // 365) + "y " + str((days % 365) // 30) + "m"
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
        days = (datetime.utcnow() - datetime.utcfromtimestamp(ts)).days
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


def score_wallet(tx_count: int, balance: float, failed: int, age: str) -> float:
    balance_score = min(balance * 0.5, 3.0)
    tx_score = min(tx_count * 0.4, 5.0)
    penalty = min(failed * 0.3, 2.0)
    age_bonus = 0.5 if "y" in age else 0.0
    return min(10.0, round(tx_score + balance_score + age_bonus - penalty, 1))


# --- AI analysis via Groq (free) ---

def get_ai_analysis(address: str, balance: float, tx_count: int, failed: int,
                    age: str, last_active: str, tokens: list, score: float) -> str:
    try:
        token_str = ", ".join([s for s, _ in tokens]) if tokens else "none detected"

        prompt = (
            "You are ArbiAgent, a sharp and lively onchain analyst. "
            "You analyze Arbitrum wallets and give real, human-sounding takes. "
            "You are direct, a little informal, and genuinely insightful. "
            "Never sound like a robot. Never use bullet points. "
            "Write 3-4 sentences in flowing prose like a real analyst talking to someone.\n\n"
            "Wallet data:\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n"
            "ETH Balance: " + str(round(balance, 4)) + " ETH\n"
            "Recent transactions: " + str(tx_count) + "\n"
            "Failed transactions: " + str(failed) + "\n"
            "Wallet age: " + age + "\n"
            "Last active: " + last_active + "\n"
            "Token interactions: " + token_str + "\n"
            "Score: " + str(score) + "/10\n\n"
            "Give your analyst take on this wallet. What type of wallet is this? "
            "What does the behavior suggest? What should someone watch out for or note? "
            "Keep it punchy, real, and interesting."
        )

        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + GROQ_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 300,
                "temperature": 0.8
            },
            timeout=20
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()

    except Exception as e:
        logger.error("Error calling Groq API: %s", e)
        return "AI analysis unavailable right now -- but the data above tells a clear story."


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
            "Send a 0x address that is 42 characters long."
        )
        return

    if not all(c in "0123456789abcdef" for c in address[2:]):
        await update.message.reply_text(
            "That address has invalid characters.\n"
            "Only 0-9 and a-f are allowed after 0x."
        )
        return

    await update.message.reply_text("On it... pulling the data now \U0001f50d")

    try:
        balance = get_eth_balance(address)
        txs = get_transactions(address, limit=20)
        tokens = get_token_interactions(address)
        tx_count = len(txs)
        failed = count_failed_txs(txs)
        age = get_wallet_age(txs)
        last_active = get_last_active(txs)
        score = score_wallet(tx_count, balance, failed, age)

        if txs:
            tx_lines = ""
            for tx in txs[:3]:
                tx_hash = tx.get("hash", "")
                short = tx_hash[:10] + "..." if tx_hash else "unknown"
                status = "failed" if tx.get("isError") == "1" else "ok"
                tx_lines += "  " + short + " [" + status + "]\n"
        else:
            tx_lines = "  no transactions found\n"

        if tokens:
            token_lines = ""
            for symbol, name in tokens:
                token_lines += "  " + symbol + " -- " + name + "\n"
        else:
            token_lines = "  none detected\n"

        ai_take = get_ai_analysis(
            address, balance, tx_count, failed, age, last_active, tokens, score
        )

        response = (
            "Wallet Report\n"
            "========================\n\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n"
            "Balance: " + str(round(balance, 4)) + " ETH\n"
            "Wallet age: " + age + "\n"
            "Last active: " + last_active + "\n"
            "Transactions: " + str(tx_count) + " recent\n"
            "Failed: " + str(failed) + "\n"
            "Score: " + str(score) + " / 10\n\n"
            "Token History:\n" + token_lines + "\n"
            "Recent Transactions:\n" + tx_lines + "\n"
            "My Take:\n" + ai_take + "\n\n"
            "Drop another address whenever you are ready."
        )

        await update.message.reply_text(response)

    except Exception as e:
        logger.error("Unhandled error in handle_message for %s: %s", address, e)
        await update.message.reply_text(
            "Something went wrong pulling that wallet. Try again in a moment."
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("ArbiAgent is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
