import os
import sys
import re
import asyncio
import urllib.request

from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands

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
WORDS_URL = os.getenv("WORDS_URL")  # optional


if not os.path.exists(WORDS_FILE):
    if not WORDS_URL:
        raise RuntimeError("WORDS_FILE not found and WORDS_URL not set")

    print("Downloading word data...")
    urllib.request.urlretrieve(WORDS_URL, WORDS_FILE)
    print("Download complete.")
    
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment (.env).")
if not WORDS_FILE:
    raise RuntimeError("Missing WORDS_FILE in environment (.env).")
if not os.path.exists(WORDS_FILE):
    raise RuntimeError(f"WORDS_FILE not found: {WORDS_FILE}")

# ---------------------------
# Streaming parser (your format)
# ---------------------------

CAT_RE = re.compile(
    r"""\br0\s*=\s*\{(?P<body>.*?)\}\s*;""",
    re.VERBOSE,
)

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
    allowed_categories: Optional[Set[str]] = None,
    mode: str = "exact",
    limit: int = 50,
) -> Dict[str, List[str]]:
    """
    Returns mapping: category header -> list(words).
    allowed_categories: set of normalized category names and/or IDs.
    mode: exact/contains/startswith/endswith
    limit: stop after N matches total (for safety)
    """
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


# ---------------------------
# Discord bot
# ---------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents)

# per-user filters (in-memory). key = user_id, value = set of normalized category names/ids
USER_CATS: Dict[int, Set[str]] = {}


def parse_quoted_args(s: str) -> List[str]:
    """
    Parses:  .categories "Everyday Objects" "Foods & Drinks"
    Returns: ["Everyday Objects", "Foods & Drinks"]
    Also allows unquoted tokens.
    """
    # match either "quoted strings" or bare tokens
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in re.finditer(r'"([^"]+)"|(\S+)', s)]


def format_compact(grouped: Dict[str, List[str]]) -> str:
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        return "No matches."

    # Deduplicate words across categories, but preserve category separation in output if you want.
    # Your example shows just a flat list; we’ll do that for simplicity.
    words = []
    seen = set()
    for cat in sorted(grouped.keys(), key=lambda c: (-len(grouped[c]), c.lower())):
        for w in grouped[cat]:
            wl = w.lower()
            if wl in seen:
                continue
            seen.add(wl)
            words.append(w)

    if total == 1 and len(words) == 1:
        return f"1 Match:\n{words[0]}"
    return f"{len(words)} Matches:\n" + "\n".join(words)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.command(name="categories")
async def categories_cmd(ctx: commands.Context, *args):
    """
    Usage:
      .categories "Everyday Objects" "Foods & Drinks"
      .categories everyday_objects food_drinks
      .categories clear
    """
    raw = ctx.message.content[len(".categories"):].strip()
    if not raw:
        await ctx.reply('Usage: .categories "Everyday Objects" "Foods & Drinks"  OR  .categories clear')
        return

    tokens = parse_quoted_args(raw)

    if len(tokens) == 1 and tokens[0].lower() == "clear":
        USER_CATS.pop(ctx.author.id, None)
        await ctx.reply("Cleared category filter (all categories allowed).")
        return

    USER_CATS[ctx.author.id] = {_norm(t) for t in tokens}
    await ctx.reply("Set!")


@bot.event
async def on_message(message: discord.Message):
    # let commands work
    await bot.process_commands(message)

    # ignore bot itself
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    # If it’s a command like .categories, don’t treat as hint
    if content.lower().startswith(".categories"):
        return

    # Treat ".yeast" as hint "yeast"
    hint = content[1:].strip()
    if not hint:
        return

    allowed = USER_CATS.get(message.author.id)

    # Run solver off the event loop (file scan is CPU/IO heavy)
    loop = asyncio.get_running_loop()
    grouped = await loop.run_in_executor(
        None,
        lambda: solve_hint(
            WORDS_FILE,
            hint,
            allowed_categories=allowed,
            mode="exact",
            limit=200,  # safety
        ),
    )

    reply = format_compact(grouped)

    # Keep messages from blowing up Discord limits
    if len(reply) > 1800:
        reply = reply[:1800] + "\n…(truncated)"

    await message.reply(reply)


bot.run(TOKEN)


