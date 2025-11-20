import os
import json
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()
import os

import discord
from discord.ext import tasks, commands

# =========================
# CONFIG – EDIT THESE
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # <-- paste your token here
CHANNEL_ID = 1440955336704921694  # <-- replace with the ID of the channel for alerts
PRODUCTS_FILE = "products.json"

CHECK_INTERVAL_SECONDS = 90  # how often to check (be polite!)


# =========================
# DATA STORAGE
# =========================

PRODUCTS = []  # in-memory list; loaded from products.json


def load_products():
    """
    Load products from products.json if it exists.
    Ensure 'last_in_stock' and 'last_price' keys exist.
    """
    global PRODUCTS
    if not os.path.exists(PRODUCTS_FILE):
        PRODUCTS = []
        return

    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        try:
            PRODUCTS = json.load(f)
        except json.JSONDecodeError:
            print("Error: products.json is invalid JSON. Starting with empty list.")
            PRODUCTS = []

    for p in PRODUCTS:
        p.setdefault("last_in_stock", False)
        p.setdefault("last_price", None)


def save_products():
    """
    Save products to products.json.
    """
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(PRODUCTS, f, indent=2, ensure_ascii=False)


# =========================
# CHECKER FUNCTIONS
# =========================

def check_generic_structured_product(url: str) -> dict:
    """
    Generic checker that tries to read price + stock from JSON-LD (schema.org).
    This works on many store product pages that embed structured data.

    Returns:
        {"price": float | None, "in_stock": bool | None}
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; personal-stock-alert-bot/1.0)"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")

    price = None
    in_stock = None

    for script in scripts:
        try:
            raw = script.string or ""
            data = json.loads(raw)
        except Exception:
            continue

        # Could be a dict or list of dicts
        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue

            # Look for Product / Offer
            if item.get("@type") not in ("Product", "Offer"):
                continue

            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None

            if isinstance(offers, dict):
                # Price
                raw_price = offers.get("price")
                if raw_price is None:
                    price_spec = offers.get("priceSpecification", {})
                    raw_price = price_spec.get("price")

                if raw_price is not None:
                    try:
                        price = float(raw_price)
                    except ValueError:
                        pass

                # Availability
                availability = offers.get("availability", "")
                if isinstance(availability, str):
                    availability = availability.lower()
                    if "instock" in availability:
                        in_stock = True
                    elif "outofstock" in availability or "out_of_stock" in availability:
                        in_stock = False

            # If we got both, we can return early
            if price is not None and in_stock is not None:
                return {"price": price, "in_stock": in_stock}

    return {"price": price, "in_stock": in_stock}


def check_product(product: dict) -> dict:
    """
    Decide how to check a product based on its 'store'.

    Right now:
      - 'generic' → use JSON-LD checker

    Later you can add custom functions:
      - if store == "target": return check_target_product(product["url"])
    """
    store = product.get("store", "generic")

    if store == "generic":
        return check_generic_structured_product(product["url"])

    # Fallback: still try generic
    return check_generic_structured_product(product["url"])


# =========================
# DISCORD BOT SETUP
# =========================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    load_products()
    check_products_loop.start()


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_products_loop():
    """
    Background loop: checks all products every CHECK_INTERVAL_SECONDS.
    Sends alerts if something changes.
    """
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found. Check CHANNEL_ID.")
        return

    if not PRODUCTS:
        return

    for product in PRODUCTS:
        try:
            result = check_product(product)
            in_stock = result.get("in_stock")
            price = result.get("price")

            if in_stock is None and price is None:
                print(f"Could not read stock/price for {product['name']}")
                continue

            last_in_stock = product.get("last_in_stock", False)
            last_price = product.get("last_price", None)
            threshold = product.get("threshold_price")

            should_alert = False

            # Case 1: was not clearly in stock, now in stock
            if in_stock is True and last_in_stock is not True:
                should_alert = True

            # Case 2: price drop under threshold while in stock
            if in_stock is True and price is not None and threshold is not None:
                if price <= threshold and (last_price is None or price < last_price):
                    should_alert = True

            if should_alert:
                lines = [f"🎴 **{product['name']}**"]
                if price is not None:
                    lines.append(f"💲 Price (approx): ${price:.2f}")
                if in_stock is True:
                    lines.append("✅ Status: Possibly **IN STOCK**")
                elif in_stock is False:
                    lines.append("❌ Status: **Out of stock** (but something changed)")
                else:
                    lines.append("ℹ️ Status: Unknown, but something changed")

                lines.append(f"🔗 {product['url']}")
                await channel.send("\n".join(lines))

            product["last_in_stock"] = in_stock
            product["last_price"] = price

        except Exception as e:
            print(f"Error checking {product.get('name', 'Unknown')}: {e}")

    save_products()


# =========================
# COMMANDS
# =========================

@bot.command()
async def listwatch(ctx):
    """
    Show all watched products with index numbers.
    """
    if not PRODUCTS:
        await ctx.send("No products are being watched right now.")
        return

    lines = []
    for idx, p in enumerate(PRODUCTS):
        line = (
            f"**[{idx}] {p['name']}**\n"
            f"• Store: `{p.get('store', 'generic')}`\n"
            f"• URL: {p['url']}\n"
            f"• Threshold: ${p.get('threshold_price', 0):.2f}"
        )
        lines.append(line)

    await ctx.send("\n\n".join(lines))


@bot.command()
async def addwatch(ctx, store: str, threshold: float, url: str, *, name: str):
    """
    Add a new product.

    Usage:
        !addwatch <store> <threshold_price> <url> <name...>

    Example:
        !addwatch generic 60 https://example.com/pokemon-box Pokemon 151 ETB
    """
    product = {
        "name": name,
        "store": store,
        "url": url,
        "threshold_price": float(threshold),
        "last_in_stock": False,
        "last_price": None,
    }

    PRODUCTS.append(product)
    save_products()

    await ctx.send(
        "✅ Added watch:\n"
        f"**Name:** {name}\n"
        f"**Store:** {store}\n"
        f"**Threshold:** ${threshold:.2f}\n"
        f"**URL:** {url}"
    )


@bot.command()
async def delwatch(ctx, index: int):
    """
    Delete a watched product by index.

    Usage:
        !delwatch 0
    """
    if index < 0 or index >= len(PRODUCTS):
        await ctx.send("❌ Invalid index.")
        return

    removed = PRODUCTS.pop(index)
    save_products()

    await ctx.send(f"🗑️ Removed watch: **{removed['name']}**")


# =========================
# RUN
# =========================

def main():
    if DISCORD_TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError("Please set your Discord bot token in DISCORD_TOKEN.")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
