import os
import json
import socket
import aiohttp
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands, tasks

from urllib.parse import urlparse, urljoin
import asyncio
import time
import logging
import re


######################################################################
# CONFIG
######################################################################

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1440955336704921694

PRODUCTS_FILE = "products.json"
STORE_WATCH_FILE = "store_watches.json"

CHECK_INTERVAL_SECONDS = 60
STORE_CHECK_INTERVAL_SECONDS = 60
NO_CHANGE_MESSAGE_INTERVAL = 600

MAX_STORE_ITEMS_PER_WATCH = 80


######################################################################
# DATA
######################################################################

PRODUCTS = []
STORE_WATCHES = []


######################################################################
# PREFILTER: What counts as Pokémon TCG
######################################################################

IGNORED_POKEMON_KEYWORDS = [
    "sleeve", "sleeves", "binder", "binder collection",
    "plush", "plushie", "plushies",
    "figure", "figures", "statue", "statues",
    "toy", "toys", "funko", "pop", "pop!", "lego",
    "shirt", "t-shirt", "tee", "hoodie",
    "sock", "socks", "hat", "cap",
    "backpack", "bag", "wallet",
    "mug", "cup", "glass", "bottle",
    "keychain", "poster", "canvas",
    "pajamas", "blanket", "comforter",
    "towel", "bath", "rug", "mat"
]

TCG_POSITIVE = [
    "booster", "box", "boxes", "etb", "elite trainer",
    "booster pack", "booster packs", "trainer box",
    "build & battle", "collection", "premium",
    "tin", "tins", "deck", "league battle deck",
    "gx box", "blister", "bundle"
]


def is_relevant_pokemon_title(title: str) -> bool:
    t = title.lower()
    if "pokemon" not in t:
        return False
    if not any(k in t for k in TCG_POSITIVE):
        return False
    if any(bad in t for bad in IGNORED_POKEMON_KEYWORDS):
        return False
    return True


######################################################################
# LOAD/SAVE
######################################################################

def load_products():
    global PRODUCTS
    if not os.path.exists(PRODUCTS_FILE):
        PRODUCTS = []
        return

    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            PRODUCTS = json.load(f)
    except json.JSONDecodeError:
        PRODUCTS = []

    for p in PRODUCTS:
        p.setdefault("last_in_stock", False)
        p.setdefault("last_price", None)
        p.setdefault("last_alert_in_stock", None)


def save_products():
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(PRODUCTS, f, indent=2, ensure_ascii=False)


def load_store_watches():
    global STORE_WATCHES
    if not os.path.exists(STORE_WATCH_FILE):
        STORE_WATCHES = []
        return

    try:
        with open(STORE_WATCH_FILE, "r", encoding="utf-8") as f:
            STORE_WATCHES = json.load(f)
    except json.JSONDecodeError:
        STORE_WATCHES = []

    for w in STORE_WATCHES:
        w.setdefault("last_titles", [])
        w.setdefault("last_nothing_new_ts", 0)


def save_store_watches():
    with open(STORE_WATCH_FILE, "w", encoding="utf-8") as f:
        json.dump(STORE_WATCHES, f, indent=2, ensure_ascii=False)


######################################################################
# NETWORK: async threaded HTTP
######################################################################

async def fetch(url: str, headers=None):
    headers = headers or {"User-Agent": "Mozilla/5.0 (stock-alert-bot)"}

    def _get():
        return requests.get(url, headers=headers, timeout=10)

    try:
        return await asyncio.to_thread(_get)
    except Exception:
        return None


######################################################################
# PRODUCT SCRAPING
######################################################################

def parse_json_ld(soup):
    scripts = soup.find_all("script", type="application/ld+json")
    price = None
    in_stock = None
    is_preorder = False

    for sc in scripts:
        try:
            data = json.loads(sc.string or "")
        except:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") not in ("Product", "Offer"):
                continue

            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None

            if isinstance(offers, dict):
                raw_price = offers.get("price") or \
                            offers.get("priceSpecification", {}).get("price")
                if raw_price and price is None:
                    try:
                        price = float(raw_price)
                    except:
                        pass

                avail = (offers.get("availability") or "").lower()
                if "instock" in avail:
                    in_stock = True
                elif "preorder" in avail:
                    in_stock = None
                    is_preorder = True
                elif "outofstock" in avail:
                    in_stock = False

        if price is not None and in_stock is not None:
            break

    return price, in_stock, is_preorder


def refine_html(soup, price, in_stock, is_preorder):
    text = soup.get_text(" ", strip=True).lower()

    negative = [
        "coming soon", "available soon", "temporarily unavailable",
        "currently unavailable", "sold out", "no longer available",
        "no longer accepting pre-orders", "out of stock",
        "out-of-stock"
    ]

    if any(p in text for p in negative):
        in_stock = False

    for b in soup.find_all(["button", "a"]):
        label = b.get_text(" ", strip=True).lower()
        cl = " ".join(b.get("class") or []).lower()
        aria = (b.get("aria-disabled") or "").lower()
        disabled = b.has_attr("disabled") or "disabled" in cl or aria == "true"

        if "coming soon" in label:
            in_stock = False

        if ("preorder" in label or "pre-order" in label) and disabled:
            in_stock = False

        if any(k in label for k in ("add to cart", "buy now", "ship it")) and not disabled:
            if in_stock is not False:
                in_stock = True

    if in_stock is None:
        if "add to cart" in text or "ship it" in text:
            in_stock = True

    if price is None:
        m = re.search(r"\$\s*([0-9]+\.[0-9]{2})", text)
        if m:
            try:
                price = float(m.group(1))
            except:
                pass

    return price, in_stock


async def check_product(url: str):
    resp = await fetch(url)
    if not resp:
        return {"price": None, "in_stock": None}

    soup = BeautifulSoup(resp.text, "html.parser")

    price, in_stock, preorder_flag = parse_json_ld(soup)
    price, in_stock = refine_html(soup, price, in_stock, preorder_flag)

    return {"price": price, "in_stock": in_stock}

######################################################################
# STORE SCRAPING
######################################################################

async def scrape_store(url: str):
    resp = await fetch(url)
    if not resp:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = []

    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True)
        if not txt:
            continue
        if not is_relevant_pokemon_title(txt):
            continue

        item_url = urljoin(url, a["href"])
        items.append({"title": txt, "url": item_url})

    # Deduplicate
    unique = {(i["title"], i["url"]): i for i in items}
    lst = list(unique.values())
    return lst[:MAX_STORE_ITEMS_PER_WATCH]


######################################################################
# BOT SETUP (prefix "!")
######################################################################

intents = discord.Intents.default()
intents.message_content = True

# Create the bot normally — IPv4 connector is added in on_ready()
bot = commands.Bot(command_prefix="!", intents=intents)


######################################################################
# BOT COMMANDS
######################################################################

@bot.command()
async def addwatch(ctx, name: str, store: str, url: str, threshold: float):
    # Prevent duplicates
    for p in PRODUCTS:
        if p["url"].lower() == url.lower():
            await ctx.send("❌ This product is already being watched.")
            return

    PRODUCTS.append({
        "name": name,
        "store": store,
        "url": url,
        "threshold_price": float(threshold),
        "last_in_stock": False,
        "last_price": None,
        "last_alert_in_stock": None
    })
    save_products()

    await ctx.send(f"✅ Now watching **{name}**")


@bot.command()
async def delwatch(ctx, index: int):
    index -= 1
    if index < 0 or index >= len(PRODUCTS):
        await ctx.send("❌ Invalid index.")
        return

    removed = PRODUCTS.pop(index)
    save_products()
    await ctx.send(f"🗑️ Removed: **{removed['name']}**")


@bot.command()
async def watchstore(ctx, url: str):
    STORE_WATCHES.append({
        "url": url,
        "last_titles": [],
        "last_nothing_new_ts": 0
    })
    save_store_watches()

    await ctx.send(f"📁 Now monitoring store page:\n{url}")


@bot.command()
async def delstore(ctx, index: int):
    index -= 1
    if index < 0 or index >= len(STORE_WATCHES):
        await ctx.send("❌ Invalid index.")
        return

    removed = STORE_WATCHES.pop(index)
    save_store_watches()
    await ctx.send(f"🗑️ Removed store watch:\n{removed['url']}")


@bot.command()
async def list(ctx):
    msg = []

    msg.append("**📦 Watched Products:**")
    if not PRODUCTS:
        msg.append("  (none)")
    else:
        for i, p in enumerate(PRODUCTS, start=1):
            msg.append(f"{i}. {p['name']} — {p['url']}")

    msg.append("\n**📂 Watched Store Pages:**")
    if not STORE_WATCHES:
        msg.append("  (none)")
    else:
        for i, s in enumerate(STORE_WATCHES, start=1):
            msg.append(f"{i}. {s['url']}")

    await ctx.send("\n".join(msg))


@bot.command()
async def helpme(ctx):
    await ctx.send(
        "**Commands:**\n"
        "`!addwatch <name> <store> <url> <threshold>`\n"
        "`!delwatch <index>`\n"
        "`!watchstore <url>`\n"
        "`!delstore <index>`\n"
        "`!list`\n"
        "`!helpme`\n"
    )


######################################################################
# BACKGROUND LOOPS
######################################################################

@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def product_loop():
    if not bot.is_ready():
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    for product in PRODUCTS:
        result = await check_product(product["url"])
        price = result["price"]
        stock = result["in_stock"]

        # If newly in stock → announce ONCE
        if stock is True and product["last_alert_in_stock"] is not True:
            msg = (
                "@everyone\n"
                f"🎉 **IN STOCK**: {product['name']}\n"
            )
            if price is not None:
                msg += f"Price: **${price:.2f}**\n"
            msg += product["url"]

            await channel.send(msg)
            product["last_alert_in_stock"] = True

        # If out of stock → reset alert state
        if stock is False:
            product["last_alert_in_stock"] = False

        product["last_in_stock"] = stock
        product["last_price"] = price

    save_products()


@tasks.loop(seconds=STORE_CHECK_INTERVAL_SECONDS)
async def store_loop():
    if not bot.is_ready():
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    for s in STORE_WATCHES:
        items = await scrape_store(s["url"])
        titles = [i["title"] for i in items]

        old_titles = set(s["last_titles"])
        new_items = [i for i in items if i["title"] not in old_titles]

        if new_items:
            msg = ["@everyone", "🆕 **New Pokémon TCG Items Appeared:**"]
            for n in new_items:
                msg.append(f"- {n['title']}\n  {n['url']}")

            await channel.send("\n".join(msg))
            s["last_titles"] = titles
        else:
            s["last_nothing_new_ts"] = time.time()

    save_store_watches()


######################################################################
# BOT READY EVENT
######################################################################

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # Force IPv4 connector AFTER bot has an event loop
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(family=socket.AF_INET)
    )
    bot.http._HTTPClient__session = session

    # Startup announcement
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send("🤖 Bot is now **online** and monitoring items!")

    product_loop.start()
    store_loop.start()

######################################################################
# RUN BOT
######################################################################

def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN missing — check your .env file.")
    load_products()
    load_store_watches()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

