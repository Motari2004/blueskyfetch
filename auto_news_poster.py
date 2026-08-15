"""
Auto news fetch & post module (keeps app.py smaller).

Watches Bluesky handlers for *new* posts with media, then posts to a chosen
Zernio Instagram account.

Usage (CLI):
  python auto_news_poster.py once
  python auto_news_poster.py loop --interval 300

Or via Flask routes registered by register_auto_news_routes(app).
Config is stored in Postgres table auto_news_config + auto_news_seen.

------------------------------------------------------------------------------
IMPORTANT: this module runs an ALWAYS-ON background heartbeat (default every
10s) when register_auto_news_routes(app) is called. On each heartbeat tick:
  1. It checks every ENABLED config against its own `poll_interval_sec`
     (e.g. set this to 10 in the Auto News page to actually check every
     10 seconds — the heartbeat itself is always awake and checking whether
     something is due).
  2. If a config is due, it ALWAYS runs the fetch/check step, no matter what.
  3. If it finds new (unseen) posts that match the filters -> posts them.
  4. If it finds nothing (empty handler, no new posts, etc.) -> logs
     "checked, nothing new" and just waits for the next cycle. It NEVER
     stops, disables itself, or exits because a cycle found 0 posts.

Posting failures for one post no longer abort the rest of the batch, and no
error anywhere in a cycle (including after posting finishes) can kill the
heartbeat or the CLI loop — every layer catches its own exceptions.

DUPLICATE-POST SAFETY: each cycle is claimed atomically in the database
(claim_due_config) before it runs, so if more than one process is alive at
once — e.g. Flask's dev-mode reloader running both a parent watcher process
and a child server process, or multiple production workers — only ONE of
them ever wins a given cycle. The others see the claim already taken and
skip, so the same post is never sent twice.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

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


def get_db():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def init_auto_tables():
    conn = get_db()
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
            poll_interval_sec INTEGER DEFAULT 300,
            media_only BOOLEAN DEFAULT TRUE,
            include_reposts BOOLEAN DEFAULT FALSE,
            include_replies BOOLEAN DEFAULT FALSE,
            caption_template TEXT DEFAULT '{text}',
            bluesky_handle TEXT,
            bluesky_app_password TEXT,
            last_run_at TIMESTAMP,
            last_error TEXT,
            last_result TEXT,
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
    # Migrate: add last_result if this table pre-dates it
    try:
        cur.execute("ALTER TABLE auto_news_config ADD COLUMN IF NOT EXISTS last_result TEXT")
    except Exception as mig_e:
        print(f"auto_news_config migrate: {mig_e}")
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Bluesky fetch (lightweight, no full atproto session store)
# ---------------------------------------------------------------------------

def bluesky_login(handle: str, app_password: str):
    from atproto import Client
    client = Client()
    client.login(handle, app_password)
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
            embed = getattr(record, "embed", None)
            # images in embed
            if embed is not None:
                # app.bsky.embed.images
                imgs = getattr(embed, "images", None)
                if imgs:
                    for im in imgs:
                        # need full URL from post.embed if available
                        pass
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
        import backend_app as app_mod  # when run next to app
        return app_mod.post_to_zernio(
            image_url=image_url,
            caption=caption,
            platforms=[platform],
            content_type=content_type,
            account_ids=[account_id],
        )
    except Exception:
        pass
    try:
        # If imported from Flask app context
        from flask import current_app
        # direct function on module loaded as app
    except Exception:
        pass

    # Fallback: try import by filename patterns
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

    return {"success": False, "error": "Could not import post_to_zernio from app"}


# ---------------------------------------------------------------------------
# Core job
# ---------------------------------------------------------------------------

def get_config(name: str = "default"):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM auto_news_config WHERE name = %s", (name,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
        cur.close()
        conn.close()
        if not row:
            return None
        return dict(zip(cols, row))
    except Exception as e:
        # A DB hiccup here must never propagate and kill a running loop —
        # treat it as "config unavailable this cycle", not a fatal error.
        print(f"[auto_news] get_config DB error: {e}")
        return None


def get_all_configs(enabled_only: bool = False):
    """Return every saved config (optionally only the enabled ones)."""
    conn = get_db()
    cur = conn.cursor()
    if enabled_only:
        cur.execute("SELECT * FROM auto_news_config WHERE enabled = TRUE")
    else:
        cur.execute("SELECT * FROM auto_news_config")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    cur.close()
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


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
        conn.close()
        if not row:
            return None
        return dict(zip(cols, row))
    except Exception as e:
        print(f"[auto_news] claim_due_config DB error (skipping this cycle, will retry): {e}")
        return None


def save_config(cfg: dict):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auto_news_config (
            name, enabled, handler_handle, account_id, platform, content_type,
            poll_interval_sec, media_only, include_reposts, include_replies,
            caption_template, bluesky_handle, bluesky_app_password, updated_at
        ) VALUES (
            %(name)s, %(enabled)s, %(handler_handle)s, %(account_id)s, %(platform)s, %(content_type)s,
            %(poll_interval_sec)s, %(media_only)s, %(include_reposts)s, %(include_replies)s,
            %(caption_template)s, %(bluesky_handle)s, %(bluesky_app_password)s, CURRENT_TIMESTAMP
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
            "poll_interval_sec": int(cfg.get("poll_interval_sec", 300)),
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
    conn.close()
    return rid


def uri_seen(config_id: int, uri: str) -> bool:
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM auto_news_seen WHERE config_id = %s AND uri = %s", (config_id, uri))
        found = cur.fetchone() is not None
        cur.close()
        conn.close()
        return found
    except Exception as e:
        print(f"[auto_news] uri_seen DB error (treating as unseen, will retry): {e}")
        # Fail safe: if we can't check, skip this post THIS cycle rather than
        # crash the whole run — the caller's per-post try/except already
        # protects the loop, this is belt-and-suspenders.
        return True


def mark_seen(config_id: int, uri: str, posted: bool):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_news_seen (config_id, uri, posted)
            VALUES (%s, %s, %s)
            ON CONFLICT (config_id, uri) DO UPDATE SET posted = EXCLUDED.posted
            """,
            (config_id, uri, posted),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        # Never let a bookkeeping write kill an otherwise-successful cycle.
        print(f"[auto_news] mark_seen DB error (non-fatal): {e}")


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
        cur = conn.cursor()
        cur.execute(
            "UPDATE auto_news_config SET last_run_at = CURRENT_TIMESTAMP, last_error = %s, last_result = %s WHERE name = %s",
            (error, result, name),
        )
        conn.commit()
        cur.close()
        conn.close()
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
    init_auto_tables()
    cfg = get_config(name)
    if not cfg:
        return {"success": False, "error": f"Config '{name}' not found. Save config first."}
    if not cfg.get("enabled"):
        # Not an error — just nothing to do this cycle.
        return {"success": True, "skipped": True, "reason": "Config is disabled"}

    handle = cfg["handler_handle"]
    account_id = cfg["account_id"]
    bsky_user = cfg.get("bluesky_handle")
    bsky_pass = cfg.get("bluesky_app_password")
    if not bsky_user or not bsky_pass:
        err = "Bluesky credentials missing in auto_news_config"
        set_last_run(name, err, "error")
        return {"success": False, "error": err}

    # --- ALWAYS attempt the fetch/check step -----------------------------
    try:
        client = bluesky_login(bsky_user, bsky_pass)
        posts = fetch_author_feed(client, handle, limit=15)
    except Exception as e:
        err = f"Fetch failed: {e}"
        set_last_run(name, err, "error")
        # This cycle failed, but the caller (scheduler) will simply try
        # again next interval — the config stays enabled and active.
        return {"success": False, "error": err}

    config_id = cfg["id"]

    if not posts:
        # No posts came back at all — perfectly normal, just wait for next cycle.
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

    # Process oldest first so news order is chronological when posting immediately
    posts_sorted = sorted(posts, key=lambda p: p.get("created_at") or "")

    for p in posts_sorted:
        uri = p["uri"]
        try:
            if uri_seen(config_id, uri):
                skipped.append(uri)
                continue
            if not cfg.get("include_reposts") and p.get("is_repost"):
                mark_seen(config_id, uri, False)
                skipped.append(uri)
                continue
            if not cfg.get("include_replies") and p.get("is_reply"):
                mark_seen(config_id, uri, False)
                skipped.append(uri)
                continue
            if cfg.get("media_only") and not p.get("has_media"):
                mark_seen(config_id, uri, False)
                skipped.append(uri)
                continue
            if not p.get("images"):
                mark_seen(config_id, uri, False)
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
                mark_seen(config_id, uri, True)
                posted.append({"uri": uri, "text": text[:80]})
                time.sleep(2)
            else:
                # One post failing to publish should NOT stop the rest of
                # the batch, and should NOT stop future cycles from running.
                # Leave it unmarked so it's retried next cycle.
                errors.append({"uri": uri, "error": result.get("error")})
                continue
        except Exception as e:
            # Never let a single post's unexpected exception kill the whole run.
            errors.append({"uri": uri, "error": str(e)})
            traceback.print_exc()
            continue

    err = errors[0]["error"] if errors else None
    result_label = "posted" if posted else ("errors" if errors else "no_new_posts")
    set_last_run(name, err, result_label)
    return {
        "success": True,  # the CYCLE ran successfully even if some posts errored
        "posted_count": len(posted),
        "posted": posted,
        "skipped": len(skipped),
        "errors": errors,
        "handler": handle,
        "account_id": account_id,
    }


def run_all_enabled(log_prefix: str = "") -> dict:
    """Run the check/post cycle for every enabled config. Always executes,
    regardless of whether any individual config finds new posts, and
    regardless of whether posting succeeded — one config's outcome never
    stops the others, and this function itself never raises."""
    try:
        init_auto_tables()
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
            results[name] = run_once(name)  # run_once() never raises, but stay defensive
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
    init_auto_tables()
    while True:
        sec = 300
        try:
            cfg = get_config(name)
            sec = interval or (cfg.get("poll_interval_sec") if cfg else 300) or 300
            print(f"[{datetime.now().isoformat()}] auto_news run '{name}'")
            result = run_once(name)  # run_once() itself never raises, but stay defensive
            print(json.dumps(result, indent=2, default=str))
        except Exception as e:
            # Whatever went wrong — even after a successful posting run —
            # log it and keep the loop alive instead of exiting.
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
            interval = int(cfg.get("poll_interval_sec") or 300)
        except (TypeError, ValueError):
            interval = 300
        interval = max(MIN_POLL_INTERVAL_SEC, interval)

        try:
            print(f"[{datetime.now().isoformat()}] [auto_news] '{name}' claimed this cycle (every {interval}s) — checking now")
            result = run_once(name)
            posted = result.get("posted_count", 0) if isinstance(result, dict) else 0
            if posted:
                print(f"[auto_news] '{name}' posted {posted} item(s) this cycle.")
            else:
                print(f"[auto_news] '{name}' checked, nothing new — will check again in {interval}s.")
        except Exception as e:
            # run_once() already guards itself, but stay defensive here too —
            # one config's failure must never stop the heartbeat for others.
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
                    # Absolute last line of defense: the heartbeat thread
                    # itself must never die.
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

    init_auto_tables()

    if autostart:
        start_background_scheduler(default_interval_sec=default_interval_sec)

    @app.route("/api/auto-news/config", methods=["GET", "POST"])
    def auto_news_config():
        if request.method == "GET":
            cfg = get_config(request.args.get("name", "default"))
            if not cfg:
                return jsonify({"success": True, "config": None})
            # hide password
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
        # don't overwrite password with stars
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
        # The scheduler is already running - just return status
        return jsonify({
            "success": True,
            "message": "Auto-news scheduler is already running",
            "running": _scheduler is not None and getattr(_scheduler, "is_alive", lambda: False)()
        })

    @app.route("/api/auto-news/stop", methods=["POST"])
    def auto_news_stop():
        """Stop the auto-news scheduler (cannot be stopped, only disabled via config)."""
        # The scheduler cannot be stopped - but you can disable the config
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
        cur = conn.cursor()
        cur.execute(
            "SELECT uri, posted, seen_at FROM auto_news_seen WHERE config_id = %s ORDER BY seen_at DESC LIMIT 50",
            (cfg["id"],),
        )
        rows = [{"uri": r[0], "posted": r[1], "seen_at": r[2].isoformat() if r[2] else None} for r in cur.fetchall()]
        cur.close()
        conn.close()
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