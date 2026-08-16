"""
Auto news fetch & post module (keeps app.py smaller).

Watches Bluesky handlers for *new* posts with media, then posts to a chosen
Zernio Instagram account.

Usage (CLI):
  python auto_news_poster.py once
  python auto_news_poster.py loop --interval 15  # CHANGED: default 300 → 15

Or via Flask routes registered by register_auto_news_routes(app).
Config is stored in Postgres table auto_news_config + auto_news_seen.

------------------------------------------------------------------------------
IMPORTANT: When enabled, Auto News only fetches posts created AFTER the
config was enabled. Old posts are skipped automatically.
------------------------------------------------------------------------------

PERFORMANCE NOTES (this version):
- DB connections come from a pooled connection instead of a fresh TCP/TLS
  handshake on every single query (huge win — the old version opened a new
  connection for every uri_seen()/mark_seen() call, i.e. dozens per cycle).
- init_auto_tables() only actually hits the DB once per process, not once
  per heartbeat tick.
- The Bluesky client is cached per-handle and reused across cycles instead
  of doing a full login() every single run; it only re-authenticates if a
  fetch actually fails.
- uri_seen/mark_seen are now batched: one SELECT of all seen URIs up front,
  one bulk INSERT at the end, instead of 1 query per post per direction.
- The Zernio fallback HTTP calls now go through a requests.Session with
  automatic retries/backoff on transient failures (429/500/502/503/504).

CHANGELOG:
- v2: Changed default poll_interval_sec from 300 → 15 seconds for faster
  auto-posting (posts appear within ~20-30 seconds instead of 5+ minutes).
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# DB helpers (reuse same DATABASE_URL as app)
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_0URQHn2lKXeh@ep-divine-rice-ayb8kut7-pooler.c-5.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
)

ZERNIO_API_KEY = os.environ.get(
    "ZERNIO_API_KEY",
    "sk_7b484f17f3be9f7faa5cabc822983e5760cf4256db3ffa565fb3befdfb6535a6",
)
ZERNIO_BASE_URL = "https://zernio.com/api/v1"
SCHEDULE_TIMEZONE = "Africa/Nairobi"

# Global scheduler state (module-level so it survives across requests, and so
# we don't spin up duplicate schedulers if register_auto_news_routes gets
# called more than once, e.g. under a reloader).
_scheduler = None
_scheduler_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Pooled DB connections
# ---------------------------------------------------------------------------
# A fresh psycopg2.connect() is a full TCP+TLS handshake to a remote Neon
# endpoint — expensive, and the old code paid that cost on EVERY query
# (get_config, uri_seen x N posts, mark_seen x N posts, set_last_run...).
# A small pool reuses live connections across calls.

_db_pool = None
_db_pool_lock = threading.Lock()


def _get_db_pool():
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                import psycopg2.pool
                _db_pool = psycopg2.pool.ThreadedConnectionPool(1, 50, DATABASE_URL)  # 10 → 50= psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)
    return _db_pool


def get_db():
    """Get a pooled connection. Always pair with release_db(conn) — use
    try/finally, not conn.close()."""
    return _get_db_pool().getconn()


def release_db(conn):
    """Return a connection to the pool instead of closing the socket."""
    if conn is None:
        return
    try:
        _get_db_pool().putconn(conn)
    except Exception:
        # Pool may not be initialized yet in some edge case — fail safe.
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared HTTP session with retries (Zernio fallback calls)
# ---------------------------------------------------------------------------

def _make_retry_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PUT"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_http = _make_retry_session()


def init_auto_tables():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_news_config (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL DEFAULT 'default',
                enabled BOOLEAN DEFAULT FALSE,
                handler_handle TEXT NOT NULL,
                account_id TEXT NOT NULL,
                platform TEXT DEFAULT 'instagram',
                content_type TEXT DEFAULT 'feed',
                poll_interval_sec INTEGER DEFAULT 15,  -- 🔥 FIXED: was 300 (5 min), now 15 seconds
                media_only BOOLEAN DEFAULT TRUE,
                include_reposts BOOLEAN DEFAULT FALSE,
                include_replies BOOLEAN DEFAULT FALSE,
                caption_template TEXT DEFAULT '{text}',
                bluesky_handle TEXT,
                bluesky_app_password TEXT,
                last_run_at TIMESTAMP,
                last_error TEXT,
                last_result TEXT,
                enabled_at TIMESTAMP,  -- tracks when config was enabled
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_news_seen (
                id SERIAL PRIMARY KEY,
                config_id INTEGER REFERENCES auto_news_config(id) ON DELETE CASCADE,
                uri TEXT NOT NULL,
                posted BOOLEAN DEFAULT FALSE,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(config_id, uri)
            )
            """
        )
        # Migrate: add missing columns if they don't exist
        try:
            cur.execute("ALTER TABLE auto_news_config ADD COLUMN IF NOT EXISTS last_result TEXT")
            cur.execute("ALTER TABLE auto_news_config ADD COLUMN IF NOT EXISTS enabled_at TIMESTAMP")
        except Exception as mig_e:
            print(f"auto_news_config migrate: {mig_e}")
        conn.commit()
        cur.close()
        print("✅ Auto-news tables initialized")
    finally:
        release_db(conn)


# Cache so init_auto_tables() (CREATE TABLE / ALTER TABLE round-trips) only
# actually runs once per process instead of once per heartbeat tick.
_tables_ready = False
_tables_ready_lock = threading.Lock()


def ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    with _tables_ready_lock:
        if _tables_ready:
            return
        init_auto_tables()
        _tables_ready = True


# ---------------------------------------------------------------------------
# Bluesky fetch (lightweight, no full atproto session store)
# ---------------------------------------------------------------------------

def bluesky_login(handle: str, app_password: str):
    from atproto import Client
    client = Client()
    client.login(handle, app_password)
    return client


# Cache authenticated Bluesky clients per (handle, password) so we don't pay
# a full login handshake on every single cycle — only re-auth when a fetch
# actually fails (expired session, revoked app password, etc).
_bsky_clients: dict[str, tuple] = {}
_bsky_clients_lock = threading.Lock()


def get_bluesky_client(handle: str, app_password: str, force_new: bool = False):
    if not force_new:
        with _bsky_clients_lock:
            entry = _bsky_clients.get(handle)
        if entry and entry[1] == app_password:
            return entry[0]

    client = bluesky_login(handle, app_password)
    with _bsky_clients_lock:
        _bsky_clients[handle] = (client, app_password)
    return client


def fetch_author_feed(client, actor: str, limit: int = 20):
    """Return list of post dicts with uri, text, images, created_at, is_repost, is_reply."""
    posts = []
    try:
        feed = client.get_author_feed(actor=actor, limit=limit)
        for item in feed.feed:
            post = item.post
            record = post.record
            text = getattr(record, "text", "") or ""
            uri = post.uri
            created = str(getattr(record, "created_at", "") or "")
            is_repost = bool(getattr(item, "reason", None))
            reply = getattr(record, "reply", None)
            is_reply = reply is not None

            images = []
            # Prefer hydrated embed on post
            post_embed = getattr(post, "embed", None)
            if post_embed is not None:
                # images view
                imgs = getattr(post_embed, "images", None)
                if imgs:
                    for im in imgs:
                        full = getattr(im, "fullsize", None) or getattr(im, "thumb", None)
                        thumb = getattr(im, "thumb", None) or full
                        if full:
                            images.append({"url": full, "thumb": thumb})
                # external with thumb
                external = getattr(post_embed, "external", None)
                if external and not images:
                    thumb = getattr(external, "thumb", None)
                    if thumb:
                        images.append({"url": thumb, "thumb": thumb})
                # recordWithMedia
                media = getattr(post_embed, "media", None)
                if media is not None:
                    imgs = getattr(media, "images", None)
                    if imgs:
                        for im in imgs:
                            full = getattr(im, "fullsize", None) or getattr(im, "thumb", None)
                            if full:
                                images.append({"url": full, "thumb": getattr(im, "thumb", None) or full})

            author = getattr(getattr(post, "author", None), "handle", None) or actor
            posts.append({
                "uri": uri,
                "text": text,
                "images": images,
                "created_at": created,
                "is_repost": is_repost,
                "is_reply": is_reply,
                "author": author,
                "has_media": len(images) > 0,
            })
    except Exception as e:
        print(f"fetch_author_feed error: {e}")
        traceback.print_exc()
        raise
    return posts


# ---------------------------------------------------------------------------
# Post via Zernio — import from app when available, else minimal local
# ---------------------------------------------------------------------------

def post_image_to_account(image_url: str, caption: str, account_id: str, platform: str = "instagram", content_type: str = "feed"):
    """Try app.post_to_zernio; fallback simple call."""
    try:
        import importlib
        for name in ("app", "backend_app"):
            try:
                mod = importlib.import_module(name)
                if hasattr(mod, "post_to_zernio"):
                    return mod.post_to_zernio(
                        image_url=image_url,
                        caption=caption,
                        platforms=[platform],
                        content_type=content_type,
                        account_ids=[account_id],
                    )
            except Exception:
                continue
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Fallback: direct Zernio API call (uses retry-enabled session)
    try:
        headers = {
            'Authorization': f'Bearer {ZERNIO_API_KEY}',
            'Content-Type': 'application/json'
        }

        # Download image
        img_response = _http.get(image_url, timeout=30)
        if img_response.status_code != 200:
            return {"success": False, "error": f"Failed to download image: {img_response.status_code}"}

        # Upload to Zernio
        presign_payload = {
            "filename": "post.jpg",
            "contentType": "image/jpeg"
        }
        presign_response = _http.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=headers,
            json=presign_payload,
            timeout=30
        )

        if presign_response.status_code not in [200, 201]:
            return {"success": False, "error": f"Presign failed: {presign_response.text}"}

        data = presign_response.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')

        if not upload_url or not public_url:
            return {"success": False, "error": "Missing upload URL"}

        upload_response = _http.put(
            upload_url,
            headers={'Content-Type': 'image/jpeg'},
            data=img_response.content,
            timeout=60
        )

        if upload_response.status_code not in [200, 201, 204]:
            return {"success": False, "error": f"Upload failed: {upload_response.text}"}

        payload = {
            "mediaItems": [{
                "type": "image",
                "url": public_url
            }],
            "platforms": [{
                "platform": platform,
                "accountId": account_id
            }],
            "content": caption[:2200] if len(caption) > 2200 else caption,
            "publishNow": True
        }

        post_response = _http.post(
            f"{ZERNIO_BASE_URL}/posts",
            headers=headers,
            json=payload,
            timeout=60
        )

        if post_response.status_code in [200, 201]:
            return {"success": True, "post_id": post_response.json().get('post', {}).get('_id')}
        else:
            return {"success": False, "error": f"Post failed: {post_response.text}"}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Core job
# ---------------------------------------------------------------------------

def get_config(name: str = "default"):
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM auto_news_config WHERE name = %s", (name,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
            cur.close()
            if not row:
                return None
            return dict(zip(cols, row))
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] get_config DB error: {e}")
        return None


def get_all_configs(enabled_only: bool = False):
    """Return every saved config (optionally only the enabled ones)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        if enabled_only:
            cur.execute("SELECT * FROM auto_news_config WHERE enabled = TRUE")
        else:
            cur.execute("SELECT * FROM auto_news_config")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        cur.close()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        release_db(conn)


def get_enabled_config_names() -> list[str]:
    try:
        return [c.get("name", "default") for c in get_all_configs(enabled_only=True)]
    except Exception as e:
        print(f"[auto_news] get_enabled_config_names DB error: {e}")
        return []


def claim_due_config(name: str, min_interval_sec: int = 10) -> dict | None:
    """
    Atomically checks whether this ENABLED config is due for a run (based on
    its own poll_interval_sec) and, if so, claims it in the SAME statement by
    updating last_run_at. This makes the database the single source of truth
    for "who runs this cycle" — critical because Flask's dev-mode reloader
    runs the whole app TWICE (a parent watcher process + a child server
    process), and a production deploy may run multiple worker processes.
    Without a DB-level atomic claim, every process's own in-memory clock
    thinks it's the one that should run, and the same post gets sent twice.

    Postgres guarantees only one concurrent UPDATE...WHERE...RETURNING can
    match+claim a given row — every other concurrent caller gets 0 rows back
    and skips this cycle. Returns the claimed config dict, or None if it
    wasn't due yet (or someone else just claimed it).
    """
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE auto_news_config
                SET last_run_at = CURRENT_TIMESTAMP
                WHERE name = %s
                  AND enabled = TRUE
                  AND (
                        last_run_at IS NULL
                        OR last_run_at <= CURRENT_TIMESTAMP - make_interval(secs => GREATEST(poll_interval_sec, %s))
                      )
                RETURNING *
                """,
                (name, min_interval_sec),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
            conn.commit()
            cur.close()
            if not row:
                return None
            return dict(zip(cols, row))
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] claim_due_config DB error (skipping this cycle, will retry): {e}")
        return None


def save_config(cfg: dict):
    conn = get_db()
    try:
        cur = conn.cursor()

        # Check if config is being enabled (was disabled and now enabled)
        existing = get_config(cfg.get('name', 'default'))
        was_disabled = existing and not existing.get('enabled', False) and bool(cfg.get('enabled', False))

        # Get the current enabled_at if it exists
        current_enabled_at = None
        if existing:
            current_enabled_at = existing.get('enabled_at')

        # Determine the new enabled_at value
        # If enabling (was disabled -> now enabled), set to CURRENT_TIMESTAMP
        # Otherwise, keep the existing value
        if was_disabled:
            enabled_at_value = 'CURRENT_TIMESTAMP'
        elif current_enabled_at:
            enabled_at_value = f"'{current_enabled_at}'"
        else:
            enabled_at_value = 'CURRENT_TIMESTAMP'

        cur.execute(
            f"""
            INSERT INTO auto_news_config (
                name, enabled, handler_handle, account_id, platform, content_type,
                poll_interval_sec, media_only, include_reposts, include_replies,
                caption_template, bluesky_handle, bluesky_app_password,
                enabled_at, updated_at
            ) VALUES (
                %(name)s, %(enabled)s, %(handler_handle)s, %(account_id)s, %(platform)s, %(content_type)s,
                %(poll_interval_sec)s, %(media_only)s, %(include_reposts)s, %(include_replies)s,
                %(caption_template)s, %(bluesky_handle)s, %(bluesky_app_password)s,
                {enabled_at_value},
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (name) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                handler_handle = EXCLUDED.handler_handle,
                account_id = EXCLUDED.account_id,
                platform = EXCLUDED.platform,
                content_type = EXCLUDED.content_type,
                poll_interval_sec = EXCLUDED.poll_interval_sec,
                media_only = EXCLUDED.media_only,
                include_reposts = EXCLUDED.include_reposts,
                include_replies = EXCLUDED.include_replies,
                caption_template = EXCLUDED.caption_template,
                bluesky_handle = COALESCE(EXCLUDED.bluesky_handle, auto_news_config.bluesky_handle),
                bluesky_app_password = COALESCE(EXCLUDED.bluesky_app_password, auto_news_config.bluesky_app_password),
                enabled_at = CASE
                    WHEN EXCLUDED.enabled = TRUE AND auto_news_config.enabled = FALSE THEN CURRENT_TIMESTAMP
                    ELSE auto_news_config.enabled_at
                END,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            {
                "name": cfg.get("name", "default"),
                "enabled": bool(cfg.get("enabled", False)),
                "handler_handle": cfg["handler_handle"],
                "account_id": cfg["account_id"],
                "platform": cfg.get("platform", "instagram"),
                "content_type": cfg.get("content_type", "feed"),
                "poll_interval_sec": int(cfg.get("poll_interval_sec", 15)),  # 🔥 FIXED: was 300, now 15
                "media_only": bool(cfg.get("media_only", True)),
                "include_reposts": bool(cfg.get("include_reposts", False)),
                "include_replies": bool(cfg.get("include_replies", False)),
                "caption_template": cfg.get("caption_template") or "{text}",
                "bluesky_handle": cfg.get("bluesky_handle"),
                "bluesky_app_password": cfg.get("bluesky_app_password"),
            },
        )
        rid = cur.fetchone()[0]
        conn.commit()
        cur.close()

        # A password/handle change means the cached client (if any) is stale.
        handle = cfg.get("bluesky_handle")
        if handle:
            with _bsky_clients_lock:
                _bsky_clients.pop(handle, None)

        return rid
    finally:
        release_db(conn)


def get_seen_uris(config_id: int) -> set:
    """Batched replacement for calling uri_seen() once per post. One query
    instead of N."""
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT uri FROM auto_news_seen WHERE config_id = %s", (config_id,))
            result = {r[0] for r in cur.fetchall()}
            cur.close()
            return result
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] get_seen_uris DB error (treating all as unseen, will retry): {e}")
        return set()


def mark_seen_bulk(config_id: int, entries: list[tuple[str, bool]]):
    """Batched replacement for calling mark_seen() once per post. One
    multi-row INSERT instead of N single-row INSERTs."""
    if not entries:
        return
    try:
        from psycopg2.extras import execute_values
        conn = get_db()
        try:
            cur = conn.cursor()
            execute_values(
                cur,
                """
                INSERT INTO auto_news_seen (config_id, uri, posted)
                VALUES %s
                ON CONFLICT (config_id, uri) DO UPDATE SET posted = EXCLUDED.posted
                """,
                [(config_id, uri, posted) for uri, posted in entries],
            )
            conn.commit()
            cur.close()
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] mark_seen_bulk DB error (non-fatal): {e}")


# Kept for backwards compatibility (CLI/manual use, or if anything external
# imports these single-row helpers directly) — but the hot path in
# _run_once_inner now uses get_seen_uris()/mark_seen_bulk() instead.

def uri_seen(config_id: int, uri: str) -> bool:
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM auto_news_seen WHERE config_id = %s AND uri = %s", (config_id, uri))
            found = cur.fetchone() is not None
            cur.close()
            return found
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] uri_seen DB error (treating as unseen, will retry): {e}")
        return True


def mark_seen(config_id: int, uri: str, posted: bool):
    mark_seen_bulk(config_id, [(uri, posted)])


def set_last_run(name: str, error: str | None = None, result: str | None = None):
    """
    This runs AFTER posting is done for the cycle. It must NEVER raise —
    a DB hiccup here (e.g. an idle connection timing out during a long
    posting run) previously killed the whole process right after a
    successful posting run, instead of just sleeping and waiting for the
    next cycle.
    """
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE auto_news_config SET last_run_at = CURRENT_TIMESTAMP, last_error = %s, last_result = %s WHERE name = %s",
                (error, result, name),
            )
            conn.commit()
            cur.close()
        finally:
            release_db(conn)
    except Exception as e:
        print(f"[auto_news] set_last_run DB error (non-fatal, cycle already completed): {e}")


def run_once(name: str = "default") -> dict:
    """
    Always runs the fetch/check step for this config. Posting only happens
    if new, unseen posts matching the filters are found — finding zero new
    posts is a normal, successful outcome, not an error, and never disables
    or breaks the config.

    This function is guaranteed to return a dict and never raise — every
    internal DB/network step is already wrapped, and the outer try/except
    below is a final safety net so a caller that doesn't wrap this call
    (e.g. the CLI loop) can never be crashed by it.
    """
    try:
        return _run_once_inner(name)
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "error": f"Unexpected error in run_once: {e}"}


def _run_once_inner(name: str = "default") -> dict:
    ensure_tables()
    cfg = get_config(name)
    if not cfg:
        return {"success": False, "error": f"Config '{name}' not found. Save config first."}
    if not cfg.get("enabled"):
        return {"success": True, "skipped": True, "reason": "Config is disabled"}

    handle = cfg["handler_handle"]
    account_id = cfg["account_id"]
    bsky_user = cfg.get("bluesky_handle")
    bsky_pass = cfg.get("bluesky_app_password")
    if not bsky_user or not bsky_pass:
        err = "Bluesky credentials missing in auto_news_config"
        set_last_run(name, err, "error")
        return {"success": False, "error": err}

    # ============================================================
    # GET ENABLED AT TIME - Only fetch posts after this time
    # ============================================================
    enabled_at = cfg.get("enabled_at")
    if enabled_at:
        if isinstance(enabled_at, str):
            try:
                enabled_at = datetime.fromisoformat(enabled_at.replace('Z', '+00:00'))
            except Exception:
                enabled_at = datetime.now(timezone.utc)
    else:
        enabled_at = datetime.now(timezone.utc)

    if enabled_at.tzinfo is None:
        enabled_at = enabled_at.replace(tzinfo=timezone.utc)

    print(f"📅 Auto-news enabled at: {enabled_at.isoformat()}")
    print(f"⏳ Only fetching posts created after this time...")

    # --- ALWAYS attempt the fetch/check step -----------------------------
    # Reuse a cached, already-authenticated client instead of logging in
    # fresh every cycle. Only force a relogin if the first attempt fails
    # (e.g. session expired) — this makes fetches both faster (no repeated
    # login handshake) and more resilient (self-heals from a stale session
    # instead of failing the whole cycle).
    try:
        client = get_bluesky_client(bsky_user, bsky_pass)
        posts = fetch_author_feed(client, handle, limit=30)
    except Exception as e:
        print(f"[auto_news] fetch failed ({e}), forcing relogin and retrying once...")
        try:
            client = get_bluesky_client(bsky_user, bsky_pass, force_new=True)
            posts = fetch_author_feed(client, handle, limit=30)
        except Exception as e2:
            err = f"Fetch failed: {e2}"
            set_last_run(name, err, "error")
            return {"success": False, "error": err}

    config_id = cfg["id"]

    if not posts:
        set_last_run(name, None, "no_posts_found")
        return {
            "success": True,
            "posted_count": 0,
            "posted": [],
            "skipped": 0,
            "errors": [],
            "handler": handle,
            "account_id": account_id,
            "message": "Checked feed, no posts found this cycle.",
        }

    posted = []
    skipped = []
    errors = []
    seen_updates: list[tuple[str, bool]] = []  # batched instead of per-post writes

    # One query for everything already seen instead of N (uri_seen per post).
    seen_uris = get_seen_uris(config_id)

    # Process oldest first so news order is chronological when posting immediately
    posts_sorted = sorted(posts, key=lambda p: p.get("created_at") or "")

    print(f"📊 Found {len(posts_sorted)} posts, checking against enabled_at...")

    for p in posts_sorted:
        uri = p["uri"]

        # ============================================================
        # SKIP POSTS CREATED BEFORE ENABLED AT TIME
        # ============================================================
        created_at = p.get("created_at")
        if created_at:
            try:
                if isinstance(created_at, str):
                    post_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                else:
                    post_time = created_at

                if post_time.tzinfo is None:
                    post_time = post_time.replace(tzinfo=timezone.utc)

                # Skip posts created before the config was enabled
                if post_time < enabled_at:
                    print(f"⏭️ Skipping old post from {post_time.isoformat()} (enabled at {enabled_at.isoformat()})")
                    seen_updates.append((uri, False))
                    skipped.append(uri)
                    continue
            except Exception as e:
                print(f"⚠️ Could not parse date for {uri}: {e}")
                # If we can't parse the date, process it anyway (fail safe)

        try:
            if uri in seen_uris:
                skipped.append(uri)
                continue
            if not cfg.get("include_reposts") and p.get("is_repost"):
                seen_updates.append((uri, False))
                skipped.append(uri)
                continue
            if not cfg.get("include_replies") and p.get("is_reply"):
                seen_updates.append((uri, False))
                skipped.append(uri)
                continue
            if cfg.get("media_only") and not p.get("has_media"):
                seen_updates.append((uri, False))
                skipped.append(uri)
                continue
            if not p.get("images"):
                seen_updates.append((uri, False))
                skipped.append(uri)
                continue

            image_url = p["images"][0]["url"]
            text = p.get("text") or ""
            template = cfg.get("caption_template") or "{text}"
            caption = template.replace("{text}", text).replace("{author}", p.get("author") or handle)
            if len(caption) > 2200:
                caption = caption[:2197] + "..."

            result = post_image_to_account(
                image_url=image_url,
                caption=caption,
                account_id=account_id,
                platform=cfg.get("platform") or "instagram",
                content_type=cfg.get("content_type") or "feed",
            )
            if result.get("success"):
                seen_updates.append((uri, True))
                posted.append({"uri": uri, "text": text[:80]})
                time.sleep(2)
            else:
                errors.append({"uri": uri, "error": result.get("error")})
                continue
        except Exception as e:
            errors.append({"uri": uri, "error": str(e)})
            traceback.print_exc()
            continue

    # One bulk write for everything marked seen this cycle instead of N
    # single-row writes.
    mark_seen_bulk(config_id, seen_updates)

    err = errors[0]["error"] if errors else None
    result_label = "posted" if posted else ("errors" if errors else "no_new_posts")
    set_last_run(name, err, result_label)

    return {
        "success": True,
        "posted_count": len(posted),
        "posted": posted,
        "skipped": len(skipped),
        "errors": errors,
        "handler": handle,
        "account_id": account_id,
        "enabled_at": enabled_at.isoformat(),
        "posts_processed": len(posts_sorted),
        "old_posts_skipped": len([s for s in skipped if s not in posted]),
        "message": f"Processed {len(posts_sorted)} posts, skipped old posts created before {enabled_at.strftime('%Y-%m-%d %H:%M')}"
    }


def run_all_enabled(log_prefix: str = "") -> dict:
    """Run the check/post cycle for every enabled config. Always executes,
    regardless of whether any individual config finds new posts, and
    regardless of whether posting succeeded — one config's outcome never
    stops the others, and this function itself never raises."""
    try:
        ensure_tables()
        configs = get_all_configs(enabled_only=True)
    except Exception as e:
        print(f"{log_prefix}[auto_news] tick: could not list configs: {e}")
        traceback.print_exc()
        return {"success": False, "error": str(e), "configs_run": 0, "results": {}}

    results = {}
    if not configs:
        print(f"{log_prefix}[auto_news] tick: no enabled configs, nothing to check.")
        return {"success": True, "configs_run": 0, "results": {}}

    for cfg in configs:
        name = cfg.get("name", "default")
        try:
            results[name] = run_once(name)
        except Exception as e:
            traceback.print_exc()
            results[name] = {"success": False, "error": str(e)}
    print(f"{log_prefix}[auto_news] tick complete, will run again next interval.")
    return {"success": True, "configs_run": len(configs), "results": results}


def run_loop(name: str = "default", interval: int | None = None):
    """Standalone CLI loop (not used when running inside the Flask app —
    see start_background_scheduler for that).

    This loop must NEVER exit because of an error in a single cycle —
    including errors that happen after posting has already finished for
    that cycle. Every step below is wrapped so the loop always reaches
    time.sleep() and waits for the next cycle, no matter what."""
    ensure_tables()
    while True:
        sec = 15  # 🔥 FIXED: was 300, now 15
        try:
            cfg = get_config(name)
            sec = interval or (cfg.get("poll_interval_sec") if cfg else 15) or 15  # 🔥 FIXED: was 300
            print(f"[{datetime.now().isoformat()}] auto_news run '{name}'")
            result = run_once(name)
            print(json.dumps(result, indent=2, default=str))
        except Exception as e:
            print(f"[auto_news] run_loop cycle error (continuing, will retry): {e}")
            traceback.print_exc()
        print(f"[{datetime.now().isoformat()}] auto_news sleeping {sec}s before next check…")
        time.sleep(int(sec))


# ---------------------------------------------------------------------------
# Always-on background scheduler (runs INSIDE the Flask process)
# ---------------------------------------------------------------------------

MIN_POLL_INTERVAL_SEC = 10  # hard floor so a misconfigured 0/negative value can't hammer things


def _heartbeat_tick(heartbeat_sec: int):
    """
    Runs on every heartbeat (default every 10s). For each ENABLED config,
    atomically CLAIMS the cycle via claim_due_config() — this is what
    prevents duplicate posts when more than one process is alive (Flask's
    dev reloader running a parent + child process, or multiple production
    workers): only the process that wins the atomic DB claim actually runs
    run_once() for this cycle; everyone else sees 0 rows and skips.

    Finding zero posts, or a config's fetch turning up empty, NEVER stops
    or skips future heartbeats — this function always returns and the loop
    always continues.
    """
    try:
        names = get_enabled_config_names()
    except Exception as e:
        print(f"[auto_news] heartbeat: could not list configs: {e}")
        traceback.print_exc()
        return

    if not names:
        return

    for name in names:
        cfg = claim_due_config(name, min_interval_sec=MIN_POLL_INTERVAL_SEC)
        if not cfg:
            continue  # not due yet, or another process already claimed this cycle

        try:
            interval = int(cfg.get("poll_interval_sec") or 15)  # 🔥 FIXED: was 300, now 15
        except (TypeError, ValueError):
            interval = 15  # 🔥 FIXED: was 300, now 15
        interval = max(MIN_POLL_INTERVAL_SEC, interval)

        try:
            print(f"[{datetime.now().isoformat()}] [auto_news] '{name}' claimed this cycle (every {interval}s) — checking now")
            result = run_once(name)
            posted = result.get("posted_count", 0) if isinstance(result, dict) else 0
            if posted:
                print(f"[auto_news] '{name}' posted {posted} item(s) this cycle.")
            else:
                old_skipped = result.get("old_posts_skipped", 0) if isinstance(result, dict) else 0
                if old_skipped > 0:
                    print(f"[auto_news] '{name}' checked, skipped {old_skipped} old posts. Will check again in {interval}s.")
                else:
                    print(f"[auto_news] '{name}' checked, nothing new — will check again in {interval}s.")
        except Exception as e:
            print(f"[auto_news] '{name}' heartbeat run error (non-fatal): {e}")
            traceback.print_exc()


def start_background_scheduler(default_interval_sec: int = 10):
    """
    Starts a persistent background thread that ticks every `default_interval_sec`
    seconds (a heartbeat — default 10s) and, on each tick, checks whether any
    enabled config is due for a check based on ITS OWN poll_interval_sec.

    This is what makes auto-news actually autonomous and responsive: even a
    config configured to poll every 10 seconds will really be checked every
    10 seconds, and finding nothing new never stops or pauses future checks.

    Safe to call multiple times; only starts one scheduler thread globally.
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler

        heartbeat_sec = max(1, int(default_interval_sec))

        def _loop():
            print(f"✅ auto_news heartbeat started (every {heartbeat_sec}s)")
            while True:
                try:
                    _heartbeat_tick(heartbeat_sec)
                except Exception as e:
                    print(f"[auto_news] heartbeat loop error (continuing): {e}")
                    traceback.print_exc()
                time.sleep(heartbeat_sec)

        thread = threading.Thread(target=_loop, name="auto-news-heartbeat", daemon=True)
        thread.start()
        _scheduler = thread
        return thread


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

def register_auto_news_routes(app, autostart: bool = True, default_interval_sec: int = 10):
    from flask import request, jsonify

    ensure_tables()

    if autostart:
        start_background_scheduler(default_interval_sec=default_interval_sec)

    @app.route("/api/auto-news/config", methods=["GET", "POST"])
    def auto_news_config():
        if request.method == "GET":
            cfg = get_config(request.args.get("name", "default"))
            if not cfg:
                return jsonify({"success": True, "config": None})
            safe = dict(cfg)
            if safe.get("bluesky_app_password"):
                safe["bluesky_app_password"] = "********"
            return jsonify({"success": True, "config": safe})

        data = request.json or {}
        required = ("handler_handle", "account_id")
        for r in required:
            if not data.get(r):
                return jsonify({"success": False, "error": f"{r} required"}), 400
        data.setdefault("name", "default")
        if data.get("bluesky_app_password") == "********":
            data["bluesky_app_password"] = None
        rid = save_config(data)
        return jsonify({"success": True, "id": rid, "message": "Config saved"})

    @app.route("/api/auto-news/run", methods=["POST"])
    def auto_news_run():
        """Manual/on-demand trigger (still works, e.g. for an external cron
        or a 'Run now' button) — but the background scheduler above means
        this is no longer required for auto-news to function."""
        data = request.json or {}
        name = data.get("name", "default")
        result = run_once(name)
        status = 200 if result.get("success") or result.get("skipped") else 500
        return jsonify(result), status

    @app.route("/api/auto-news/run-all", methods=["POST"])
    def auto_news_run_all():
        """Manually trigger a check cycle across every enabled config right now."""
        result = run_all_enabled()
        return jsonify(result)

    @app.route("/api/auto-news/status", methods=["GET"])
    def auto_news_status():
        """See scheduler state and each config's last run info."""
        configs = get_all_configs()
        safe_configs = []
        for c in configs:
            c = dict(c)
            if c.get("bluesky_app_password"):
                c["bluesky_app_password"] = "********"
            safe_configs.append(c)
        return jsonify({
            "success": True,
            "scheduler_running": _scheduler is not None and getattr(_scheduler, "is_alive", lambda: False)(),
            "configs": safe_configs,
        })

    @app.route("/api/auto-news/start", methods=["POST"])
    def auto_news_start():
        """Start the auto-news scheduler (already running by default)."""
        return jsonify({
            "success": True,
            "message": "Auto-news scheduler is already running",
            "running": _scheduler is not None and getattr(_scheduler, "is_alive", lambda: False)()
        })

    @app.route("/api/auto-news/stop", methods=["POST"])
    def auto_news_stop():
        """Stop the auto-news scheduler (cannot be stopped, only disabled via config)."""
        return jsonify({
            "success": True,
            "message": "To stop auto-news, set Enabled = OFF in config and save",
            "running": _scheduler is not None and getattr(_scheduler, "is_alive", lambda: False)()
        })

    @app.route("/api/auto-news/seen", methods=["GET"])
    def auto_news_seen():
        name = request.args.get("name", "default")
        cfg = get_config(name)
        if not cfg:
            return jsonify({"success": True, "seen": []})
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT uri, posted, seen_at FROM auto_news_seen WHERE config_id = %s ORDER BY seen_at DESC LIMIT 50",
                (cfg["id"],),
            )
            rows = [{"uri": r[0], "posted": r[1], "seen_at": r[2].isoformat() if r[2] else None} for r in cur.fetchall()]
            cur.close()
        finally:
            release_db(conn)
        return jsonify({"success": True, "seen": rows})


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Auto news fetch & Instagram post")
    parser.add_argument("command", choices=["once", "loop", "init"])
    parser.add_argument("--name", default="default")
    parser.add_argument("--interval", type=int, default=None)
    args = parser.parse_args()
    if args.command == "init":
        init_auto_tables()
        print("Tables ready")
    elif args.command == "once":
        print(json.dumps(run_once(args.name), indent=2, default=str))
    else:
        run_loop(args.name, args.interval)