from flask import Flask, request, jsonify, send_from_directory, send_file, Response
from flask_cors import CORS
from atproto import Client
import json
import os
import requests
from datetime import datetime, timedelta
import zipfile
import io
import traceback
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import psycopg2
from psycopg2.extras import Json
import uuid
import urllib.parse
import re
import random
import time
import base64
import pytz
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import textwrap


import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__, static_folder='static')
CORS(app)

# ============================================================
# DATABASE SETUP
# ============================================================

DATABASE_URL = 'postgresql://neondb_owner:npg_0URQHn2lKXeh@ep-divine-rice-ayb8kut7-pooler.c-5.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require'

def get_db_connection():
    """Get a database connection"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None

def init_db():
    """Initialize database tables"""
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        cur = conn.cursor()
        
        # Create handlers table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS handlers (
                id SERIAL PRIMARY KEY,
                handle TEXT UNIQUE NOT NULL,
                display_name TEXT,
                avatar TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                selected BOOLEAN DEFAULT TRUE,
                is_default BOOLEAN DEFAULT FALSE
            )
        ''')

        try:
            cur.execute("ALTER TABLE handlers ADD COLUMN IF NOT EXISTS selected BOOLEAN DEFAULT TRUE")
            cur.execute("ALTER TABLE handlers ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE")
        except Exception as mig_e:
            print(f"handlers column migrate: {mig_e}")
        
        # Create vault table with video support
        cur.execute('''
            CREATE TABLE IF NOT EXISTS vault (
                id SERIAL PRIMARY KEY,
                uri TEXT UNIQUE NOT NULL,
                author TEXT NOT NULL,
                display_name TEXT,
                text TEXT,
                images JSONB,
                video JSONB,
                likes INTEGER DEFAULT 0,
                reposts INTEGER DEFAULT 0,
                replies INTEGER DEFAULT 0,
                created_at TIMESTAMP,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                handler_handle TEXT,
                notes TEXT
            )
        ''')
        
        # Create deleted_posts table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS deleted_posts (
                id SERIAL PRIMARY KEY,
                uri TEXT UNIQUE NOT NULL,
                handler_handle TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create posted_posts table with UNIQUE constraint
        cur.execute('''
            CREATE TABLE IF NOT EXISTS posted_posts (
                id SERIAL PRIMARY KEY,
                vault_id INTEGER REFERENCES vault(id),
                uri TEXT NOT NULL,
                platform VARCHAR(50) NOT NULL,
                platform_post_id VARCHAR(200),
                status VARCHAR(50) DEFAULT 'pending',
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                metadata JSONB,
                UNIQUE(uri, platform)
            )
        ''')
        
        # Create zernio_profiles table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS zernio_profiles (
                id SERIAL PRIMARY KEY,
                profile_id VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(200),
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create zernio_accounts table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS zernio_accounts (
                id SERIAL PRIMARY KEY,
                account_id VARCHAR(100) UNIQUE NOT NULL,
                platform VARCHAR(50),
                display_name VARCHAR(200),
                username VARCHAR(100),
                profile_picture TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                last_sync TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database init error: {e}")

# Initialize database on startup
init_db()

# ============================================================
# ZERNIO CONFIGURATION
# ============================================================

ZERNIO_API_KEY = os.environ.get('ZERNIO_API_KEY', 'sk_7b484f17f3be9f7faa5cabc822983e5760cf4256db3ffa565fb3befdfb6535a6')
ZERNIO_BASE_URL = "https://zernio.com/api/v1"
SCHEDULE_TIMEZONE = "Africa/Nairobi"

# Store client sessions
sessions = {}

# ============================================================
# IMAGE CONVERSION FUNCTIONS
# ============================================================

def convert_image_to_jpeg(image_url):
    """Download image and convert to JPEG if needed"""
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code != 200:
            print(f"Failed to download image: {response.status_code}")
            return None
        
        img = Image.open(BytesIO(response.content))
        
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        
        print(f"✅ Converted image to JPEG (original format: {img.format or 'unknown'})")
        return output
    except Exception as e:
        print(f"Error converting image: {e}")
        traceback.print_exc()
        return None

def fix_image_for_feed(image_bytes, target_ratio='4:5', max_width=1080):
    """Fix image aspect ratio for Instagram Feed posts."""
    try:
        img = Image.open(image_bytes)
        width, height = img.size
        
        target = 0.8  # 4:5 ratio
        current = width / height
        
        print(f"   Feed - Original: {width}×{height}px, ratio: {current:.4f}")
        print(f"   Target ratio: 4:5 ({target:.4f})")
        
        if abs(current - target) < 0.01:
            print("   Ratio already acceptable, resizing...")
            if width > max_width:
                new_width = max_width
                new_height = int(height * (max_width / width))
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                print(f"   Resized to: {new_width}×{new_height}px")
        else:
            print(f"   Cropping to 4:5...")
            
            if current > target:
                new_width = int(height * target)
                left = (width - new_width) // 2
                img = img.crop((left, 0, left + new_width, height))
                print(f"   Cropped width to: {new_width}×{height}px")
            else:
                new_height = int(width / target)
                top = (height - new_height) // 2
                img = img.crop((0, top, width, top + new_height))
                print(f"   Cropped height to: {width}×{new_height}px")
            
            if width > max_width:
                new_width = max_width
                new_height = int(img.height * (max_width / img.width))
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                print(f"   Resized to: {new_width}×{new_height}px")
        
        final_width, final_height = img.size
        final_ratio = final_width / final_height
        print(f"   Feed final: {final_width}×{final_height}px, ratio: {final_ratio:.4f}")
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        return output
        
    except Exception as e:
        print(f"Error fixing feed image: {e}")
        traceback.print_exc()
        return image_bytes

def fix_image_for_story(image_bytes, target_ratio='9:16', max_width=1080):
    """Fix image aspect ratio for Instagram Stories."""
    try:
        img = Image.open(image_bytes)
        width, height = img.size
        
        target = 9/16
        current = width / height
        
        print(f"   Story - Original: {width}×{height}px, ratio: {current:.4f}")
        print(f"   Target ratio: 9:16 ({target:.4f})")
        
        if abs(current - target) < 0.02:
            print("   Ratio already acceptable, resizing...")
            if width > max_width:
                new_width = max_width
                new_height = int(height * (max_width / width))
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                print(f"   Resized to: {new_width}×{new_height}px")
        else:
            print(f"   Cropping to 9:16...")
            
            if current > target:
                new_width = int(height * target)
                left = (width - new_width) // 2
                img = img.crop((left, 0, left + new_width, height))
                print(f"   Cropped width to: {new_width}×{height}px")
            else:
                new_height = int(width / target)
                top = (height - new_height) // 2
                img = img.crop((0, top, width, top + new_height))
                print(f"   Cropped height to: {width}×{new_height}px")
            
            story_width = 1080
            story_height = 1920
            img.thumbnail((story_width, story_height), Image.Resampling.LANCZOS)
            print(f"   Thumbnailed to: {img.size[0]}×{img.size[1]}px")
        
        final_width, final_height = img.size
        final_ratio = final_width / final_height
        print(f"   Story final: {final_width}×{final_height}px, ratio: {final_ratio:.4f}")
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=92, optimize=True)
        output.seek(0)
        return output
        
    except Exception as e:
        print(f"Error fixing story image: {e}")
        traceback.print_exc()
        return image_bytes

# ============================================================
# ZERNIO HELPER FUNCTIONS
# ============================================================

def get_zernio_headers():
    """Get headers for Zernio API calls"""
    if not ZERNIO_API_KEY:
        print("❌ ZERNIO_API_KEY not set!")
        return {}
    return {
        "Authorization": f"Bearer {ZERNIO_API_KEY}",
        "Content-Type": "application/json"
    }

def test_zernio_connection():
    """Test if Zernio API key is valid"""
    try:
        headers = get_zernio_headers()
        if not headers:
            return False, "ZERNIO_API_KEY not configured"
        
        response = requests.get(
            f"{ZERNIO_BASE_URL}/accounts",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            return True, "Connected successfully"
        elif response.status_code == 401:
            return False, "Invalid API key. Please check your Zernio API key."
        else:
            return False, f"API error: {response.status_code} - {response.text[:100]}"
    except Exception as e:
        return False, f"Connection error: {str(e)}"

def get_zernio_accounts():
    """Fetch connected Zernio accounts"""
    try:
        print("Fetching Zernio accounts...")
        response = requests.get(
            f"{ZERNIO_BASE_URL}/accounts",
            headers=get_zernio_headers()
        )
        
        if response.status_code == 200:
            data = response.json()
            accounts = data.get('accounts', [])
            
            for acc in accounts:
                try:
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute('''
                            INSERT INTO zernio_accounts (account_id, platform, display_name, username, profile_picture, last_sync)
                            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (account_id) DO UPDATE SET
                                platform = EXCLUDED.platform,
                                display_name = EXCLUDED.display_name,
                                username = EXCLUDED.username,
                                profile_picture = EXCLUDED.profile_picture,
                                last_sync = CURRENT_TIMESTAMP,
                                is_active = TRUE
                        ''', (
                            acc.get('_id'),
                            acc.get('platform'),
                            acc.get('displayName'),
                            acc.get('username'),
                            acc.get('profilePicture')
                        ))
                        conn.commit()
                        cur.close()
                        conn.close()
                except Exception as e:
                    print(f"Error saving account: {e}")
            
            return accounts
        else:
            print(f"Error response: {response.text}")
            return []
    except Exception as e:
        print(f"Error fetching Zernio accounts: {e}")
        return []

def get_account_id_for_platform(platform, account_id=None):
    """Get account ID for a platform. If account_id given, validate it belongs to platform."""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            if account_id:
                cur.execute("""
                    SELECT account_id FROM zernio_accounts
                    WHERE account_id = %s AND platform = %s AND is_active = TRUE
                    LIMIT 1
                """, (account_id, platform))
            else:
                cur.execute("""
                    SELECT account_id FROM zernio_accounts
                    WHERE platform = %s AND is_active = TRUE
                    ORDER BY username NULLS LAST
                    LIMIT 1
                """, (platform,))
            result = cur.fetchone()
            cur.close()
            conn.close()
            if result:
                return result[0]
    except Exception as e:
        print(f"Error getting account from DB: {e}")

    accounts = get_zernio_accounts()
    for acc in accounts:
        if acc.get('platform') != platform:
            continue
        if account_id and acc.get('_id') != account_id:
            continue
        return acc.get('_id')
    return None


def list_accounts_for_platform(platform='instagram'):
    """Return all active accounts for a platform from DB (refresh from Zernio first)."""
    get_zernio_accounts()  # sync
    results = []
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT account_id, platform, display_name, username, profile_picture
                FROM zernio_accounts
                WHERE platform = %s AND is_active = TRUE
                ORDER BY username NULLS LAST, display_name NULLS LAST
            """, (platform,))
            for row in cur.fetchall():
                results.append({
                    'account_id': row[0],
                    'platform': row[1],
                    'display_name': row[2] or row[3] or row[0],
                    'username': row[3] or '',
                    'profile_picture': row[4],
                    'label': row[3] or row[2] or row[0]
                })
            cur.close()
            conn.close()
    except Exception as e:
        print(f"Error listing accounts: {e}")
    return results

def get_or_create_zernio_profile(profile_name="Bluesky Vault Poster"):
    """Get or create a Zernio profile ID with better error handling"""
    
    connected, message = test_zernio_connection()
    if not connected:
        print(f"❌ Zernio connection failed: {message}")
        return None
    
    try:
        headers = get_zernio_headers()
        if not headers:
            print("❌ No API key configured")
            return None
        
        print(f"🔍 Looking for profile: {profile_name}")
        response = requests.get(
            f"{ZERNIO_BASE_URL}/profiles",
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            profiles = data.get('profiles', [])
            
            for profile in profiles:
                if profile.get('name') == profile_name:
                    profile_id = profile.get('_id')
                    print(f"✅ Found existing profile: {profile_id}")
                    return profile_id
        else:
            print(f"⚠️ Could not list profiles: {response.status_code} - {response.text[:100]}")
        
        print(f"🆕 Creating new profile: {profile_name}")
        payload = {
            "name": profile_name,
            "description": "Posts from Bluesky vault"
        }
        
        response = requests.post(
            f"{ZERNIO_BASE_URL}/profiles",
            headers=get_zernio_headers(),
            json=payload,
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            data = response.json()
            profile_id = data.get('profile', {}).get('_id')
            if profile_id:
                print(f"✅ Profile created successfully: {profile_id}")
                return profile_id
            else:
                print(f"❌ No profile ID in response: {data}")
                return None
        else:
            print(f"❌ Failed to create profile: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
            return None
            
    except Exception as e:
        print(f"❌ Error in get_or_create_zernio_profile: {e}")
        traceback.print_exc()
        return None

def upload_media_to_zernio(image_url, content_type='feed'):
    """Upload an image to Zernio with JPEG conversion and aspect ratio fixing"""
    try:
        image_bytes = convert_image_to_jpeg(image_url)
        if not image_bytes:
            print("Failed to convert image to JPEG")
            return None
        
        print(f"🔄 Fixing aspect ratio for {content_type}...")
        
        if content_type == 'story':
            fixed_image = fix_image_for_story(image_bytes)
        else:
            fixed_image = fix_image_for_feed(image_bytes)
        
        print("Requesting presigned URL from Zernio...")
        presign_payload = {
            "filename": "bluesky_post.jpg",
            "contentType": "image/jpeg"
        }
        
        presign_response = requests.post(
            f"{ZERNIO_BASE_URL}/media/presign",
            headers=get_zernio_headers(),
            json=presign_payload,
            timeout=30
        )
        
        if presign_response.status_code not in [200, 201]:
            print(f"Presign failed: {presign_response.text}")
            return None
        
        data = presign_response.json()
        upload_url = data.get('uploadUrl')
        public_url = data.get('publicUrl')
        
        if not upload_url or not public_url:
            print(f"Missing uploadUrl or publicUrl in response")
            return None
        
        print(f"Got presigned URL, uploading JPEG...")
        fixed_image.seek(0)
        
        upload_response = requests.put(
            upload_url,
            headers={'Content-Type': 'image/jpeg'},
            data=fixed_image,
            verify=False,
            timeout=60
        )
        
        if upload_response.status_code not in [200, 201, 204]:
            print(f"Upload failed: {upload_response.text}")
            return None
        
        print("✅ File uploaded successfully!")
        return public_url
        
    except Exception as e:
        print(f"Error uploading media: {e}")
        traceback.print_exc()
        return None

def post_to_zernio(image_url, caption, platforms, scheduled_time=None, content_type='feed', account_ids=None):
    """Post to Zernio with image URL.
    account_ids: optional dict {platform: account_id} or list of account_id strings.
    When provided, posts to those specific Zernio accounts instead of the first match.
    """
    if not platforms and not account_ids:
        return {
            "success": False,
            "error": "No platforms selected"
        }
    # Normalize account_ids to dict platform -> account_id (one post can target multiple platforms)
    account_map = {}
    if isinstance(account_ids, dict):
        account_map = {k: v for k, v in account_ids.items() if v}
    elif isinstance(account_ids, list):
        # look up platform for each id
        for aid in account_ids:
            if not aid:
                continue
            try:
                conn = get_db_connection()
                if conn:
                    cur = conn.cursor()
                    cur.execute("SELECT platform FROM zernio_accounts WHERE account_id = %s", (aid,))
                    row = cur.fetchone()
                    cur.close(); conn.close()
                    if row:
                        # allow multiple instagram accounts: collect as list later
                        account_map.setdefault(row[0], [])
                        if isinstance(account_map[row[0]], list):
                            account_map[row[0]].append(aid)
                        else:
                            account_map[row[0]] = [account_map[row[0]], aid]
            except Exception as e:
                print(f"account lookup: {e}")
    # flatten single values
    for k, v in list(account_map.items()):
        if not isinstance(v, list):
            account_map[k] = [v]
    
    try:
        connected, message = test_zernio_connection()
        if not connected:
            return {
                "success": False,
                "error": f"Zernio connection failed: {message}"
            }
        
        profile_id = get_or_create_zernio_profile()
        if not profile_id:
            return {
                "success": False,
                "error": "Failed to create or find Zernio profile. Please check your Zernio API key and account."
            }
        
        public_url = upload_media_to_zernio(image_url, content_type)
        
        if not public_url:
            return {
                "success": False,
                "error": "Failed to upload image to Zernio. The image format may not be supported."
            }
        
        platform_entries = []
        platforms_to_use = list(platforms) if platforms else list(account_map.keys())
        if not platforms_to_use and account_map:
            platforms_to_use = list(account_map.keys())

        for platform in platforms_to_use:
            ids = account_map.get(platform)
            if ids:
                target_ids = ids if isinstance(ids, list) else [ids]
            else:
                aid = get_account_id_for_platform(platform)
                target_ids = [aid] if aid else []

            if not target_ids:
                print(f"Skipping {platform} - no account connected")
                return {
                    "success": False,
                    "error": f"No connected account found for {platform}. Please connect your account in Zernio."
                }

            for account_id in target_ids:
                platform_entry = {
                    "platform": platform,
                    "accountId": account_id
                }
                if platform == 'instagram':
                    platform_entry['platformSpecificData'] = {
                        "contentType": content_type
                    }
                platform_entries.append(platform_entry)

        if not platform_entries:
            return {
                "success": False,
                "error": "No connected accounts found for selected platforms."
            }
        
        payload = {
            "mediaItems": [{
                "type": "image",
                "url": public_url
            }],
            "platforms": platform_entries,
            "content": caption if caption else ""
        }
        
        if scheduled_time:
            tz = pytz.timezone(SCHEDULE_TIMEZONE)
            if scheduled_time.tzinfo is None:
                scheduled_time = tz.localize(scheduled_time)
            
            scheduled_time_utc = scheduled_time.astimezone(pytz.UTC)
            payload["scheduledFor"] = scheduled_time_utc.isoformat()
            payload["timezone"] = SCHEDULE_TIMEZONE
        else:
            payload["publishNow"] = True
        
        content_type_label = "Story" if content_type == 'story' else "Feed"
        print(f"Posting to Zernio as {content_type_label}...")
        response = requests.post(
            f"{ZERNIO_BASE_URL}/posts",
            headers=get_zernio_headers(),
            json=payload,
            timeout=60
        )
        
        print(f"Post response status: {response.status_code}")
        
        if response.status_code in [200, 201]:
            data = response.json()
            zernio_post_id = data.get('post', {}).get('_id')
            
            return {
                "success": True,
                "post_id": zernio_post_id,
                "message": f"Post scheduled as {content_type_label}!" if scheduled_time else f"Post published as {content_type_label}!"
            }
        else:
            error_detail = response.text
            try:
                error_json = response.json()
                error_detail = error_json.get('error', error_detail)
                if 'details' in error_json:
                    error_detail = f"{error_detail} - {error_json['details']}"
            except:
                pass
            
            if 'aspect ratio' in error_detail.lower():
                error_detail += "\n\n💡 Tip: Try using a 1:1 (square) or 4:5 (portrait) image for Instagram feed. For stories, select 'Story' as content type."
            
            return {
                "success": False,
                "error": f"Zernio API error: {error_detail}"
            }
            
    except Exception as e:
        print(f"Error posting to Zernio: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "error": f"Error posting to Zernio: {str(e)}"
        }

def mark_post_as_posted(vault_id, uri, platform, platform_post_id=None, status='completed'):
    """Mark a vault post as posted to social media"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
        
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id FROM posted_posts 
            WHERE uri = %s AND platform = %s
        """, (uri, platform))
        
        result = cur.fetchone()
        
        if result:
            cur.execute('''
                UPDATE posted_posts 
                SET vault_id = %s, platform_post_id = %s, status = %s, posted_at = CURRENT_TIMESTAMP
                WHERE uri = %s AND platform = %s
            ''', (vault_id, platform_post_id, status, uri, platform))
        else:
            cur.execute('''
                INSERT INTO posted_posts (vault_id, uri, platform, platform_post_id, status, posted_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ''', (vault_id, uri, platform, platform_post_id, status))
        
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error marking post as posted: {e}")
        traceback.print_exc()
        return False

def is_post_already_posted(uri, platform):
    """Check if a post has already been posted to a platform"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
        
        cur = conn.cursor()
        cur.execute("""
            SELECT id FROM posted_posts 
            WHERE uri = %s AND platform = %s AND status = 'completed'
        """, (uri, platform))
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except Exception as e:
        print(f"Error checking if post is posted: {e}")
        return False

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def extract_images_from_embed(embed):
    """Extract image URLs from post embed"""
    images = []
    
    if not embed:
        return images
    
    if hasattr(embed, 'images') and embed.images:
        for img in embed.images:
            image_data = {}
            
            if hasattr(img, 'fullsize'):
                image_data['url'] = img.fullsize
            elif hasattr(img, 'thumb'):
                image_data['url'] = img.thumb
            else:
                continue
            
            if hasattr(img, 'thumb'):
                image_data['thumb'] = img.thumb
            else:
                image_data['thumb'] = image_data['url']
            
            if hasattr(img, 'alt'):
                image_data['alt'] = img.alt or ''
            else:
                image_data['alt'] = ''
            
            images.append(image_data)
    
    return images

def extract_video_from_embed(embed):
    """Extract video information from post embed"""
    video = None
    
    if not embed:
        return video
    
    if hasattr(embed, 'playlist'):
        thumbnail = None
        if hasattr(embed, 'thumbnail'):
            thumbnail = embed.thumbnail
        
        if not thumbnail and hasattr(embed, 'cid'):
            cid = embed.cid
            thumbnail = f"https://video.bsky.app/watch/did%3Aplc%3A{embed.cid}/thumbnail.jpg"
        
        video = {
            'playlist': embed.playlist,
            'cid': embed.cid if hasattr(embed, 'cid') else None,
            'thumbnail': thumbnail,
            'type': 'hls'
        }
        return video
    
    if hasattr(embed, 'video'):
        vid = embed.video
        video = {
            'type': 'direct',
            'cid': vid.cid if hasattr(vid, 'cid') else None
        }
        if hasattr(vid, 'ref') and hasattr(vid.ref, 'link'):
            video['blob_ref'] = vid.ref.link
        return video
    
    return video

def is_repost(post):
    try:
        record_type = getattr(post.record, '$type', '')
        if record_type and 'repost' in record_type.lower():
            return True
        return False
    except:
        return False

def is_reply(post):
    try:
        if hasattr(post.record, 'reply') and post.record.reply:
            return True
        return False
    except:
        return False

def get_original_author(post):
    try:
        if hasattr(post, 'record') and hasattr(post.record, 'subject'):
            if hasattr(post.record.subject, 'author'):
                if hasattr(post.record.subject.author, 'handle'):
                    return post.record.subject.author.handle
        return None
    except:
        return None

def download_video_segments_cdn(cid, did, quality='720p', session_string=None):
    """Download video segments using CDN URL format"""
    segments = []
    max_segments = 100
    
    qualities = [quality, '720p', '480p', '360p', '240p']
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'video/mp2t, video/*, application/octet-stream'
    }
    if session_string:
        headers['Authorization'] = f'Bearer {session_string}'
    
    url_formats = [
        f"https://video.cdn.bsky.app/hls/{did}/{cid}/{{quality}}/video{{i}}.ts",
        f"https://video.bsky.app/hls/{did}/{cid}/{{quality}}/video{{i}}.ts",
    ]
    
    for q in qualities:
        print(f"   Trying quality: {q}")
        found_segments = []
        
        for url_format in url_formats:
            if found_segments:
                break
            
            for i in range(1, max_segments + 1):
                segment_url = url_format.format(quality=q, i=i)
                
                try:
                    response = requests.head(segment_url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        found_segments.append(segment_url)
                    elif response.status_code == 404 and i > 3:
                        break
                except Exception as e:
                    pass
        
        if found_segments:
            print(f"   ✅ Found {len(found_segments)} segments for {q}")
            return found_segments
    
    return segments

# ============================================================
# ROUTES
# ============================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'success': False, 'error': 'Username and password required'}), 400
    
    try:
        client = Client()
        client.login(username, password)
        profile = client.get_profile(username)
        
        session_id = f"{username}_{int(datetime.now().timestamp())}"
        sessions[session_id] = {
            'client': client,
            'username': username,
            'handle': profile.handle,
            'display_name': profile.display_name or username,
            'avatar': profile.avatar
        }
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'handle': profile.handle,
            'display_name': profile.display_name or username,
            'avatar': profile.avatar
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    data = request.json
    session_id = data.get('session_id')
    
    if session_id and session_id in sessions:
        del sessions[session_id]
    
    return jsonify({'success': True})

@app.route('/api/verify-session', methods=['POST'])
def verify_session():
    """Verify if a session is still valid"""
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id or session_id not in sessions:
        return jsonify({
            'success': False, 
            'error': 'Session expired or invalid',
            'valid': False
        }), 401
    
    session_data = sessions[session_id]
    return jsonify({
        'success': True,
        'valid': True,
        'handle': session_data['handle'],
        'display_name': session_data['display_name'],
        'avatar': session_data['avatar']
    })

@app.route('/api/fetch-posts', methods=['POST'])
def fetch_posts():
    data = request.json
    session_id = data.get('session_id')
    actor = data.get('actor')
    limit = data.get('limit', 50)
    cursor = data.get('cursor', None)
    filter_type = data.get('filter', 'posts_no_replies')
    include_reposts = data.get('include_reposts', True)
    only_target_user = data.get('only_target_user', True)
    
    if not session_id or session_id not in sessions:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    if not actor:
        return jsonify({'success': False, 'error': 'Actor (handle) required'}), 400
    
    try:
        client = sessions[session_id]['client']
        
        print(f"🔍 Fetching posts for @{actor}")
        print(f"   Filter: {filter_type}, Include reposts: {include_reposts}, Only target user: {only_target_user}")
        
        resolved = client.resolve_handle(actor)
        actor_did = resolved.did if hasattr(resolved, 'did') else None
        
        if not actor_did:
            return jsonify({'success': False, 'error': f'Could not resolve handle: {actor}'}), 400
        
        result = client.get_author_feed(
            actor=actor,
            limit=min(limit * 2, 100),
            cursor=cursor
        )
        
        posts = []
        total_images = 0
        total_videos = 0
        repost_count = 0
        post_count = 0
        reply_count = 0
        filtered_count = 0
        other_user_posts = 0
        target_user_posts = 0
        
        conn = get_db_connection()
        deleted_uris = set()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT uri FROM deleted_posts WHERE handler_handle = %s", (actor,))
                deleted_uris = {row[0] for row in cur.fetchall()}
                cur.close()
                conn.close()
            except Exception as e:
                print(f"Error fetching deleted posts: {e}")
        
        for item in result.feed:
            post = item.post
            post_uri = post.uri if hasattr(post, 'uri') else ''
            
            if post_uri in deleted_uris:
                continue
            
            post_author_handle = post.author.handle if hasattr(post.author, 'handle') else None
            is_from_target_user = post_author_handle and post_author_handle.lower() == actor.lower()
            
            if only_target_user and not is_from_target_user:
                other_user_posts += 1
                continue
            
            if is_from_target_user:
                target_user_posts += 1
            
            is_repost_flag = is_repost(post)
            is_reply_flag = is_reply(post)
            original_author = None
            
            should_include = True
            
            if filter_type == 'posts_no_replies':
                if is_reply_flag:
                    should_include = False
                    reply_count += 1
                if is_repost_flag and not include_reposts:
                    should_include = False
            
            elif filter_type == 'posts_and_author_threads':
                if is_reply_flag:
                    reply_count += 1
                if is_repost_flag and not include_reposts:
                    should_include = False
            
            elif filter_type == 'posts_with_media':
                has_media = hasattr(post, 'embed') and post.embed
                has_images = has_media and hasattr(post.embed, 'images') and post.embed.images
                has_video = has_media and (hasattr(post.embed, 'playlist') or hasattr(post.embed, 'video'))
                if not (has_images or has_video):
                    should_include = False
                if is_repost_flag and not include_reposts:
                    should_include = False
            
            elif filter_type == 'posts_with_replies':
                if post.reply_count == 0 or post.reply_count is None:
                    should_include = False
                if is_repost_flag and not include_reposts:
                    should_include = False
            
            if not should_include:
                filtered_count += 1
                continue
            
            if is_repost_flag:
                repost_count += 1
                original_author = get_original_author(post)
            else:
                post_count += 1
            
            images = []
            video = None
            
            if hasattr(post, 'embed') and post.embed:
                images = extract_images_from_embed(post.embed)
                video = extract_video_from_embed(post.embed)
                total_images += len(images)
                if video:
                    total_videos += 1
            
            post_data = {
                'uri': post_uri,
                'author': post_author_handle or 'unknown',
                'display_name': post.author.display_name if hasattr(post.author, 'display_name') else post_author_handle or 'unknown',
                'text': post.record.text if hasattr(post.record, 'text') else '',
                'likes': post.like_count if hasattr(post, 'like_count') else 0,
                'reposts': post.repost_count if hasattr(post, 'repost_count') else 0,
                'replies': post.reply_count if hasattr(post, 'reply_count') else 0,
                'created_at': post.record.created_at if hasattr(post.record, 'created_at') else '',
                'avatar': post.author.avatar if hasattr(post.author, 'avatar') else None,
                'images': images,
                'video': video,
                'has_media': bool(images or video),
                'media_count': len(images),
                'has_video': bool(video),
                'is_repost': is_repost_flag,
                'is_reply': is_reply_flag,
                'original_author': original_author,
                'is_from_target_user': is_from_target_user
            }
            
            posts.append(post_data)
            
            if len(posts) >= limit:
                break
        
        print(f"✅ Fetched {len(posts)} posts")
        print(f"   Target user posts: {target_user_posts}, Other users: {other_user_posts}")
        print(f"   Original: {post_count}, Reposts: {repost_count}, Replies: {reply_count}")
        print(f"   Filtered out: {filtered_count}, Total images: {total_images}, Total videos: {total_videos}")
        
        return jsonify({
            'success': True,
            'posts': posts,
            'cursor': result.cursor if hasattr(result, 'cursor') else None,
            'count': len(posts),
            'total_images': total_images,
            'total_videos': total_videos,
            'actor': actor,
            'actor_did': actor_did,
            'filter_used': filter_type,
            'include_reposts': include_reposts,
            'only_target_user': only_target_user,
            'stats': {
                'original_posts': post_count,
                'reposts': repost_count,
                'replies': reply_count,
                'total_fetched': len(posts),
                'filtered_out': filtered_count,
                'target_user_posts': target_user_posts,
                'other_user_posts': other_user_posts
            }
        })
    except Exception as e:
        print(f"❌ Error in fetch_posts: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/resolve-handle', methods=['POST'])
def resolve_handle():
    data = request.json
    session_id = data.get('session_id')
    handle = data.get('handle')
    
    if not session_id or session_id not in sessions:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    if not handle:
        return jsonify({'success': False, 'error': 'Handle required'}), 400
    
    try:
        client = sessions[session_id]['client']
        profile = client.get_profile(handle)
        
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO handlers (handle, display_name, avatar, selected, is_default)
                    VALUES (%s, %s, %s, TRUE, FALSE)
                    ON CONFLICT (handle) DO UPDATE SET 
                        display_name = EXCLUDED.display_name,
                        avatar = EXCLUDED.avatar
                ''', (handle, profile.display_name or handle, profile.avatar))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print(f"Error saving handler: {e}")
        
        return jsonify({
            'success': True,
            'profile': {
                'handle': profile.handle,
                'display_name': profile.display_name or profile.handle,
                'avatar': profile.avatar if hasattr(profile, 'avatar') else None,
                'description': profile.description if hasattr(profile, 'description') else '',
                'followers': profile.followers_count if hasattr(profile, 'followers_count') else 0,
                'following': profile.follows_count if hasattr(profile, 'follows_count') else 0,
                'posts': profile.posts_count if hasattr(profile, 'posts_count') else 0
            }
        })
    except Exception as e:
        print(f"❌ Error in resolve_handle: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# VIDEO STREAMING ENDPOINT
# ============================================================

@app.route('/api/download-video', methods=['GET', 'POST'])
def download_video():
    """Stream video segments - returns video file for playback"""
    if request.method == 'GET':
        cid = request.args.get('cid')
        did = request.args.get('did')
        quality = request.args.get('quality', '720p')
        session_id = request.args.get('session_id')
    else:
        data = request.json
        cid = data.get('cid')
        did = data.get('did')
        quality = data.get('quality', '720p')
        session_id = data.get('session_id')
    
    if not cid or not did:
        return jsonify({'success': False, 'error': 'CID and DID required'}), 400
    
    if '.' in did and not did.startswith('did:'):
        try:
            if session_id and session_id in sessions:
                client = sessions[session_id]['client']
                resolved = client.resolve_handle(did)
                did = resolved.did
                print(f"   Resolved handle to DID: {did}")
            else:
                temp_client = Client()
                resolved = temp_client.resolve_handle(did)
                did = resolved.did
                print(f"   Resolved handle to DID: {did}")
        except Exception as e:
            print(f"   Could not resolve handle: {e}")
    
    try:
        session_string = None
        if session_id and session_id in sessions:
            try:
                client = sessions[session_id]['client']
                session_string = client.export_session_string()
            except:
                pass
        
        segments = download_video_segments_cdn(cid, did, quality, session_string)
        
        if not segments:
            return jsonify({'success': False, 'error': 'No video segments found'}), 404
        
        print(f"   Found {len(segments)} segments, streaming...")
        
        def generate_video():
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            if session_string:
                headers['Authorization'] = f'Bearer {session_string}'
            
            for i, segment_url in enumerate(segments, 1):
                try:
                    response = requests.get(segment_url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        yield response.content
                        print(f"   Segment {i}/{len(segments)} streamed")
                    else:
                        print(f"   Segment {i} failed: {response.status_code}")
                except Exception as e:
                    print(f"   Segment {i} error: {e}")
        
        return Response(
            generate_video(),
            mimetype='video/mp4',
            headers={
                'Content-Disposition': f'inline; filename="video_{cid[:8]}.mp4"',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-cache'
            }
        )
    except Exception as e:
        print(f"❌ Error streaming video: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# VAULT ROUTES
# ============================================================

@app.route('/api/vault/add', methods=['POST'])
def vault_add():
    """Add a post to the vault"""
    data = request.json
    post = data.get('post')
    handler_handle = data.get('handler_handle')
    notes = data.get('notes', '')
    
    if not post:
        return jsonify({'success': False, 'error': 'Post data required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        
        images_json = Json(post.get('images', []))
        video_json = Json(post.get('video')) if post.get('video') else None
        
        try:
            cur.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS video JSONB")
            conn.commit()
        except Exception as e:
            print(f"Note: video column may already exist: {e}")
        
        cur.execute('''
            INSERT INTO vault (uri, author, display_name, text, images, video, likes, reposts, replies, created_at, handler_handle, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (uri) DO UPDATE SET
                author = EXCLUDED.author,
                display_name = EXCLUDED.display_name,
                text = EXCLUDED.text,
                images = EXCLUDED.images,
                video = EXCLUDED.video,
                likes = EXCLUDED.likes,
                reposts = EXCLUDED.reposts,
                replies = EXCLUDED.replies,
                notes = EXCLUDED.notes
        ''', (
            post.get('uri'),
            post.get('author'),
            post.get('display_name'),
            post.get('text'),
            images_json,
            video_json,
            post.get('likes', 0),
            post.get('reposts', 0),
            post.get('replies', 0),
            post.get('created_at'),
            handler_handle,
            notes
        ))
        
        cur.execute("SELECT id FROM vault WHERE uri = %s", (post.get('uri'),))
        result = cur.fetchone()
        vault_id = result[0] if result else None
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Post saved to vault', 'vault_id': vault_id})
    except Exception as e:
        print(f"❌ Error adding to vault: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vault/list', methods=['POST'])
def vault_list():
    """Get all posts from the vault"""
    data = request.json
    handler_handle = data.get('handler_handle')
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        
        if handler_handle:
            cur.execute('''
                SELECT * FROM vault WHERE handler_handle = %s ORDER BY saved_at DESC
            ''', (handler_handle,))
        else:
            cur.execute('SELECT * FROM vault ORDER BY saved_at DESC')
        
        rows = cur.fetchall()
        
        vault_items = []
        for row in rows:
            vault_items.append({
                'id': row[0],
                'uri': row[1],
                'author': row[2],
                'display_name': row[3],
                'text': row[4],
                'images': row[5],
                'video': row[6],
                'likes': row[7],
                'reposts': row[8],
                'replies': row[9],
                'created_at': row[10],
                'saved_at': row[11],
                'handler_handle': row[12],
                'notes': row[13]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'vault': vault_items})
    except Exception as e:
        print(f"❌ Error listing vault: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vault/remove', methods=['POST'])
def vault_remove():
    """Remove a post from the vault and add to deleted_posts"""
    data = request.json
    uri = data.get('uri')
    handler_handle = data.get('handler_handle')
    
    if not uri:
        return jsonify({'success': False, 'error': 'URI required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        
        cur.execute('DELETE FROM vault WHERE uri = %s', (uri,))
        
        cur.execute('''
            INSERT INTO deleted_posts (uri, handler_handle) 
            VALUES (%s, %s)
            ON CONFLICT (uri) DO NOTHING
        ''', (uri, handler_handle))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Post removed from vault'})
    except Exception as e:
        print(f"❌ Error removing from vault: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vault/delete-permanent', methods=['POST'])
def vault_delete_permanent():
    """Permanently delete a post from vault"""
    data = request.json
    uri = data.get('uri')
    
    if not uri:
        return jsonify({'success': False, 'error': 'URI required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        
        cur.execute('DELETE FROM vault WHERE uri = %s', (uri,))
        cur.execute('DELETE FROM deleted_posts WHERE uri = %s', (uri,))
        cur.execute('DELETE FROM posted_posts WHERE uri = %s', (uri,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Post permanently deleted'})
    except Exception as e:
        print(f"❌ Error permanently deleting: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vault/restore', methods=['POST'])
def vault_restore():
    """Restore a post from deleted_posts (make it fetchable again)"""
    data = request.json
    uri = data.get('uri')
    
    if not uri:
        return jsonify({'success': False, 'error': 'URI required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute('DELETE FROM deleted_posts WHERE uri = %s', (uri,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Post restored'})
    except Exception as e:
        print(f"❌ Error restoring: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/vault/update-notes', methods=['POST'])
def vault_update_notes():
    """Update notes for a vault item"""
    data = request.json
    uri = data.get('uri')
    notes = data.get('notes', '')
    
    if not uri:
        return jsonify({'success': False, 'error': 'URI required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute('UPDATE vault SET notes = %s WHERE uri = %s', (notes, uri))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Notes updated'})
    except Exception as e:
        print(f"❌ Error updating notes: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/handlers/list', methods=['POST'])
def handlers_list():
    """Get all saved handlers (DB is source of truth)"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500

        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT handle, display_name, avatar, "
                "COALESCE(selected, TRUE), COALESCE(is_default, FALSE) "
                "FROM handlers ORDER BY is_default DESC, added_at DESC"
            )
            rows = cur.fetchall()
            handlers = []
            for row in rows:
                handlers.append({
                    'handle': row[0],
                    'display_name': row[1] or row[0],
                    'avatar': row[2],
                    'selected': bool(row[3]),
                    'is_default': bool(row[4])
                })
        except Exception as col_err:
            print(f"handlers list fallback: {col_err}")
            cur.execute('SELECT handle, display_name, avatar FROM handlers ORDER BY added_at DESC')
            rows = cur.fetchall()
            handlers = [{
                'handle': row[0],
                'display_name': row[1] or row[0],
                'avatar': row[2],
                'selected': True,
                'is_default': i == 0
            } for i, row in enumerate(rows)]



        cur.close()
        conn.close()
        return jsonify({'success': True, 'handlers': handlers})
    except Exception as e:
        print(f"❌ Error listing handlers: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/handlers/add', methods=['POST'])
def add_handler_route():
    """Add a handler to the database (persists across refresh)"""
    data = request.json or {}
    session_id = data.get('session_id')
    handle = (data.get('handle') or '').strip().lstrip('@')

    if not handle:
        return jsonify({'success': False, 'error': 'Handle required'}), 400
    if '.' not in handle:
        handle = handle + '.bsky.social'

    display_name = handle
    avatar = None

    if session_id and session_id in sessions:
        try:
            client = sessions[session_id]['client']
            profile = client.get_profile(handle)
            handle = profile.handle or handle
            display_name = profile.display_name or handle
            avatar = profile.avatar if hasattr(profile, 'avatar') else None
        except Exception as e:
            print(f"⚠️ resolve on add failed (still saving handle): {e}")

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500

        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM handlers')
        count = cur.fetchone()[0]
        is_default = (count == 0)

        cur.execute(
            "INSERT INTO handlers (handle, display_name, avatar, selected, is_default) "
            "VALUES (%s, %s, %s, TRUE, %s) "
            "ON CONFLICT (handle) DO UPDATE SET "
            "display_name = COALESCE(EXCLUDED.display_name, handlers.display_name), "
            "avatar = COALESCE(EXCLUDED.avatar, handlers.avatar) "
            "RETURNING handle, display_name, avatar, "
            "COALESCE(selected, TRUE), COALESCE(is_default, FALSE)",
            (handle, display_name, avatar, is_default)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            'success': True,
            'handler': {
                'handle': row[0],
                'display_name': row[1] or row[0],
                'avatar': row[2],
                'selected': bool(row[3]),
                'is_default': bool(row[4])
            },
            'message': f'Handler @{handle} saved'
        })
    except Exception as e:
        print(f"❌ Error adding handler: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/handlers/save-selection', methods=['POST'])
def save_handlers_selection():
    """Persist which handlers are selected / default"""
    data = request.json or {}
    items = data.get('handlers', [])
    if not items:
        return jsonify({'success': False, 'error': 'No handlers provided'}), 400

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        cur = conn.cursor()

        default_handle = None
        for item in items:
            handle = (item.get('handle') or '').strip()
            if not handle:
                continue
            selected = bool(item.get('selected', True))
            is_default = bool(item.get('is_default', False))
            if is_default:
                default_handle = handle
            cur.execute(
                "INSERT INTO handlers (handle, display_name, selected, is_default) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (handle) DO UPDATE SET "
                "selected = EXCLUDED.selected, is_default = EXCLUDED.is_default",
                (handle, handle, selected, is_default)
            )

        if default_handle:
            cur.execute('UPDATE handlers SET is_default = FALSE')
            cur.execute(
                'UPDATE handlers SET is_default = TRUE, selected = TRUE WHERE handle = %s',
                (default_handle,)
            )

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True, 'message': f'Saved selection for {len(items)} handlers'})
    except Exception as e:
        print(f"❌ Error saving selection: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/handlers/remove', methods=['POST'])
def remove_handler():
    """Remove a handler from the database"""
    data = request.json
    session_id = data.get('session_id')
    handle = data.get('handle')
    
    if not session_id or session_id not in sessions:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    if not handle:
        return jsonify({'success': False, 'error': 'Handle required'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute('DELETE FROM handlers WHERE handle = %s', (handle,))
        cur.execute('DELETE FROM deleted_posts WHERE handler_handle = %s', (handle,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': f'Handler @{handle} removed'})
    except Exception as e:
        print(f"❌ Error removing handler: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# VAULT BULK SCHEDULING ROUTES
# ============================================================




@app.route('/api/vault/schedule-bulk', methods=['POST'])
def vault_schedule_bulk():
    """Schedule multiple vault posts with random distribution over a period"""
    data = request.json
    uris = data.get('uris', [])
    platforms = data.get('platforms', ['instagram'])
    period = data.get('period', '24h')
    start_date_str = data.get('start_date')
    min_hours_between = data.get('min_hours_between', 2)
    content_type = data.get('content_type', 'feed')
    timezone = data.get('timezone', 'Africa/Nairobi')
    account_ids = data.get('account_ids')
    
    if not uris:
        return jsonify({'success': False, 'error': 'No posts selected'}), 400
    
    if not platforms:
        return jsonify({'success': False, 'error': 'No platforms selected'}), 400
    
    try:
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            if start_date.tzinfo is None:
                start_date = tz.localize(start_date)
            if start_date < now:
                start_date = now + timedelta(hours=1)
        else:
            start_date = now + timedelta(hours=1)
        
        # PERIOD MAPPING - FIXED
        period_days = {
            '24h': 1,      # 1 day = 24 hours
            'week': 7,
            'month': 30,
            'year': 365
        }
        days_to_add = period_days.get(period, 7)
        end_date = start_date + timedelta(days=days_to_add)
        
        print(f"📅 Schedule period: {period} -> {days_to_add} days")
        print(f"📅 Start: {start_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📅 End: {end_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 Total posts: {len(uris)}")
        print(f"⏰ Min hours between: {min_hours_between}")
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(uris))
        cur.execute(f"""
            SELECT * FROM vault WHERE uri IN ({placeholders})
        """, uris)
        rows = cur.fetchall()
        cur.close()
        
        if not rows:
            conn.close()
            return jsonify({'success': False, 'error': 'No posts found in vault'}), 404
        
        vault_posts = []
        for row in rows:
            vault_posts.append({
                'id': row[0],
                'uri': row[1],
                'author': row[2],
                'display_name': row[3],
                'text': row[4],
                'images': row[5],
                'video': row[6],
                'likes': row[7],
                'reposts': row[8],
                'replies': row[9],
                'created_at': row[10],
                'saved_at': row[11],
                'handler_handle': row[12],
                'notes': row[13]
            })
        
        conn.close()
        
        total_posts = len(vault_posts)
        
        # Generate schedule times within the range
        schedule_times = generate_random_schedule_times(
            start_date=start_date,
            end_date=end_date,
            total_posts=total_posts,
            min_hours_between=min_hours_between,
            tz=tz
        )
        
        # Verify schedule times are within range
        print(f"✅ Generated {len(schedule_times)} schedule times")
        if schedule_times:
            print(f"   First: {schedule_times[0].strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"   Last: {schedule_times[-1].strftime('%Y-%m-%d %H:%M:%S')}")
        
        scheduled_count = 0
        failed_posts = []
        scheduled_posts = []
        
        for i, (post, schedule_time) in enumerate(zip(vault_posts, schedule_times)):
            try:
                images = post.get('images', [])
                image_url = None
                if images and len(images) > 0:
                    image_url = images[0].get('url')
                
                if not image_url:
                    failed_posts.append({
                        'uri': post['uri'],
                        'text': post.get('text', '')[:50],
                        'error': 'No image found'
                    })
                    continue
                
                text = post.get('text', '')
                author = post.get('author', '')
                
                caption = f"📝 {text[:200]}" if text else f"Post from @{author}"
                if len(caption) > 2200:
                    caption = caption[:2200] + "..."
                
                result = post_to_zernio(
                    image_url=image_url,
                    caption=caption,
                    platforms=platforms,
                    scheduled_time=schedule_time,
                    content_type=content_type
                )
                
                if result.get('success'):
                    for platform in platforms:
                        mark_post_as_posted(
                            vault_id=post['id'],
                            uri=post['uri'],
                            platform=platform,
                            platform_post_id=result.get('post_id'),
                            status='scheduled'
                        )
                    
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        notes = post.get('notes', '') or ''
                        # Store full datetime with time
                        schedule_str = schedule_time.strftime('%Y-%m-%d %H:%M')
                        notes += f"\n\n📅 Scheduled for {schedule_str} ({timezone})"
                        print(f"📝 Saving: {schedule_str}")
                        cur.execute("UPDATE vault SET notes = %s WHERE uri = %s", (notes, post['uri']))
                        conn.commit()
                        cur.close()
                        conn.close()
                    
                    scheduled_count += 1
                    scheduled_posts.append({
                        'uri': post['uri'],
                        'text': post.get('text', '')[:50],
                        'scheduled_time': schedule_time.isoformat(),
                        'scheduled_display': schedule_time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                else:
                    failed_posts.append({
                        'uri': post['uri'],
                        'text': post.get('text', '')[:50],
                        'error': result.get('error', 'Unknown error')
                    })
                
                time.sleep(1)
                
            except Exception as e:
                failed_posts.append({
                    'uri': post.get('uri', 'unknown'),
                    'text': post.get('text', '')[:50],
                    'error': str(e)
                })
                print(f"Error scheduling post: {e}")
        
        period_names = {
            '24h': '24 hours',
            'week': '1 week',
            'month': '1 month',
            'year': '1 year'
        }
        
        return jsonify({
            'success': True,
            'scheduled_count': scheduled_count,
            'failed_count': len(failed_posts),
            'scheduled_posts': scheduled_posts,
            'failed_posts': failed_posts,
            'period': period,
            'period_name': period_names.get(period, period),
            'timezone': timezone,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat(),
            'message': f"✅ Scheduled {scheduled_count} posts randomly over {period_names.get(period, period)}!"
        })
        
    except Exception as e:
        print(f"Error in bulk scheduling: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500









def generate_random_schedule_times(start_date, end_date, total_posts, min_hours_between=2, tz=None):
    """Generate random schedule times within a date range"""
    if tz is None:
        tz = pytz.timezone('Africa/Nairobi')
    
    if start_date.tzinfo is None:
        start_date = tz.localize(start_date)
    if end_date.tzinfo is None:
        end_date = tz.localize(end_date)
    
    now = datetime.now(tz)
    if start_date < now:
        start_date = now + timedelta(hours=1)
    
    total_seconds = (end_date - start_date).total_seconds()
    
    # If total_seconds is too small, extend the range
    if total_seconds <= 0:
        end_date = start_date + timedelta(days=1)
        total_seconds = 86400
    
    # Ensure we have enough time for all posts with min gap
    min_total_seconds = (total_posts - 1) * min_hours_between * 3600
    if total_seconds < min_total_seconds:
        # Extend end date to accommodate all posts with minimum gap
        extra_seconds = min_total_seconds - total_seconds
        end_date = end_date + timedelta(seconds=extra_seconds)
        total_seconds = (end_date - start_date).total_seconds()
        print(f"⏰ Extended schedule range to accommodate {total_posts} posts with {min_hours_between}h gap")
    
    schedule_times = []
    
    # Generate random times
    for i in range(total_posts):
        # Generate random offset within the range
        random_offset = random.random() * total_seconds
        post_time = start_date + timedelta(seconds=random_offset)
        
        # Ensure post_time is within bounds
        if post_time > end_date:
            post_time = end_date - timedelta(minutes=random.randint(1, 60))
        
        schedule_times.append(post_time)
    
    # Sort times
    schedule_times.sort()
    
    # Apply minimum gap between posts
    if min_hours_between > 0:
        final_times = []
        for time in schedule_times:
            if not final_times:
                final_times.append(time)
            else:
                last_time = final_times[-1]
                gap_seconds = (time - last_time).total_seconds()
                if gap_seconds < min_hours_between * 3600:
                    # Move this post forward to maintain minimum gap
                    new_time = last_time + timedelta(hours=min_hours_between) + timedelta(minutes=random.randint(0, 30))
                    # Check if new_time exceeds end_date
                    if new_time <= end_date:
                        final_times.append(new_time)
                    else:
                        # Try to spread within remaining time
                        remaining_time = (end_date - last_time).total_seconds()
                        if remaining_time >= min_hours_between * 3600:
                            # Insert somewhere in the remaining window
                            new_time = last_time + timedelta(seconds=random.randint(int(min_hours_between * 3600), int(remaining_time)))
                            final_times.append(new_time)
                        else:
                            # Just append with minimum gap even if it exceeds end_date slightly
                            final_times.append(last_time + timedelta(hours=min_hours_between))
                else:
                    final_times.append(time)
        schedule_times = final_times
    
    # Ensure we have exactly total_posts times
    while len(schedule_times) < total_posts:
        # Add more times within the range
        random_offset = random.random() * total_seconds
        post_time = start_date + timedelta(seconds=random_offset)
        if post_time > end_date:
            post_time = end_date - timedelta(minutes=random.randint(1, 60))
        schedule_times.append(post_time)
        schedule_times.sort()
    
    # Trim to total_posts
    schedule_times = schedule_times[:total_posts]
    
    # Log the schedule for debugging
    print(f"📊 Generated {len(schedule_times)} schedule times:")
    for i, t in enumerate(schedule_times):
        print(f"   {i+1}: {t.strftime('%Y-%m-%d %H:%M:%S')}")
    
    return schedule_times



















@app.route('/api/vault/schedule-preview', methods=['POST'])
def vault_schedule_preview():
    """Preview schedule times without actually posting"""
    data = request.json
    total_posts = data.get('total_posts', 10)
    period = data.get('period', '24h')
    start_date_str = data.get('start_date')
    min_hours_between = data.get('min_hours_between', 2)
    timezone = data.get('timezone', 'Africa/Nairobi')
    
    try:
        tz = pytz.timezone(timezone)
        now = datetime.now(tz)
        
        if start_date_str:
            start_date = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
            if start_date.tzinfo is None:
                start_date = tz.localize(start_date)
        else:
            start_date = now + timedelta(hours=1)
        
        period_days = {
            '24h': 1,
            'week': 7,
            'month': 30,
            'year': 365
        }
        days_to_add = period_days.get(period, 7)
        end_date = start_date + timedelta(days=days_to_add)
        
        schedule_times = generate_random_schedule_times(
            start_date=start_date,
            end_date=end_date,
            total_posts=total_posts,
            min_hours_between=min_hours_between,
            tz=tz
        )
        
        formatted_times = []
        for time in schedule_times:
            formatted_times.append({
                'datetime': time.isoformat(),
                'formatted': time.strftime('%Y-%m-%d %H:%M:%S'),
                'day': time.strftime('%A'),
                'date': time.strftime('%Y-%m-%d'),
                'time': time.strftime('%H:%M')
            })
        
        period_names = {
            '24h': '24 hours',
            'week': '1 week',
            'month': '1 month',
            'year': '1 year'
        }
        
        return jsonify({
            'success': True,
            'total': len(formatted_times),
            'times': formatted_times,
            'period': period,
            'period_name': period_names.get(period, period),
            'timezone': timezone,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        })
        
    except Exception as e:
        print(f"Error generating preview: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# DOWNLOAD ROUTES
# ============================================================

@app.route('/api/download-images', methods=['POST'])
def download_images():
    data = request.json
    posts = data.get('posts', [])
    actor = data.get('actor', 'unknown')
    
    if not posts:
        return jsonify({'success': False, 'error': 'No posts to download'}), 400
    
    try:
        downloaded = []
        failed = []
        total_images = 0
        
        download_dir = f"downloads_{actor}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(download_dir, exist_ok=True)
        
        for post_idx, post in enumerate(posts, 1):
            if post.get('images'):
                for img_idx, img in enumerate(post['images'], 1):
                    image_url = img.get('url')
                    if not image_url:
                        continue
                    
                    total_images += 1
                    filename = f"post_{post_idx}_img_{img_idx}.jpg"
                    filepath = os.path.join(download_dir, filename)
                    
                    try:
                        response = requests.get(image_url, timeout=60)
                        if response.status_code == 200:
                            with open(filepath, 'wb') as f:
                                f.write(response.content)
                            downloaded.append({
                                'file': filepath,
                                'url': image_url,
                                'post_text': post.get('text', '')[:100]
                            })
                        else:
                            failed.append({'url': image_url, 'error': f'HTTP {response.status_code}'})
                    except Exception as e:
                        failed.append({'url': image_url, 'error': str(e)})
            
            if post.get('video') and post.get('video', {}).get('cid'):
                video_data = post['video']
                cid = video_data.get('cid')
                did = post.get('author')
                
                if cid:
                    try:
                        segments = download_video_segments_cdn(cid, did, '720p')
                        if segments:
                            video_filename = f"post_{post_idx}_video.mp4"
                            video_filepath = os.path.join(download_dir, video_filename)
                            with open(video_filepath, 'wb') as outfile:
                                for segment_url in segments:
                                    seg_response = requests.get(segment_url, timeout=30)
                                    if seg_response.status_code == 200:
                                        outfile.write(seg_response.content)
                            downloaded.append({
                                'file': video_filepath,
                                'url': f"Video from post {post_idx}",
                                'post_text': post.get('text', '')[:100]
                            })
                    except Exception as e:
                        print(f"Error downloading video for post {post_idx}: {e}")
        
        metadata = {
            'actor': actor,
            'downloaded_at': datetime.now().isoformat(),
            'total_images': total_images,
            'downloaded': len(downloaded),
            'failed': len(failed),
            'files': downloaded
        }
        
        with open(os.path.join(download_dir, 'metadata.json'), 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, os.path.dirname(download_dir))
                    zip_file.write(file_path, arcname)
        
        zip_buffer.seek(0)
        
        return send_file(
            zip_buffer,
            as_attachment=True,
            download_name=f"{actor}_media_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mimetype='application/zip'
        )
        
    except Exception as e:
        print(f"❌ Error in download_images: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/save-posts', methods=['POST'])
def save_posts_json():
    data = request.json
    posts = data.get('posts', [])
    actor = data.get('actor', 'unknown')
    filename = data.get('filename', f"{actor}_posts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    
    if not posts:
        return jsonify({'success': False, 'error': 'No posts to save'}), 400
    
    try:
        os.makedirs('exports', exist_ok=True)
        filepath = os.path.join('exports', filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(posts, f, indent=2, ensure_ascii=False)
        
        return send_file(
            filepath,
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"❌ Error in save_posts_json: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# ZERNIO POSTING ROUTES
# ============================================================

@app.route('/api/zernio/accounts', methods=['GET'])
def zernio_accounts():
    """Get connected Zernio accounts (optionally filter ?platform=instagram)"""
    try:
        platform = request.args.get('platform')
        accounts = get_zernio_accounts()
        db_accounts = list_accounts_for_platform(platform) if platform else []
        if not platform:
            # all platforms
            for p in ('instagram', 'facebook', 'twitter', 'linkedin', 'threads'):
                db_accounts.extend(list_accounts_for_platform(p))
            # dedupe
            seen = set()
            uniq = []
            for a in db_accounts:
                if a['account_id'] in seen:
                    continue
                seen.add(a['account_id'])
                uniq.append(a)
            db_accounts = uniq

        return jsonify({
            'success': True,
            'accounts': accounts,
            'db_accounts': db_accounts,
            'instagram': list_accounts_for_platform('instagram')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/zernio/post', methods=['POST'])
def zernio_post():
    """Post a vault item to Zernio"""
    data = request.json
    vault_id = data.get('vault_id')
    uri = data.get('uri')
    platforms = data.get('platforms', [])
    caption = data.get('caption', '')
    schedule_time = data.get('schedule_time')
    post_type = data.get('post_type', 'image')
    content_type = data.get('content_type', 'feed')
    account_ids = data.get('account_ids')  # list of zernio account ids or dict
    
    if not uri:
        return jsonify({'success': False, 'error': 'URI required'}), 400
    
    if not platforms:
        return jsonify({'success': False, 'error': 'No platforms selected'}), 400
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute("SELECT * FROM vault WHERE uri = %s", (uri,))
        row = cur.fetchone()
        cur.close()
        
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Post not found in vault'}), 404
        
        for platform in platforms:
            if is_post_already_posted(uri, platform):
                return jsonify({
                    'success': False, 
                    'error': f'This post has already been posted to {platform}',
                    'already_posted': True
                }), 400
        
        images = row[5]
        image_url = None
        
        if images and len(images) > 0:
            image_url = images[0].get('url')
        
        if not image_url:
            conn.close()
            return jsonify({'success': False, 'error': 'No image found in this post'}), 400
        
        text = row[4]
        author = row[2]
        display_name = row[3]
        
        if not caption:
            caption = f"📝 {text[:200]}" if text else f"Post from @{author}"
            if len(caption) > 2200:
                caption = caption[:2200] + "..."
        
        scheduled_datetime = None
        if schedule_time:
            try:
                scheduled_datetime = datetime.fromisoformat(schedule_time.replace('Z', '+00:00'))
            except:
                pass
        
        result = post_to_zernio(image_url, caption, platforms, scheduled_datetime, content_type, account_ids=account_ids)
        
        if result.get('success'):
            for platform in platforms:
                mark_post_as_posted(
                    vault_id=row[0],
                    uri=uri,
                    platform=platform,
                    platform_post_id=result.get('post_id'),
                    status='completed'
                )
            
            notes = row[13] or ''
            posted_platforms = ', '.join(platforms)
            content_type_label = 'Story' if content_type == 'story' else 'Feed'
            notes += f"\n\n📤 Posted to {posted_platforms} ({content_type_label}) on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            cur = conn.cursor()
            cur.execute("UPDATE vault SET notes = %s WHERE uri = %s", (notes, uri))
            conn.commit()
            cur.close()
        
        conn.close()
        
        return jsonify(result)
        
    except Exception as e:
        print(f"❌ Error posting to Zernio: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/zernio/test', methods=['GET'])
def test_zernio():
    """Test Zernio API connection"""
    try:
        accounts = get_zernio_accounts()
        profile_id = get_or_create_zernio_profile()
        
        return jsonify({
            'success': True,
            'accounts_found': len(accounts),
            'profile_id': profile_id,
            'accounts': accounts
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/zernio/profiles', methods=['GET'])
def zernio_profiles():
    """Get Zernio profiles from database"""
    try:
        conn = get_db_connection()
        profiles = []
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT profile_id, name, description, created_at FROM zernio_profiles ORDER BY created_at DESC")
            for row in cur.fetchall():
                profiles.append({
                    'profile_id': row[0],
                    'name': row[1],
                    'description': row[2],
                    'created_at': row[3]
                })
            cur.close()
            conn.close()
        
        return jsonify({'success': True, 'profiles': profiles})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500








@app.route('/api/vault/scheduled', methods=['GET'])
def get_scheduled_posts():
    """Get all scheduled posts with their full datetime - using Threads-style datetime handling"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute("""
            SELECT uri, notes, text, author, display_name, created_at, images, saved_at
            FROM vault 
            WHERE notes LIKE '%Scheduled for%'
            ORDER BY saved_at DESC
        """)
        
        rows = cur.fetchall()
        scheduled_posts = []
        
        for row in rows:
            notes = row[1] or ''
            # Extract the scheduled time from notes
            import re
            match = re.search(r'Scheduled for ([\d\-:]+)', notes)
            schedule_time = None
            scheduled_datetime = None
            timezone = 'Africa/Nairobi'
            
            if match:
                schedule_time_str = match.group(1).strip()
                print(f"📅 Found scheduled time: {schedule_time_str}")
                
                # ============================================================
                # THREADS-STYLE DATETIME PARSING
                # ============================================================
                try:
                    # Try parsing with time (YYYY-MM-DD HH:MM)
                    if ' ' in schedule_time_str:
                        dt = datetime.strptime(schedule_time_str, '%Y-%m-%d %H:%M')
                        scheduled_datetime = dt
                        schedule_time = dt.isoformat()
                        print(f"   ✅ Parsed with time: {dt}")
                    else:
                        # Date only - check if notes contain a time
                        time_match = re.search(r'Scheduled for \d{4}-\d{2}-\d{2} (\d{2}:\d{2})', notes)
                        if time_match:
                            time_str = time_match.group(1)
                            dt = datetime.strptime(f"{schedule_time_str} {time_str}", '%Y-%m-%d %H:%M')
                            scheduled_datetime = dt
                            schedule_time = dt.isoformat()
                            print(f"   ✅ Parsed with extracted time: {dt}")
                        else:
                            # Try to find any time in the notes
                            any_time = re.search(r'(\d{1,2}):(\d{2})', notes)
                            if any_time:
                                hour = int(any_time.group(1))
                                minute = int(any_time.group(2))
                                dt = datetime.strptime(schedule_time_str, '%Y-%m-%d')
                                dt = dt.replace(hour=hour, minute=minute)
                                scheduled_datetime = dt
                                schedule_time = dt.isoformat()
                                print(f"   ✅ Parsed with any time: {dt}")
                            else:
                                # Default to 03:00 AM if no time found
                                dt = datetime.strptime(schedule_time_str, '%Y-%m-%d')
                                dt = dt.replace(hour=3, minute=0)
                                scheduled_datetime = dt
                                schedule_time = dt.isoformat()
                                print(f"   ⚠️ Using default 03:00 for: {dt}")
                except ValueError as e:
                    print(f"   ❌ Parse error: {e}")
                    # Try ISO format
                    try:
                        dt = datetime.fromisoformat(schedule_time_str)
                        scheduled_datetime = dt
                        schedule_time = dt.isoformat()
                    except:
                        pass
                
                # Extract timezone
                tz_match = re.search(r'\(([^)]+)\)', notes)
                if tz_match:
                    timezone = tz_match[1]
            
            # Check if the datetime is in the future
            is_future = False
            if scheduled_datetime:
                # Make it timezone-aware for comparison
                if scheduled_datetime.tzinfo is None:
                    tz = pytz.timezone('Africa/Nairobi')
                    scheduled_datetime = tz.localize(scheduled_datetime)
                is_future = scheduled_datetime > datetime.now(pytz.UTC)
            
            scheduled_posts.append({
                'uri': row[0],
                'notes': notes,
                'text': row[2],
                'author': row[3],
                'display_name': row[4],
                'created_at': row[5].isoformat() if row[5] else None,
                'images': row[6],
                'saved_at': row[7].isoformat() if row[7] else None,
                'scheduled_time': schedule_time,
                'scheduled_datetime': scheduled_datetime.isoformat() if scheduled_datetime else None,
                'timezone': timezone,
                'is_future': is_future
            })
        
        cur.close()
        conn.close()
        
        # Count future posts
        future_count = sum(1 for p in scheduled_posts if p.get('is_future', False))
        
        return jsonify({
            'success': True,
            'scheduled_posts': scheduled_posts,
            'total': len(scheduled_posts),
            'future_count': future_count
        })
    except Exception as e:
        print(f"❌ Error getting scheduled posts: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500














# ============================================================
# POST NOW - Direct Instagram Posting
# ============================================================

@app.route('/api/post-now/accounts', methods=['GET'])
def get_post_now_accounts():
    """Get available Instagram accounts for Post Now"""
    try:
        accounts = []
        
        # Get accounts from Zernio
        zernio_accounts = get_zernio_accounts()
        
        # Filter Instagram accounts
        ig_accounts = [acc for acc in zernio_accounts if acc.get('platform') == 'instagram']
        
        for acc in ig_accounts:
            username = acc.get('username', '')
            display_name = acc.get('displayName', '')
            acc_id = acc.get('_id', '')
            
            # Try to identify the account
            account_type = 'unknown'
            label = username or display_name or acc_id
            
            if 'serpent' in username.lower() or 'serpent' in display_name.lower():
                account_type = 'serpent'
                label = '🐍 Serpent'
            elif 'eastern' in username.lower() or 'eastern' in display_name.lower():
                account_type = 'easternfront'
                label = '🌅 Eastern Front'
            
            accounts.append({
                'account_id': acc_id,
                'username': username,
                'display_name': display_name,
                'account_type': account_type,
                'label': label,
                'platform': 'instagram',
                'profile_picture': acc.get('profilePicture')
            })
        
        return jsonify({
            'success': True,
            'accounts': accounts,
            'count': len(accounts)
        })
    except Exception as e:
        print(f"❌ Error getting post-now accounts: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500











@app.route('/api/post-now', methods=['POST'])
def post_now():
    data = request.json
    image_url = data.get('image_url')
    caption = data.get('caption', '')
    account_id = data.get('account_id')
    account_name = data.get('account')
    content_type = data.get('content_type', 'feed')
    vault_uri = data.get('vault_uri')  # NEW — real vault post uri, when posting from vault

    if not image_url:
        return jsonify({'success': False, 'error': 'Image URL required'}), 400

    if account_name and not account_id:
        accounts = get_zernio_accounts()
        for acc in accounts:
            if acc.get('platform') != 'instagram':
                continue
            username = acc.get('username', '').lower()
            display_name = acc.get('displayName', '').lower()
            if account_name.lower() in username or account_name.lower() in display_name:
                account_id = acc.get('_id')
                break

    if not account_id:
        return jsonify({
            'success': False,
            'error': 'No Instagram account selected. Please select an account to post to.'
        }), 400

    # NEW — same guard /api/zernio/post already has
    vault_id = None
    if vault_uri:
        if is_post_already_posted(vault_uri, 'instagram'):
            return jsonify({
                'success': False,
                'error': 'This post has already been posted to instagram',
                'already_posted': True
            }), 400
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM vault WHERE uri = %s", (vault_uri,))
                row = cur.fetchone()
                if row:
                    vault_id = row[0]
                cur.close()
                conn.close()
        except Exception as e:
            print(f"post_now vault lookup error: {e}")

    try:
        result = post_to_zernio(
            image_url=image_url,
            caption=caption or "📸 Posted via Bluesky Vault",
            platforms=['instagram'],
            scheduled_time=None,
            content_type=content_type,
            account_ids=[account_id]
        )

        if result.get('success'):
            # Use the REAL vault uri when we have one, so it lines up with
            # is_post_already_posted()/loadPostedStatus() everywhere else.
            tracking_uri = vault_uri or f"post_now_{datetime.now().timestamp()}"

            mark_post_as_posted(
                vault_id=vault_id,
                uri=tracking_uri,
                platform='instagram',
                platform_post_id=result.get('post_id'),
                status='completed'
            )

            # Mirror the notes-append that /api/zernio/post does
            if vault_uri:
                try:
                    conn = get_db_connection()
                    if conn:
                        cur = conn.cursor()
                        cur.execute("SELECT notes FROM vault WHERE uri = %s", (vault_uri,))
                        row = cur.fetchone()
                        notes = (row[0] if row else '') or ''
                        label = 'Story' if content_type == 'story' else 'Feed'
                        notes += f"\n\n📤 Posted to instagram ({label}) on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                        cur.execute("UPDATE vault SET notes = %s WHERE uri = %s", (notes, vault_uri))
                        conn.commit()
                        cur.close()
                        conn.close()
                except Exception as e:
                    print(f"post_now notes update error: {e}")

            return jsonify({
                'success': True,
                'message': '✅ Posted successfully!',
                'post_id': result.get('post_id'),
                'account': account_name or account_id
            })
        else:
            return jsonify({'success': False, 'error': result.get('error', 'Unknown error')}), 500

    except Exception as e:
        print(f"❌ Post now error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500





























@app.route('/api/post-now/preview', methods=['POST'])
def post_now_preview():
    """Preview a post before sending"""
    data = request.json
    image_url = data.get('image_url')
    caption = data.get('caption', '')
    account_name = data.get('account', 'serpent')
    
    return jsonify({
        'success': True,
        'preview': {
            'image_url': image_url,
            'caption': caption or '📸 Posted via Bluesky Vault',
            'account': account_name,
            'platform': 'instagram',
            'content_type': 'feed'
        }
    })











@app.route('/api/zernio/posts', methods=['GET'])
def zernio_posts():
    """Get posts that have been posted to social media"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute("""
            SELECT pp.*, v.text, v.author, v.display_name 
            FROM posted_posts pp
            LEFT JOIN vault v ON pp.uri = v.uri
            ORDER BY pp.posted_at DESC
            LIMIT 100
        """)
        
        posts = []
        for row in cur.fetchall():
            posts.append({
                'id': row[0],
                'vault_id': row[1],
                'uri': row[2],
                'platform': row[3],
                'platform_post_id': row[4],
                'status': row[5],
                'posted_at': row[6],
                'error_message': row[7],
                'text': row[9] if len(row) > 9 else None,
                'author': row[10] if len(row) > 10 else None,
                'display_name': row[11] if len(row) > 11 else None
            })
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'posts': posts})
    except Exception as e:
        print(f"❌ Error getting posted posts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500














# ============================================================
# AUTO-NEWS MODULE - Register routes
# ============================================================

try:
    from auto_news_poster import register_auto_news_routes
    AUTO_NEWS_AVAILABLE = True
except ImportError as e:
    AUTO_NEWS_AVAILABLE = False
    print(f"⚠️ Auto news module not found: {e}")

if AUTO_NEWS_AVAILABLE:
    try:
        # Register routes with autostart=True to start the background scheduler
        register_auto_news_routes(app, autostart=True, default_interval_sec=10)
        print("✅ Auto-news routes registered successfully")
        print("✅ Auto-news background scheduler started (checks every 10s)")
    except Exception as e:
        print(f"⚠️ Error registering auto-news routes: {e}")
        traceback.print_exc()
else:
    # Add placeholder routes to avoid 404s if module not found
    @app.route('/api/auto-news/config', methods=['GET', 'POST'])
    def auto_news_placeholder_config():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded. Please check auto_news_poster.py exists.'
        }), 404
    
    @app.route('/api/auto-news/status', methods=['GET'])
    def auto_news_placeholder_status():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded.'
        }), 404
    
    @app.route('/api/auto-news/run', methods=['POST'])
    def auto_news_placeholder_run():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded.'
        }), 404
    
    @app.route('/api/auto-news/seen', methods=['GET'])
    def auto_news_placeholder_seen():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded.'
        }), 404
    
    @app.route('/api/auto-news/start', methods=['POST'])
    def auto_news_placeholder_start():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded.'
        }), 404
    
    @app.route('/api/auto-news/stop', methods=['POST'])
    def auto_news_placeholder_stop():
        return jsonify({
            'success': False, 
            'error': 'Auto-news module not loaded.'
        }), 404
    
    print("⚠️ Auto-news routes not registered - module not available")







if __name__ == "__main__":
    print("🚀 Starting Bluesky Vault with Zernio Integration...")
    print(f"📦 Database: {os.environ.get('DATABASE_URL', '')[:30]}...")
    port = int(os.environ.get('PORT', 10000))
    app.run(debug=False, host='0.0.0.0', port=port)