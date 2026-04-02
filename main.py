import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")

ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"

def get_eth_balance(address):
    try:
        data = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1
        }
        res = requests.post(ARBITRUM_RPC, json=data, timeout=10).json()
        balance_wei = int(res["result"], 16)
        return balance_wei / 1e18
    except:
        return 0

def get_transactions(address):
    try:
        url = f"https://api.arbiscan.io/api?module=account&action=txlist&address={address}&page=1&offset=5&sort=desc"
        res = requests.get(url, timeout=10).json()
        if res.get("status") == "1":
            return res.get("result", [])
        return []
    except:
        return []

def analyze_wallet(balance, txs):
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

    score = min(10, round((tx_count * 1.5) + (balance * 0.5), 1))

    return wallet_type, risk, score

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey 👋\n\nSend me any Arbitrum wallet address and I’ll take a look at it with you."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()

    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text(
            "Hmm… that doesn’t look like a valid wallet address.\nTry sending a proper 0x address."
        )
        return

    try:
        balance = get_eth_balance(address)
        txs = get_transactions(address)

        wallet_type, risk, score = analyze_wallet(balance, txs)

        if txs:
            tx_summary = ""
            for tx in txs[:3]:
                tx_summary += f"• {tx.get('hash','')[:10]}...\n"
        else:
            tx_summary = "• no recent transactions\n"

        response = f"""
Hey 👋

I had a look at that wallet on Arbitrum.

Address: {address[:6]}...{address[-4:]}

It’s holding about {round(balance,4)} ETH right now, and overall it looks {wallet_type}.

From what I can see, the risk level feels {risk}, and I’d give it roughly a {score}/10.

A few quick observations:
• activity level comes from how often it’s transacting  
• recent interactions help hint at behavior  

Recent transactions:
{tx_summary}

If this were mine, I’d probably:
→ keep an eye on new transactions  
→ check contract interactions  
→ move carefully before making decisions  

Send another wallet if you want 👍
"""
        await update.message.reply_text(response)

    except:
        await update.message.reply_text(
            "Something went wrong while checking that wallet. Try again in a bit."
        )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("ArbiAgent is running...")
    app.run_polling()
