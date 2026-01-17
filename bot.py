#!/usr/bin/env python3
import os
import re
import sys
import asyncio
from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional, Set, Tuple

import requests
import discord
from discord.ext import commands

# =========================
# Env / Config
# =========================

def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing env var: {name}")
    return val

DISCORD_TOKEN = require_env("DISCORD_TOKEN")
WORDS_FILE = require_env("WORDS_FILE")
WORDS_URL = os.getenv("WORDS_URL")  # recommended for Railway

# =========================
# Download helpers (Google Drive aware)
# =========================

_GDRIVE_ID_RE = re.compile(r"(?:/d/|id=)([a-zA-Z0-9_-]{10,})")

def _extract_gdrive_file_id(url: str) -> Optional[str]:
    m = _GDRIVE_ID_RE.search(url)
    return m.group(1) if m else None

def _looks_like_html(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(256).lstrip()
        return head.lower().startswith(b"<!doctype html") or head.lower().startswith(b"<html")
    except Exception:
        return False

def _download_stream(session: requests.Session, url: str, out_path: str) -> None:
    with session.get(url, stream=True, allow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def download_words_file(url: str, out_path: str) -> None:
    """
    Supports:
      - Normal direct URLs
      - Google Drive share/view links (handles confirm page)
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    file_id = _extract_gdrive_file_id(url)
    if file_id:
        # Use Google Drive download endpoint
        base = "https://drive.google.com/uc?export=download"
        first = f"{base}&id={file_id}"

        # 1st request (may return confirm HTML)
        resp = session.get(first, allow_redirects=True, timeout=120)
        resp.raise_for_status()

        # If it looks like a file download, stream it
        cd = resp.headers.get("Content-Disposition", "")
        ct = resp.headers.get("Content-Type", "")
        if "attachment" in cd.lower():
            # Stream the actual file from the same URL
            _download_stream(session, first, out_path)
            return

        # Otherwise, it’s likely the “confirm download” HTML page.
        # Find confirm token in HTML and retry.
        text = resp.text

        # Common pattern: confirm=XXXX
        m = re.search(r"confirm=([0-9A-Za-z_]+)", text)
        if not m:
            # Another pattern: name="confirm" value="XXXX"
            m = re.search(r'name="confirm"\s+value="([^"]+)"', text)

        if not m:
            raise RuntimeError(
                "Google Drive did not return a direct file or a confirm token. "
                "Make sure the file is shared as 'Anyone with the link' and try again."
            )

        confirm = m.group(1)
        second = f"{base}&confirm={confirm}&id={file_id}"
        _download_stream(session, second, out_path)
        return

    # Non-GDrive URL
    _download_stream(session, url, out_path)

# Ensure dataset exists in Railway container
if not os.path.exists(WORDS_FILE):
    if not WORDS_URL:
        raise RuntimeError("WORDS_FILE missing and WORDS_URL not set.")
    print("Downloading word data...", flush=True)
    download_words_file(WORDS_URL, WORDS_FILE)
    size = os.path.getsize(WORDS_FILE)
    print(f"Download complete. Size: {size:,} bytes", flush=True)

    # Safety: if we downloaded HTML, fail loudly.
    if _looks_like_html(WORDS_FILE):
        raise RuntimeError(
            "Downloaded content looks like HTML (likely Google Drive confirm page), not your dataset. "
            "Fix WORDS_URL or keep the Google Drive file public."
        )

# =========================
# Minimal robust parser (statement-based)
# =========================

def norm_hint(s: str) -> str:
    return s.strip().lower()

def norm_cat(s: str) -> str:
    s = s.strip().lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def iter_statements(path: str, max_stmt_chars: int = 200_000):
    buf = []
    buf_len = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            buf.append(line)
            buf_len += len(line)

            if buf_len > max_stmt_chars:
                stmt = "".join(buf)
                yield stmt
                buf.clear()
                buf_len = 0
                continue

            joined = "".join(buf)
            if ";" not in joined:
                continue

            parts = joined.split(";")
            for stmt in parts[:-1]:
                if stmt.strip():
                    yield stmt + ";"
            tail = parts[-1]
            buf = [tail]
            buf_len = len(tail)

    tail = "".join(buf).strip()
    if tail:
        yield tail

CAT_ID_RE   = re.compile(r"""['"]category_id['"]\s*:\s*['"]([^'"]+)['"]""")
CAT_NAME_RE = re.compile(r"""['"]name['"]\s*:\s*['"]([^'"]+)['"]""")
WORD_HINT_RE = re.compile(
    r"""\{\s*['"]word['"]\s*:\s*['"](?P<word>[^'"]+)['"]\s*,\s*['"]hint['"]\s*:\s*['"](?P<hint>[^'"]+)['"]\s*\}"""
)

def build_category_maps(path: str):
    cat_by_id: Dict[str, str] = {}
    current_id = "unknown"
    current_name = "Unknown"

    for stmt in iter_statements(path):
        if "category_id" in stmt and "name" in stmt:
            mid = CAT_ID_RE.search(stmt)
            mname = CAT_NAME_RE.search(stmt)
            if mid and mname:
                current_id = mid.group(1)
                current_name = mname.group(1)
                cat_by_id[current_id] = current_name
                continue

    aliases: Dict[str, Tuple[str, str]] = {}
    for cid, cname in cat_by_id.items():
        aliases[norm_cat(cid)] = (cid, cname)
        aliases[norm_cat(cname)] = (cid, cname)

    return cat_by_id, aliases

print("Building category maps...", flush=True)
CAT_BY_ID, CAT_ALIASES = build_category_maps(WORDS_FILE)
print(f"Categories discovered: {len(CAT_BY_ID)}", flush=True)

def solve_hint(path: str, query_hint: str, allowed_cat_ids_norm: Optional[Set[str]] = None, limit: int = 200):
    q = norm_hint(query_hint)
    grouped: DefaultDict[str, List[str]] = defaultdict(list)

    current_id = "unknown"
    current_name = "Unknown"
    total = 0

    for stmt in iter_statements(path):
        if "category_id" in stmt and "name" in stmt:
            mid = CAT_ID_RE.search(stmt)
            mname = CAT_NAME_RE.search(stmt)
            if mid and mname:
                current_id = mid.group(1)
                current_name = mname.group(1)
                continue

        if "word" in stmt and "hint" in stmt:
            m = WORD_HINT_RE.search(stmt)
            if not m:
                continue

            if allowed_cat_ids_norm is not None and norm_cat(current_id) not in allowed_cat_ids_norm:
                continue

            if norm_hint(m.group("hint")) == q:
                grouped[f"{current_name} ({current_id})"].append(m.group("word"))
                total += 1
                if total >= limit:
                    break

    return grouped

def format_compact(grouped: Dict[str, List[str]]) -> str:
    total = sum(len(v) for v in grouped.values())
    if total == 0:
        return "No matches."
    words = []
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
# Discord
# =========================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

USER_ALLOWED_CAT_IDS: Dict[int, Set[str]] = {}

def parse_quoted_args(s: str) -> List[str]:
    return [m.group(1) if m.group(1) else m.group(2) for m in re.finditer(r'"([^"]+)"|(\S+)', s)]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})", flush=True)

@bot.command(name="listcats")
async def listcats_cmd(ctx: commands.Context):
    if not CAT_BY_ID:
        await ctx.reply("No categories discovered from dataset. (This usually means the downloaded file is wrong.)")
        return
    sample = list(CAT_BY_ID.items())[:25]
    lines = [f"- {name} (id: {cid})" for cid, name in sample]
    await ctx.reply(f"Categories discovered: {len(CAT_BY_ID)}\nSample:\n" + "\n".join(lines))

@bot.command(name="findcat")
async def findcat_cmd(ctx: commands.Context, *, query: str = ""):
    q = norm_cat(query)
    if not q:
        await ctx.reply("Usage: .findcat <term> (ex: .findcat food)")
        return
    matches = []
    for cid, cname in CAT_BY_ID.items():
        if q in norm_cat(cname) or q in norm_cat(cid):
            matches.append((cname, cid))
    matches.sort(key=lambda x: (x[0].lower(), x[1].lower()))
    if not matches:
        await ctx.reply("No category matches.")
        return
    lines = [f"- {name} (id: {cid})" for name, cid in matches[:40]]
    await ctx.reply("Matching categories:\n" + "\n".join(lines))

@bot.command(name="categories")
async def categories_cmd(ctx: commands.Context):
    raw = ctx.message.content[len(".categories"):].strip()
    tokens = parse_quoted_args(raw)
    if not tokens:
        await ctx.reply('Usage: .categories "Everyday Objects" "Foods & Drinks" | .categories clear | .categories show')
        return

    if len(tokens) == 1 and tokens[0].lower() == "clear":
        USER_ALLOWED_CAT_IDS.pop(ctx.author.id, None)
        await ctx.reply("Cleared category filter (all categories allowed).")
        return

    if len(tokens) == 1 and tokens[0].lower() == "show":
        allowed = USER_ALLOWED_CAT_IDS.get(ctx.author.id)
        await ctx.reply("No category filter set." if not allowed else "Allowed IDs:\n" + "\n".join(sorted(allowed)))
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
        await ctx.reply("No valid categories recognized. Try .listcats or .findcat <term>.")
        return

    USER_ALLOWED_CAT_IDS[ctx.author.id] = resolved_ids
    msg = "Set!\nAllowed:\n" + "\n".join(f"- {x}" for x in resolved_pretty)
    if unresolved:
        msg += "\nIgnored (unknown):\n" + "\n".join(f"- {x}" for x in unresolved)
    await ctx.reply(msg)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()
    if not content.startswith("."):
        return

    lower = content.lower()
    if lower.startswith(".categories") or lower.startswith(".findcat") or lower.startswith(".listcats"):
        await bot.process_commands(message)
        return

    hint = content[1:].strip()
    if not hint:
        return

    allowed_ids = USER_ALLOWED_CAT_IDS.get(message.author.id)

    loop = asyncio.get_running_loop()
    grouped = await loop.run_in_executor(None, lambda: solve_hint(WORDS_FILE, hint, allowed_ids, 200))
    await message.reply(format_compact(grouped))

bot.run(DISCORD_TOKEN)
