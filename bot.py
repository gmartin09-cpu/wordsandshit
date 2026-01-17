#!/usr/bin/env python3
"""
Discord hint-solver bot (Railway-friendly)

Commands:
  .categories "Everyday Objects" "Foods & Drinks"
  .categories clear
  .categories show
  .findcat <search term>        (ex: .findcat food)
  .listcats                     (prints a small sample + count)

Hint query:
  .yeast
  .compass
  .british
(Anything starting with "." but NOT a bot command is treated as a hint query.)
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
WORDS_URL = os.getenv("WORDS_URL")  # optional (needed if WORDS_FILE isn't in container)


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
# Statement-based streaming parser (robust)
# =========================
# We split the file into "statements" by ';' so category objects / word objects
# can span multiple lines and still be parsed.

CAT_ID_RE   = re.compile(r"""['"]category_id['"]\s*:\s*['"]([^'"]+)['"]""")
CAT_NAME_RE = re.compile(r"""['"]name['"]\s*:\s*['"]([^'"]+)['"]""")

WORD_HINT_RE = re.compile(
    r"""\{\s*['"]word['"]\s*:\s*['"](?P<word>[^'"]+)['"]\s*,\s*['"]hint['"]\s*:\s*['"](?P<hint>[^'"]+)['"]\s*\}""",
    re.VERBOSE,
)

Record = Tuple[str, str, str, str]  # (category_id, category_name, word, hint)

def iter_statements(path: str, max_stmt_chars: int = 200_000):
    """
    Yield semicolon-terminated statements from a huge file.
    Keeps memory bounded by trimming if a statement goes insane.
    """
    buf = []
    buf_len = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            buf.append(line)
            buf_len += len(line)

            # If buffer becomes huge without ';', yield it anyway to avoid memory blowups.
            if buf_len > max_stmt_chars:
                stmt = "".join(buf)
                yield stmt
                buf.clear()
                buf_len = 0
                continue

            # Split on ';' but keep everything before each ';' as a statement.
            joined = "".join(buf)
            if ";" not in joined:
                continue

            parts = joined.split(";")
            # everything except the last part is complete statements
            for stmt in parts[:-1]:
                if stmt.strip():
                    yield stmt + ";"
            # last part becomes the new buffer (incomplete statement)
            tail = parts[-1]
            buf = [tail]
            buf_len = len(tail)

    # leftover tail
    tail = "".join(buf).strip()
    if tail:
        yield tail

def iter_records_and_categories(path: str):
    """
    Single pass: yields word/hint records AND builds category mapping.
    Returns (records_generator, category_map) pattern is awkward for Python generators,
    so instead this yields records and updates a shared dict.
    """
    current_cat_id = "unknown"
    current_cat_name = "Unknown"

    categories: Dict[str, str] = {}  # id -> name

    for stmt in iter_statements(path):
        # Category object detection (works even if not on one line, and with ' or ")
        if "category_id" in stmt and "name" in stmt:
            mid = CAT_ID_RE.search(stmt)
            mname = CAT_NAME_RE.search(stmt)
            if mid and mname:
                current_cat_id = mid.group(1)
                current_cat_name = mname.group(1)
                categories[current_cat_id] = current_cat_name
                continue

        # Word/hint object detection
        if "word" in stmt and "hint" in stmt:
            m = WORD_HINT_RE.search(stmt)
            if m:
                yield (current_cat_id, current_cat_name, m.group("word"), m.group("hint")), categories

def build_category_maps(path: str):
    """
    Scan until end, building:
      - cat_id -> name
      - aliases: normalized name/id -> (id, name)
    """
    cat_by_id: Dict[str, str] = {}
    # We don't actually need to store all records here, just walk statements once
    for _rec, cats in iter_records_and_categories(path):
        cat_by_id = cats  # same dict reference, updated over time

    aliases: Dict[str, Tuple[str, str]] = {}
    for cid, cname in cat_by_id.items():
        aliases[norm_cat(cid)] = (cid, cname)
        aliases[norm_cat(cname)] = (cid, cname)

    return cat_by_id, aliases


# =========================
# Build category maps at startup (one pass)
# =========================

print("Building category maps (one pass)...", flush=True)
CAT_BY_ID, CAT_ALIASES = build_category_maps(WORDS_FILE)
print(f"Categories discovered: {len(CAT_BY_ID)}", flush=True)


# =========================
# Solver (single streaming pass per query)
# =========================

def solve_hint(
    path: str,
    query_hint: str,
    allowed_cat_ids_norm: Optional[Set[str]] = None,  # normalized cat_ids
    mode: str = "exact",
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

    current_cat_id = "unknown"
    current_cat_name = "Unknown"

    for stmt in iter_statements(path):
        # Track category as we stream
        if "category_id" in stmt and "name" in stmt:
            mid = CAT_ID_RE.search(stmt)
            mname = CAT_NAME_RE.search(stmt)
            if mid and mname:
                current_cat_id = mid.group(1)
                current_cat_name = mname.group(1)
                continue

        if "word" in stmt and "hint" in stmt:
            m = WORD_HINT_RE.search(stmt)
            if not m:
                continue

            if allowed_cat_ids_norm is not None:
                if norm_cat(current_cat_id) not in allowed_cat_ids_norm:
                    continue

            hint = m.group("hint")
            if hint_ok(hint):
                word = m.group("word")
                header = f"{current_cat_name} ({current_cat_id})"
                grouped[header].append(word)
                total += 1
                if total >= limit:
                    break

    return grouped

def format_compact(grouped: Dict[str, List[str]]) -> str:
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        return "No matches."

    # Unique words across categories
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
intents.message_content = True  # also enable in Discord Developer Portal

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Per-user: store normalized category IDs
USER_ALLOWED_CAT_IDS: Dict[int, Set[str]] = {}

def parse_quoted_args(s: str) -> List[str]:
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in re.finditer(r'"([^"]+)"|(\S+)', s)
    ]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})", flush=True)


@bot.command(name="listcats")
async def listcats_cmd(ctx: commands.Context):
    """Quick sanity command to prove categories were discovered."""
    if not CAT_BY_ID:
        await ctx.reply("No categories discovered from dataset.")
        return
    sample = list(CAT_BY_ID.items())[:25]
    lines = [f"- {name} (id: {cid})" for cid, name in sample]
    await ctx.reply(f"Categories discovered: {len(CAT_BY_ID)}\nSample:\n" + "\n".join(lines))


@bot.command(name="findcat")
async def findcat_cmd(ctx: commands.Context, *, query: str = ""):
    """
    Usage:
      .findcat food
      .findcat drink
      .findcat everyday
    """
    q = norm_cat(query)
    if not q:
        await ctx.reply('Usage: .findcat <search term>  (ex: .findcat food)')
        return

    # Search across real names + ids
    matches: List[Tuple[str, str]] = []
    for cid, cname in CAT_BY_ID.items():
        if q in norm_cat(cname) or q in norm_cat(cid):
            matches.append((cname, cid))

    matches.sort(key=lambda x: (x[0].lower(), x[1].lower()))
    if not matches:
        await ctx.reply("No category matches.")
        return

    lines = [f"- {name} (id: {cid})" for name, cid in matches[:40]]
    more = "" if len(matches) <= 40 else f"\n…(+{len(matches)-40} more)"
    await ctx.reply("Matching categories:\n" + "\n".join(lines) + more)


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
        hit = CAT_ALIASES.get(norm_cat(t))
        if hit:
            cid, cname = hit
            resolved_ids.add(norm_cat(cid))
            resolved_pretty.append(f"{cname} ({cid})")
        else:
            unresolved.append(t)

    if not resolved_ids:
        await ctx.reply(
            "No valid categories recognized.\n"
            "Tip: try `.listcats` to see a sample, or `.findcat everyday` etc."
        )
        return

    USER_ALLOWED_CAT_IDS[ctx.author.id] = resolved_ids

    msg = "Set!\nAllowed:\n" + "\n".join(f"- {x}" for x in resolved_pretty)
    if unresolved:
        msg += "\nIgnored (unknown):\n" + "\n".join(f"- {x}" for x in unresolved) + \
               "\nTip: use `.findcat <term>` to find the exact category name/id."
    await ctx.reply(msg)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    lower = content.lower()
    # Let discord.py handle our actual commands
    if lower.startswith(".categories") or lower.startswith(".findcat") or lower.startswith(".listcats"):
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
