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
import builtins


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
        p.setdefault("already_announced", False)
        p.setdefault("last_announce_ts", 0)  # 👈 NEW



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
        except Exception:
            continue

        # Use the real built-in list type even if `list` was shadowed
        if builtins.isinstance(data, builtins.list):
            items = data
        else:
            items = [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") not in ("Product", "Offer"):
                continue

            offers = item.get("offers")
            if builtins.isinstance(offers, builtins.list):
                offers = offers[0] if offers else None

            if isinstance(offers, dict):
                raw_price = offers.get("price") or \
                            offers.get("priceSpecification", {}).get("price")
                if raw_price and price is None:
                    try:
                        price = float(raw_price)
                    except Exception:
                        pass

                availability = (offers.get("availability") or "").lower()
                if "instock" in availability:
                    in_stock = True
                elif "preorder" in availability or "pre_order" in availability:
                    in_stock = None
                    is_preorder = True
                elif "outofstock" in availability or "out_of_stock" in availability:
                    in_stock = False

        if price is not None and in_stock is not None:
            break

    return price, in_stock, is_preorder


def detect_in_stock_from_html(soup):
    """
    Decide in_stock purely from the HTML of a product page.
    Target-specific: if a NonbuyableSection exists, it's not in stock.
    """
    text = soup.get_text(" ", strip=True).lower()

    # 1) Target-specific: NonbuyableSection = definitely not buyable
    if soup.find(attrs={"data-test": "NonbuyableSection"}) is not None:
        return False

    # VERY aggressive "this is not available" phrases
    negative_phrases = [
        # Generic
        "out of stock",
        "out-of-stock",
        "sold out",
        "no longer available",
        "no longer accepting pre-orders",
        "no longer accepting preorders",
        "temporarily unavailable",
        "currently unavailable",
        "unavailable",

        # Target-specific preorder messages
        "preorders have sold out",
        "preorder has sold out",
        "preorders sold out",
        "preorder sold out",
        "check back on release date",

        # Other soft blocks / CTAs
        "coming soon",
        "coming...",
        "not available",
        "notify me when available",
    ]

    # If ANY of these appear anywhere in the page text → definitely NOT in stock
    for phrase in negative_phrases:
        if phrase in text:
            return False

    # 2) Look for enabled buy/add-to-cart buttons
    BUY_KEYWORDS = (
        "add to cart",
        "buy now",
        "ship it",
        "pickup",
        "delivery",
        "preorder",
        "pre-order",
    )

    found_enabled_buy_button = False

    for b in soup.find_all(["button", "a"]):
        label = b.get_text(" ", strip=True).lower()
        classes = " ".join(b.get("class") or []).lower()
        aria = (b.get("aria-disabled") or "").lower()
        disabled = b.has_attr("disabled") or "disabled" in classes or aria == "true"

        is_buy_button = any(k in label for k in BUY_KEYWORDS)

        # Disabled buy/preorder button never counts as in-stock
        if is_buy_button and disabled:
            continue

        if is_buy_button and not disabled:
            found_enabled_buy_button = True

    # Must have an enabled buy button and no negatives
    return found_enabled_buy_button


async def check_product(url: str):
    resp = await fetch(url)
    if not resp:
        return {"price": None, "in_stock": None}

    soup = BeautifulSoup(resp.text, "html.parser")
    price, _, _ = parse_json_ld(soup)  # still useful for price

    host = urlparse(url).netloc.lower()

    if "target.com" in host:
        in_stock = detect_in_stock_target(soup)
    elif "bestbuy.com" in host:
        in_stock = detect_in_stock_bestbuy(soup)
    elif "walmart.com" in host:
        in_stock = detect_in_stock_walmart(soup)
    else:
        in_stock = detect_in_stock_generic(soup)

    return {"price": price, "in_stock": in_stock}

def guess_store_name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()

    # Simple mapping of known stores
    STORE_MAP = {
        "target.com": "Target",
        "www.target.com": "Target",
        "gamestop.com": "GameStop",
        "www.gamestop.com": "GameStop",
        "walmart.com": "Walmart",
        "www.walmart.com": "Walmart",
        "bestbuy.com": "Best Buy",
        "www.bestbuy.com": "Best Buy",
    }

    for key, name in STORE_MAP.items():
        if key in host:
            return name

    # Fallback: just show the hostname
    return host


def extract_product_title(soup: BeautifulSoup) -> str:
    # Prefer Open Graph title if present
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
        if title:
            return title

    # Fallback to document <title>
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
        if title:
            return title

    # Fallback to first h1 heading
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
        if title:
            return title

    return "Unknown product"


######################################################################
# STORE SCRAPING
######################################################################

async def scrape_store(url: str):
    """
    Fetch a store/category/search page and return a list of Pokémon TCG items.
    Each item is: {"title": str, "url": str}
    """
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

    # Deduplicate using the real built-in list type
    unique = {}
    for item in items:
        key = (item["title"], item["url"])
        unique[key] = item

    deduped_items = builtins.list(unique.values())

    if len(deduped_items) > MAX_STORE_ITEMS_PER_WATCH:
        deduped_items = deduped_items[:MAX_STORE_ITEMS_PER_WATCH]

    return deduped_items

def detect_in_stock_target(soup):
    # If a NonbuyableSection exists at all → not buyable
    if soup.find(attrs={"data-test": "NonbuyableSection"}) is not None:
        return False

    text = soup.get_text(" ", strip=True).lower()

    negative = [
        "out of stock",
        "sold out",
        "preorders have sold out",
        "preorder has sold out",
        "preorders sold out",
        "preorder sold out",
        "no longer available",
        "no longer accepting pre-orders",
        "no longer accepting preorders",
        "notify me when available",
        "temporarily unavailable",
        "currently unavailable",
        "coming soon",
        "coming..."
    ]

    if any(p in text for p in negative):
        return False

    # Look for a specific Add to Cart button that is actually enabled
    for b in soup.find_all("button"):
        label = (b.get_text(" ", strip=True) or "").lower()
        aria = (b.get("aria-disabled") or "").lower()
        classes = " ".join(b.get("class") or []).lower()
        disabled = b.has_attr("disabled") or "disabled" in classes or aria == "true"

        # Target uses Add to cart with id like addToCartButtonOrTextIdForXXXXX
        bid = b.get("id") or ""
        is_add_to_cart = "add to cart" in label or "addtocartbuttonortextidfor" in bid.lower()

        if is_add_to_cart and not disabled:
            return True

    # No enabled add-to-cart, no positive signal
    return False

def detect_in_stock_bestbuy(soup):
    text = soup.get_text(" ", strip=True).lower()

    if "sold out" in text or "out of stock" in text:
        return False

    for b in soup.find_all(["button", "a"]):
        label = (b.get_text(" ", strip=True) or "").lower()
        aria = (b.get("aria-disabled") or "").lower()
        disabled = b.has_attr("disabled") or aria == "true"

        if "add to cart" in label and not disabled:
            return True

    return False


def detect_in_stock_walmart(soup):
    text = soup.get_text(" ", strip=True).lower()

    if "out of stock" in text or "sold out" in text:
        return False

    for b in soup.find_all(["button", "a"]):
        label = (b.get_text(" ", strip=True) or "").lower()
        disabled = b.has_attr("disabled")
        if "add to cart" in label and not disabled:
            return True

    return False


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
async def addwatch(ctx, url: str):
    """
    Add a product to the watch list by URL only.
    The bot will fetch the page and try to determine:
    - store name
    - product title
    - current price
    """
    # Prevent duplicates
    for p in PRODUCTS:
        if p["url"].lower() == url.lower():
            await ctx.send("❌ This product is already being watched.")
            return

    # Fetch and parse the product page
    resp = await fetch(url)
    if not resp:
        await ctx.send("❌ Could not fetch that URL. Please check that it is correct.")
        return

    soup = BeautifulSoup(resp.text, "html.parser")

    # Derive info
    name = extract_product_title(soup)
    store = guess_store_name_from_url(url)

    result = await check_product(url)
    price = result["price"]
    stock = result["in_stock"]

    if price is None:
        await ctx.send(
            "⚠️ I couldn't detect a price on that page. "
            "I'll still watch stock status, but no price-based alerts."
        )

    PRODUCTS.append({
        "name": name,
        "store": store,
        "url": url,
        # use the detected price as the initial threshold; you can edit the JSON later if desired
        "threshold_price": float(price) if price is not None else None,
        "last_in_stock": stock,
        "last_price": price,
        "already_announced": False,
        "last_announce_ts": 0,
    })

    save_products()

    details = f"Store: **{store}**\nName: **{name}**"
    if price is not None:
        details += f"\nPrice: **${price:.2f}**"
    await ctx.send(f"✅ Now watching this product:\n{details}\n{url}")



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


@bot.command(name="list")
async def list_watches(ctx):
    print("DEBUG: !list command invoked")

    lines = []

    lines.append("**📦 Watched Products:**")
    if not PRODUCTS:
        lines.append("(none)")
    else:
        for i, p in enumerate(PRODUCTS, start=1):
            name = p.get("name", "Unknown product")
            store = p.get("store", "Unknown store")
            url = p.get("url", "")
            last_price = p.get("last_price")
            threshold_price = p.get("threshold_price")

            header = f"{i}. **{name}**"
            details_parts = [f"Store: {store}"]

            if last_price is not None:
                try:
                    details_parts.append(f"Last price: ${float(last_price):.2f}")
                except Exception:
                    pass
            elif threshold_price is not None:
                try:
                    details_parts.append(f"Price: ${float(threshold_price):.2f}")
                except Exception:
                    pass

            if url:
                details_parts.append(f"Link: {url}")

            lines.append(header)
            lines.append("   " + " | ".join(details_parts))

    lines.append("\n**📂 Watched Store Pages:**")
    if not STORE_WATCHES:
        lines.append("(none)")
    else:
        for i, s in enumerate(STORE_WATCHES, start=1):
            lines.append(f"{i}. {s['url']}")

    # Discord has a message length limit of 2000 chars
    MAX_LEN = 1800

    full_text = "\n".join(lines)
    chunks = []

    while len(full_text) > MAX_LEN:
        # find last newline before limit
        split_at = full_text.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(full_text[:split_at])
        full_text = full_text[split_at:]

    chunks.append(full_text)  # remainder

    for chunk in chunks:
        await ctx.send(chunk)



@bot.command()
async def helpme(ctx):
    await ctx.send(
        "**Commands:**\n"
        "`!addwatch <url>` — add a product by URL; the bot will detect store, name, and price.\n"
        "`!delwatch <index>` — remove a watched product by its number from !list.\n"
        "`!watchstore <url>` — watch a store/category page for new Pokémon TCG items.\n"
        "`!delstore <index>` — remove a watched store page by its number from !list.\n"
        "`!list` — show all watched products and store pages.\n"
        "`!helpme` — show this help message.\n"
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

    # how often we're allowed to shout about the SAME product (in seconds)
    MIN_ALERT_INTERVAL = 60 * 5  # 5 minutes, tweak if you want

    now = time.time()

    for product in PRODUCTS:
        result = await check_product(product["url"])
        price = result["price"]
        stock = result["in_stock"]

        prev_stock = product.get("last_in_stock", False)
        already = product.get("already_announced", False)
        last_announce_ts = product.get("last_announce_ts", 0)

        is_now_in_stock = (stock is True)
        was_in_stock_before = (prev_stock is True)
        enough_time_passed = (now - last_announce_ts) >= MIN_ALERT_INTERVAL

        # fire only on TRUE stock, a state change from not-in-stock, and not rate-limited
        if (
            is_now_in_stock
            and not was_in_stock_before
            and not already
            and enough_time_passed
        ):
            msg = (
                "@everyone\n"
                f"🎉 **IN STOCK**: {product['name']}\n"
            )
            if price is not None:
                msg += f"Price: **${price:.2f}**\n"
            msg += product["url"]

            await channel.send(msg)

            product["already_announced"] = True
            product["last_announce_ts"] = now

        # if it is definitely out of stock, reset flags for future real restock
        if stock is False:
            product["already_announced"] = False

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

@bot.event
async def on_command_error(ctx, error):
    # Log to console
    print(f"[COMMAND ERROR] {ctx.command}: {error!r}")
    # And show *something* in Discord so it doesn't just silently fail
    try:
        await ctx.send(f"⚠️ Command error: `{error}`")
    except Exception:
        pass

def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("❌ DISCORD_TOKEN missing — check your .env file.")
    load_products()
    load_store_watches()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

