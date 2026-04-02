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
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
MAINNET_RPC = "https://eth.llamarpc.com"

if not BOT_TOKEN:
    raise EnvironmentError("BOT_TOKEN environment variable is not set.")
if not GROQ_API_KEY:
    raise EnvironmentError("GROQ_API_KEY environment variable is not set.")

WELCOME = (
    "Hey, welcome.\n\n"
    "I am ArbiAgent -- an onchain analyst.\n\n"
    "Drop any wallet address and I will dig into it. "
    "I check both Arbitrum and Ethereum mainnet, so nothing slips through.\n\n"
    "Balance, activity, tokens, age, risk -- and a real take, not a template.\n\n"
    "/help to see how it works."
)

HELP = (
    "How to use ArbiAgent:\n\n"
    "Paste any Ethereum wallet address (0x, 42 characters).\n\n"
    "I check:\n"
    "- ETH balance on Arbitrum and Mainnet\n"
    "- Transaction history on both chains\n"
    "- Token interactions\n"
    "- Wallet age and last activity\n"
    "- AI-generated analyst take\n\n"
    "Example:\n"
    "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
)


# --- Data fetchers ---

def get_balance_on_chain(address: str, rpc: str) -> float:
    try:
        res = requests.post(rpc, json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1
        }, timeout=10)
        res.raise_for_status()
        result = res.json().get("result")
        if result is None:
            return 0.0
        return int(result, 16) / 1e18
    except Exception as e:
        logger.error("Balance fetch failed (%s): %s", rpc, e)
        return 0.0


def get_txs_arbiscan(address: str, limit: int = 20) -> list:
    try:
        url = (
            "https://api.arbiscan.io/api?module=account&action=txlist"
            "&address=" + address +
            "&page=1&offset=" + str(limit) + "&sort=desc"
            "&apikey=" + ARBISCAN_API_KEY
        )
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") == "1":
            return data.get("result", [])
        return []
    except Exception as e:
        logger.error("Arbiscan tx fetch failed: %s", e)
        return []


def get_txs_etherscan(address: str, limit: int = 20) -> list:
    try:
        url = (
            "https://api.etherscan.io/api?module=account&action=txlist"
            "&address=" + address +
            "&page=1&offset=" + str(limit) + "&sort=desc"
            "&apikey=" + ETHERSCAN_API_KEY
        )
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") == "1":
            return data.get("result", [])
        return []
    except Exception as e:
        logger.error("Etherscan tx fetch failed: %s", e)
        return []


def get_token_interactions(address: str) -> list:
    seen = {}
    # Try Arbitrum first
    try:
        url = (
            "https://api.arbiscan.io/api?module=account&action=tokentx"
            "&address=" + address +
            "&page=1&offset=50&sort=desc&apikey=" + ARBISCAN_API_KEY
        )
        res = requests.get(url, timeout=10).json()
        if res.get("status") == "1":
            for tx in res.get("result", []):
                s = tx.get("tokenSymbol", "?")
                if s not in seen:
                    seen[s] = tx.get("tokenName", "?")
    except Exception:
        pass
    # Also try Mainnet
    try:
        url = (
            "https://api.etherscan.io/api?module=account&action=tokentx"
            "&address=" + address +
            "&page=1&offset=50&sort=desc&apikey=" + ETHERSCAN_API_KEY
        )
        res = requests.get(url, timeout=10).json()
        if res.get("status") == "1":
            for tx in res.get("result", []):
                s = tx.get("tokenSymbol", "?")
                if s not in seen:
                    seen[s] = tx.get("tokenName", "?")
    except Exception:
        pass
    return list(seen.items())[:8]


def get_wallet_age(txs: list) -> str:
    try:
        if not txs:
            return "unknown"
        oldest = min(txs, key=lambda t: int(t.get("timeStamp", 0)))
        ts = int(oldest.get("timeStamp", 0))
        if ts == 0:
            return "unknown"
        days = (datetime.utcnow() - datetime.utcfromtimestamp(ts)).days
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


def count_failed(txs: list) -> int:
    return sum(1 for tx in txs if tx.get("isError") == "1")


def score_wallet(tx_count: int, balance: float, failed: int, age: str) -> float:
    b = min(balance * 0.4, 3.0)
    t = min(tx_count * 0.35, 5.0)
    p = min(failed * 0.3, 2.0)
    a = 0.5 if "y" in age else 0.0
    return min(10.0, round(t + b + a - p, 1))


# --- AI via Groq ---

def get_ai_analysis(address: str, arb_balance: float, eth_balance: float,
                    arb_txs: list, eth_txs: list, tokens: list,
                    age: str, last_active: str, score: float) -> str:
    try:
        total_txs = len(arb_txs) + len(eth_txs)
        failed = count_failed(arb_txs) + count_failed(eth_txs)
        token_str = ", ".join([s for s, _ in tokens]) if tokens else "none"

        # Pull a few real tx details to give AI actual context
        sample_txs = ""
        for tx in (arb_txs + eth_txs)[:4]:
            val = round(int(tx.get("value", "0")) / 1e18, 4)
            ts = int(tx.get("timeStamp", 0))
            date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"
            status = "failed" if tx.get("isError") == "1" else "ok"
            sample_txs += "  " + date + " | " + str(val) + " ETH | " + status + "\n"

        if not sample_txs:
            sample_txs = "  no transaction detail available\n"

        prompt = (
            "You are ArbiAgent, a brutally sharp onchain analyst. "
            "You read wallets like a detective reads a crime scene. "
            "You are direct, specific, a little edgy, and never generic. "
            "You ONLY talk about what the data actually shows. "
            "No filler. No 'it could be'. If the data is thin, say what that actually means. "
            "Write exactly 3 punchy sentences. No bullet points. No greetings.\n\n"
            "Here is the real data:\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n"
            "Arbitrum ETH: " + str(round(arb_balance, 4)) + "\n"
            "Mainnet ETH: " + str(round(eth_balance, 4)) + "\n"
            "Total transactions: " + str(total_txs) + "\n"
            "Failed transactions: " + str(failed) + "\n"
            "Wallet age: " + age + "\n"
            "Last active: " + last_active + "\n"
            "Tokens touched: " + token_str + "\n"
            "Score: " + str(score) + "/10\n\n"
            "Recent transaction samples:\n" + sample_txs + "\n"
            "Now give your take. Be specific to THIS data. "
            "What does this wallet actually tell you? What kind of person or entity is behind it? "
            "What stands out, and what should someone pay attention to?"
        )

        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + GROQ_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 250,
                "temperature": 0.85
            },
            timeout=20
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()

    except Exception as e:
        logger.error("Groq API error: %s", e)
        return "AI analysis offline right now -- read the numbers above, they speak for themselves."


# --- Telegram handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.strip()
    address = raw.lower()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "That is not a valid wallet address.\n"
            "Send a 0x address that is exactly 42 characters."
        )
        return

    if not all(c in "0123456789abcdef" for c in address[2:]):
        await update.message.reply_text(
            "Invalid characters in that address.\n"
            "Only 0-9 and a-f after the 0x."
        )
        return

    await update.message.reply_text("Scanning both chains... \U0001f50d")

    try:
        arb_balance = get_balance_on_chain(address, ARBITRUM_RPC)
        eth_balance = get_balance_on_chain(address, MAINNET_RPC)
        arb_txs = get_txs_arbiscan(address)
        eth_txs = get_txs_etherscan(address)
        tokens = get_token_interactions(address)

        all_txs = arb_txs + eth_txs
        total_txs = len(all_txs)
        failed = count_failed(all_txs)
        age = get_wallet_age(all_txs)
        last_active = get_last_active(all_txs)
        score = score_wallet(total_txs, arb_balance + eth_balance, failed, age)

        # Recent tx display
        if all_txs:
            sorted_txs = sorted(all_txs, key=lambda t: int(t.get("timeStamp", 0)), reverse=True)
            tx_lines = ""
            for tx in sorted_txs[:4]:
                tx_hash = tx.get("hash", "")
                short = tx_hash[:10] + "..." if tx_hash else "unknown"
                val = round(int(tx.get("value", "0")) / 1e18, 4)
                status = "failed" if tx.get("isError") == "1" else "ok"
                tx_lines += "  " + short + " | " + str(val) + " ETH [" + status + "]\n"
        else:
            tx_lines = "  no transactions found on either chain\n"

        # Token display
        if tokens:
            token_lines = ""
            for symbol, name in tokens:
                token_lines += "  " + symbol + " -- " + name + "\n"
        else:
            token_lines = "  none detected\n"

        ai_take = get_ai_analysis(
            address, arb_balance, eth_balance,
            arb_txs, eth_txs, tokens,
            age, last_active, score
        )

        response = (
            "Wallet Report\n"
            "========================\n\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n"
            "Arbitrum: " + str(round(arb_balance, 4)) + " ETH\n"
            "Mainnet:  " + str(round(eth_balance, 4)) + " ETH\n"
            "Wallet age: " + age + "\n"
            "Last active: " + last_active + "\n"
            "Total txs: " + str(total_txs) + "\n"
            "Failed: " + str(failed) + "\n"
            "Score: " + str(score) + " / 10\n\n"
            "Tokens Touched:\n" + token_lines + "\n"
            "Recent Transactions:\n" + tx_lines + "\n"
            "Agent Take:\n" + ai_take + "\n\n"
            "Drop another address to keep going."
        )

        await update.message.reply_text(response)

    except Exception as e:
        logger.error("Error in handle_message for %s: %s", address, e)
        await update.message.reply_text(
            "Something broke pulling that wallet. Try again in a moment."
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("ArbiAgent is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
