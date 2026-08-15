from flask import Flask, request, jsonify, send_from_directory, send_file, Response
from flask_cors import CORS
from atproto import Client
import json
import os
import requests
from datetime import datetime
import zipfile
import io
import traceback
import psycopg2
from psycopg2.extras import Json
import uuid
import urllib.parse
import re

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
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
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
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database init error: {e}")

# Initialize database on startup
init_db()

# Store client sessions
sessions = {}

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
    """Extract video information from post embed - returns thumbnail URL and CID"""
    video = None
    
    if not embed:
        return video
    
    # Check for View type with playlist (most common for videos)
    if hasattr(embed, 'playlist'):
        # Get thumbnail URL
        thumbnail = None
        if hasattr(embed, 'thumbnail'):
            thumbnail = embed.thumbnail
        
        # If no thumbnail, try to construct from CID
        if not thumbnail and hasattr(embed, 'cid'):
            cid = embed.cid
            # Thumbnail URL pattern
            thumbnail = f"https://video.bsky.app/watch/did%3Aplc%3A{embed.cid}/thumbnail.jpg"
        
        video = {
            'playlist': embed.playlist,
            'cid': embed.cid if hasattr(embed, 'cid') else None,
            'thumbnail': thumbnail,
            'type': 'hls'
        }
        return video
    
    # Check for direct video embed
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

def download_video_segments(cid, did, quality='720p'):
    """Download video segments from CDN"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        segments = []
        max_segments = 100
        
        for i in range(1, max_segments + 1):
            segment_url = f"https://video.cdn.bsky.app/hls/{did}/{cid}/{quality}/video{i}.ts"
            response = requests.head(segment_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                segments.append(segment_url)
            elif response.status_code == 404 and i > 3:
                break
            else:
                break
        
        return segments
    except Exception as e:
        print(f"Error getting video segments: {e}")
        return []

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

def post_to_dict(post, source_handler=None):
    """Convert a post to dictionary for saving"""
    images = []
    video = None
    
    if hasattr(post, 'embed') and post.embed:
        images = extract_images_from_embed(post.embed)
        video = extract_video_from_embed(post.embed)
    
    return {
        'uri': post.uri if hasattr(post, 'uri') else '',
        'author': post.author.handle if hasattr(post.author, 'handle') else 'unknown',
        'display_name': post.author.display_name if hasattr(post.author, 'display_name') else post.author.handle if hasattr(post.author, 'handle') else 'unknown',
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
        'is_repost': is_repost(post),
        'is_reply': is_reply(post),
        'original_author': get_original_author(post),
        'source_handler': source_handler
    }

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
                    INSERT INTO handlers (handle, display_name, avatar) 
                    VALUES (%s, %s, %s)
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
    
    # If DID is a handle, resolve it
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
# VAULT ROUTES WITH VIDEO SUPPORT
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
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Post saved to vault'})
    except Exception as e:
        print(f"❌ Error adding to vault: {e}")
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
    """Get all saved handlers"""
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
        
        cur = conn.cursor()
        cur.execute('SELECT handle, display_name, avatar FROM handlers ORDER BY added_at DESC')
        rows = cur.fetchall()
        
        handlers = []
        for row in rows:
            handlers.append({
                'handle': row[0],
                'display_name': row[1] or row[0],
                'avatar': row[2]
            })
        
        cur.close()
        conn.close()
        
        return jsonify({'success': True, 'handlers': handlers})
    except Exception as e:
        print(f"❌ Error listing handlers: {e}")
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
            
            # Download videos if present
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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=10000)