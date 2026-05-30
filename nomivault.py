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

import argparse
import configparser
import io
import json
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


def _cache_path(safe_name: str) -> Path:
    return OUTPUT_DIR / f"{safe_name}.json"


def load_cache(safe_name: str) -> dict:
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
    }
    path = _cache_path(safe_name)
    if not path.exists():
        return empty
    try:
        return {**empty, **json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return empty


def save_cache(safe_name: str, nomi: dict, messages: list,
               voice_calls: list | None = None,
               transcripts: dict | None = None,
               numeric_nomi_id: int | None = None,
               mind_map_terms: list | None = None,
               selfies: list | None = None) -> None:
    """Persist messages, voice-call data, mind-map terms, and selfie metadata."""
    path = _cache_path(safe_name)
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

def fetch_mind_map_category(nomi_id, category: str, token: str) -> list:
    """Fetch every page of memory-terms for one category."""
    terms: list = []
    page = 1
    while True:
        data = beta_api_get(
            f"/mind-maps/nomis/{nomi_id}/memory-terms", token,
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


def fetch_mind_map_term_detail(nomi_id, term_uuid: str, token: str) -> dict | None:
    """Fetch the full detail (including dossier HTML) for a single memory term."""
    data = beta_api_get(f"/mind-maps/nomis/{nomi_id}/memory-terms/{term_uuid}", token)
    if not data or data == "AUTH_FAILED":
        return None
    return data


def fetch_full_mind_map(nomi_id, token: str,
                        cached_terms: dict) -> dict[str, list]:
    """Fetch all three categories, pulling fresh dossiers only for new/updated terms.

    *cached_terms* maps term UUID → previously saved full-term dict.
    Returns a dict mapping category name → list of full term dicts.
    """
    result: dict[str, list] = {}
    for category, label in MIND_MAP_CATEGORIES.items():
        print(f"  Mind map – {label}: ", end="", flush=True)
        terms = fetch_mind_map_category(nomi_id, category, token)
        full_terms: list = []
        fetched = 0
        for term in terms:
            uid = term["uuid"]
            cached = cached_terms.get(uid)
            # Re-fetch detail if never cached, or if the AI edited it since
            if (not cached
                    or "dossier" not in cached
                    or cached.get("aiEdited") != term.get("aiEdited")):
                detail = fetch_mind_map_term_detail(nomi_id, uid, token)
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

def fetch_selfies(nomi_id, token: str) -> list:
    """Return metadata for every non-hidden, completed selfie (all pages)."""
    all_selfies: list = []
    page = 1
    while True:
        data = beta_api_get(
            f"/nomis/{nomi_id}/medias", token,
            extra={"page": page, "withCharacterImages": "true"},
        )
        if not data or data == "AUTH_FAILED":
            break
        batch = [
            m for m in data.get("medias", [])
            if m.get("mediaType") in ("Selfie", "VideoRequest", "CharacterImage")
            and not m.get("hidden")
            # CharacterImage has no "completed" field — it's always available
            and (m.get("completed") or m.get("mediaType") == "CharacterImage")
        ]
        all_selfies.extend(batch)
        if page >= data.get("maxPages", 1):
            break
        page += 1
        time.sleep(0.2)
    return all_selfies


def _selfie_filename(selfie: dict, safe_name: str) -> str:
    """Return a descriptive filename for a selfie: <NomiName>_<date>_<time>_<id8>.webp"""
    completed = selfie.get("completed", "")
    try:
        dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        ts = dt.strftime("%Y-%m-%d_%H-%M-%S")
    except Exception:
        ts = "unknown"
    return f"{safe_name}_{ts}_{selfie['id'][:8]}.webp"


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


def download_selfie_image(selfie: dict, selfies_dir: Path, token: str,
                          safe_name: str) -> str | None:
    """Download a single selfie .webp to *selfies_dir* using a descriptive filename.

    Returns the filename string on success (new download or already present),
    or None if the download failed.
    """
    img_id   = selfie["id"]
    req_id   = selfie["selfieRequestId"]
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


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _msg_timestamp(msg: dict) -> str:
    for key in ("sent", "sentAt", "timestamp", "createdAt", "created"):
        if msg.get(key):
            return msg[key]
    return ""


def _format_ts(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
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
    return f'<time class="{cls}" data-utc="{raw}">{fallback}</time>'


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
        f'<a href="index.html" class="nav-btn">&#8962; Home</a>',
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
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="apple-touch-icon" href="favicon.png">
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
                        mind_map_link: str | None = None) -> str:
    """Render a self-contained photo gallery page for all downloaded selfies."""
    nomi_name   = nomi["name"]
    export_date = datetime.now().strftime("%b %d, %Y %I:%M %p")

    # Oldest first
    sorted_selfies = sorted(
        selfies, key=lambda s: s.get("completed", "")
    )

    cards: list[str] = []
    for item in sorted_selfies:
        media_type = item.get("mediaType", "Selfie")
        ts_elem    = _ts_html(item.get("completed", ""), "gal-ts")

        if media_type == "VideoRequest":
            preview_fn = item.get("preview_filename") or _video_filename(item, safe_name)[0]
            video_fn   = item.get("video_filename")   or _video_filename(item, safe_name)[1]
            img_src    = f"media/{safe_name}/{preview_fn}"
            video_src  = f"media/{safe_name}/{video_fn}"
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
            img_src   = f"media/{safe_name}/{filename}"
            type_tag  = '<span class="gal-type gal-type-char">Character Image</span>'
            cards.append(
                f'<div class="gal-card" data-src="{img_src}" onclick="openLb(this)">'
                f'  <img src="{img_src}" alt="Character image" loading="lazy">'
                f'  <div class="gal-info">{type_tag}</div>'
                f'</div>'
            )
        else:
            filename = item.get("local_filename") or _selfie_filename(item, safe_name)
            img_src  = f"media/{safe_name}/{filename}"
            s_type   = _html_escape(item.get("type") or "")
            type_tag = f'<span class="gal-type">{s_type}</span>' if s_type else ""
            cards.append(
                f'<div class="gal-card" data-src="{img_src}" onclick="openLb(this)">'
                f'  <img src="{img_src}" alt="Selfie" loading="lazy">'
                f'  <div class="gal-info">{type_tag}{ts_elem}</div>'
                f'</div>'
            )

    grid = "\n".join(cards) if cards else '<p class="gal-empty">No media downloaded yet.</p>'

    photo_count = sum(1 for s in selfies if s.get("mediaType") != "VideoRequest")
    video_count = sum(1 for s in selfies if s.get("mediaType") == "VideoRequest")
    count_parts: list[str] = []
    if photo_count:
        count_parts.append(f'{photo_count} photo{"s" if photo_count != 1 else ""}')
    if video_count:
        count_parts.append(f'{video_count} video{"s" if video_count != 1 else ""}')
    count_str = " &middot; ".join(count_parts) if count_parts else "0 items"

    nav = _nav_bar("media", chat_link, mind_map_link, None)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="apple-touch-icon" href="favicon.png">
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
    .gal-type-video {{ color: #e9a020; }}
    .gal-type-char  {{ color: #a060e0; }}
    .gal-ts  {{ font-size: 0.68rem; color: #556; }}
    .gal-empty {{ color: #445; font-style: italic; padding: 40px 0; text-align: center; }}
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
    <div class="gal-grid">
{grid}
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
      name, safe_name, first_msg_ts (ISO-8601 str), char_image_src (path or None)
    Entries must already be sorted before calling this function.
    """
    export_date = datetime.now().strftime("%b %d, %Y %I:%M %p")
    count = len(entries)

    cards: list[str] = []
    for e in entries:
        name     = _html_escape(e["name"])
        href     = e["safe_name"] + ".html"
        img_src  = e.get("char_image_src") or ""
        first_ts = e.get("first_msg_ts", "")

        if img_src:
            bg_style = (f"background-image:url('{img_src}');"
                        f"background-size:cover;background-position:center top;")
        else:
            bg_style = "background:linear-gradient(135deg,#16213e 0%,#0f3460 100%);"

        # "Since …" date label — JS will localise; Python fallback is date-only UTC
        if first_ts:
            try:
                dt = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                fallback_date = dt.strftime("%b %d, %Y")
            except Exception:
                fallback_date = first_ts[:10]
            since = (f'<div class="card-date">'
                     f'<time data-utc="{first_ts}">Since {fallback_date}</time>'
                     f'</div>')
        else:
            since = ""

        cards.append(
            f'<a href="{href}" class="nomi-card">'
            f'  <div class="card-bg" style="{bg_style}"></div>'
            f'  <div class="card-overlay"></div>'
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
    .no-nomis {{ color: #445; font-style: italic; text-align: center; padding: 60px 0; }}
    footer {{ text-align: center; padding: 32px 16px; color: #555; font-size: 0.78rem; }}
    @media (max-width: 640px) {{
      header {{ padding: 16px 18px; }}
      header h1 {{ font-size: 1.3rem; }}
      main {{ padding: 18px 12px 60px; }}
      .nomi-grid {{ grid-template-columns: 1fr; gap: 14px; }}
      .nomi-card {{ height: auto; aspect-ratio: 1 / 1; }}
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
                safe_name: str = "") -> str:
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

    # Build a merged timeline of messages and selfies, sorted by timestamp
    timeline: list[tuple[str, str, dict]] = []
    for msg in messages:
        timeline.append(("msg", _msg_timestamp(msg), msg))
    for s in (selfies or []):
        if s.get("mediaType", "Selfie") == "Selfie":
            timeline.append(("selfie", s.get("completed", ""), s))
    timeline.sort(key=lambda x: x[1])

    bubbles: list[str] = []
    for kind, _ts, data in timeline:

        if kind == "selfie":
            selfie   = data
            filename = selfie.get("local_filename") or _selfie_filename(selfie, safe_name)
            img_src  = f"media/{safe_name}/{filename}"
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
        sender_raw = ""
        for key in ("role", "sender", "from", "type", "senderType"):
            if msg.get(key) is not None:
                sender_raw = str(msg[key]).lower()
                break

        is_user = sender_raw in ("user", "human", "me", "0") or sender_raw.startswith("user")
        sender_label = "You" if is_user else nomi_name
        bubble_cls = "user-bubble" if is_user else "nomi-bubble"
        side_cls = "user-side" if is_user else "nomi-side"

        ts_raw    = _msg_timestamp(msg)
        safe_text = _html_escape(text)
        ts_html   = f'<div class="timestamp">{_ts_html(ts_raw)}</div>' if ts_raw else ""

        bubbles.append(
            f'        <div class="message-row {side_cls}">\n'
            f'          <div class="bubble {bubble_cls}">\n'
            f'            <div class="sender-name">{sender_label}</div>\n'
            f'            <div class="message-text">{safe_text}</div>\n'
            f'            {ts_html}\n'
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
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="apple-touch-icon" href="favicon.png">
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
{_NAV_CSS}
    .chat-container {{
      max-width: 760px;
      margin: 0 auto;
      padding: 24px 16px 80px;
    }}
    .message-row {{ display: flex; margin-bottom: 14px; }}
    .nomi-side   {{ justify-content: flex-start; }}
    .user-side   {{ justify-content: flex-end;   }}
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

    confirmed_pattern: str | None = None   # used only in no-token mode
    landing_entries:   list[dict] = []

    for nomi in nomis:
        name      = nomi["name"]
        uuid      = nomi["uuid"]
        safe_name = _safe_name(name)
        print(f"Processing: {name}  ({uuid})")

        # --- load local cache -------------------------------------------
        cache            = {} if args.full else load_cache(safe_name)
        cached_messages  = cache.get("messages",        [])
        cached_vc        = cache.get("voice_calls",     [])
        cached_tx        = cache.get("transcripts",     {})
        cached_num_id    = cache.get("numeric_nomi_id", None)
        is_incremental   = bool(cached_messages)

        # ================================================================
        # Path A: beta.nomi.ai  (messages + voice calls in one response)
        # ================================================================
        if args.token:
            nomi_id = cached_num_id or args.nomi_id or uuid
            if nomi_id == uuid and not args.nomi_id:
                print(f"  ⚠  Skipping {name} — numeric nomi ID not yet cached.")
                print(f"     To archive this Nomi, run the script once with:")
                print(f"       python3 nomivault.py --key KEY --token TOKEN --nomi-id XXXXXXX")
                print(f"     where XXXXXXX is the number in the URL when viewing this")
                print(f"     Nomi on beta.nomi.ai:  beta.nomi.ai/nomis/XXXXXXX")
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
        chat_lnk       = f"{safe_name}.html"
        mm_lnk         = f"{safe_name}-mind-map.html"    if (using_token or (OUTPUT_DIR / f"{safe_name}-mind-map.html").exists())    else None
        selfies_lnk    = f"{safe_name}-media.html"        if (using_token or (OUTPUT_DIR / f"{safe_name}-media.html").exists())        else None

        # --- mind map (token path only) ---------------------------------
        all_mm_terms: list = cache.get("mind_map_terms", [])
        if using_token:
            cached_mm_lookup = {t["uuid"]: t for t in all_mm_terms}
            mm_by_category   = fetch_full_mind_map(cached_num_id, args.token, cached_mm_lookup)
            all_mm_terms     = [t for terms in mm_by_category.values() for t in terms]

            mm_html  = render_mind_map_html(nomi, mm_by_category,
                                             chat_link=chat_lnk,
                                             selfies_link=selfies_lnk)
            mm_path  = OUTPUT_DIR / f"{safe_name}-mind-map.html"
            mm_path.write_text(mm_html, encoding="utf-8")
            print(f"  Mind map → {mm_path}")

        # --- selfies (token path only) ----------------------------------
        all_selfies: list = cache.get("selfies", [])
        if using_token:
            print("  Fetching media list ...", end=" ", flush=True)
            fresh_selfies     = fetch_selfies(cached_num_id, args.token)
            cached_selfie_ids = {s["id"] for s in all_selfies}
            new_selfies       = [s for s in fresh_selfies if s["id"] not in cached_selfie_ids]
            all_selfies       = all_selfies + new_selfies
            print(f"{len(fresh_selfies)} total, {len(new_selfies)} new")

            selfies_dir = OUTPUT_DIR / "media" / safe_name
            selfies_dir.mkdir(parents=True, exist_ok=True)

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
                else:
                    if "local_filename" not in s or not (selfies_dir / s["local_filename"]).exists():
                        fn = download_selfie_image(s, selfies_dir, args.token, safe_name)
                        if fn:
                            s["local_filename"] = fn
                            newly_dl += 1
            if newly_dl:
                print(f"  Downloaded {newly_dl} new media file(s)")

            gallery_html = render_gallery_html(nomi, all_selfies, safe_name,
                                               chat_link=chat_lnk,
                                               mind_map_link=mm_lnk)
            gallery_path = OUTPUT_DIR / f"{safe_name}-media.html"
            gallery_path.write_text(gallery_html, encoding="utf-8")
            print(f"  Media gallery → {gallery_path}")

        # --- collect landing page entry ---------------------------------
        char_imgs = [s for s in all_selfies
                     if s.get("mediaType") == "CharacterImage"
                     and s.get("local_filename")]
        char_img_src = (
            f"media/{safe_name}/{char_imgs[0]['local_filename']}"
            if char_imgs else None
        )
        first_msg_ts = min((_msg_timestamp(m) for m in merged), default="") if merged else ""
        landing_entries.append({
            "name":           name,
            "safe_name":      safe_name,
            "first_msg_ts":   first_msg_ts,
            "char_image_src": char_img_src,
        })

        # --- persist cache ----------------------------------------------
        save_cache(safe_name, nomi, merged,
                   voice_calls=all_vc,
                   transcripts=all_tx,
                   numeric_nomi_id=cached_num_id,
                   mind_map_terms=all_mm_terms,
                   selfies=all_selfies)

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
        )
        out_path = OUTPUT_DIR / f"{safe_name}.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"  Saved → {out_path}\n")

    # --- render landing page + PWA support files -----------------------
    if landing_entries:
        landing_entries.sort(key=lambda e: e["first_msg_ts"])
        landing_html = render_landing_html(landing_entries)
        landing_path = OUTPUT_DIR / "index.html"
        landing_path.write_text(landing_html, encoding="utf-8")
        print(f"Landing page → {landing_path}")

        (OUTPUT_DIR / "manifest.json").write_text(_PWA_MANIFEST, encoding="utf-8")
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
        help="Numeric nomi ID used by beta.nomi.ai (e.g. 1234567890). "
             "Required on the very first --token run; stored in the cache afterwards. "
             "Find it in the URL when viewing your Nomi: beta.nomi.ai/nomis/XXXXXXX",
    )
    parser.add_argument(
        "--messages-url",
        help='Override the api.nomi.ai message endpoint pattern (no-token mode only), '
             'e.g. "/v1/nomis/{uuid}/chats"',
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Ignore the local cache and re-download the entire conversation history.",
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
