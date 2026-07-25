#!/usr/bin/env python3
"""
NomiVault — Nomi.ai conversation archiver.
Exports all chat history to self-contained HTML files.

Usage:
  python3 nomivault.py --key YOUR_API_KEY
  python3 nomivault.py --key YOUR_API_KEY --messages-url "/v1/nomis/{uuid}/your-endpoint"

The script auto-discovers the message history endpoint by probing common patterns.
If auto-discovery fails, follow the DevTools step in the README to find the URL,
then pass it via --messages-url.
"""

__version__ = "1.6.2"

import argparse
import configparser
import io
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error
import uuid as _uuid_mod

try:
    import markdown as _md_lib        # pip install markdown
    _HAS_MARKDOWN = True
except ImportError:
    _md_lib = None
    _HAS_MARKDOWN = False

BASE_URL  = "https://api.nomi.ai"
BETA_BASE = "https://beta.nomi.ai/api"
# App-version string sent by the web client.  Update this if requests start
# failing with 400 errors — grab the current value from the 'av=' param in
# any beta.nomi.ai Network request in DevTools.
BETA_AV   = "20260524201704-428b82ed8a3daa886058087234544447686db197"
OUTPUT_DIR = Path(__file__).parent / "output"

# Mind-map category API names → display labels shown in the web app
MIND_MAP_CATEGORIES: dict[str, str] = {
    "Entity":  "Lore",
    "Keyword": "Topics",
    "Goal":    "Goals",
}

# Endpoint patterns tried in order when auto-discovering message history.
# {uuid} is replaced with the Nomi's UUID at runtime.
ENDPOINT_CANDIDATES = [
    "/v1/nomis/{uuid}/chats",
    "/v1/nomis/{uuid}/messages",
    "/v1/nomis/{uuid}/chat-messages",
    "/v1/nomis/{uuid}/chat",
]

# PWA manifest written alongside index.html so the archive can be added to
# the iOS / Android home screen and open without browser chrome.
_PWA_MANIFEST = """\
{
  "name": "NomiVault",
  "short_name": "NomiVault",
  "description": "NomiVault — Nomi.ai conversation archive",
  "start_url": "index.html",
  "scope": ".",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#1a1a2e",
  "theme_color": "#16213e",
  "icons": [
    {
      "src": "favicon.png",
      "sizes": "512x512",
      "type": "image/png",
      "purpose": "any maskable"
    },
    {
      "src": "nomi-icon.svg",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    }
  ]
}
"""

# Minimal service worker — registers so browsers treat the site as an
# installable PWA (required by Chrome/Android for standalone mode).
# No caching: always fetches fresh from the server, which is correct for a
# frequently-updated local archive.
_SW_JS = """\
// NomiVault service worker
// Pass-through only — satisfies the PWA installability requirement without
// caching anything, so the archive always reflects the latest run.
self.addEventListener('fetch', function(event) {
  event.respondWith(fetch(event.request));
});
"""

# Simple SVG app icon — dark background with the "N" brand letter.
_PWA_ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#16213e"/>
  <text x="256" y="375"
        font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
        font-size="340" font-weight="700" text-anchor="middle" fill="#e94560">N</text>
</svg>
"""


# ---------------------------------------------------------------------------
# Helpers: naming, cache, deduplication
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    """Filesystem-safe version of a Nomi name used for output filenames."""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()


def _nomi_folder_name(safe_name: str, numeric_id) -> str:
    """Return this Nomi's output folder name.

    Suffixed with its numeric ID (once known) to guarantee uniqueness even
    if two Nomis share a display name. Before the ID is known (no --token
    yet, or the very first run before beta discovery/--nomi-id resolves
    it), falls back to a bare placeholder folder named after the Nomi;
    ``_resolve_nomi_dir`` renames it once the ID becomes known.
    """
    return f"{safe_name}-{numeric_id}" if numeric_id else safe_name


def _resolve_nomi_dir(safe_name: str, numeric_id) -> Path:
    """Return this Nomi's output folder, migrating older layouts if needed.

    Two migrations happen automatically, both one-time and idempotent:
      1. A placeholder folder (created before the numeric ID was known) is
         renamed to the ID-suffixed name as soon as the ID becomes known.
      2. Pre-1.5 flat output (root-level <Name>.html/.json/-mind-map.html/
         -media.html and a shared media/<Name>/ folder) is moved into this
         Nomi's own folder.
    """
    target      = OUTPUT_DIR / _nomi_folder_name(safe_name, numeric_id)
    placeholder = OUTPUT_DIR / safe_name

    if numeric_id and placeholder != target and placeholder.exists() and not target.exists():
        placeholder.rename(target)

    if not target.exists():
        # (old_path, new filename) — the chat page is renamed to the
        # <Name>-chat.html convention (matching -mind-map.html/-media.html)
        # as part of this migration; everything else keeps its name.
        old_renames = [
            (OUTPUT_DIR / f"{safe_name}.html",          f"{safe_name}-chat.html"),
            (OUTPUT_DIR / f"{safe_name}.json",          f"{safe_name}.json"),
            (OUTPUT_DIR / f"{safe_name}-mind-map.html", f"{safe_name}-mind-map.html"),
            (OUTPUT_DIR / f"{safe_name}-media.html",    f"{safe_name}-media.html"),
        ]
        old_media = OUTPUT_DIR / "media" / safe_name
        target.mkdir(parents=True, exist_ok=True)
        for old_path, new_name in old_renames:
            if old_path.exists():
                old_path.rename(target / new_name)
        if old_media.exists():
            old_media.rename(target / "media")

    return target


def _patch_legacy_html_links(html_text: str, safe_name: str) -> str:
    """Fix cross-page/asset references baked into HTML rendered by an older
    layout, for a Nomi whose page is never re-rendered again (e.g. one
    that's been deleted on Nomi.ai, so the main loop no longer visits it).
    Active Nomis don't need this — their pages are freshly re-rendered
    every run, which already produces correct links.

    Safe to apply repeatedly: every replacement is a no-op if the text is
    already up to date.
    """
    html_text = html_text.replace('href="index.html"', 'href="../index.html"')
    html_text = html_text.replace('href="favicon.png"', 'href="../favicon.png"')
    html_text = html_text.replace(f"media/{safe_name}/", "media/")
    html_text = html_text.replace(f'href="{safe_name}.html"', f'href="{safe_name}-chat.html"')
    return html_text


def _peek_cached_numeric_id(safe_name: str, known_numeric_id) -> int | None:
    """Read numeric_nomi_id from wherever this Nomi's cache currently lives,
    without migrating or renaming anything. Used only to decide whether
    --nomi-id is ambiguous before the main loop starts.
    """
    for candidate in (
        OUTPUT_DIR / _nomi_folder_name(safe_name, known_numeric_id) / f"{safe_name}.json",
        OUTPUT_DIR / safe_name / f"{safe_name}.json",
        OUTPUT_DIR / f"{safe_name}.json",
    ):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8")).get("numeric_nomi_id")
            except Exception:
                pass
    return None


def _cache_path(nomi_dir: Path, safe_name: str) -> Path:
    return nomi_dir / f"{safe_name}.json"


def load_cache(nomi_dir: Path, safe_name: str) -> dict:
    """Load the full cache record from the JSON sidecar.

    Returns a dict with keys: messages, voice_calls, transcripts, numeric_nomi_id.
    Always returns the empty-structure dict if no cache file exists or on error.
    """
    empty: dict = {
        "messages": [],
        "voice_calls": [],
        "transcripts": {},
        "numeric_nomi_id": None,
        "mind_map_terms": [],
        "selfies": [],
        "user_uploads": [],
    }
    path = _cache_path(nomi_dir, safe_name)
    if not path.exists():
        return empty
    try:
        return {**empty, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return empty


def save_cache(nomi_dir: Path, safe_name: str, nomi: dict, messages: list,
               voice_calls: list | None = None,
               transcripts: dict | None = None,
               numeric_nomi_id: int | None = None,
               mind_map_terms: list | None = None,
               selfies: list | None = None,
               user_uploads: list | None = None) -> None:
    """Persist messages, voice-call data, mind-map terms, selfie metadata, and user uploads."""
    path = _cache_path(nomi_dir, safe_name)
    payload = {
        "uuid": nomi["uuid"],
        "name": nomi["name"],
        "last_fetched": datetime.now(timezone.utc).isoformat(),
        "numeric_nomi_id": numeric_nomi_id,
        "messages": messages,
        "voice_calls": voice_calls or [],
        "transcripts": transcripts or {},
        "mind_map_terms": mind_map_terms or [],
        "selfies": selfies or [],
        "user_uploads": user_uploads or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_group_cache(group_dir: Path, safe_name: str) -> dict:
    """Load a group chat's cache record. Mirrors load_cache(), but stores
    raw_selfies (the nested per-selfie-request wrapper shape from the
    group messages endpoint) instead of the flat selfies list individual
    Nomis use, since that's what's needed to correctly dedupe on the next
    incremental run.
    """
    empty: dict = {
        "messages": [],
        "raw_selfies": [],
        "mind_map_terms": [],
        "numeric_group_id": None,
    }
    path = _cache_path(group_dir, safe_name)
    if not path.exists():
        return empty
    try:
        return {**empty, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return empty


def save_group_cache(group_dir: Path, safe_name: str, group: dict, messages: list,
                     raw_selfies: list | None = None,
                     mind_map_terms: list | None = None) -> None:
    """Persist a group chat's messages, raw selfies, and mind-map terms."""
    path = _cache_path(group_dir, safe_name)
    payload = {
        "uuid": group["uuid"],
        "name": group["name"],
        "numeric_group_id": group.get("id"),
        "last_fetched": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
        "raw_selfies": raw_selfies or [],
        "mind_map_terms": mind_map_terms or [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def msg_fingerprint(msg: dict) -> str:
    """Stable identity for a message used for deduplication.

    Prefers an explicit ID field; falls back to a (timestamp, text-prefix) hash
    so the script works even if the API doesn't expose message IDs.
    """
    for key in ("id", "uuid", "messageId", "message_id", "msgId"):
        if msg.get(key):
            return f"id:{msg[key]}"
    ts = _msg_timestamp(msg)
    text = ""
    for key in ("text", "message", "content", "body"):
        if msg.get(key):
            text = str(msg[key])[:120]
            break
    return f"ts:{ts}|{text}"


def merge_messages(cached: list, fresh: list) -> tuple[list, int]:
    """Append any messages from *fresh* that are not already in *cached*.

    Returns (merged_list, number_of_new_messages).
    """
    seen = {msg_fingerprint(m) for m in cached}
    new_msgs = [m for m in fresh if msg_fingerprint(m) not in seen]
    return cached + new_msgs, len(new_msgs)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path: str, api_key: str):
    """GET from the Nomi.ai API. Returns parsed JSON or None on 404."""
    req = urllib.request.Request(
        BASE_URL + path,
        headers={"Authorization": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {body}") from exc


def fetch_nomis(api_key: str) -> list:
    data = api_get("/v1/nomis", api_key)
    if data is None:
        return []
    return data.get("nomis", [])


def discover_endpoint(nomi_uuid: str, api_key: str, override: str | None) -> tuple[str | None, any]:
    """Try each candidate endpoint; return (pattern, first_response) or (None, None)."""
    candidates = [override] if override else ENDPOINT_CANDIDATES
    for pattern in candidates:
        path = pattern.replace("{uuid}", nomi_uuid)
        print(f"  Trying {path} ...", end=" ", flush=True)
        result = api_get(path, api_key)
        if result is not None:
            print("OK")
            return pattern, result
        print("not found")
    return None, None


def extract_message_list(data) -> tuple[list, str | None]:
    """Pull the message array and next-page cursor out of an API response."""
    if isinstance(data, list):
        return data, None

    if isinstance(data, dict):
        batch = []
        for key in ("messages", "chats", "items", "data", "results"):
            if key in data and isinstance(data[key], list):
                batch = data[key]
                break

        cursor = None
        for key in ("cursor", "nextCursor", "next", "nextPage", "after"):
            if data.get(key):
                cursor = data[key]
                break

        return batch, cursor

    return [], None


def fetch_all_messages(nomi_uuid: str, api_key: str, pattern: str,
                       since_ts: str | None = None) -> list:
    """Page through the history endpoint and collect every message.

    If *since_ts* is provided (ISO-8601 string), it is passed as ``?since=``
    on the very first request as a hint to the API to return only newer
    messages.  Deduplication in the caller guarantees correctness even if the
    API ignores the parameter.
    """
    from urllib.parse import quote

    all_messages: list = []
    base_path = pattern.replace("{uuid}", nomi_uuid)
    cursor = None
    first_page = True

    while True:
        # Build query string: first page may include ?since=; all pages may
        # include &cursor= / ?cursor= for pagination.
        qs_parts: list[str] = []
        if first_page and since_ts:
            qs_parts.append(f"since={quote(since_ts)}")
        if cursor:
            qs_parts.append(f"cursor={quote(str(cursor))}")
        path = base_path + ("?" + "&".join(qs_parts) if qs_parts else "")
        first_page = False

        data = api_get(path, api_key)
        if data is None:
            break

        batch, cursor = extract_message_list(data)
        if not batch:
            break

        all_messages.extend(batch)
        if not cursor:
            break

        time.sleep(0.3)

    return all_messages


# ---------------------------------------------------------------------------
# beta.nomi.ai API (voice-call transcripts)
# ---------------------------------------------------------------------------

def beta_api_get(path: str, token: str, extra: dict | None = None):
    """GET from the beta.nomi.ai internal web API.

    Authentication is cookie-based (NextAuth).  *token* is the value of the
    ``__Secure-next-auth.session-token`` cookie found in DevTools.

    *extra* is merged into the query-string (used for pagination cursors).

    Returns parsed JSON, None on 404, or the string ``"AUTH_FAILED"`` on
    401 / 403.  A fresh correlation-request-ID (cri) is generated per call.
    """
    from urllib.parse import urlencode
    params = {
        "v":   "1",
        "p":   "Web",
        "av":  BETA_AV,
        "cri": str(_uuid_mod.uuid4()),
    }
    if extra:
        params.update(extra)
    qs = urlencode(params)
    url = f"{BETA_BASE}{path}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie":  f"__Secure-next-auth.session-token={token}",
            "Accept":  "application/json",
            "Referer": "https://beta.nomi.ai/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "AUTH_FAILED"
        if exc.code == 404:
            return None
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {BETA_BASE}{path}: {body}") from exc


def fetch_beta_nomi_info(token: str) -> dict:
    """Return per-Nomi info from the beta.nomi.ai internal Nomi list, keyed by UUID.

    The public /v1/nomis endpoint (used without --token) only returns
    name/uuid; the beta endpoint additionally exposes each Nomi's numeric
    ID ("id" — needed for every other beta.nomi.ai call) and its
    currently-selected profile picture. This lets the numeric ID be
    auto-discovered on a Nomi's very first --token run, without the user
    needing to look it up in the browser URL and pass --nomi-id manually.

    The profile picture is tracked in one of three fields, checked in this
    order: "imageEditRequestUuid" (set when the active picture is an
    edited image — pictureImageId/pictureSelfieImageId are NOT updated in
    this case and keep pointing at whichever picture was active before
    the edit, so this field must be checked first), "pictureSelfieImageId"
    (a generated selfie), or "pictureImageId" (a character image; also
    the field populated at Nomi creation).

    Returns an empty dict on any failure (missing/expired token,
    unexpected response shape).
    """
    data = beta_api_get("/nomis", token)
    if not data or data == "AUTH_FAILED":
        return {}
    nomis = data.get("nomis", data) if isinstance(data, dict) else data
    if not isinstance(nomis, list):
        return {}
    return {
        n["uuid"]: {
            "numeric_id":           n.get("id"),
            "pictureImageId":       n.get("pictureImageId"),
            "pictureSelfieImageId": n.get("pictureSelfieImageId"),
            "imageEditRequestUuid": n.get("imageEditRequestUuid"),
        }
        for n in nomis
        if n.get("uuid")
    }


def fetch_beta_group_chats(token: str) -> list:
    """Return every group chat on the account, each with full metadata
    (name, uuid, numeric id, participant Nomis, the group's memory
    "note", etc.) from the beta.nomi.ai internal API. Unlike individual
    Nomis, a group chat's numeric ID is already present in this listing —
    no separate auto-discovery step is needed. Returns [] on any failure.
    """
    data = beta_api_get("/group-chats", token)
    if not data or data == "AUTH_FAILED":
        return []
    groups = data.get("groupChats", data) if isinstance(data, dict) else data
    return groups if isinstance(groups, list) else []


def fetch_beta_group_messages(group_id, token: str,
                              known_ids: set | None = None) -> tuple[list, list]:
    """Fetch new group-chat messages by paginating backwards through history.

    Mirrors ``fetch_beta_messages`` (same ``?max=<id>`` backwards-cursor
    pagination), but for /group-chats/{id}/messages instead of a single
    Nomi's /chat/messages. Each response also embeds any selfies
    generated in that batch of messages (nested per selfie-request, with
    a list of participant Nomi IDs rather than a single owner) — these
    are collected and deduplicated by their outer id across pages.

    Returns ``(new_messages, raw_selfies)`` — raw_selfies is still in the
    nested wrapper shape; see ``_flatten_group_selfies``.
    """
    seen_ids:        set  = set(known_ids) if known_ids else set()
    incremental           = bool(known_ids)
    all_messages:    list = []
    selfies_by_id:   dict = {}
    extra:           dict = {}
    page                   = 0
    prev_oldest_uuid       = None

    while True:
        page += 1
        data = beta_api_get(f"/group-chats/{group_id}/messages", token, extra=extra)

        if data == "AUTH_FAILED":
            print("  ⚠  beta.nomi.ai auth failed — check your --token value.")
            break
        if not data:
            break

        batch = data.get("messages", [])
        for s in data.get("selfies", []):
            if s.get("id"):
                selfies_by_id[s["id"]] = s

        if not batch:
            print(f"    ↳ empty page — reached beginning of history")
            break

        new_msgs = []
        for m in batch:
            mid = m.get("uuid") or m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                new_msgs.append(m)

        if not new_msgs:
            if incremental:
                print(f"    ↳ reached cached history — stopping early")
            else:
                print(f"    ↳ no new messages on page {page} — stopping")
            break

        all_messages.extend(new_msgs)
        print(f"    page {page}: {len(new_msgs)} new  ({len(all_messages)} total so far)",
              flush=True)

        oldest    = min(batch, key=lambda m: m.get("sent", ""))
        oldest_id = oldest.get("id") or oldest.get("uuid")

        if not oldest_id or oldest_id == prev_oldest_uuid:
            print(f"    ↳ cursor did not advance — stopping")
            break

        prev_oldest_uuid = oldest_id
        extra = {"max": oldest_id}
        time.sleep(0.2)

    return all_messages, list(selfies_by_id.values())


def _flatten_group_selfies(raw_selfies: list, group_id) -> list[dict]:
    """Expand the nested group-selfie wrapper shape into flat per-image
    dicts matching the shape individual selfies already use elsewhere
    (mediaType, id, selfieRequestId, completed, type, local_filename),
    so the existing gallery renderer and download_selfie_image() can be
    reused as-is for group chats too.
    """
    flat: list[dict] = []
    for wrapper in raw_selfies:
        req_id    = wrapper.get("id")
        completed = wrapper.get("completed", "")
        for img in wrapper.get("selfies", []):
            flat.append({
                "mediaType":       "Selfie",
                "type":            "Photo",
                "id":              img.get("id"),
                "selfieRequestId": req_id,
                "completed":       completed,
                "hidden":          img.get("hidden", False),
                "groupChatId":     group_id,
                "nomiIds":         wrapper.get("nomiIds", []),
            })
    return flat


def fetch_beta_messages(nomi_id, token: str,
                        known_ids: set | None = None) -> tuple[list, list, int | None]:
    """Fetch new chat messages by paginating backwards through history.

    The API returns the most-recent page first (up to ~200 messages).  Each
    subsequent request passes ``?max=<oldest_id>`` to ask for the next older
    page.

    *known_ids* should contain the ``uuid``/``id`` values of every message
    already in the local cache.  It is pre-seeded into ``seen_ids`` so that
    when all messages on a page are already cached the existing dedup check
    fires immediately and pagination stops — no need to walk all the way back
    to the beginning of history on every incremental run.

    Passing ``None`` (first run or ``--full``) downloads the complete history.

    Returns ``(new_messages, voice_calls, numeric_nomi_id)``.
    """
    # Pre-seed with cached IDs so pagination stops when we hit known history.
    seen_ids:        set  = set(known_ids) if known_ids else set()
    incremental           = bool(known_ids)
    all_messages:    list = []
    voice_calls:     list = []
    numeric_id             = None
    extra:           dict = {}
    page                   = 0
    prev_oldest_uuid       = None

    while True:
        page += 1
        data = beta_api_get(f"/nomis/{nomi_id}/chat/messages", token, extra=extra)

        if data == "AUTH_FAILED":
            print("  ⚠  beta.nomi.ai auth failed — check your --token value.")
            break
        if not data:
            break

        batch = data.get("messages", [])

        # Voice calls and numeric ID are only in the first (newest) response
        if page == 1:
            voice_calls = data.get("voiceCalls", [])
            numeric_id  = voice_calls[0]["nomiId"] if voice_calls else None

        if not batch:
            print(f"    ↳ empty page — reached beginning of history")
            break

        # Keep only messages not already seen in this fetch or in the cache
        new_msgs = []
        for m in batch:
            mid = m.get("uuid") or m.get("id")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                new_msgs.append(m)

        if not new_msgs:
            # All messages on this page are already known.  In incremental
            # mode this means we've reached the cached overlap; in full mode
            # it means the API cursor stopped advancing (loop guard).
            if incremental:
                print(f"    ↳ reached cached history — stopping early")
            else:
                print(f"    ↳ no new messages on page {page} — stopping")
            break

        all_messages.extend(new_msgs)
        print(f"    page {page}: {len(new_msgs)} new  ({len(all_messages)} total so far)",
              flush=True)

        # Determine the oldest message to use as the backwards cursor.
        # The API paginates via ?max=<id> where <id> is the time-based UUID
        # stored in the message's "id" field (distinct from "uuid").
        oldest      = min(batch, key=lambda m: m.get("sent", ""))
        oldest_id   = oldest.get("id") or oldest.get("uuid")

        if not oldest_id or oldest_id == prev_oldest_uuid:
            # No usable cursor, or cursor didn't advance — we're done
            print(f"    ↳ cursor did not advance — stopping")
            break

        prev_oldest_uuid = oldest_id
        extra = {"max": oldest_id}
        time.sleep(0.2)

    return all_messages, voice_calls, numeric_id


def fetch_voice_transcript(nomi_id, call_uuid: str, token: str) -> list:
    """Return the spoken-word transcript lines for a single voice call."""
    data = beta_api_get(f"/nomis/{nomi_id}/voice-calls/{call_uuid}/messages", token)
    if not data or data == "AUTH_FAILED":
        return []
    return data.get("voiceCallMessages", [])


def _parse_dt(ts: str):
    """Parse an ISO-8601 string into an aware datetime, or return None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def match_voice_call(sentinel_sent: str, voice_calls: list) -> dict | None:
    """Return the voice call whose start time is closest to *sentinel_sent*.

    Only matches if the gap is within 5 minutes; otherwise returns None.
    """
    sentinel_dt = _parse_dt(sentinel_sent)
    if sentinel_dt is None or not voice_calls:
        return None
    best, best_delta = None, None
    for vc in voice_calls:
        dt = _parse_dt(vc.get("started", ""))
        if dt is None:
            continue
        delta = abs((dt - sentinel_dt).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = vc, delta
    return best if (best_delta is not None and best_delta < 300) else None


# ---------------------------------------------------------------------------
# Mind-map fetching
# ---------------------------------------------------------------------------

def fetch_mind_map_category(entity_id, category: str, token: str,
                            entity_type: str = "nomis") -> list:
    """Fetch every page of memory-terms for one category.

    *entity_type* is "nomis" for an individual Nomi or "group-chats" for a
    group chat's shared memory (undocumented; may not exist/return data for
    every group — treated the same as any other empty response).
    """
    terms: list = []
    page = 1
    while True:
        data = beta_api_get(
            f"/mind-maps/{entity_type}/{entity_id}/memory-terms", token,
            extra={
                "page": page,
                "category": category,
                "sort": "default",
                "dir": "desc",
                "hideInactive": "false",
            },
        )
        if not data or data == "AUTH_FAILED":
            break
        batch = data.get("memoryTerms", [])
        terms.extend(batch)
        if page >= data.get("totalPages", 1):
            break
        page += 1
        time.sleep(0.2)
    return terms


def fetch_mind_map_term_detail(entity_id, term_uuid: str, token: str,
                               entity_type: str = "nomis") -> dict | None:
    """Fetch the full detail (including dossier HTML) for a single memory term."""
    data = beta_api_get(f"/mind-maps/{entity_type}/{entity_id}/memory-terms/{term_uuid}", token)
    if not data or data == "AUTH_FAILED":
        return None
    return data


def fetch_full_mind_map(entity_id, token: str,
                        cached_terms: dict, entity_type: str = "nomis") -> dict[str, list]:
    """Fetch all three categories, pulling fresh dossiers only for new/updated terms.

    *cached_terms* maps term UUID → previously saved full-term dict.
    Returns a dict mapping category name → list of full term dicts.
    """
    result: dict[str, list] = {}
    for category, label in MIND_MAP_CATEGORIES.items():
        print(f"  Mind map – {label}: ", end="", flush=True)
        terms = fetch_mind_map_category(entity_id, category, token, entity_type=entity_type)
        full_terms: list = []
        fetched = 0
        for term in terms:
            uid = term["uuid"]
            cached = cached_terms.get(uid)
            # Re-fetch detail if never cached, or if the AI edited it since
            if (not cached
                    or "dossier" not in cached
                    or cached.get("aiEdited") != term.get("aiEdited")):
                detail = fetch_mind_map_term_detail(entity_id, uid, token, entity_type=entity_type)
                full_terms.append(detail if detail else term)
                fetched += 1
                time.sleep(0.1)
            else:
                full_terms.append(cached)
        print(f"{len(full_terms)} terms ({fetched} updated)")
        result[category] = full_terms
    return result


# ---------------------------------------------------------------------------
# Selfie fetching and downloading
# ---------------------------------------------------------------------------

def _media_item_key(item: dict) -> str | None:
    """Return whichever identifier a media item is actually keyed by.

    Most media types (Selfie, CharacterImage) use "id". VideoRequest and
    ImageEditRequest use "uuid" instead and have no "id" field at all —
    the download functions for those two already know this (see
    download_video_media / download_image_edit), but the generic
    dedup/caching logic needs the same fallback so those items don't get
    silently dropped for lacking an "id".
    """
    return item.get("id") or item.get("uuid")


def fetch_selfies(nomi_id, token: str) -> list:
    """Return metadata for every non-hidden, completed media item (all pages).

    Accepts any ``mediaType`` the API returns so that future or lesser-known
    types (e.g. image transformations) are not silently dropped.  The download
    loop in ``_run()`` handles known types explicitly and falls back to the
    selfie-image URL for anything else.
    """
    all_selfies: list = []
    page = 1
    while True:
        data = beta_api_get(
            f"/nomis/{nomi_id}/medias", token,
            extra={"page": page, "withCharacterImages": "true"},
        )
        if not data or data == "AUTH_FAILED":
            break
        batch = []
        for m in data.get("medias", []):
            if m.get("hidden"):
                continue
            # CharacterImage has no "completed" field — always include it.
            # For every other type, require "completed" so in-progress
            # generations are not included.
            if not (m.get("completed") or m.get("mediaType") == "CharacterImage"):
                continue
            if not _media_item_key(m):
                # Every media item is keyed by "id" or "uuid" elsewhere
                # (dedup, filenames, downloads) — skip anything missing
                # both rather than crash, but flag it since it means the
                # API returned a shape we haven't seen before.
                print(f"  ℹ  Skipping media item with no \"id\" or \"uuid\" field "
                      f"(mediaType={m.get('mediaType')!r}): {m}")
                continue
            batch.append(m)
        all_selfies.extend(batch)
        if page >= data.get("maxPages", 1):
            break
        page += 1
        time.sleep(0.2)

    # The API can return the same item on more than one page (pagination
    # overlap), which would otherwise slip past the caller's dedup-against-
    # cache check as two "new" entries pointing at the same downloaded
    # files. Some VideoRequest/ImageEditRequest items also appear to gain
    # an "id" field on a later fetch that they didn't have before (only
    # "uuid" that first time) — since a single preferred key would treat
    # that as a different item even though _video_filename/_image_edit_
    # filename still resolve it to the same downloaded file, dedup here by
    # ANY shared identifier rather than one fixed key.
    seen_ids: set = set()
    deduped: list = []
    for item in all_selfies:
        ids = {v for v in (item.get("id"), item.get("uuid")) if v}
        if ids and ids & seen_ids:
            continue
        seen_ids |= ids
        deduped.append(item)
    return deduped


def _selfie_filename(selfie: dict, safe_name: str) -> str:
    """Return a descriptive filename for a selfie: <NomiName>_<date>_<time>_<id8>.webp"""
    completed = selfie.get("completed", "")
    try:
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        ts = dt.strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        ts = "unknown"
    uid = str(selfie.get("id", "unknown"))
    return f"{safe_name}_{ts}_{uid[:8]}.webp"


def _video_filename(video: dict, safe_name: str) -> tuple[str, str]:
    """Return (preview_filename, video_filename) for a VideoRequest."""
    completed = video.get("completed", "")
    try:
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        ts = dt.strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        ts = "unknown"
    uid = str(video.get("uuid", video.get("id", "unknown")))[:8]
    return (
        f"{safe_name}_{ts}_{uid}_preview.webp",
        f"{safe_name}_{ts}_{uid}.mp4",
    )


def download_video_media(video: dict, selfies_dir: Path, token: str,
                         safe_name: str) -> tuple[str | None, str | None]:
    """Download a VideoRequest's preview thumbnail and video file.

    Returns ``(preview_filename, video_filename)``; individual values are
    ``None`` if that download failed.
    """
    uid = video.get("uuid", "")
    if not uid:
        return None, None

    preview_fn, video_fn = _video_filename(video, safe_name)
    preview_dest = selfies_dir / preview_fn
    video_dest   = selfies_dir / video_fn

    def _dl(url: str, dest: Path, accept: str) -> bool:
        if dest.exists():
            return True
        req = urllib.request.Request(
            url,
            headers={
                "Cookie":  f"__Secure-next-auth.session-token={token}",
                "Referer": "https://beta.nomi.ai/",
                "Accept":  accept,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                dest.write_bytes(resp.read())
            return True
        except Exception as exc:
            print(f"    ⚠  Could not download {dest.name}: {exc}")
            return False

    preview_ok = _dl(
        f"https://beta.nomi.ai/api/video-requests/{uid}/preview.webp",
        preview_dest, "image/webp,image/*",
    )
    video_ok = _dl(
        f"https://beta.nomi.ai/api/video-requests/{uid}.mp4",
        video_dest, "video/mp4,video/*",
    )
    return (preview_fn if preview_ok else None,
            video_fn   if video_ok   else None)


def _character_image_filename(img: dict, safe_name: str) -> str:
    """Return a descriptive filename for a CharacterImage: <NomiName>_character_<style>_<id8>.webp"""
    style = img.get("style", "")
    uid   = img["id"][:8]
    return f"{safe_name}_character_{style}_{uid}.webp" if style else f"{safe_name}_character_{uid}.webp"


def download_character_image(img: dict, selfies_dir: Path, token: str,
                              safe_name: str, nomi_id) -> str | None:
    """Download a CharacterImage to *selfies_dir*.

    The URL requires the numeric nomi ID, not the UUID.
    Returns the filename on success or None on failure.
    """
    img_id   = img["id"]
    filename = _character_image_filename(img, safe_name)
    dest     = selfies_dir / filename
    if dest.exists():
        return filename
    url = f"https://beta.nomi.ai/api/nomis/{nomi_id}/images/{img_id}.webp"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie":  f"__Secure-next-auth.session-token={token}",
            "Referer": "https://beta.nomi.ai/",
            "Accept":  "image/webp,image/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return filename
    except Exception as exc:
        print(f"    ⚠  Could not download character image {img_id[:8]}…: {exc}")
        return None


def download_profile_picture(picture_image_id: str, selfies_dir: Path, token: str,
                              safe_name: str, nomi_id,
                              all_selfies: list | None = None,
                              image_edit_uuid: str | None = None) -> tuple[str | None, bool]:
    """Download the Nomi's currently-selected profile picture.

    *picture_image_id* should be whichever of imageEditRequestUuid /
    pictureSelfieImageId / pictureImageId is currently active (see
    ``fetch_beta_nomi_info``). The filename is keyed by that ID, so a
    change to it triggers a fresh download instead of reusing a stale
    cached one.

    Character images are served from the generic per-Nomi image endpoint,
    but selfies are served from a selfie-request-scoped endpoint instead
    (see ``download_selfie_image``), and edited images (ImageEditRequest)
    from yet another endpoint (see ``download_image_edit``) — since the ID
    alone doesn't say which kind it is, the generic endpoint is tried
    first. If *image_edit_uuid* is given (the caller already knows this is
    an ImageEditRequest, from the imageEditRequestUuid field), the
    edit-specific URL is tried directly. Otherwise, as a fallback, we look
    the ID up in *all_selfies* (matching on either "id" or "uuid", since
    ImageEditRequest items only carry a "uuid") for a matching
    selfieRequestId or ImageEditRequest to try the type-specific URL.

    Returns ``(filename, was_freshly_downloaded)`` — filename is None on
    failure.
    """
    filename = f"{safe_name}_profile_{picture_image_id[:16]}.webp"
    dest     = selfies_dir / filename
    if dest.exists():
        return filename, False

    headers = {
        "Cookie":  f"__Secure-next-auth.session-token={token}",
        "Referer": "https://beta.nomi.ai/",
        "Accept":  "image/webp,image/*",
    }
    urls = [f"https://beta.nomi.ai/api/nomis/{nomi_id}/images/{picture_image_id}.webp"]
    if image_edit_uuid:
        urls.append(
            f"https://beta.nomi.ai/api/image-edit-requests/{image_edit_uuid}"
            f"/edited-image.webp"
        )
    match = next((s for s in (all_selfies or [])
                  if str(s.get("id")) == picture_image_id
                  or str(s.get("uuid")) == picture_image_id), None)
    if match and match.get("selfieRequestId"):
        urls.append(
            f"https://beta.nomi.ai/api/selfie-requests/{match['selfieRequestId']}"
            f"/images/{picture_image_id}.webp"
        )
    if match and match.get("mediaType") == "ImageEditRequest" and match.get("uuid"):
        urls.append(
            f"https://beta.nomi.ai/api/image-edit-requests/{match['uuid']}"
            f"/edited-image.webp"
        )

    last_exc: Exception | None = None
    for url in urls:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            return filename, True
        except Exception as exc:
            last_exc = exc
            continue

    print(f"    ⚠  Could not download profile picture: {last_exc}")
    return None, False


def download_selfie_image(selfie: dict, selfies_dir: Path, token: str,
                          safe_name: str) -> str | None:
    """Download a single selfie .webp to *selfies_dir* using a descriptive filename.

    Returns the filename string on success (new download or already present),
    or None if the download failed or the download URL could not be determined.
    """
    img_id   = str(selfie.get("id", ""))
    req_id   = selfie.get("selfieRequestId")
    if not req_id:
        mt = selfie.get("mediaType", "unknown")
        print(f"    ⚠  Skipping {mt} (id={img_id[:8] if img_id else '?'}) "
              f"— no selfieRequestId; download URL unknown for this media type.")
        return None
    filename = _selfie_filename(selfie, safe_name)
    dest     = selfies_dir / filename
    if dest.exists():
        return filename
    url = (
        f"https://beta.nomi.ai/api/selfie-requests/{req_id}"
        f"/images/{img_id}.webp"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Cookie":  f"__Secure-next-auth.session-token={token}",
            "Referer": "https://beta.nomi.ai/",
            "Accept":  "image/webp,image/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return filename
    except Exception as exc:
        print(f"    ⚠  Could not download selfie {img_id[:8]}…: {exc}")
        return None


def _image_edit_filename(item: dict, safe_name: str) -> str:
    """Return a descriptive filename for an ImageEditRequest: <NomiName>_edit_<date>_<uuid8>.webp"""
    completed = item.get("completed", "")
    try:
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        ts = dt.strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        ts = "unknown"
    uid = str(item.get("uuid") or item.get("id") or "unknown")
    return f"{safe_name}_edit_{ts}_{uid[:8]}.webp"


def download_image_edit(item: dict, selfies_dir: Path, token: str,
                        safe_name: str) -> str | None:
    """Download an ImageEditRequest .webp to *selfies_dir*.

    The result image is always served at:
      image-edit-requests/{uuid}/edited-image.webp
    Returns the filename on success or None on failure.
    """
    req_uuid = item.get("uuid")
    if not req_uuid:
        print(f"    ⚠  Skipping ImageEditRequest — missing uuid")
        return None

    filename = _image_edit_filename(item, safe_name)
    dest     = selfies_dir / filename
    if dest.exists():
        return filename

    url = f"https://beta.nomi.ai/api/image-edit-requests/{req_uuid}/edited-image.webp"
    req = urllib.request.Request(
        url,
        headers={
            "Cookie":  f"__Secure-next-auth.session-token={token}",
            "Referer": "https://beta.nomi.ai/",
            "Accept":  "image/webp,image/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return filename
    except Exception as exc:
        print(f"    ⚠  Could not download image edit {req_uuid[:8]}: {exc}")
        return None


_MIME_TO_EXT: dict = {
    "image/jpeg":  ".jpg",
    "image/jpg":   ".jpg",
    "image/png":   ".png",
    "image/webp":  ".webp",
    "image/gif":   ".gif",
    "image/heic":  ".heic",
    "image/heif":  ".heif",
}


def _upload_filename(msg: dict, safe_name: str, suffix: str = "") -> str:
    """Return a local filename for a user-uploaded attachment.

    For images the original filename (and thus its extension) is preserved.
    For video thumbnails pass suffix='.thumb.webp'; the stem of the original
    filename is used with that suffix instead.
    """
    att      = msg.get("attachment") or {}
    original = Path(att.get("fileName") or "upload").name
    sent     = msg.get("sent", "")
    try:
        dt = datetime.fromisoformat(sent.replace("Z", "+00:00"))
        ts = dt.strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        ts = "unknown"
    uid = str(msg.get("uuid", ""))[:8]
    if suffix:
        stem = Path(original).stem
        return f"{safe_name}_upload_{ts}_{uid}_{stem}{suffix}"
    return f"{safe_name}_upload_{ts}_{uid}_{original}"


def download_user_upload(msg: dict, media_dir: Path, token: str,
                         safe_name: str, nomi_id=None) -> str | None:
    """Download a user-uploaded image or video thumbnail from a chat message.

    URL pattern (discovered via DevTools):
      images : .../nomis/{id}/message-attachments/{sha256}.{ext}
      videos : .../nomis/{id}/message-attachments/{sha256}.preview.webp
               (the original video file is not retained by Nomi.ai)

    Returns the local filename on success, or None on failure / reaped file.
    """
    att = msg.get("attachment") or {}
    if att.get("reaped"):
        return None

    if not nomi_id:
        print(f"    ⚠  Cannot download upload — nomi_id unknown (re-run with --nomi-id)")
        return None

    sha  = att.get("sha256HashBase64", "")
    mime = att.get("mimeType", "")
    base = f"https://beta.nomi.ai/api/nomis/{nomi_id}/message-attachments/{sha}"

    if mime.startswith("video/"):
        url      = f"{base}.preview.webp"
        filename = _upload_filename(msg, safe_name, suffix=".thumb.webp")
    else:
        ext      = _MIME_TO_EXT.get(mime) or Path(att.get("fileName", "")).suffix or ".jpg"
        url      = f"{base}{ext}"
        filename = _upload_filename(msg, safe_name)

    dest = media_dir / filename
    if dest.exists():
        return filename

    headers = {
        "Cookie":  f"__Secure-next-auth.session-token={token}",
        "Referer": "https://beta.nomi.ai/",
        "Accept":  "*/*",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return filename
    except urllib.error.HTTPError as exc:
        print(f"    ⚠  Could not download upload {str(msg.get('uuid',''))[:8]}: HTTP {exc.code} ({url})")
        return None
    except Exception as exc:
        print(f"    ⚠  Could not download upload {str(msg.get('uuid',''))[:8]}: {exc}")
        return None


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _msg_timestamp(msg: dict) -> str:
    for key in ("sent", "sentAt", "timestamp", "createdAt", "created"):
        if msg.get(key):
            return msg[key]
    return ""


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _ensure_utc(raw: str) -> str:
    """Guarantee an ISO timestamp string carries an explicit UTC/offset marker.

    Some API endpoints (e.g. mind map term details) return timestamps without
    a trailing "Z" or offset. Without one, JavaScript's Date() constructor
    parses the string as local time instead of UTC, throwing off every
    downstream "convert to viewer's local time" display by the browser's UTC
    offset. All Nomi.ai timestamps are UTC, so default to that when missing.
    """
    if not raw or _TZ_SUFFIX_RE.search(raw):
        return raw
    return raw + "Z"


def _format_ts(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(_ensure_utc(raw).replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return raw


def _ts_html(raw: str, cls: str = "ts") -> str:
    """Return a <time> element whose text the browser will localise at page load.

    The inline JS snippet in each HTML template reads the data-utc attribute
    and replaces the element's text content with the viewer's local time via
    Date.toLocaleString().  The Python-formatted UTC string is kept as fallback
    content for environments where JS is unavailable.
    """
    if not raw:
        return ""
    fallback = _format_ts(raw)
    return f'<time class="{cls}" data-utc="{_ensure_utc(raw)}">{fallback}</time>'


def _nav_bar(current: str,
             chat_link: str | None,
             mind_map_link: str | None,
             selfies_link: str | None) -> str:
    """Render the shared 3-button navigation bar used in every page header.

    *current* is ``'chat'``, ``'mindmap'``, or ``'selfies'``.
    The active page is a non-linked ``<span>``; missing pages are omitted.
    """
    def btn(label: str, href: str | None, name: str) -> str:
        if name == current:
            return f'<span class="nav-btn active">{label}</span>'
        if not href:
            return ""
        return f'<a href="{href}" class="nav-btn">{label}</a>'

    parts = [
        f'<a href="../index.html" class="nav-btn">&#8962; Home</a>',
        btn("Chat",     chat_link,     "chat"),
        btn("Mind Map", mind_map_link, "mindmap"),
        btn("Media",    selfies_link,  "media"),
    ]
    parts = [p for p in parts if p]
    return '<nav class="top-nav">' + "".join(parts) + "</nav>" if parts else ""


# Shared nav CSS snippet embedded in every HTML template
_NAV_CSS = """\
    .top-nav { display: flex; gap: 8px; flex-shrink: 0; flex-wrap: wrap; }
    .nav-btn {
      font-size: 0.82rem; text-decoration: none; border-radius: 6px;
      padding: 6px 12px; white-space: nowrap; border: 1px solid #1e3a5f;
      color: #4a9eff; transition: background 0.15s;
    }
    .nav-btn:hover { background: #1a2d3d; }
    .nav-btn.active {
      background: #1a2d3d; color: #e0e0e0; border-color: #4a9eff; cursor: default;
    }
    @media (max-width: 640px) {
      header { flex-wrap: wrap; padding: 10px 14px; gap: 8px; }
      .header-info { width: 100%; }
      header h1 { font-size: 1.1rem; }
      header p  { font-size: 0.72rem; }
      .top-nav { gap: 5px; }
      .nav-btn { font-size: 0.72rem; padding: 5px 9px; }
    }"""


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
    )


def _md_to_html(text: str) -> str:
    """Convert markdown to HTML, passing through any embedded HTML tags unchanged.

    Uses the ``markdown`` library when available (pip install markdown).
    The ``extra`` extension enables tables, fenced code blocks, and other
    common markdown features.  ``safe_mode`` is intentionally *not* used so
    that any HTML tags the Nomi.ai dossier already contains are kept as-is.

    Falls back to a plain ``<pre>`` block (with HTML-escaped text) when the
    library is not installed, so the archive still works without extra deps.
    """
    if _HAS_MARKDOWN:
        return _md_lib.markdown(
            text,
            extensions=["extra"],   # tables, fenced-code, etc.
        )
    # Graceful fallback: preserve whitespace and special chars, but no markup
    return "<div style='white-space:pre-wrap'>" + _html_escape(text) + "</div>"


def render_mind_map_html(nomi: dict, mind_map: dict[str, list],
                         chat_link: str | None = None,
                         selfies_link: str | None = None) -> str:
    """Render the full mind map as a standalone, self-contained HTML file."""
    nomi_name  = nomi["name"]
    export_date = datetime.now().strftime("%b %d, %Y %I:%M %p")

    sections_html: list[str] = []
    total_terms = 0

    for category, label in MIND_MAP_CATEGORIES.items():
        terms = mind_map.get(category, [])
        if not terms:
            continue
        total_terms += len(terms)

        cards: list[str] = []
        for term in sorted(terms, key=lambda t: t.get("title", "").lower()):
            title       = _html_escape(term.get("title", "(untitled)"))
            priority    = term.get("priority", "Standard")
            mem_count   = term.get("memoryCount", 0)
            ai_edited   = _ts_html(term.get("aiEdited", ""), "mm-ts")
            dossier     = term.get("dossier") or ""
            state       = term.get("state", "Default")
            candidate   = term.get("candidate", False)

            badges: list[str] = []
            if priority == "High":
                badges.append('<span class="mm-badge mm-high">High Priority</span>')
            if state != "Default":
                badges.append(f'<span class="mm-badge mm-state">{_html_escape(state)}</span>')
            if candidate:
                badges.append('<span class="mm-badge mm-cand">Candidate</span>')
            badge_html = "".join(badges)

            dossier_block = (
                f'<div class="mm-dossier">{_md_to_html(dossier)}</div>'
                if dossier else
                '<p class="mm-empty">No details yet.</p>'
            )

            cards.append(
                f'<div class="mm-card">'
                f'  <div class="mm-card-head">'
                f'    <span class="mm-title">{title}</span>'
                f'    {badge_html}'
                f'  </div>'
                f'  <div class="mm-meta">{mem_count} memories'
                f'{(" &middot; Updated " + ai_edited) if ai_edited else ""}'
                f'  </div>'
                f'  {dossier_block}'
                f'</div>'
            )

        sections_html.append(
            f'<details class="mm-section">'
            f'<summary class="mm-section-title">'
            f'<span class="mm-section-label">{label}</span>'
            f'<span class="mm-count">{len(terms)}</span>'
            f'<span class="mm-chevron">&#9654;</span>'
            f'</summary>'
            f'<div class="mm-cards">{"".join(cards)}</div>'
            f'</details>'
        )

    body = "\n".join(sections_html) if sections_html else "<p>No mind map data found.</p>"
    nav = _nav_bar("mindmap", chat_link, None, selfies_link)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="../favicon.png">
  <link rel="apple-touch-icon" href="../favicon.png">
  <title>{nomi_name} &mdash; Mind Map</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e; color: #e0e0e0; min-height: 100vh;
    }}
    header {{
      background: #16213e; border-bottom: 1px solid #0f3460;
      padding: 18px 24px; position: sticky; top: 0; z-index: 10;
      display: flex; align-items: center; gap: 16px;
    }}
    .header-info {{ flex: 1; min-width: 0; }}
    header h1 {{ font-size: 1.3rem; color: #e94560; font-weight: 600; }}
    header p  {{ font-size: 0.8rem; color: #888; margin-top: 3px; }}
{_NAV_CSS}
    main {{ max-width: 900px; margin: 0 auto; padding: 28px 16px 80px; }}
    /* ---------- accordion sections ---------- */
    .mm-section {{ margin-bottom: 8px; }}
    .mm-section[open] {{ margin-bottom: 32px; }}
    .mm-section-title {{
      font-size: 1.05rem; font-weight: 700; color: #4a9eff;
      border: 1px solid #0f3460; border-radius: 8px; background: #12192e;
      padding: 10px 14px; margin-bottom: 0;
      display: flex; align-items: center; gap: 10px;
      cursor: pointer; list-style: none; user-select: none;
    }}
    .mm-section-title::-webkit-details-marker {{ display: none; }}
    .mm-section[open] > summary.mm-section-title {{
      border-bottom-left-radius: 0; border-bottom-right-radius: 0;
      border-bottom-color: transparent;
    }}
    .mm-section-label {{ flex: 1; }}
    .mm-chevron {{
      font-size: 0.58rem; color: #4a9eff; flex-shrink: 0;
      display: inline-block; transition: transform 0.18s ease;
    }}
    .mm-section[open] > summary.mm-section-title .mm-chevron {{ transform: rotate(90deg); }}
    .mm-count {{
      font-size: 0.72rem; font-weight: 500; color: #555;
      background: #16213e; border: 1px solid #0f3460;
      border-radius: 10px; padding: 2px 8px;
    }}
    .mm-cards {{ display: flex; flex-direction: column; gap: 12px; padding-top: 4px; }}
    .mm-card {{
      background: #16213e; border: 1px solid #0f3460;
      border-left: 3px solid #4a9eff; border-radius: 10px;
      padding: 14px 16px; overflow: hidden;
    }}
    .mm-card-head {{
      display: flex; align-items: flex-start;
      gap: 8px; flex-wrap: wrap; margin-bottom: 6px;
    }}
    .mm-title {{ font-weight: 600; font-size: 0.97rem; flex: 1; color: #ddd; }}
    .mm-badge {{
      font-size: 0.65rem; font-weight: 600; padding: 2px 7px;
      border-radius: 8px; white-space: nowrap; flex-shrink: 0;
    }}
    .mm-high  {{ background: #3d1a1a; color: #e94560; border: 1px solid #6b2020; }}
    .mm-state {{ background: #1a2d3d; color: #4a9eff; border: 1px solid #1e3a5f; }}
    .mm-cand  {{ background: #1a3020; color: #4aaa70; border: 1px solid #1e5030; }}
    .mm-meta  {{ font-size: 0.72rem; color: #556; margin-bottom: 10px; }}
    .mm-dossier {{
      font-size: 0.88rem; line-height: 1.6; color: #ccc;
      border-top: 1px solid #0f3460; padding-top: 10px; margin-top: 4px;
    }}
    /* Style whatever HTML tags the dossier contains */
    .mm-dossier p  {{ margin-bottom: 0.6em; }}
    .mm-dossier ul, .mm-dossier ol {{ padding-left: 1.4em; margin-bottom: 0.6em; }}
    .mm-dossier li {{ margin-bottom: 0.25em; }}
    .mm-dossier strong, .mm-dossier b {{ color: #e0e0e0; }}
    .mm-dossier h1,.mm-dossier h2,.mm-dossier h3 {{
      color: #aaa; font-size: 0.9rem; margin: 0.6em 0 0.3em;
    }}
    /* Keep any markdown-generated code blocks sans-serif to match the page */
    .mm-dossier pre, .mm-dossier code {{
      font-family: inherit; background: #0d1a2e;
      border-radius: 4px; font-size: 0.85em;
    }}
    .mm-dossier code {{ padding: 0.1em 0.35em; }}
    .mm-dossier pre  {{ padding: 8px 12px; display: block; overflow-x: auto; white-space: pre-wrap; }}
    .mm-empty {{ font-size: 0.82rem; color: #445; font-style: italic; }}
    footer {{ text-align: center; padding: 32px 16px; color: #555; font-size: 0.78rem; }}
    @media (max-width: 640px) {{
      main {{ padding: 16px 10px 60px; }}
      .mm-card {{ padding: 12px 12px; }}
      .mm-dossier {{ font-size: 0.82rem; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-info">
      <h1>{nomi_name} &mdash; Mind Map</h1>
      <p>{total_terms} entries &middot; Exported {export_date}</p>
    </div>
    {nav}
  </header>
  <main>
{body}
  </main>
  <footer>NomiVault &middot; Nomi.ai &middot; {export_date}</footer>
  <script>
    document.querySelectorAll('[data-utc]').forEach(function(el) {{
      var d = new Date(el.getAttribute('data-utc'));
      if (!isNaN(d.getTime())) {{
        el.textContent = d.toLocaleString(undefined, {{
          month: 'short', day: 'numeric', year: 'numeric',
          hour: 'numeric', minute: '2-digit'
        }});
      }}
    }});
  </script>
</body>
</html>
"""


def render_gallery_html(nomi: dict, selfies: list, safe_name: str,
                        chat_link: str | None = None,
                        mind_map_link: str | None = None,
                        user_uploads: list | None = None) -> str:
    """Render a self-contained photo gallery page for all downloaded selfies."""
    nomi_name    = nomi["name"]
    export_date  = datetime.now().strftime("%b %d, %Y %I:%M %p")
    _uploads     = [u for u in (user_uploads or []) if u.get("local_filename")]

    # Oldest first
    sorted_selfies = sorted(
        selfies, key=lambda s: s.get("completed", "")
    )

    _GALLERY_TYPES = {"Selfie", "VideoRequest", "CharacterImage", "ImageEditRequest"}

    cards: list[str] = []
    for item in sorted_selfies:
        media_type = item.get("mediaType") or "Selfie"
        if media_type not in _GALLERY_TYPES:
            continue
        ts_elem    = _ts_html(item.get("completed", ""), "gal-ts")

        if media_type == "VideoRequest":
            preview_fn = item.get("preview_filename") or _video_filename(item, safe_name)[0]
            video_fn   = item.get("video_filename")   or _video_filename(item, safe_name)[1]
            img_src    = f"media/{preview_fn}"
            video_src  = f"media/{video_fn}"
            cards.append(
                f'<div class="gal-card" data-type="video" data-src="{img_src}"'
                f' data-video-src="{video_src}" onclick="openLb(this)">'
                f'  <div class="gal-thumb">'
                f'    <img src="{img_src}" alt="Video thumbnail" loading="lazy">'
                f'    <div class="play-overlay"><div class="play-btn"></div></div>'
                f'  </div>'
                f'  <div class="gal-info">'
                f'    <span class="gal-type gal-type-video">Video</span>{ts_elem}'
                f'  </div>'
                f'</div>'
            )
        elif media_type == "CharacterImage":
            filename  = item.get("local_filename") or _character_image_filename(item, safe_name)
            img_src   = f"media/{filename}"
            type_tag  = '<span class="gal-type gal-type-char">Character Image</span>'
            cards.append(
                f'<div class="gal-card" data-src="{img_src}" onclick="openLb(this)">'
                f'  <img src="{img_src}" alt="Character image" loading="lazy">'
                f'  <div class="gal-info">{type_tag}</div>'
                f'</div>'
            )
        elif media_type == "ImageEditRequest":
            filename  = item.get("local_filename") or _image_edit_filename(item, safe_name)
            img_src   = f"media/{filename}"
            type_tag  = '<span class="gal-type gal-type-edit">Edited</span>'
            cards.append(
                f'<div class="gal-card" data-src="{img_src}" onclick="openLb(this)">'
                f'  <img src="{img_src}" alt="Edited image" loading="lazy">'
                f'  <div class="gal-info">{type_tag}{ts_elem}</div>'
                f'</div>'
            )
        else:
            filename = item.get("local_filename") or _selfie_filename(item, safe_name)
            img_src  = f"media/{filename}"
            s_type   = _html_escape(item.get("type") or "")
            type_tag = f'<span class="gal-type">{s_type}</span>' if s_type else ""
            cards.append(
                f'<div class="gal-card" data-src="{img_src}" onclick="openLb(this)">'
                f'  <img src="{img_src}" alt="Selfie" loading="lazy">'
                f'  <div class="gal-info">{type_tag}{ts_elem}</div>'
                f'</div>'
            )

    nomi_grid = "\n".join(cards) if cards else '<p class="gal-empty">No media downloaded yet.</p>'

    # Build the "You" tab — user-uploaded images and videos
    upload_cards: list[str] = []
    for u in sorted(_uploads, key=lambda x: x.get("sent", "")):
        att_src  = f"media/{u['local_filename']}"
        ts_elem  = _ts_html(u.get("sent", ""), "gal-ts")
        if u.get("is_video"):
            # Only the thumbnail is available; open it in the image lightbox
            upload_cards.append(
                f'<div class="gal-card" data-src="{att_src}" onclick="openLb(this)">'
                f'  <div class="gal-thumb">'
                f'    <img src="{att_src}" alt="Video thumbnail" loading="lazy">'
                f'    <div class="play-overlay"><div class="play-btn"></div></div>'
                f'  </div>'
                f'  <div class="gal-info">'
                f'    <span class="gal-type gal-type-video">Video</span>{ts_elem}'
                f'  </div>'
                f'</div>'
            )
        else:
            upload_cards.append(
                f'<div class="gal-card" data-src="{att_src}" onclick="openLb(this)">'
                f'  <img src="{att_src}" alt="Uploaded image" loading="lazy">'
                f'  <div class="gal-info">'
                f'    <span class="gal-type gal-type-upload">Photo</span>{ts_elem}'
                f'  </div>'
                f'</div>'
            )
    upload_grid = "\n".join(upload_cards) if upload_cards else '<p class="gal-empty">No uploaded media found.</p>'

    photo_count  = sum(1 for s in selfies if s.get("mediaType") != "VideoRequest")
    video_count  = sum(1 for s in selfies if s.get("mediaType") == "VideoRequest")
    upload_count = len(_uploads)
    count_parts: list[str] = []
    if photo_count:
        count_parts.append(f'{photo_count} photo{"s" if photo_count != 1 else ""}')
    if video_count:
        count_parts.append(f'{video_count} video{"s" if video_count != 1 else ""}')
    count_str = " &middot; ".join(count_parts) if count_parts else "0 items"
    if upload_count:
        count_str += f' &middot; {upload_count} upload{"s" if upload_count != 1 else ""}'

    show_tabs = bool(_uploads)   # only render tab bar when there are user uploads
    tab_bar_html = (
        '<div class="tab-bar">'
        f'<button class="tab-btn active" data-tab="nomi" onclick="switchTab(this,\'nomi\')">{nomi_name}</button>'
        '<button class="tab-btn" data-tab="yours" onclick="switchTab(this,\'yours\')">You</button>'
        '</div>'
    ) if show_tabs else ""

    nav = _nav_bar("media", chat_link, mind_map_link, None)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="../favicon.png">
  <link rel="apple-touch-icon" href="../favicon.png">
  <title>{nomi_name} &mdash; Media</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e; color: #e0e0e0; min-height: 100vh;
    }}
    header {{
      background: #16213e; border-bottom: 1px solid #0f3460;
      padding: 18px 24px; position: sticky; top: 0; z-index: 10;
      display: flex; align-items: center; gap: 16px;
    }}
    .header-info {{ flex: 1; min-width: 0; }}
    header h1 {{ font-size: 1.3rem; color: #e94560; font-weight: 600; }}
    header p  {{ font-size: 0.8rem; color: #888; margin-top: 3px; }}
{_NAV_CSS}
    main {{
      max-width: 1100px; margin: 0 auto; padding: 28px 16px 80px;
    }}
    .gal-grid {{
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 14px;
    }}
    @media (max-width: 1000px) {{
      .gal-grid {{ grid-template-columns: repeat(3, 1fr); }}
    }}
    @media (max-width: 600px) {{
      .gal-grid {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
      main {{ padding: 14px 10px 60px; }}
    }}
    .gal-card {{
      background: #16213e; border: 1px solid #0f3460; border-radius: 10px;
      overflow: hidden; cursor: zoom-in; transition: transform 0.15s, border-color 0.15s;
    }}
    .gal-card:hover {{ transform: translateY(-3px); border-color: #4a9eff; }}
    .gal-card > img, .gal-thumb img {{
      width: 100%; aspect-ratio: 1 / 1; object-fit: cover; display: block;
    }}
    .gal-thumb {{ position: relative; }}
    .play-overlay {{
      position: absolute; inset: 0;
      display: flex; align-items: center; justify-content: center;
      pointer-events: none;
    }}
    .play-btn {{
      width: 52px; height: 52px; border-radius: 50%;
      background: rgba(0,0,0,0.62); border: 2px solid rgba(255,255,255,0.85);
      display: flex; align-items: center; justify-content: center;
    }}
    .play-btn::after {{
      content: ''; border-style: solid;
      border-width: 10px 0 10px 18px;
      border-color: transparent transparent transparent #fff;
      margin-left: 4px;
    }}
    .gal-info {{
      padding: 8px 10px; display: flex; flex-direction: column;
      align-items: flex-start; gap: 2px;
    }}
    .gal-type {{ font-size: 0.7rem; color: #4a9eff; font-weight: 600; }}
    .gal-type-video  {{ color: #e9a020; }}
    .gal-type-char   {{ color: #a060e0; }}
    .gal-type-edit   {{ color: #40c080; }}
    .gal-type-upload {{ color: #4a9eff; }}
    .gal-ts  {{ font-size: 0.68rem; color: #556; }}
    .gal-empty {{ color: #445; font-style: italic; padding: 40px 0; text-align: center; }}
    /* Tab bar */
    .tab-bar {{ display: flex; gap: 4px; padding: 16px 24px 0; }}
    .tab-btn {{
      padding: 8px 20px; border-radius: 8px 8px 0 0; border: none; cursor: pointer;
      font-size: 0.9rem; font-weight: 600; background: #0f3460; color: #8899bb;
      transition: background 0.15s, color 0.15s;
    }}
    .tab-btn.active {{ background: #1a2a5e; color: #e0e8ff; }}
    .tab-btn:hover {{ background: #1a2a5e; color: #c0ccee; }}
    footer {{ text-align: center; padding: 32px 16px; color: #555; font-size: 0.78rem; }}
    /* Lightbox */
    #lightbox {{
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.92);
      z-index: 1000; align-items: center; justify-content: center; cursor: zoom-out;
    }}
    #lightbox.open {{ display: flex; }}
    #lb-img {{
      max-width: 92vw; max-height: 92vh;
      object-fit: contain; border-radius: 6px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.6);
    }}
    #lb-video {{
      max-width: 92vw; max-height: 92vh;
      border-radius: 6px; box-shadow: 0 8px 40px rgba(0,0,0,0.6);
      display: none;
    }}
    #lb-close {{
      position: fixed; top: 16px; right: 20px;
      color: #aaa; font-size: 1.8rem; cursor: pointer; line-height: 1;
      background: none; border: none;
    }}
    #lb-close:hover {{ color: #fff; }}
    /* ---- scroll buttons ---- */
    .scroll-btn {{
      position: fixed; right: 20px;
      width: 44px; height: 44px; border-radius: 50%;
      background: #16213e; border: 1px solid #1e3a5f;
      color: #4a9eff; font-size: 1rem; line-height: 1;
      cursor: pointer; z-index: 100;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 2px 10px rgba(0,0,0,0.5);
      transition: background 0.15s, border-color 0.15s;
    }}
    .scroll-btn:hover {{ background: #1a2d3d; border-color: #4a9eff; }}
    #btn-top {{ top: 72px; }}
    #btn-bot {{ bottom: 24px; }}
    @media (max-width: 640px) {{
      .scroll-btn {{ width: 38px; height: 38px; right: 10px; }}
      #btn-top {{ top: 100px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-info">
      <h1>{nomi_name} &mdash; Media</h1>
      <p>{count_str} &middot; Exported {export_date}</p>
    </div>
    {nav}
  </header>
  <main>
    {tab_bar_html}
    <div id="tab-nomi" class="tab-pane">
      <div class="gal-grid">
{nomi_grid}
      </div>
    </div>
    <div id="tab-yours" class="tab-pane" style="display:none">
      <div class="gal-grid">
{upload_grid}
      </div>
    </div>
  </main>
  <button id="btn-top" class="scroll-btn"
          onclick="document.documentElement.scrollTop=0"
          title="Scroll to top">&#9650;</button>
  <button id="btn-bot" class="scroll-btn"
          onclick="scrollToBottom()"
          title="Scroll to bottom">&#9660;</button>
  <div id="lightbox" onclick="closeLb(event)">
    <button id="lb-close" onclick="closeLb()">&times;</button>
    <img id="lb-img" src="" alt="">
    <video id="lb-video" controls></video>
  </div>
  <footer>NomiVault &middot; Nomi.ai &middot; {export_date}</footer>
  <script>
    function scrollToBottom() {{
      document.documentElement.scrollTop = document.documentElement.scrollHeight;
    }}
    function openLb(card) {{
      var img  = document.getElementById('lb-img');
      var vid  = document.getElementById('lb-video');
      var isVid = card.getAttribute('data-type') === 'video';
      if (isVid) {{
        vid.src = card.getAttribute('data-video-src');
        vid.style.display = 'block';
        img.style.display = 'none';
        img.src = '';
        vid.play();
      }} else {{
        img.src = card.getAttribute('data-src');
        img.style.display = 'block';
        vid.style.display = 'none';
        vid.pause(); vid.src = '';
      }}
      document.getElementById('lightbox').classList.add('open');
    }}
    function closeLb(e) {{
      var img = document.getElementById('lb-img');
      var vid = document.getElementById('lb-video');
      if (!e || (e.target !== img && e.target !== vid)) {{
        document.getElementById('lightbox').classList.remove('open');
        img.src = '';
        vid.pause(); vid.src = ''; vid.style.display = 'none';
        img.style.display = 'block';
      }}
    }}
    function switchTab(btn, tabName) {{
      document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      document.querySelectorAll('.tab-pane').forEach(function(p) {{ p.style.display = 'none'; }});
      btn.classList.add('active');
      document.getElementById('tab-' + tabName).style.display = 'block';
    }}
    document.addEventListener('keydown', function(e) {{
      if (e.key === 'Escape') closeLb();
    }});
    document.querySelectorAll('[data-utc]').forEach(function(el) {{
      var d = new Date(el.getAttribute('data-utc'));
      if (!isNaN(d.getTime())) {{
        el.textContent = d.toLocaleString(undefined, {{
          month: 'short', day: 'numeric', year: 'numeric',
          hour: 'numeric', minute: '2-digit'
        }});
      }}
    }});
  </script>
</body>
</html>
"""


def render_landing_html(entries: list[dict]) -> str:
    """Render index.html — a card grid linking to each archived Nomi's chat log.

    Each entry dict must contain:
      name, safe_name, folder_name, first_msg_ts (ISO-8601 str),
      char_image_src (path or None)
    An entry may also set ``deleted: True`` for a Nomi that no longer exists
    on the Nomi.ai account but still has local archive files — its card is
    shown desaturated with a "Deleted on Nomi.ai" badge, still linking to
    its existing HTML.
    A group-chat entry sets ``is_group: True`` and ``participants: [{name,
    img_src}, ...]`` instead of a single ``char_image_src`` — its card
    background is a side-by-side split of each participant's own profile
    picture (each anchored top-center, like the individual cards) rather
    than one image, and it gets a "Group Chat" badge.
    Entries must already be sorted before calling this function.
    """
    export_date = datetime.now().strftime("%b %d, %Y %I:%M %p")
    count = len(entries)

    cards: list[str] = []
    for e in entries:
        name     = _html_escape(e["name"])
        href     = f"{e.get('folder_name', e['safe_name'])}/{e['safe_name']}-chat.html"
        img_src  = e.get("char_image_src") or ""
        first_ts = e.get("first_msg_ts", "")
        deleted  = e.get("deleted", False)
        is_group = e.get("is_group", False)
        participants = e.get("participants") or []

        if is_group and participants:
            panel_divs: list[str] = []
            for p in participants:
                if p.get("img_src"):
                    panel_style = (f"background-image:url('{p['img_src']}');"
                                   f"background-size:cover;background-position:center top;")
                else:
                    panel_style = "background:linear-gradient(135deg,#16213e 0%,#0f3460 100%);"
                panel_divs.append(f'<div class="card-bg-panel" style="{panel_style}"></div>')
            bg_html = f'<div class="card-bg card-bg-split">{"".join(panel_divs)}</div>'
        elif img_src:
            bg_html = (f'<div class="card-bg" style="background-image:url(\'{img_src}\');'
                       f'background-size:cover;background-position:center top;"></div>')
        else:
            bg_html = '<div class="card-bg" style="background:linear-gradient(135deg,#16213e 0%,#0f3460 100%);"></div>'

        # "Since …" date label — JS will localise; Python fallback is date-only UTC
        if first_ts:
            try:
                dt = datetime.fromisoformat(_ensure_utc(first_ts).replace("Z", "+00:00"))
                fallback_date = dt.strftime("%b %d, %Y")
            except Exception:
                fallback_date = first_ts[:10]
            since = (f'<div class="card-date">'
                     f'<time data-utc="{_ensure_utc(first_ts)}">Since {fallback_date}</time>'
                     f'</div>')
        else:
            since = ""

        card_class = "nomi-card is-deleted" if deleted else "nomi-card"
        if deleted:
            badge = '<div class="card-badge">Deleted on Nomi.ai</div>'
        elif is_group:
            badge = '<div class="card-badge card-badge-group">Group Chat</div>'
        else:
            badge = ""

        cards.append(
            f'<a href="{href}" class="{card_class}">'
            f'  {bg_html}'
            f'  <div class="card-overlay"></div>'
            f'  {badge}'
            f'  <div class="card-info">'
            f'    <div class="card-name">{name}</div>'
            f'    {since}'
            f'  </div>'
            f'</a>'
        )

    grid = "\n".join(cards) if cards else '<p class="no-nomis">No archived Nomis found.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="apple-touch-icon" href="favicon.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="NomiVault">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#16213e">
  <link rel="manifest" href="manifest.json">
  <title>NomiVault</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e; color: #e0e0e0; min-height: 100vh;
    }}
    header {{
      background: #16213e; border-bottom: 1px solid #0f3460;
      padding: 24px 32px;
    }}
    header h1 {{ font-size: 1.6rem; color: #e94560; font-weight: 700; }}
    header p  {{ font-size: 0.82rem; color: #888; margin-top: 4px; }}
    main {{
      max-width: 1200px; margin: 0 auto; padding: 32px 20px 80px;
    }}
    .nomi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 20px;
    }}
    .nomi-card {{
      position: relative; height: 480px;
      border-radius: 14px; overflow: hidden;
      display: block; text-decoration: none;
      border: 1px solid #0f3460;
      transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
    }}
    .nomi-card:hover {{
      transform: translateY(-5px);
      box-shadow: 0 16px 48px rgba(0,0,0,0.6);
      border-color: #4a9eff;
    }}
    .card-bg {{
      position: absolute; inset: 0;
      transition: transform 0.35s ease;
    }}
    .nomi-card:hover .card-bg {{ transform: scale(1.06); }}
    .card-bg-split {{ display: flex; }}
    .card-bg-panel {{ flex: 1 1 0; height: 100%; }}
    .card-overlay {{
      position: absolute; inset: 0;
      background: linear-gradient(to bottom,
        rgba(0,0,0,0.05) 0%,
        rgba(0,0,0,0.25) 50%,
        rgba(0,0,0,0.82) 100%);
    }}
    .card-info {{
      position: absolute; bottom: 0; left: 0; right: 0;
      padding: 18px 20px;
    }}
    .card-name {{
      font-size: 1.45rem; font-weight: 700; color: #fff;
      text-shadow: 0 2px 10px rgba(0,0,0,0.9);
    }}
    .card-date {{
      font-size: 0.76rem; color: rgba(255,255,255,0.72);
      margin-top: 5px; text-shadow: 0 1px 4px rgba(0,0,0,0.8);
    }}
    .nomi-card.is-deleted .card-bg {{ filter: grayscale(1) brightness(0.55); }}
    .card-badge {{
      position: absolute; top: 12px; right: 12px;
      background: rgba(233,69,96,0.9); color: #fff;
      font-size: 0.68rem; font-weight: 700; letter-spacing: 0.02em;
      padding: 4px 10px; border-radius: 20px;
      text-shadow: none;
    }}
    .card-badge-group {{ background: rgba(74,158,255,0.9); }}
    .no-nomis {{ color: #445; font-style: italic; text-align: center; padding: 60px 0; }}
    footer {{ text-align: center; padding: 32px 16px; color: #555; font-size: 0.78rem; }}
    @media (max-width: 640px) {{
      header {{ padding: 16px 18px; }}
      header h1 {{ font-size: 1.3rem; }}
      main {{ padding: 18px 12px 60px; }}
      .nomi-grid {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
      .nomi-card {{ height: auto; aspect-ratio: 1 / 1; }}
      .card-info {{ padding: 10px 12px; }}
      .card-name {{ font-size: 1.05rem; }}
      .card-date {{ font-size: 0.68rem; }}
      .card-badge {{ font-size: 0.6rem; padding: 3px 8px; top: 8px; right: 8px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>NomiVault</h1>
    <p>{count} Nomi{"s" if count != 1 else ""} archived &middot; Updated {export_date}</p>
  </header>
  <main>
    <div class="nomi-grid">
{grid}
    </div>
  </main>
  <footer>NomiVault &middot; Nomi.ai &middot; {export_date}</footer>
  <script>
    if ('serviceWorker' in navigator) {{
      navigator.serviceWorker.register('sw.js');
    }}
    document.querySelectorAll('[data-utc]').forEach(function(el) {{
      var d = new Date(el.getAttribute('data-utc'));
      if (!isNaN(d.getTime())) {{
        el.textContent = 'Since ' + d.toLocaleString(undefined, {{
          month: 'short', day: 'numeric', year: 'numeric'
        }});
      }}
    }});
  </script>
</body>
</html>
"""


def render_voice_call_block(call: dict, transcript: list, nomi_name: str) -> str:
    """Render a single voice call + its transcript as an inline HTML block."""
    started = _ts_html(call.get("started", ""), "vc-time-val")
    ended   = _ts_html(call.get("ended",   ""), "vc-time-val")

    duration_str = ""
    s_dt = _parse_dt(call.get("started", ""))
    e_dt = _parse_dt(call.get("ended",   ""))
    if s_dt and e_dt:
        total = max(int((e_dt - s_dt).total_seconds()), 0)
        mins, secs = divmod(total, 60)
        duration_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    lines_html: list[str] = []
    for msg in transcript:
        is_user    = str(msg.get("type", "")).lower() == "user"
        speaker    = "You" if is_user else nomi_name
        spk_cls    = "vc-user" if is_user else "vc-nomi"
        text       = _html_escape(str(msg.get("text", "")).strip())
        ts_tag     = _ts_html(msg.get("created", ""), "vc-ts")
        lines_html.append(
            f'<div class="vc-line {spk_cls}">'
            f'<span class="vc-speaker">{speaker}</span>'
            f'<span class="vc-text">{text}</span>'
            f'{ts_tag}'
            f'</div>'
        )

    body = "\n".join(lines_html) if lines_html else '<p class="vc-empty">No transcript available</p>'
    dur_tag  = f'<span class="vc-duration">{duration_str}</span>' if duration_str else ""
    time_line = " &rarr; ".join(filter(None, [started, ended]))

    return (
        '\n        <div class="voice-call-block">'
        '\n          <div class="vc-header">'
        '\n            <span class="vc-icon">&#x1F4DE;</span>'
        '\n            <span class="vc-title">Voice&nbsp;Call</span>'
        f'\n            {dur_tag}'
        '\n          </div>'
        f'\n          <div class="vc-time">{time_line}</div>'
        f'\n          <div class="vc-transcript">\n{body}\n          </div>'
        '\n        </div>\n'
    )


def render_html(nomi: dict, messages: list, new_count: int = 0,
                voice_call_map: dict | None = None,
                voice_calls: list | None = None,
                mind_map_link: str | None = None,
                selfies_link: str | None = None,
                selfies: list | None = None,
                safe_name: str = "",
                user_uploads: list | None = None,
                is_group: bool = False,
                speaker_avatars: dict | None = None,
                description: str | None = None,
                own_avatar_src: str | None = None) -> str:
    nomi_name = nomi["name"]
    relationship = nomi.get("relationshipType", "")
    created_raw = (nomi.get("created") or "")
    try:
        created = datetime.fromisoformat(created_raw.replace("Z", "+00:00")).strftime("%b %d, %Y") if created_raw else ""
    except Exception:
        created = created_raw[:10]
    export_date = datetime.now().strftime("%b %d, %Y %I:%M %p")

    _vc_map    = voice_call_map or {}
    _vc_list   = voice_calls    or []
    seen_calls: set = set()

    # Lookup: message UUID → upload record (for inline attachment rendering)
    _upload_map: dict = {
        u["message_uuid"]: u
        for u in (user_uploads or [])
        if u.get("local_filename")
    }

    # Build a merged timeline of messages and selfies, sorted by timestamp
    timeline: list[tuple[str, str, dict]] = []
    for msg in messages:
        timeline.append(("msg", _msg_timestamp(msg), msg))
    for s in (selfies or []):
        # Group-chat-generated selfies stay in this Nomi's media gallery
        # (they legitimately involve this Nomi), but don't belong inline
        # in this 1:1 conversation's timeline — they were generated in a
        # different conversation entirely. That exclusion doesn't apply
        # when rendering the group's own page, though — there, its own
        # selfies belong in its own timeline.
        if (s.get("mediaType") == "Selfie" and s.get("type") != "Art"
                and (is_group or not s.get("groupChatId"))):
            timeline.append(("selfie", s.get("completed", ""), s))
    timeline.sort(key=lambda x: x[1])

    bubbles: list[str] = []
    for kind, _ts, data in timeline:

        if kind == "selfie":
            selfie   = data
            filename = selfie.get("local_filename") or _selfie_filename(selfie, safe_name)
            img_src  = f"media/{filename}"
            ts_elem  = _ts_html(selfie.get("completed", ""), "timestamp")
            bubbles.append(
                f'        <div class="message-row selfie-row">\n'
                f'          <div class="selfie-inline" data-src="{img_src}"'
                f' onclick="openLb(this)">\n'
                f'            <img src="{img_src}" alt="Selfie" loading="lazy">\n'
                f'            <div class="timestamp">{ts_elem}</div>\n'
                f'          </div>\n'
                f'        </div>'
            )
            continue

        msg      = data
        msg_text = (msg.get("text") or "").strip()

        # Hidden system messages: voice-call sentinels
        if msg.get("hidden"):
            if msg_text == "*we start a voice call*" and _vc_list:
                vc = match_voice_call(msg.get("sent", ""), _vc_list)
                if vc and vc["id"] not in seen_calls:
                    seen_calls.add(vc["id"])
                    call_info, transcript = _vc_map.get(vc["id"], (vc, []))
                    bubbles.append(render_voice_call_block(call_info, transcript, nomi_name))
            continue

        # Extract text
        text = ""
        for key in ("text", "message", "content", "body"):
            if msg.get(key):
                text = str(msg[key]).strip()
                break

        # Determine sender
        avatar_src = None
        if is_group:
            speaker_id   = msg.get("nomiId")
            is_user      = not speaker_id
            sender_label = msg.get("nomiName") or nomi_name if speaker_id else "You"
            if speaker_id and speaker_avatars:
                avatar_src = speaker_avatars.get(speaker_id)
        else:
            sender_raw = ""
            for key in ("role", "sender", "from", "type", "senderType"):
                if msg.get(key) is not None:
                    sender_raw = str(msg[key]).lower()
                    break
            is_user = sender_raw in ("user", "human", "me", "0") or sender_raw.startswith("user")
            sender_label = "You" if is_user else nomi_name
            if not is_user:
                avatar_src = own_avatar_src

        bubble_cls = "user-bubble" if is_user else "nomi-bubble"
        side_cls = "user-side" if is_user else "nomi-side"

        ts_raw    = _msg_timestamp(msg)
        safe_text = _html_escape(text)
        ts_html   = f'<div class="timestamp">{_ts_html(ts_raw)}</div>' if ts_raw else ""

        # Inline attachment (user-uploaded image or video thumbnail)
        upload      = _upload_map.get(msg.get("uuid", ""))
        attach_html = ""
        if upload:
            att_src = f"media/{upload['local_filename']}"
            if upload.get("is_video"):
                attach_html = (
                    f'<div class="attach-wrap attach-video-wrap"'
                    f' data-src="{att_src}" onclick="openLb(this)">'
                    f'<img class="attach-img" src="{att_src}" alt="Video" loading="lazy">'
                    f'<div class="play-overlay"><div class="play-btn"></div></div>'
                    f'<div class="attach-video-label">Video</div>'
                    f'</div>'
                )
            else:
                attach_html = (
                    f'<div class="attach-wrap" data-src="{att_src}" onclick="openLb(this)">'
                    f'<img class="attach-img" src="{att_src}" alt="Uploaded image" loading="lazy">'
                    f'</div>'
                )

        avatar_html = (
            f'<img class="speaker-avatar" src="{avatar_src}" alt="{_html_escape(sender_label)}">'
            if avatar_src else ''
        )
        bubbles.append(
            f'        <div class="message-row {side_cls}">\n'
            + (f'          {avatar_html}\n' if avatar_html else '')
            + f'          <div class="bubble {bubble_cls}">\n'
            f'            <div class="sender-name">{sender_label}</div>\n'
            + (f'            {attach_html}\n' if attach_html else '')
            + (f'            <div class="message-text">{safe_text}</div>\n' if safe_text else '')
            + f'            {ts_html}\n'
            f'          </div>\n'
            f'        </div>'
        )

    new_label = f"+{new_count} new" if new_count else ""
    meta_parts = [p for p in [
        relationship,
        f"Since {created}" if created else "",
        f"{len(messages)} messages",
        new_label,
        f"Updated {export_date}",
    ] if p]
    meta_line = " &middot; ".join(meta_parts)
    nav = _nav_bar("chat", None, mind_map_link, selfies_link)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="../favicon.png">
  <link rel="apple-touch-icon" href="../favicon.png">
  <title>{nomi_name} &mdash; Chat</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #1a1a2e;
      color: #e0e0e0;
      min-height: 100vh;
    }}
    header {{
      background: #16213e;
      border-bottom: 1px solid #0f3460;
      padding: 18px 24px;
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .header-info {{ flex: 1; min-width: 0; }}
    header h1 {{ font-size: 1.3rem; color: #e94560; font-weight: 600; }}
    header p  {{ font-size: 0.8rem; color: #888; margin-top: 3px; }}
    .group-desc {{ font-size: 0.78rem; color: #99a3c2; margin-top: 6px; line-height: 1.4; }}
{_NAV_CSS}
    .chat-container {{
      max-width: 760px;
      margin: 0 auto;
      padding: 24px 16px 80px;
    }}
    .message-row {{ display: flex; align-items: flex-end; gap: 8px; margin-bottom: 14px; }}
    .nomi-side   {{ justify-content: flex-start; }}
    .user-side   {{ justify-content: flex-end;   }}
    .speaker-avatar {{
      width: 64px; height: 64px; border-radius: 50%;
      object-fit: cover; object-position: center top;
      flex-shrink: 0; border: 1px solid #0f3460;
    }}
    .bubble {{
      max-width: 70%;
      padding: 10px 14px;
      border-radius: 18px;
      line-height: 1.5;
      font-size: 0.95rem;
      word-break: break-word;
    }}
    .nomi-bubble {{
      background: #16213e;
      border: 1px solid #0f3460;
      border-bottom-left-radius: 4px;
    }}
    .user-bubble {{
      background: #e94560;
      color: #fff;
      border-bottom-right-radius: 4px;
    }}
    .sender-name {{ font-size: 0.72rem; font-weight: 600; margin-bottom: 4px; opacity: 0.7; }}
    .message-text {{ white-space: pre-wrap; }}
    .timestamp   {{ font-size: 0.68rem; margin-top: 5px; opacity: 0.5; text-align: right; }}
    footer {{ text-align: center; padding: 32px 16px; color: #555; font-size: 0.78rem; }}
    /* ---- voice call blocks ---- */
    .voice-call-block {{
      max-width: 92%;
      margin: 4px auto 22px;
      border-radius: 10px;
      border: 1px solid #1e3a5f;
      border-left: 3px solid #4a9eff;
      background: #0a1628;
      overflow: hidden;
    }}
    .vc-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      background: #0f2040;
      border-bottom: 1px solid #1a3050;
    }}
    .vc-icon     {{ font-size: 0.95rem; }}
    .vc-title    {{ font-weight: 600; font-size: 0.82rem; color: #4a9eff; flex: 1; }}
    .vc-duration {{ font-size: 0.7rem; color: #556; }}
    .vc-time     {{ padding: 4px 14px 5px; font-size: 0.68rem; color: #445; border-bottom: 1px solid #0c1a2e; }}
    .vc-transcript {{ padding: 10px 14px; display: flex; flex-direction: column; gap: 5px; }}
    .vc-line     {{ display: flex; gap: 10px; align-items: baseline; line-height: 1.45; }}
    .vc-speaker  {{ min-width: 42px; font-weight: 600; font-size: 0.7rem; flex-shrink: 0;
                    text-transform: uppercase; letter-spacing: 0.03em; }}
    .vc-user .vc-speaker {{ color: #e94560; }}
    .vc-nomi .vc-speaker {{ color: #4a9eff; }}
    .vc-text     {{ flex: 1; font-size: 0.88rem; color: #bbb; }}
    .vc-ts       {{ font-size: 0.63rem; color: #334; white-space: nowrap; }}
    .vc-empty    {{ font-size: 0.82rem; color: #445; font-style: italic; padding: 4px 0; margin: 0; }}
    /* ---- inline selfie thumbnails ---- */
    .selfie-row  {{ justify-content: flex-start; }}
    .selfie-inline {{
      width: 200px; cursor: zoom-in;
      border: 1px solid #0f3460; border-radius: 10px; overflow: hidden;
      transition: border-color 0.15s, transform 0.15s;
    }}
    .selfie-inline:hover {{ border-color: #4a9eff; transform: translateY(-2px); }}
    .selfie-inline img {{ width: 100%; display: block; }}
    .selfie-inline .timestamp {{ padding: 4px 8px; font-size: 0.68rem; color: #556; text-align: center; opacity: 1; }}
    /* ---- user-uploaded attachments ---- */
    .attach-wrap {{
      margin-bottom: 6px; border-radius: 8px; overflow: hidden;
      max-width: 240px; cursor: zoom-in; position: relative;
      border: 1px solid rgba(255,255,255,0.1);
    }}
    .attach-img {{ width: 100%; display: block; }}
    .attach-video-wrap {{ cursor: zoom-in; }}
    .play-overlay {{
      position: absolute; inset: 0;
      display: flex; align-items: center; justify-content: center;
      pointer-events: none;
    }}
    .play-btn {{
      width: 52px; height: 52px; border-radius: 50%;
      background: rgba(0,0,0,0.62); border: 2px solid rgba(255,255,255,0.85);
      display: flex; align-items: center; justify-content: center;
    }}
    .play-btn::after {{
      content: ''; border-style: solid;
      border-width: 10px 0 10px 18px;
      border-color: transparent transparent transparent #fff;
      margin-left: 4px;
    }}
    .attach-video-label {{
      position: absolute; bottom: 4px; right: 6px;
      font-size: 0.65rem; font-weight: 700; color: #fff;
      background: rgba(0,0,0,0.55); padding: 1px 5px; border-radius: 4px;
      letter-spacing: 0.03em;
    }}
    /* ---- lightbox ---- */
    #lightbox {{
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.92);
      z-index: 1000; align-items: center; justify-content: center; cursor: zoom-out;
    }}
    #lightbox.open {{ display: flex; }}
    #lb-img {{
      max-width: 92vw; max-height: 92vh;
      object-fit: contain; border-radius: 6px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.6);
    }}
    #lb-close {{
      position: fixed; top: 16px; right: 20px;
      color: #aaa; font-size: 1.8rem; cursor: pointer; line-height: 1;
      background: none; border: none;
    }}
    #lb-close:hover {{ color: #fff; }}
    @media (max-width: 640px) {{
      .chat-container {{ padding: 14px 8px 80px; }}
      .bubble {{ max-width: 88%; font-size: 0.88rem; }}
      .voice-call-block {{ max-width: 100%; }}
      .scroll-btn {{ width: 38px; height: 38px; right: 10px; }}
      #btn-top {{ top: 100px; }}
    }}
    /* ---- scroll buttons ---- */
    .scroll-btn {{
      position: fixed; right: 20px;
      width: 44px; height: 44px; border-radius: 50%;
      background: #16213e; border: 1px solid #1e3a5f;
      color: #4a9eff; font-size: 1rem; line-height: 1;
      cursor: pointer; z-index: 100;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 2px 10px rgba(0,0,0,0.5);
      transition: background 0.15s, border-color 0.15s;
    }}
    .scroll-btn:hover {{ background: #1a2d3d; border-color: #4a9eff; }}
    #btn-top {{ top: 72px; }}
    #btn-bot {{ bottom: 24px; }}
  </style>
</head>
<body>
  <header>
    <div class="header-info">
      <h1>{nomi_name} &mdash; Chat</h1>
      <p>{meta_line}</p>
      {f'<p class="group-desc">{_html_escape(description)}</p>' if description else ''}
    </div>
    {nav}
  </header>
  <div class="chat-container">
{chr(10).join(bubbles)}
  </div>
  <button id="btn-top" class="scroll-btn"
          onclick="document.documentElement.scrollTop=0"
          title="Scroll to top">&#9650;</button>
  <button id="btn-bot" class="scroll-btn"
          onclick="scrollToBottom()"
          title="Scroll to bottom">&#9660;</button>
  <div id="lightbox" onclick="closeLb(event)">
    <button id="lb-close" onclick="closeLb()">&times;</button>
    <img id="lb-img" src="" alt="">
  </div>
  <footer>NomiVault &middot; Nomi.ai &middot; Last updated {export_date}</footer>
  <script>
    function scrollToBottom() {{
      document.documentElement.scrollTop = document.documentElement.scrollHeight;
    }}
    function openLb(el) {{
      document.getElementById('lb-img').src = el.getAttribute('data-src');
      document.getElementById('lightbox').classList.add('open');
    }}
    function closeLb(e) {{
      if (!e || e.target !== document.getElementById('lb-img')) {{
        document.getElementById('lightbox').classList.remove('open');
        document.getElementById('lb-img').src = '';
      }}
    }}
    document.addEventListener('keydown', function(e) {{ if (e.key === 'Escape') closeLb(); }});
    document.querySelectorAll('[data-utc]').forEach(function(el) {{
      var d = new Date(el.getAttribute('data-utc'));
      if (!isNaN(d.getTime())) {{
        el.textContent = d.toLocaleString(undefined, {{
          month: 'short', day: 'numeric', year: 'numeric',
          hour: 'numeric', minute: '2-digit'
        }});
      }}
    }});
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Output capture / email notification helpers (for cron / scheduled runs)
# ---------------------------------------------------------------------------

class _Tee(io.TextIOBase):
    """Write to multiple streams simultaneously.

    Replaces sys.stdout so every print() call goes to both the real terminal
    and an in-memory StringIO buffer.  The buffer is later included in the
    error-notification email when the run fails.
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s: str) -> int:
        for stream in self._streams:
            stream.write(s)
        return len(s)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass


def _load_smtp_config(path: str | None) -> dict | None:
    """Read SMTP settings from an INI file.

    Returns a dict with keys ``host``, ``port``, ``user``, ``password``,
    ``from``, ``to``, or ``None`` if the file is absent or cannot be parsed.
    The file is silently skipped (returns None) when it does not exist, so
    the script works without any SMTP setup by default.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    cfg = configparser.ConfigParser()
    try:
        cfg.read(p, encoding="utf-8")
        s = cfg["smtp"]
        return {
            "host":     s.get("host",     "smtp.gmail.com"),
            "port":     s.getint("port",   587),
            "user":     s.get("user",     ""),
            "password": s.get("password", ""),
            "from":     s.get("from",     s.get("user", "")),
            "to":       s.get("to",       ""),
        }
    except Exception as exc:
        print(f"Warning: could not read SMTP config {path}: {exc}", file=sys.stderr)
        return None


def _send_error_email(cfg: dict, subject: str, body: str) -> None:
    """Send a plain-text error notification via SMTP with STARTTLS."""
    import smtplib
    from email.mime.text import MIMEText

    msg            = MIMEText(body, "plain", "utf-8")
    msg["From"]    = cfg["from"] or cfg["user"]
    msg["To"]      = cfg["to"]
    msg["Subject"] = subject
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(msg["From"], [cfg["to"]], msg.as_string())
        print(f"Error notification sent to {cfg['to']}", file=sys.stderr)
    except Exception as exc:
        print(f"Warning: failed to send error email: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run(args) -> None:
    """Execute the full archive run.  Called by main(), which handles I/O capture."""
    global OUTPUT_DIR
    if args.output:
        OUTPUT_DIR = Path(args.output).expanduser().resolve()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching list of Nomis...")
    try:
        nomis = fetch_nomis(args.key)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        print("Check that your API key is correct.")
        sys.exit(1)

    if not nomis:
        print("No Nomis found. Check your API key (Profile → Integration).")
        sys.exit(1)

    print(f"Found {len(nomis)} Nomi(s): {', '.join(n['name'] for n in nomis)}\n")
    if args.token:
        print("Voice-call transcripts enabled (--token provided).\n")
    else:
        print("Tip: pass --token to also archive voice-call transcripts.\n")

    # Numeric IDs and profile pictures aren't in the public /v1/nomis
    # response; only the beta.nomi.ai internal Nomi list has them. Fetch it
    # once here rather than per-Nomi, so a brand-new Nomi's numeric ID can be
    # auto-discovered without the user having to look it up in the browser
    # URL and pass --nomi-id manually.
    beta_nomi_info: dict = fetch_beta_nomi_info(args.token) if args.token else {}

    confirmed_pattern: str | None = None   # used only in no-token mode
    landing_entries:   list[dict] = []

    # --nomi-id is now only a manual fallback for when auto-discovery above
    # doesn't have an entry for a Nomi (e.g. the beta API call failed). It's
    # a single global CLI value, but it must only ever apply to the one Nomi
    # it was meant for — if it were applied to *any* Nomi still missing a
    # numeric ID, and more than one happened to be in that state during the
    # same run, it would silently leak onto the wrong Nomi and both would
    # end up fetching the same conversation. Only auto-apply it when exactly
    # one Nomi in this run needs it.
    nomi_id_target_uuid: str | None = None
    if args.token and args.nomi_id:
        uncached_uuids = [
            n["uuid"] for n in nomis
            if not beta_nomi_info.get(n["uuid"], {}).get("numeric_id")
            and not (None if args.full else _peek_cached_numeric_id(
                _safe_name(n["name"]), beta_nomi_info.get(n["uuid"], {}).get("numeric_id")))
        ]
        if len(uncached_uuids) == 1:
            nomi_id_target_uuid = uncached_uuids[0]
        elif len(uncached_uuids) > 1:
            print(f"⚠  --nomi-id was provided, but {len(uncached_uuids)} Nomis are "
                  f"missing a numeric ID (cached or auto-discovered), so it's "
                  f"ambiguous which one it's for.")
            print("   Ignoring --nomi-id this run — archive Nomis one at a time:")
            print("   run with --nomi-id set to just one Nomi's ID, let it finish,")
            print("   then repeat for the next.\n")

    for nomi in nomis:
        name      = nomi["name"]
        uuid      = nomi["uuid"]
        safe_name = _safe_name(name)
        print(f"Processing: {name}  ({uuid})")

        # --- resolve this Nomi's output folder, migrating older layouts --
        auto_numeric_id = beta_nomi_info.get(uuid, {}).get("numeric_id")
        nomi_dir = _resolve_nomi_dir(safe_name, auto_numeric_id)

        # --- load local cache -------------------------------------------
        cache            = {} if args.full else load_cache(nomi_dir, safe_name)
        cached_messages  = cache.get("messages",        [])
        cached_vc        = cache.get("voice_calls",     [])
        cached_tx        = cache.get("transcripts",     {})
        cached_num_id    = cache.get("numeric_nomi_id", None)
        is_incremental   = bool(cached_messages)

        # The numeric ID may not have been known ahead of time (no --token
        # yet, or this run's beta lookup failed) but could already be
        # cached from an earlier run — rename the placeholder folder now
        # that we know it.
        if not auto_numeric_id and cached_num_id:
            nomi_dir = _resolve_nomi_dir(safe_name, cached_num_id)

        # ================================================================
        # Path A: beta.nomi.ai  (messages + voice calls in one response)
        # ================================================================
        if args.token:
            this_nomi_id_arg = args.nomi_id if uuid == nomi_id_target_uuid else None
            nomi_id = cached_num_id or auto_numeric_id or this_nomi_id_arg or uuid
            if nomi_id == uuid and not this_nomi_id_arg:
                print(f"  ⚠  Skipping {name} — numeric nomi ID not yet cached or discoverable.")
                print(f"     This usually means the beta.nomi.ai lookup failed this run.")
                print(f"     You can force it by passing --nomi-id XXXXXXX, where XXXXXXX")
                print(f"     is the number in the URL when viewing this Nomi on beta.nomi.ai:")
                print(f"     beta.nomi.ai/nomis/XXXXXXX")
                print()
                continue
            # Build a set of already-cached message IDs so fetch_beta_messages
            # can stop as soon as it hits history we already have.
            known_ids: set = {
                m.get("uuid") or m.get("id")
                for m in cached_messages
                if m.get("uuid") or m.get("id")
            } if is_incremental else set()

            print(f"  Fetching from beta.nomi.ai (nomi_id={nomi_id}) ...")
            fresh_msgs, fresh_vc, numeric_id = fetch_beta_messages(
                nomi_id, args.token, known_ids=known_ids or None
            )

            # Populate cached_num_id from the best available source:
            #  1. API response (from voice call data)
            #  2. nomi_id used for this fetch — confirmed valid because messages
            #     were just fetched with it and the UUID-fallback was already
            #     rejected above, so nomi_id is the real numeric ID.
            if numeric_id:
                cached_num_id = numeric_id
            if not cached_num_id:
                cached_num_id = nomi_id

            if is_incremental:
                latest_ts = max((_msg_timestamp(m) for m in cached_messages), default="")
                print(f"  Cache: {len(cached_messages)} messages (since {latest_ts or '?'}) ...")
                merged, new_count = merge_messages(cached_messages, fresh_msgs)
                print(f"  {'+'+ str(new_count) + ' new message(s)' if new_count else 'No new messages'}"
                      f"  ({len(merged)} total)")
            else:
                merged    = fresh_msgs
                new_count = len(merged)
                print(f"  {new_count} messages downloaded")

            # Merge voice calls (dedup by id)
            cached_ids  = {vc["id"] for vc in cached_vc}
            new_vc      = [vc for vc in fresh_vc if vc["id"] not in cached_ids]
            all_vc      = cached_vc + new_vc
            if new_vc:
                print(f"  {len(new_vc)} new voice call(s)")

            # Fetch transcripts only for calls not already cached
            all_tx = dict(cached_tx)
            if cached_num_id and new_vc:
                for vc in new_vc:
                    cid = vc["id"]
                    print(f"  Fetching transcript for voice call {cid[:8]}...", end=" ", flush=True)
                    lines = fetch_voice_transcript(cached_num_id, cid, args.token)
                    all_tx[cid] = lines
                    print(f"{len(lines)} lines")
            elif new_vc and not cached_num_id:
                print("  ⚠  Could not fetch transcripts: numeric nomi ID not yet known.")
                print("     Run once more after a successful fetch to retry.")

        # ================================================================
        # Path B: api.nomi.ai  (fallback, no voice transcripts)
        # ================================================================
        else:
            all_vc    = cached_vc
            all_tx    = cached_tx

            if confirmed_pattern is None:
                confirmed_pattern, _ = discover_endpoint(uuid, args.key, args.messages_url)
                if confirmed_pattern is None:
                    print()
                    print("Could not auto-discover the message history endpoint.")
                    print("Re-run with --token for the full beta.nomi.ai path, or add:")
                    print('  --messages-url "/v1/nomis/{uuid}/ENDPOINT"')
                    sys.exit(1)

            if is_incremental:
                latest_ts = max((_msg_timestamp(m) for m in cached_messages), default="")
                print(f"  Cache: {len(cached_messages)} messages. Fetching updates since {latest_ts or '?'} ...")
                fresh     = fetch_all_messages(uuid, args.key, confirmed_pattern, since_ts=latest_ts)
                merged, new_count = merge_messages(cached_messages, fresh)
                print(f"  {'+'+ str(new_count) + ' new message(s)' if new_count else 'No new messages'}"
                      f"  ({len(merged)} total)")
            else:
                mode_label = "Full re-download" if args.full else "First run — full download"
                print(f"  {mode_label} ...")
                merged    = fetch_all_messages(uuid, args.key, confirmed_pattern)
                new_count = len(merged)
                print(f"  {new_count} messages downloaded")

        # --- build rendering map: call_uuid -> (call_info, transcript) --
        voice_call_map = {
            vc["id"]: (vc, all_tx.get(vc["id"], []))
            for vc in all_vc
        }

        # Pre-compute cross-page link values.  We use "will create" logic so
        # that all three pages can reference each other even on first run.
        using_token    = bool(args.token and cached_num_id)
        if args.token and not using_token:
            print(f"  ⚠  Mind map and media skipped — numeric nomi ID is still unknown.")
            print(f"     Re-run with --nomi-id XXXXXXX (find it in the URL on beta.nomi.ai).")
        chat_lnk       = f"{safe_name}-chat.html"
        mm_lnk         = f"{safe_name}-mind-map.html"    if (using_token or (nomi_dir / f"{safe_name}-mind-map.html").exists())    else None
        selfies_lnk    = f"{safe_name}-media.html"        if (using_token or (nomi_dir / f"{safe_name}-media.html").exists())        else None

        # --- mind map (token path only) ---------------------------------
        all_mm_terms: list = cache.get("mind_map_terms", [])
        if using_token:
            cached_mm_lookup = {t["uuid"]: t for t in all_mm_terms}
            mm_by_category   = fetch_full_mind_map(cached_num_id, args.token, cached_mm_lookup)
            all_mm_terms     = [t for terms in mm_by_category.values() for t in terms]

            mm_html  = render_mind_map_html(nomi, mm_by_category,
                                             chat_link=chat_lnk,
                                             selfies_link=selfies_lnk)
            mm_path  = nomi_dir / f"{safe_name}-mind-map.html"
            mm_path.write_text(mm_html, encoding="utf-8")
            print(f"  Mind map → {mm_path}")

        # --- selfies and user uploads (token path only) -----------------
        all_user_uploads: list = cache.get("user_uploads", [])
        all_selfies: list      = cache.get("selfies", [])
        profile_pic_filename: str | None = None
        if using_token:
            print("  Fetching media list ...", end=" ", flush=True)
            fresh_selfies = fetch_selfies(cached_num_id, args.token)
            cached_ids: set = set()
            for s in all_selfies:
                cached_ids |= {v for v in (s.get("id"), s.get("uuid")) if v}
            new_selfies = [
                s for s in fresh_selfies
                if not ({v for v in (s.get("id"), s.get("uuid")) if v} & cached_ids)
            ]
            all_selfies       = all_selfies + new_selfies
            print(f"{len(fresh_selfies)} total, {len(new_selfies)} new")

            # One-time cleanup: a cache saved before pagination-overlap
            # dedup existed (or before VideoRequest/ImageEditRequest were
            # correctly tracked by uuid) could already have duplicate
            # entries pointing at the same downloaded files. Collapse them
            # by shared identifier rather than one fixed key — see the
            # matching comment in fetch_selfies() for why.
            _seen_ids: set = set()
            _deduped: list = []
            for s in all_selfies:
                ids = {v for v in (s.get("id"), s.get("uuid")) if v}
                if ids and ids & _seen_ids:
                    continue
                _seen_ids |= ids
                _deduped.append(s)
            if len(_deduped) < len(all_selfies):
                print(f"  Removed {len(all_selfies) - len(_deduped)} duplicate media "
                      f"entr{'y' if len(all_selfies) - len(_deduped) == 1 else 'ies'} from the cache")
            all_selfies = _deduped

            # Report all unique media types found in the full list (cache + new).
            _known_media_types = {"Selfie", "VideoRequest", "CharacterImage", "ImageEditRequest", "Art"}
            _all_types = {s.get("mediaType") for s in all_selfies} - {None}
            _new_types = _all_types - _known_media_types
            if _new_types:
                print(f"  ℹ  Unknown media type(s) found: {', '.join(sorted(_new_types))}"
                      f" — these will be skipped.")

            selfies_dir = nomi_dir / "media"
            selfies_dir.mkdir(parents=True, exist_ok=True)

            # --- profile picture (currently selected on the Nomi's own record) ---
            _nomi_info = beta_nomi_info.get(uuid, {})
            # imageEditRequestUuid is checked first: when an edited image is
            # set as the profile picture, pictureImageId/pictureSelfieImageId
            # are NOT updated and keep pointing at whichever picture was
            # active before the edit.
            _image_edit_uuid = _nomi_info.get("imageEditRequestUuid")
            picture_image_id = (_image_edit_uuid
                                or _nomi_info.get("pictureSelfieImageId")
                                or _nomi_info.get("pictureImageId"))
            if picture_image_id:
                print(f"  Profile picture (id={picture_image_id[:16]}) ...", end=" ", flush=True)
                profile_pic_filename, was_new = download_profile_picture(
                    picture_image_id, selfies_dir, args.token, safe_name, cached_num_id,
                    all_selfies=all_selfies, image_edit_uuid=_image_edit_uuid,
                )
                if profile_pic_filename:
                    print("downloaded" if was_new else "already up to date")
                else:
                    print("failed")

            # Download new media and ensure all have local filenames stored
            newly_dl = 0
            for s in all_selfies:
                if s.get("mediaType") == "VideoRequest":
                    needs_dl = (
                        "preview_filename" not in s
                        or "video_filename" not in s
                        or not (selfies_dir / s.get("preview_filename", "\x00")).exists()
                        or not (selfies_dir / s.get("video_filename", "\x00")).exists()
                    )
                    if needs_dl:
                        pfn, vfn = download_video_media(s, selfies_dir, args.token, safe_name)
                        if pfn:
                            s["preview_filename"] = pfn
                        if vfn:
                            s["video_filename"] = vfn
                        if pfn or vfn:
                            newly_dl += 1
                elif s.get("mediaType") == "CharacterImage":
                    if "local_filename" not in s or not (selfies_dir / s["local_filename"]).exists():
                        fn = download_character_image(s, selfies_dir, args.token,
                                                      safe_name, cached_num_id)
                        if fn:
                            s["local_filename"] = fn
                            newly_dl += 1
                elif s.get("mediaType") == "ImageEditRequest":
                    if "local_filename" not in s or not (selfies_dir / s["local_filename"]).exists():
                        fn = download_image_edit(s, selfies_dir, args.token, safe_name)
                        if fn:
                            s["local_filename"] = fn
                            newly_dl += 1
                else:
                    if "local_filename" not in s or not (selfies_dir / s["local_filename"]).exists():
                        fn = download_selfie_image(s, selfies_dir, args.token, safe_name)
                        if fn:
                            s["local_filename"] = fn
                            newly_dl += 1
            if newly_dl:
                print(f"  Downloaded {newly_dl} new media file(s)")

            # --- user uploads (attached images/videos the user sent) ----
            cached_upload_ids = {u["message_uuid"] for u in all_user_uploads}
            msgs_with_att = [
                m for m in merged
                if m.get("attachment") and not m["attachment"].get("reaped")
                and m.get("uuid") not in cached_upload_ids
            ]
            if msgs_with_att:
                print(f"  Downloading {len(msgs_with_att)} new user upload(s) ...")
                for m in msgs_with_att:
                    fn = download_user_upload(m, selfies_dir, args.token,
                                              safe_name, nomi_id=cached_num_id)
                    att = m["attachment"]
                    all_user_uploads.append({
                        "message_uuid":   m.get("uuid", ""),
                        "sent":           m.get("sent", ""),
                        "mime_type":      att.get("mimeType", ""),
                        "file_name":      att.get("fileName", ""),
                        "local_filename": fn,
                        "is_video":       att.get("mimeType", "").startswith("video/"),
                    })

            gallery_html = render_gallery_html(nomi, all_selfies, safe_name,
                                               chat_link=chat_lnk,
                                               mind_map_link=mm_lnk,
                                               user_uploads=all_user_uploads)
            gallery_path = nomi_dir / f"{safe_name}-media.html"
            gallery_path.write_text(gallery_html, encoding="utf-8")
            print(f"  Media gallery → {gallery_path}")

        # --- collect landing page entry ---------------------------------
        # Prefer the Nomi's actual currently-selected profile picture; fall
        # back to the first generated CharacterImage if it's unavailable
        # (e.g. running without --token, or the picture couldn't be fetched).
        # index.html lives one level up from nomi_dir, so paths are prefixed
        # with this Nomi's folder name.
        folder_name = nomi_dir.name
        if profile_pic_filename:
            char_img_src = f"{folder_name}/media/{profile_pic_filename}"
        else:
            char_imgs = [s for s in all_selfies
                         if s.get("mediaType") == "CharacterImage"
                         and s.get("local_filename")]
            char_img_src = (
                f"{folder_name}/media/{char_imgs[0]['local_filename']}"
                if char_imgs else None
            )
        first_msg_ts = min((_msg_timestamp(m) for m in merged), default="") if merged else ""
        landing_entries.append({
            "uuid":           uuid,
            "name":           name,
            "safe_name":      safe_name,
            "folder_name":    folder_name,
            "first_msg_ts":   first_msg_ts,
            "char_image_src": char_img_src,
        })

        # Chat page lives inside nomi_dir itself, so its own avatar path
        # drops the "<folder_name>/" prefix char_img_src needs from the
        # landing page (which lives one level up).
        own_avatar_src = char_img_src[len(folder_name) + 1:] if char_img_src else None

        # --- persist cache ----------------------------------------------
        save_cache(nomi_dir, safe_name, nomi, merged,
                   voice_calls=all_vc,
                   transcripts=all_tx,
                   numeric_nomi_id=cached_num_id,
                   mind_map_terms=all_mm_terms,
                   selfies=all_selfies,
                   user_uploads=all_user_uploads)

        # --- render HTML ------------------------------------------------
        html = render_html(
            nomi, merged,
            new_count      = new_count if is_incremental else 0,
            voice_call_map = voice_call_map,
            voice_calls    = all_vc,
            mind_map_link  = mm_lnk,
            selfies_link   = selfies_lnk,
            selfies        = all_selfies,
            safe_name      = safe_name,
            user_uploads   = all_user_uploads,
            own_avatar_src = own_avatar_src,
        )
        out_path = nomi_dir / f"{safe_name}-chat.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"  Saved → {out_path}\n")

    # --- group chats (token path only — no public-API equivalent) -------
    # Looked up by participant UUID so a group's landing card and chat
    # bubbles can reuse each participant's own already-resolved profile
    # picture from this run.
    individual_by_uuid = {e["uuid"]: e for e in landing_entries if e.get("uuid")}
    group_uuids: set = set()

    if args.token:
        print("Fetching group chats...")
        groups = fetch_beta_group_chats(args.token)
        if groups:
            print(f"Found {len(groups)} group chat(s): "
                  f"{', '.join(g['name'] for g in groups)}\n")

        for group in groups:
            group_name      = group["name"]
            group_uuid      = group["uuid"]
            group_id        = group["id"]
            safe_group_name = _safe_name(group_name)
            folder_name     = f"{safe_group_name}-group-{group_id}"
            group_dir       = OUTPUT_DIR / folder_name
            group_dir.mkdir(parents=True, exist_ok=True)
            group_uuids.add(group_uuid)

            print(f"Processing group: {group_name}  ({group_uuid})")

            cache             = {} if args.full else load_group_cache(group_dir, safe_group_name)
            cached_messages   = cache.get("messages", [])
            cached_raw_selfies = cache.get("raw_selfies", [])
            cached_mm_terms   = cache.get("mind_map_terms", [])
            is_incremental    = bool(cached_messages)

            known_ids: set = {
                m.get("uuid") or m.get("id")
                for m in cached_messages
                if m.get("uuid") or m.get("id")
            } if is_incremental else set()

            print(f"  Fetching from beta.nomi.ai (group_id={group_id}) ...")
            fresh_msgs, fresh_raw_selfies = fetch_beta_group_messages(
                group_id, args.token, known_ids=known_ids or None
            )

            if is_incremental:
                merged, new_count = merge_messages(cached_messages, fresh_msgs)
                print(f"  {'+'+ str(new_count) + ' new message(s)' if new_count else 'No new messages'}"
                      f"  ({len(merged)} total)")
            else:
                merged    = fresh_msgs
                new_count = len(merged)
                print(f"  {new_count} messages downloaded")

            # Merge raw selfies (dedup by the outer selfie-request id)
            raw_selfies_by_id = {s["id"]: s for s in cached_raw_selfies if s.get("id")}
            for s in fresh_raw_selfies:
                if s.get("id"):
                    raw_selfies_by_id[s["id"]] = s
            all_raw_selfies = list(raw_selfies_by_id.values())
            flat_selfies    = _flatten_group_selfies(all_raw_selfies, group_id)

            group_media_dir = group_dir / "media"
            group_media_dir.mkdir(parents=True, exist_ok=True)
            resolved = 0
            for s in flat_selfies:
                fn = download_selfie_image(s, group_media_dir, args.token, safe_group_name)
                if fn:
                    s["local_filename"] = fn
                    resolved += 1
            if flat_selfies:
                print(f"  {resolved}/{len(flat_selfies)} group selfie(s) available")

            # Group mind maps are undocumented and may not exist as a real
            # endpoint for every account/group — degrade to empty rather
            # than letting an unexpected error status take down the run.
            cached_mm_lookup = {t["uuid"]: t for t in cached_mm_terms}
            try:
                mm_by_category = fetch_full_mind_map(
                    group_id, args.token, cached_mm_lookup, entity_type="group-chats"
                )
            except Exception as exc:
                print(f"  ⚠  Group mind map unavailable: {exc}")
                mm_by_category = {}
            all_mm_terms = [t for terms in mm_by_category.values() for t in terms]

            # Participant avatars: each participant's own already-resolved
            # profile picture from this run, used for chat bubbles (path
            # relative to the group's own folder) and the landing collage
            # (path relative to OUTPUT_DIR, matching individual entries).
            speaker_avatars: dict = {}
            participants_for_card: list = []
            for p in group.get("nomis", []):
                p_id      = p.get("id")
                p_name    = p.get("name")
                p_entry   = individual_by_uuid.get(p.get("uuid"))
                p_img_src = p_entry.get("char_image_src") if p_entry else None
                if p_img_src:
                    speaker_avatars[p_id] = f"../{p_img_src}"
                participants_for_card.append({"name": p_name, "img_src": p_img_src})

            chat_lnk  = f"{safe_group_name}-chat.html"
            mm_lnk    = f"{safe_group_name}-mind-map.html"
            media_lnk = f"{safe_group_name}-media.html"

            mm_html = render_mind_map_html(group, mm_by_category,
                                           chat_link=chat_lnk, selfies_link=media_lnk)
            (group_dir / f"{safe_group_name}-mind-map.html").write_text(mm_html, encoding="utf-8")

            gallery_html = render_gallery_html(group, flat_selfies, safe_group_name,
                                               chat_link=chat_lnk, mind_map_link=mm_lnk)
            (group_dir / f"{safe_group_name}-media.html").write_text(gallery_html, encoding="utf-8")

            description = (group.get("note") or {}).get("text") or None
            html = render_html(
                group, merged,
                new_count      = new_count if is_incremental else 0,
                mind_map_link  = mm_lnk,
                selfies_link   = media_lnk,
                selfies        = flat_selfies,
                safe_name      = safe_group_name,
                is_group       = True,
                speaker_avatars = speaker_avatars,
                description    = description,
            )
            (group_dir / chat_lnk).write_text(html, encoding="utf-8")
            print(f"  Saved → {group_dir / chat_lnk}\n")

            save_group_cache(group_dir, safe_group_name, group, merged,
                             raw_selfies=all_raw_selfies,
                             mind_map_terms=all_mm_terms)

            first_msg_ts = (min((_msg_timestamp(m) for m in merged), default="")
                           if merged else group.get("created", ""))
            landing_entries.append({
                "uuid":           group_uuid,
                "name":           group_name,
                "safe_name":      safe_group_name,
                "folder_name":    folder_name,
                "first_msg_ts":   first_msg_ts,
                "char_image_src": None,
                "is_group":       True,
                "participants":   participants_for_card,
            })

    # --- keep archives of deleted Nomis reachable on the landing page ---
    # If a Nomi no longer appears in the account (deleted on Nomi.ai), its
    # local archive files still exist but the main loop above never visits
    # it, so it would otherwise vanish from index.html even though nothing
    # was actually removed from disk. Scan for any cache file whose UUID
    # isn't in this run's active list and re-add it, marked as deleted.
    active_uuids = {n["uuid"] for n in nomis} | group_uuids

    # Migrate any leftover pre-1.5 flat-root files for a Nomi that's no
    # longer in the account at all — the main loop's own migration only
    # runs for Nomis it actually visits (i.e. ones still returned by the
    # API), so a deleted Nomi's old-layout files would otherwise never move.
    for cache_file in sorted(OUTPUT_DIR.glob("*.json")):
        safe_name_o = cache_file.stem
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("uuid"):
            continue   # not a Nomi cache file (e.g. manifest.json)
        if data.get("uuid") in active_uuids:
            continue
        _resolve_nomi_dir(safe_name_o, data.get("numeric_nomi_id"))

    for cache_file in sorted(OUTPUT_DIR.glob("*/*.json")):
        nomi_dir_o  = cache_file.parent
        safe_name_o = cache_file.stem
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("uuid"):
            continue
        if data.get("uuid") in active_uuids:
            continue

        # This Nomi is never visited by the main loop again, so its pages
        # are never re-rendered — bring an already-in-folder-but-stale
        # layout up to date by hand: rename an old-style chat file if one
        # is still sitting there, then patch every page's baked-in links.
        chat_path_o = nomi_dir_o / f"{safe_name_o}-chat.html"
        legacy_chat_path = nomi_dir_o / f"{safe_name_o}.html"
        if legacy_chat_path.exists() and not chat_path_o.exists():
            legacy_chat_path.rename(chat_path_o)
        if not chat_path_o.exists():
            continue

        for html_path in (
            chat_path_o,
            nomi_dir_o / f"{safe_name_o}-mind-map.html",
            nomi_dir_o / f"{safe_name_o}-media.html",
        ):
            if html_path.exists():
                patched = _patch_legacy_html_links(
                    html_path.read_text(encoding="utf-8"), safe_name_o
                )
                html_path.write_text(patched, encoding="utf-8")

        media_dir_o     = nomi_dir_o / "media"
        profile_matches = (sorted(media_dir_o.glob(f"{safe_name_o}_profile_*.webp"))
                           if media_dir_o.exists() else [])
        if profile_matches:
            char_img_src_o = f"{nomi_dir_o.name}/media/{profile_matches[0].name}"
        else:
            o_char_imgs = [s for s in data.get("selfies", [])
                           if s.get("mediaType") == "CharacterImage" and s.get("local_filename")]
            char_img_src_o = (f"{nomi_dir_o.name}/media/{o_char_imgs[0]['local_filename']}"
                              if o_char_imgs else None)

        o_messages     = data.get("messages", [])
        first_msg_ts_o = min((_msg_timestamp(m) for m in o_messages), default="") if o_messages else ""
        display_name   = data.get("name", safe_name_o)
        landing_entries.append({
            "name":           display_name,
            "safe_name":      safe_name_o,
            "folder_name":    nomi_dir_o.name,
            "first_msg_ts":   first_msg_ts_o,
            "char_image_src": char_img_src_o,
            "deleted":        True,
        })
        print(f"Note: {display_name} no longer appears in your Nomi.ai account — "
              f"kept on the landing page, marked as deleted.")

    # --- render landing page + PWA support files -----------------------
    if landing_entries:
        landing_entries.sort(key=lambda e: (bool(e.get("deleted")), e["first_msg_ts"]))
        landing_html = render_landing_html(landing_entries)
        landing_path = OUTPUT_DIR / "index.html"
        landing_path.write_text(landing_html, encoding="utf-8")
        print(f"Landing page → {landing_path}")

        (OUTPUT_DIR / "manifest.json").write_text(_PWA_MANIFEST, encoding="utf-8")
        (OUTPUT_DIR / "sw.js").write_text(_SW_JS, encoding="utf-8")
        icon_path = OUTPUT_DIR / "nomi-icon.svg"
        if not icon_path.exists():
            icon_path.write_text(_PWA_ICON_SVG, encoding="utf-8")

        # Copy favicon.png from next to nomivault.py into the output directory
        # if the user has placed one there.  Only copies once; a custom icon
        # already in the output directory is never overwritten.
        src_favicon = Path(__file__).parent / "favicon.png"
        dst_favicon = OUTPUT_DIR / "favicon.png"
        if src_favicon.exists() and not dst_favicon.exists():
            import shutil
            shutil.copy2(src_favicon, dst_favicon)
            print(f"  Favicon → {dst_favicon}")

    print("\nAll done. Open index.html in any browser.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NomiVault — export Nomi.ai conversations to self-contained HTML files."
    )
    parser.add_argument(
        "--version", action="version", version=f"NomiVault {__version__}",
    )
    parser.add_argument(
        "--key", required=True,
        help="Your Nomi.ai API key  (Profile → Integration in the web app)",
    )
    parser.add_argument(
        "--token",
        help=(
            "Value of the __Secure-next-auth.session-token cookie from beta.nomi.ai. "
            "Enables voice-call transcript fetching and the richer internal message API. "
            "To find it: open beta.nomi.ai in Chrome → F12 → Application tab → "
            "Storage → Cookies → https://beta.nomi.ai → copy the Value of "
            "__Secure-next-auth.session-token."
        ),
    )
    parser.add_argument(
        "--nomi-id",
        dest="nomi_id",
        help="Numeric nomi ID used by beta.nomi.ai (e.g. 1234567890). Normally "
             "auto-discovered and cached on the first --token run, so this is only "
             "needed as a manual fallback if that discovery fails. Find it in the "
             "URL when viewing your Nomi: beta.nomi.ai/nomis/XXXXXXX",
    )
    parser.add_argument(
        "--messages-url",
        help='Override the api.nomi.ai message endpoint pattern (no-token mode only), '
             'e.g. "/v1/nomis/{uuid}/chats"',
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Ignore the local cache and re-download the entire conversation history "
             "for EVERY Nomi processed this run, not just one. A brand-new Nomi does "
             "not need this — it is downloaded in full automatically since it has no "
             "cache yet. To force a clean re-download of a single already-archived "
             "Nomi, delete that Nomi's <Name>.json cache file instead of using --full.",
    )
    parser.add_argument(
        "--output",
        metavar="DIR",
        help="Directory to write HTML and cache files to "
             "(default: 'output' folder next to nomivault.py). "
             "Created automatically if it does not exist.",
    )
    parser.add_argument(
        "--silent", action="store_true",
        help="Suppress all terminal output. Run output is still captured and "
             "included in the error email when --smtp-config is configured.",
    )
    parser.add_argument(
        "--smtp-config",
        metavar="FILE",
        help=(
            "Path to an INI file with SMTP settings for error-notification emails. "
            "When omitted, the script looks for smtp.ini next to nomivault.py. "
            "No email is sent if no config file is found. "
            "See smtp.ini.example for the expected format."
        ),
    )
    args = parser.parse_args()

    # Resolve SMTP config *before* setting up output capture so that any
    # config-parse warnings always reach the terminal, even with --silent.
    smtp_cfg_path = args.smtp_config
    if smtp_cfg_path is None:
        _default_smtp = Path(__file__).parent / "smtp.ini"
        if _default_smtp.exists():
            smtp_cfg_path = str(_default_smtp)
    smtp_cfg = _load_smtp_config(smtp_cfg_path)

    # Redirect stdout: every print() in _run() goes to the StringIO buffer
    # and (unless --silent) also to the real terminal.
    buf         = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout  = buf if args.silent else _Tee(real_stdout, buf)

    exit_code = 0
    try:
        _run(args)
    except SystemExit as exc:
        # sys.exit() called inside _run() — capture the exit code but don't re-raise
        exit_code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        exit_code = 1
        traceback.print_exc(file=sys.stdout)
    finally:
        sys.stdout = real_stdout

    if exit_code != 0 and smtp_cfg:
        output  = buf.getvalue()
        subject = "nomi-archive run failed"
        try:
            import socket
            subject = f"nomi-archive failed on {socket.gethostname()}"
        except Exception:
            pass
        _send_error_email(smtp_cfg, subject, output)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
