# NomiVault

A Python script that exports your [Nomi.ai](https://nomi.ai) conversation history to self-contained HTML files — one chat transcript per Nomi, plus an optional mind map page and media gallery. No external dependencies beyond the Python standard library (one optional package for richer mind map rendering).

---

## Donation

Find this script useful?  Consider making a donation through PayPal at [toddkarwoski.com/buymeacoffee](https://www.toddkarwoski.com/buymeacoffee)

---

## Screenshots

![nomi-archive preview](screenshots/preview.gif)

<details>
<summary>Individual screenshots (desktop + mobile)</summary>

| | Desktop | Mobile |
|---|---|---|
| **Landing page** | ![](screenshots/01-landing-desktop.png) | ![](screenshots/02-landing-mobile.png) |
| **Chat transcript** | ![](screenshots/03-chat-desktop.png) | ![](screenshots/04-chat-mobile.png) |
| **Mind map** | ![](screenshots/05-mindmap-desktop.png) | ![](screenshots/06-mindmap-mobile.png) |
| **Media gallery** | ![](screenshots/07-media-desktop.png) | ![](screenshots/08-media-mobile.png) |

</details>

---

## Features

- **Full chat transcript** — every message, paginated from the beginning of history
- **Voice call transcripts** — inline in the chat, visually distinct from text messages
- **Mind map archive** — Lore, Topics, and Goals sections with dossier details rendered from markdown
- **Media gallery** — selfies, character images, and videos all in one page with a tabbed layout separating Nomi's media from your uploads
  - Selfies and character images downloaded as `.webp` files with a click-to-enlarge lightbox
  - Videos downloaded as `.mp4` with a preview thumbnail and play button; clicking autoplays in a lightbox
  - Videos are excluded from the chat transcript and appear only in the media gallery
- **User-uploaded media** — images and videos you send to your Nomi are downloaded and displayed inline in the chat transcript; video thumbnails are shown with a play-button overlay
- **Incremental updates** — re-running the script only downloads new messages, calls, and media; existing history is preserved in a JSON cache
- **Local timestamps** — all message times are converted to your browser's local timezone automatically
- **Cross-page navigation** — chat, mind map, and media pages all link to each other
- **Landing page** — `index.html` shows all archived Nomis as cards with their character image as background, sorted by first chat date
- **No external server needed** — HTML files open directly in any browser

---

## Requirements

- Python 3.9 or later
- A Nomi.ai account with at least one Nomi

**Optional** (enables markdown rendering in mind map dossiers):

```bash
pip install markdown
```

Without this package the script still works; dossier text is shown as plain pre-formatted text instead.

---

## Getting Your Credentials

You need up to three values depending on which features you want.

### 1. API Key (required)

1. Open [beta.nomi.ai](https://beta.nomi.ai) and sign in
2. Click your profile avatar → **Integration**
3. Copy the API key shown there

### 2. Session Token (required for voice transcripts, mind map, and media gallery)

The session token unlocks the richer internal API, which is needed for voice call transcripts, mind map data, and media downloads.

1. Open [beta.nomi.ai](https://beta.nomi.ai) in Chrome and sign in
2. Press **F12** to open DevTools
3. Go to the **Application** tab
4. In the left sidebar expand **Storage → Cookies → https://beta.nomi.ai**
5. Find the cookie named `__Secure-next-auth.session-token`
6. Copy its **Value** (a long string)

> **Note:** Session tokens expire when you sign out. If the script starts returning auth errors, grab a fresh token using the steps above.

### 3. Numeric Nomi ID (required for the first `--token` run per Nomi)

1. Open [beta.nomi.ai](https://beta.nomi.ai) and click into any conversation
2. Look at the URL — it will look like `beta.nomi.ai/nomis/1234567890`
3. The number at the end is your Nomi's numeric ID

The script stores this in its cache after the first successful run, so you only need to provide it once per Nomi.

**Multiple Nomis:** If you have more than one Nomi, run the script once per Nomi on the first run, each time passing that Nomi's `--nomi-id`. After that, all Nomis are handled automatically in a single run.

> **Do not add `--full`** when archiving a new Nomi — a Nomi with no existing cache is downloaded in full automatically. `--full` resets the cache for *every* Nomi processed that run, not just the new one, which can strip an already-archived Nomi of its cached numeric ID and cause its data to get mixed up with another Nomi's. If you need to force a clean re-download of one already-archived Nomi, delete that Nomi's `<Name>.json` cache file instead of passing `--full`.

---

## Usage

### Basic — text messages only

```bash
python3 nomivault.py --key YOUR_API_KEY
```

Downloads text chat history for every Nomi on your account. Voice transcripts, mind map, and media gallery are not included.

### Full — messages, voice transcripts, mind map, and media gallery

```bash
python3 nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN --nomi-id 1234567890
```

On every subsequent run the `--nomi-id` argument can be omitted because the ID is saved in the cache.

### Incremental update (after first run)

```bash
python3 nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN
```

Only new messages and voice calls since the last run are downloaded. Existing history is merged from the local cache.

### Re-download everything from scratch

```bash
python3 nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN --full
```

Ignores the local cache and fetches the entire history again — for **every** Nomi processed this run, not just one. To force a clean re-download of a single already-archived Nomi instead, delete that Nomi's `<Name>.json` cache file and run normally without `--full`.

### Save to a custom directory

```bash
python3 nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN --output ~/Documents/nomi
```

The directory is created automatically if it does not exist.

---

## Command Line Arguments

| Argument | Required | Description |
|---|---|---|
| `--key KEY` | Yes | Your Nomi.ai API key (Profile → Integration) |
| `--token TOKEN` | No* | `__Secure-next-auth.session-token` cookie value from beta.nomi.ai. Required for voice transcripts, mind map, and media gallery. |
| `--nomi-id ID` | No* | Numeric Nomi ID from the beta.nomi.ai URL (e.g. `1234567890`). Required on the first `--token` run per Nomi; cached afterwards. |
| `--output DIR` | No | Directory to write all output files. Defaults to an `output/` folder next to `nomivault.py`. |
| `--full` | No | Ignore the local cache and re-download the entire conversation history for every Nomi processed this run (not just one). Not needed for a new Nomi's first run — that happens automatically. To force a clean re-download of a single Nomi, delete its `<Name>.json` cache file instead. |
| `--messages-url PATTERN` | No | Override the message endpoint pattern for the public API (no-token mode only), e.g. `"/v1/nomis/{uuid}/chats"`. |
| `--silent` | No | Suppress all terminal output. Run output is still captured and included in the error email if SMTP is configured. |
| `--smtp-config FILE` | No | Path to an INI file with SMTP settings for error-notification emails. Defaults to `smtp.ini` next to `nomivault.py` when that file exists. |

---

## Output Files

The script writes files to the output directory:

| File | Description |
|---|---|
| `index.html` | Landing page — card grid linking to every archived Nomi |
| `<NomiName>.html` | Full chat transcript with voice call transcripts inline |
| `<NomiName>-mind-map.html` | Mind map with Lore, Topics, and Goals sections |
| `<NomiName>-media.html` | Media gallery with selfies, character images, and videos |
| `<NomiName>.json` | Cache file used for incremental updates — do not delete |
| `media/<NomiName>/*.webp` | Downloaded selfie images, character images, video preview thumbnails, and user-uploaded images |
| `media/<NomiName>/*.mp4` | Downloaded video files |
| `media/<NomiName>/*_upload_*` | Downloaded user-uploaded files (images and video thumbnails) |

Open any `.html` file directly in Chrome or Edge. No web server is needed.

---

## Mind Map Dossiers

Each mind map entry can have a detailed dossier. These are stored as markdown in the Nomi.ai API and rendered to HTML in the output. Install the `markdown` package for full rendering:

```bash
pip install markdown
```

Without it, dossier text is shown as plain formatted text and all other mind map features still work normally.

---

## Keeping the Archive Up to Date

Running the script regularly adds new messages without re-downloading old ones. A simple approach on Windows with WSL:

```bash
# Add to a scheduled task or run manually whenever you want an update
python3 /path/to/nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN
```

---

## Running as a Scheduled / Cron Job

The script is designed to run unattended. If anything goes wrong it exits with a non-zero code **and** can send you an email with the full run output so you can see exactly what failed.

### 1. Set up email notifications (optional)

Copy `smtp.ini.example` to `smtp.ini` (in the same folder as `nomivault.py`) and fill in your SMTP credentials:

```bash
cp smtp.ini.example smtp.ini
nano smtp.ini   # or open in any text editor
```

`smtp.ini` is excluded from git so your credentials are never committed.

**Gmail tip:** use an [App Password](https://support.google.com/accounts/answer/185833) rather than your account password. Generate one at Google Account → Security → App passwords.

### 2. Run silently

Pass `--silent` to suppress all terminal output. The run output is still captured internally and included in any error email:

```bash
python3 nomivault.py --key YOUR_API_KEY --token YOUR_SESSION_TOKEN --silent
```

### 3. Schedule the run

**Linux / macOS cron** — edit with `crontab -e`:

```cron
# Run every day at 3 AM
0 3 * * * /usr/bin/python3 /path/to/nomivault.py --key YOUR_KEY --token YOUR_TOKEN --silent
```

**Windows Task Scheduler** — create a basic task that runs:

```
Program:   python.exe
Arguments: C:\path\to\nomivault.py --key YOUR_KEY --token YOUR_TOKEN --silent
```

### What happens on failure

- The script exits with code `1` on any unrecoverable error.
- If `smtp.ini` is present (or `--smtp-config FILE` is passed), a notification email is sent to the address in the `to` field. The email subject includes your hostname and the body contains the complete run output.
- If no SMTP config is found, the non-zero exit code alone signals the failure to the scheduler.

---

## Troubleshooting

### Auth errors / HTTP 401 or 403
Your session token has expired. Grab a fresh one from DevTools (see [Getting Your Credentials](#getting-your-credentials)).

### HTTP 400 `InvalidRouteParams`
The numeric Nomi ID is wrong or missing. Check the URL on beta.nomi.ai and pass the correct value with `--nomi-id`.

### A Nomi is skipped with "numeric nomi ID not yet cached"
The script needs the numeric ID on the very first run for each Nomi. Find it in the URL on beta.nomi.ai (`beta.nomi.ai/nomis/XXXXXXX`) and run once with `--nomi-id XXXXXXX`. The ID is cached automatically and subsequent runs need no flag.

### Mind map, voice transcripts, or media not appearing
These require `--token`. Make sure the session token is current and that `--nomi-id` was provided on the first run.

### Script stops downloading messages early
If the script detects that the API cursor is not advancing it stops automatically to avoid an infinite loop. Run with `--full` to force a complete re-download.

### `BETA_AV` version errors
Nomi.ai occasionally updates their internal API version string. If you see unexpected 400 errors on the beta API, open DevTools on beta.nomi.ai, look at any network request to `beta.nomi.ai/api/...`, and find the `av=` query parameter. Update the `BETA_AV` constant near the top of `nomivault.py` to match.

---

## Privacy

The `output/` directory is excluded from this repository via `.gitignore`. Your conversation data, HTML exports, and JSON cache files are never committed to git. Keep that folder private.
