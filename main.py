import os
import logging
import requests
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
            logger.warning("No result in eth_getBalance response for %s", address)
            return 0.0
        return int(result, 16) / 1e18
    except Exception as e:
        logger.error("Error fetching balance for %s: %s", address, e)
        return 0.0


def get_transactions(address: str) -> list:
    try:
        url = (
            "https://api.arbiscan.io/api"
            "?module=account&action=txlist"
            "&address=" + address +
            "&page=1&offset=5&sort=desc"
            "&apikey=" + ARBISCAN_API_KEY
        )
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("status") == "1":
            return data.get("result", [])
        logger.info("Arbiscan returned no transactions for %s: %s", address, data.get("message"))
        return []
    except Exception as e:
        logger.error("Error fetching transactions for %s: %s", address, e)
        return []


def analyze_wallet(balance: float, txs: list) -> tuple:
    tx_count = len(txs)

    if tx_count >= 5:
        wallet_type = "quite active"
    elif tx_count > 0:
        wallet_type = "somewhat active"
    else:
        wallet_type = "inactive lately"

    if tx_count > 3 and balance < 0.5:
        risk = "a bit on the risky side"
    elif tx_count > 0:
        risk = "fairly normal"
    else:
        risk = "low"

    balance_score = min(balance * 0.5, 3.0)
    score = min(10.0, round((tx_count * 1.5) + balance_score, 1))

    return wallet_type, risk, score


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey \U0001f44b\n\nSend me any Arbitrum wallet address and I'll take a look at it with you."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.strip()
    address = raw.lower()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "Hmm that does not look like a valid wallet address.\n"
            "Try sending a proper 0x address (42 characters total)."
        )
        return

    if not all(c in "0123456789abcdef" for c in address[2:]):
        await update.message.reply_text(
            "That address contains invalid characters.\n"
            "A valid Ethereum address only uses 0-9 and a-f after the 0x prefix."
        )
        return

    try:
        balance = get_eth_balance(address)
        txs = get_transactions(address)
        wallet_type, risk, score = analyze_wallet(balance, txs)

        if txs:
            tx_lines = ""
            for tx in txs[:3]:
                tx_hash = tx.get("hash", "")
                short_hash = tx_hash[:10] + "..." if tx_hash else "unknown"
                tx_lines += "- " + short_hash + "\n"
        else:
            tx_lines = "- no recent transactions\n"

        response = (
            "Hey \U0001f44b\n\n"
            "I had a look at that wallet on Arbitrum.\n\n"
            "Address: " + address[:6] + "..." + address[-4:] + "\n\n"
            "It is holding about " + str(round(balance, 4)) + " ETH right now, "
            "and overall it looks " + wallet_type + ".\n\n"
            "From what I can see, the risk level feels " + risk + ", "
            "and I would give it roughly a " + str(score) + "/10.\n\n"
            "A few quick observations:\n"
            "- activity level comes from how often it is transacting\n"
            "- recent interactions help hint at behavior\n\n"
            "Recent transactions:\n" + tx_lines + "\n"
            "If this were mine, I would probably:\n"
            "-> keep an eye on new transactions\n"
            "-> check contract interactions\n"
            "-> move carefully before making decisions\n\n"
            "Send another wallet if you want \U0001f44d"
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("ArbiAgent is running...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
