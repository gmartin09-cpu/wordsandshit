#!/usr/bin/env python3
"""
Discord hint-solver bot (Railway-friendly)

Commands:
  .categories "Everyday Objects" "Foods & Drinks"
  .categories clear
  .categories show
  .findcat <search term>        (ex: .findcat food)

Hint query:
  .yeast
  .compass
  .british
(Anything that starts with "." but is NOT ".categories" or ".findcat" is treated as a hint query.)
"""

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
WORDS_URL = os.getenv("WORDS_URL")  # optional (needed if WORDS_FILE is not in container)


# Download dataset if missing (Railway container filesystem)
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
    Normalizes category strings so:
      "Foods & Drinks" == "Foods and Drinks" == "foods/drinks"
    """
    s = s.strip().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# =========================
# Streaming Parser (JS-ish dump)
# =========================

CAT_LINE_RE = re.compile(r"""\br0\s*=\s*\{(?P<body>.*?)\}\s*;""")
CAT_ID_RE   = re.compile(r"'category_id'\s*:\s*'([^']*)'")
CAT_NAME_RE = re.compile(r"'name'\s*:\s*'([^']*)'")

WORD_HINT_RE = re.compile(
    r"""\{\s*'word'\s*:\s*'(?P<word>[^']*)'\s*,\s*'hint'\s*:\s*'(?P<hint>[^']*)'\s*\}\s*;"""
)

Record = Tuple[str, str, str, str]  # (category_id, category_name, word, hint)


def iter_jsdump_records(path: str):
    """Yield (category_id, category_name, word, hint) in a single streaming pass."""
    current_cat_id = "unknown"
    current_cat_name = "Unknown"

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            # Category definition line
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

            # Word/hint line
            if "'word'" in line and "'hint'" in line:
                wm = WORD_HINT_RE.search(line)
                if wm:
                    yield (current_cat_id, current_cat_name, wm.group("word"), wm.group("hint"))


def iter_categories_only(path: str):
    """Yield (category_id, category_name) by scanning category definition lines."""
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
                    yield mid.group(1), mname.group(1)


# =========================
# Category alias map (startup)
# =========================

# alias -> (category_id, category_name)
CAT_ALIASES: Dict[str, Tuple[str, str]] = {}

def build_category_aliases(path: str) -> Dict[str, Tuple[str, str]]:
    aliases: Dict[str, Tuple[str, str]] = {}
    for cid, cname in iter_categories_only(path):
        aliases[norm_cat(cname)] = (cid, cname)   # name alias
        aliases[norm_cat(cid)] = (cid, cname)     # id alias
        # tiny convenience alias (optional)
        if cname.lower().startswith("the "):
            aliases[norm_cat(cname[4:])] = (cid, cname)
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

    # Unique words across categories (compact output)
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
intents.message_content = True  # must also be enabled in Discord Developer Portal

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Per-user: store normalized category IDs
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
      .categories "Everyday Objects" "Foods & Drinks"
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
    resolved_pretty: List[str] = []

    for t in tokens:
        key = norm_cat(t)
        hit = CAT_ALIASES.get(key)
        if hit:
            cid, cname = hit
            resolved_ids.add(norm_cat(cid))
            resolved_pretty.append(f"{cname} ({cid})")
        else:
            unresolved.append(t)

    # If ANY are unresolved, we IGNORE them (we do NOT store unknowns).
    if not resolved_ids:
        await ctx.reply(
            "No valid categories recognized.\n"
            'Tip: use `.findcat food` or `.findcat drink` to discover the exact category name/id.'
        )
        return

    USER_ALLOWED_CAT_IDS[ctx.author.id] = resolved_ids

    msg = "Set!\nAllowed:\n" + "\n".join(f"- {x}" for x in resolved_pretty)
    if unresolved:
        msg += "\nIgnored (unknown):\n" + "\n".join(f"- {x}" for x in unresolved) + \
               "\nTip: use `.findcat <term>` to find the exact category name/id."
    await ctx.reply(msg)


@bot.command(name="findcat")
async def findcat_cmd(ctx: commands.Context, *, query: str = ""):
    """
    Usage:
      .findcat food
      .findcat drink
      .findcat everyday
    Shows matching categories with exact name + id.
    """
    q = norm_cat(query)
    if not q:
        await ctx.reply('Usage: .findcat <search term>  (ex: .findcat food)')
        return

    # CAT_ALIASES maps aliases -> (id, name). Deduplicate by id.
    seen: Set[str] = set()
    matches: List[Tuple[str, str]] = []

    for alias, (cid, cname) in CAT_ALIASES.items():
        if q in alias:
            if cid in seen:
                continue
            seen.add(cid)
            matches.append((cname, cid))

    matches.sort(key=lambda x: (x[0].lower(), x[1].lower()))
    if not matches:
        await ctx.reply("No category matches.")
        return

    lines = [f"- {name}  (id: {cid})" for name, cid in matches[:40]]
    more = "" if len(matches) <= 40 else f"\n…(+{len(matches)-40} more)"
    await ctx.reply("Matching categories:\n" + "\n".join(lines) + more)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    lower = content.lower()
    # Let discord.py handle our actual commands
    if lower.startswith(".categories") or lower.startswith(".findcat"):
        await bot.process_commands(message)
        return

    # Everything else like ".british" ".compass" ".flush" is a HINT query
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
