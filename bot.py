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
# Env / Config
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
WORDS_URL = os.getenv("WORDS_URL")  # optional (needed if WORDS_FILE isn't baked into container)

# Download dataset if missing
if not os.path.exists(WORDS_FILE):
    if not WORDS_URL:
        raise RuntimeError("WORDS_FILE not found and WORDS_URL not set. Provide WORDS_URL in Railway Variables.")
    print("Downloading word data...", flush=True)
    urllib.request.urlretrieve(WORDS_URL, WORDS_FILE)
    print("Download complete.", flush=True)

# =========================
# Normalization helpers
# =========================

def norm_hint(s: str) -> str:
    return s.strip().lower()

def norm_cat(s: str) -> str:
    """
    Category normalization that makes:
      "Foods & Drinks" == "Foods and Drinks" == "foods/drinks"
    """
    s = s.strip().lower()
    s = s.replace("&", " and ")
    # remove punctuation to spaces
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# =========================
# Streaming Parser (JS-ish dump)
# =========================

CAT_LINE_RE = re.compile(r"""\br0\s*=\s*\{(?P<body>.*?)\}\s*;""")
CAT_ID_RE = re.compile(r"'category_id'\s*:\s*'([^']*)'")
CAT_NAME_RE = re.compile(r"'name'\s*:\s*'([^']*)'")

WORD_HINT_RE = re.compile(
    r"""\{\s*'word'\s*:\s*'(?P<word>[^']*)'\s*,\s*'hint'\s*:\s*'(?P<hint>[^']*)'\s*\}\s*;"""
)

Record = Tuple[str, str, str, str]  # (category_id, category_name, word, hint)

def iter_jsdump_records(path: str):
    """
    Yields (category_id, category_name, word, hint) in one streaming pass.
    Assumes format like your example.
    """
    current_cat_id = "unknown"
    current_cat_name = "Unknown"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "r0" in line and "category_id" in line:
                m = CAT_LINE_RE.search(line)
                if m:
                    body = m.group("body")
                    mid = CAT_ID_RE.search(body)
                    mname = CAT_NAME_RE.search(body)
                    if mid:
                        current_cat_id = mid.group(1)
                    if mname:
                        current_cat_name = mname.group(1)

            if "'word'" in line and "'hint'" in line:
                wm = WORD_HINT_RE.search(line)
                if wm:
                    yield (current_cat_id, current_cat_name, wm.group("word"), wm.group("hint"))

def iter_categories_only(path: str):
    """Yields (category_id, category_name) by scanning only category definition lines."""
    current_cat_id = None
    current_cat_name = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "r0" in line and "category_id" in line:
                m = CAT_LINE_RE.search(line)
                if not m:
                    continue
                body = m.group("body")
                mid = CAT_ID_RE.search(body)
                mname = CAT_NAME_RE.search(body)
                if mid and mname:
                    cid = mid.group(1)
                    cname = mname.group(1)
                    yield cid, cname

# =========================
# Category alias map (startup)
# =========================

# alias -> (category_id, category_name)
CAT_ALIASES: Dict[str, Tuple[str, str]] = {}

def build_category_aliases(path: str) -> Dict[str, Tuple[str, str]]:
    aliases: Dict[str, Tuple[str, str]] = {}
    for cid, cname in iter_categories_only(path):
        # primary aliases
        a1 = norm_cat(cname)
        a2 = norm_cat(cid)
        # store both; last write wins (normally unique)
        aliases[a1] = (cid, cname)
        aliases[a2] = (cid, cname)

        # extra alias: remove a leading "the " etc (optional)
        if a1.startswith("the "):
            aliases[a1[4:]] = (cid, cname)
    return aliases

print("Building category aliases...", flush=True)
CAT_ALIASES = build_category_aliases(WORDS_FILE)
print(f"Category aliases loaded: {len(CAT_ALIASES)}", flush=True)

# =========================
# Solver
# =========================

def solve_hint(
    path: str,
    query_hint: str,
    allowed_cat_ids_norm: Optional[Set[str]] = None,  # normalized category_id strings
    mode: str = "exact",  # exact / contains / startswith / endswith
    limit: int = 200,
) -> Dict[str, List[str]]:
    q = norm_hint(query_hint)

    def hint_ok(h: str) -> bool:
        rh = norm_hint(h)
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
        if allowed_cat_ids_norm is not None:
            if norm_cat(cat_id) not in allowed_cat_ids_norm:
                continue

        if hint_ok(hint):
            header = f"{cat_name} ({cat_id})"
            grouped[header].append(word)
            total += 1
            if total >= limit:
                break

    return grouped

def format_compact(grouped: Dict[str, List[str]]) -> str:
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        return "No matches."

    # unique words across categories (simple output)
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

# =========================
# Discord Bot
# =========================

intents = discord.Intents.default()
intents.message_content = True  # must be enabled in Discord Dev Portal too

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Per-user category filter: store normalized category IDs
USER_ALLOWED_CAT_IDS: Dict[int, Set[str]] = {}

def parse_quoted_args(s: str) -> List[str]:
    # supports: "Foods & Drinks" or unquoted tokens
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in re.finditer(r'"([^"]+)"|(\S+)', s)
    ]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})", flush=True)

@bot.command(name="categories")
async def categories_cmd(ctx: commands.Context):
    """
    Usage:
      .categories "Everyday Objects" "Foods and Drinks"
      .categories clear
      .categories show
    """
    raw = ctx.message.content[len(".categories"):].strip()
    if not raw:
        await ctx.reply('Usage: .categories "Everyday Objects" "Foods and Drinks"  OR  .categories clear  OR  .categories show')
        return

    tokens = parse_quoted_args(raw)
    if not tokens:
        await ctx.reply("No categories provided.")
        return

    if len(tokens) == 1 and tokens[0].lower() == "clear":
        USER_ALLOWED_CAT_IDS.pop(ctx.author.id, None)
        await ctx.reply("Cleared category filter (all categories allowed).")
        return

    if len(tokens) == 1 and tokens[0].lower() == "show":
        allowed = USER_ALLOWED_CAT_IDS.get(ctx.author.id)
        if not allowed:
            await ctx.reply("No category filter set (all categories allowed).")
        else:
            await ctx.reply("Current allowed category IDs:\n" + "\n".join(sorted(allowed)))
        return

    resolved_ids: Set[str] = set()
    unresolved: List[str] = []

    for t in tokens:
        key = norm_cat(t)
        hit = CAT_ALIASES.get(key)
        if hit:
            cid, _cname = hit
            resolved_ids.add(norm_cat(cid))
        else:
            # If they typed an exact category_id, this still might work
            # but if it's not in aliases, we store it and hope it matches raw data.
            unresolved.append(t)
            resolved_ids.add(norm_cat(t))

    USER_ALLOWED_CAT_IDS[ctx.author.id] = resolved_ids

    if unresolved:
        await ctx.reply("Set! (Some categories didn't resolve exactly, but were stored anyway.)")
    else:
        await ctx.reply("Set!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    # If it's the categories command, let discord.py handle it.
    if content.lower().startswith(".categories"):
        await bot.process_commands(message)
        return

    # Everything else like ".british" ".compass" ".flush" is a HINT QUERY (not a command).
    hint = content[1:].strip()
    if not hint:
        return

    allowed_ids = USER_ALLOWED_CAT_IDS.get(message.author.id)

    loop = asyncio.get_running_loop()
    grouped = await loop.run_in_executor(
        None,
        lambda: solve_hint(
            WORDS_FILE,
            hint,
            allowed_cat_ids_norm=allowed_ids,
            mode="exact",
            limit=200,
        ),
    )

    reply = format_compact(grouped)
    if len(reply) > 1800:
        reply = reply[:1800] + "\n…(truncated)"

    await message.reply(reply)

bot.run(DISCORD_TOKEN)
