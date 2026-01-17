#!/usr/bin/env python3
import os
import re
import sys
import asyncio
import urllib.request
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

# =========================
# Environment / Config
# =========================

def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        keys = sorted(os.environ.keys())
        preview = ", ".join(keys[:60]) + (" ..." if len(keys) > 60 else "")
        print(f"[FATAL] Missing env var: {name}", flush=True)
        print(f"[DEBUG] Available env keys (first 60): {preview}", flush=True)
        sys.exit(1)
    return val

DISCORD_TOKEN = require_env("DISCORD_TOKEN")
WORDS_FILE = require_env("WORDS_FILE")
WORDS_URL = os.getenv("WORDS_URL")  # optional (needed if WORDS_FILE isn't in the container)

# Download the big dataset if it doesn't exist in the container
if not os.path.exists(WORDS_FILE):
    if not WORDS_URL:
        raise RuntimeError("WORDS_FILE not found and WORDS_URL not set. Provide WORDS_URL in Railway Variables.")
    print("Downloading word data...", flush=True)
    urllib.request.urlretrieve(WORDS_URL, WORDS_FILE)
    print("Download complete.", flush=True)

# =========================
# Streaming Parser (your JS-ish dump)
# =========================

CAT_RE = re.compile(r"""\br0\s*=\s*\{(?P<body>.*?)\}\s*;""", re.VERBOSE)
CAT_ID_RE = re.compile(r"'category_id'\s*:\s*'([^']*)'")
CAT_NAME_RE = re.compile(r"'name'\s*:\s*'([^']*)'")

WORD_HINT_RE = re.compile(
    r"""\{\s*'word'\s*:\s*'(?P<word>[^']*)'\s*,\s*'hint'\s*:\s*'(?P<hint>[^']*)'\s*\}\s*;""",
    re.VERBOSE,
)

Record = Tuple[str, str, str, str]  # (category_id, category_name, word, hint)

def _norm(s: str) -> str:
    return s.strip().lower()

def iter_jsdump_records(path: str):
    """Yield (cat_id, cat_name, word, hint) in a single streaming pass."""
    current_cat_id = "unknown"
    current_cat_name = "Unknown"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # category line
            if "r0" in line and "category_id" in line:
                m = CAT_RE.search(line)
                if m:
                    body = m.group("body")
                    mid = CAT_ID_RE.search(body)
                    mname = CAT_NAME_RE.search(body)
                    if mid:
                        current_cat_id = mid.group(1)
                    if mname:
                        current_cat_name = mname.group(1)

            # word/hint line
            if "'word'" in line and "'hint'" in line:
                wm = WORD_HINT_RE.search(line)
                if wm:
                    yield (current_cat_id, current_cat_name, wm.group("word"), wm.group("hint"))

def solve_hint(
    path: str,
    query_hint: str,
    allowed_categories: Optional[Set[str]] = None,  # normalized category names and/or IDs
    mode: str = "exact",  # exact / contains / startswith / endswith
    limit: int = 200,     # safety cap
) -> Dict[str, List[str]]:
    q = _norm(query_hint)

    def hint_ok(h: str) -> bool:
        rh = _norm(h)
        if mode == "exact":
            return rh == q
        if mode == "contains":
            return q in rh
        if mode == "startswith":
            return rh.startswith(q)
        if mode == "endswith":
            return rh.endswith(q)
        raise ValueError(f"Unknown mode: {mode}")

    grouped: DefaultDict[str, List[str]] = defaultdict(list)
    total = 0

    for cat_id, cat_name, word, hint in iter_jsdump_records(path):
        if allowed_categories is not None:
            if _norm(cat_id) not in allowed_categories and _norm(cat_name) not in allowed_categories:
                continue

        if hint_ok(hint):
            header = f"{cat_name} ({cat_id})"
            grouped[header].append(word)
            total += 1
            if total >= limit:
                break

    return grouped

# =========================
# Discord Bot
# =========================

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in Discord Developer Portal

bot = commands.Bot(command_prefix=".", intents=intents)

# Per-user category filters in memory (Railway restarts reset this; add DB later if desired)
USER_CATS: Dict[int, Set[str]] = {}

def parse_quoted_args(s: str) -> List[str]:
    """
    Parses:  .categories "Everyday Objects" "Foods & Drinks"
    Returns: ["Everyday Objects", "Foods & Drinks"]
    Also allows unquoted tokens: .categories everyday_objects food_drinks
    """
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in re.finditer(r'"([^"]+)"|(\S+)', s)
    ]

def format_compact(grouped: Dict[str, List[str]]) -> str:
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        return "No matches."

    # Flatten unique words across categories (simple output like your example)
    words: List[str] = []
    seen = set()
    for cat in sorted(grouped.keys(), key=lambda c: (-len(grouped[c]), c.lower())):
        for w in grouped[cat]:
            wl = w.lower()
            if wl in seen:
                continue
            seen.add(wl)
            words.append(w)

    if len(words) == 1:
        return f"1 Match:\n{words[0]}"
    return f"{len(words)} Matches:\n" + "\n".join(words)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})", flush=True)

@bot.command(name="categories")
async def categories_cmd(ctx: commands.Context):
    """
    Usage:
      .categories "Everyday Objects" "Foods & Drinks"
      .categories everyday_objects food_drinks
      .categories clear
      .categories show
    """
    raw = ctx.message.content[len(".categories"):].strip()
    if not raw:
        await ctx.reply('Usage: .categories "Everyday Objects" "Foods & Drinks"  OR  .categories clear  OR  .categories show')
        return

    tokens = parse_quoted_args(raw)
    if not tokens:
        await ctx.reply("No categories provided.")
        return

    if len(tokens) == 1 and tokens[0].lower() == "clear":
        USER_CATS.pop(ctx.author.id, None)
        await ctx.reply("Cleared category filter (all categories allowed).")
        return

    if len(tokens) == 1 and tokens[0].lower() == "show":
        allowed = USER_CATS.get(ctx.author.id)
        if not allowed:
            await ctx.reply("No category filter set (all categories allowed).")
        else:
            # show original-ish strings (we only stored normalized; show normalized)
            await ctx.reply("Current categories:\n" + "\n".join(sorted(allowed)))
        return

    USER_CATS[ctx.author.id] = {_norm(t) for t in tokens}
    await ctx.reply("Set!")

@bot.event
async def on_message(message: discord.Message):
    # Let command handler run first
    await bot.process_commands(message)

    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    # Don't treat commands as hints
    if content.lower().startswith(".categories"):
        return

    # Interpret ".yeast" as hint "yeast"
    hint = content[1:].strip()
    if not hint:
        return

    allowed = USER_CATS.get(message.author.id)

    # Run the file scan off the event loop
    loop = asyncio.get_running_loop()
    grouped = await loop.run_in_executor(
        None,
        lambda: solve_hint(
            WORDS_FILE,
            hint,
            allowed_categories=allowed,
            mode="exact",
            limit=200,
        ),
    )

    reply = format_compact(grouped)
    if len(reply) > 1800:
        reply = reply[:1800] + "\n…(truncated)"

    await message.reply(reply)

# Start the bot
bot.run(DISCORD_TOKEN)
