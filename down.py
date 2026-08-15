import requests
import os
import sys
import re
from atproto import Client
from datetime import datetime
import json
import urllib.parse
import time

def test_noelreports_video():
    """
    Test script to download video from NOELREPORTS - USING CDN URL
    """
    
    # ============================================================
    # CONFIGURATION - EDIT THESE
    # ============================================================
    USERNAME = "dailymotivator.bsky.social"
    PASSWORD = "Hopefrey2004"
    
    TARGET_HANDLE = "noelreports.com"
    
    # ============================================================
    # LOGIN
    # ============================================================
    
    print("🔐 Logging in...")
    client = Client()
    
    try:
        client.login(USERNAME, PASSWORD)
        print(f"✅ Logged in as: {client.me.display_name or client.me.handle}")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return
    
    # ============================================================
    # FETCH LATEST POSTS
    # ============================================================
    
    print(f"\n🔍 Fetching latest posts from @{TARGET_HANDLE}...")
    
    try:
        resolved = client.resolve_handle(TARGET_HANDLE)
        did = resolved.did
        print(f"   Resolved to DID: {did}")
        
        feed = client.get_author_feed(
            actor=TARGET_HANDLE,
            limit=10,
            filter='posts_no_replies'
        )
        
        print(f"   Found {len(feed.feed)} posts")
        
    except Exception as e:
        print(f"❌ Error fetching feed: {e}")
        return
    
    # ============================================================
    # FIND POST #2 (the video post)
    # ============================================================
    
    target_post = None
    target_index = 0
    
    for idx, item in enumerate(feed.feed, 1):
        post = item.post
        text = post.record.text if hasattr(post.record, 'text') else ''
        
        print(f"\n🔍 Post #{idx}:")
        print(f"   Text: {text[:100]}{'...' if len(text) > 100 else ''}")
        
        if 'P-18 Terek radar' in text:
            target_post = post
            target_index = idx
            print(f"   ✅ Found target post #{idx}!")
            print(f"   URI: {post.uri}")
            break
    
    if not target_post:
        print("\n❌ Could not find the radar post")
        return
    
    # ============================================================
    # GET POST THREAD FOR MORE DETAILS
    # ============================================================
    
    print(f"\n📥 Fetching post thread for URI: {target_post.uri}")
    
    try:
        thread = client.get_post_thread(target_post.uri)
        
        if hasattr(thread, 'thread') and hasattr(thread.thread, 'post'):
            post = thread.thread.post
        else:
            post = target_post
            
    except Exception as e:
        print(f"   ⚠️ Could not fetch thread: {e}")
        post = target_post
    
    # ============================================================
    # EXTRACT VIDEO FROM VIEW EMBED
    # ============================================================
    
    print("\n🔍 Analyzing post structure:")
    print(f"   Post URI: {post.uri}")
    
    if hasattr(post, 'embed') and post.embed:
        embed = post.embed
        embed_type = type(embed).__name__
        print(f"   Embed type: {embed_type}")
        
        # For View type, we have playlist, cid, thumbnail
        playlist_uri = None
        cid = None
        thumbnail = None
        
        if hasattr(embed, 'playlist'):
            playlist_uri = embed.playlist
            print(f"   ✅ Playlist URI: {playlist_uri}")
        
        if hasattr(embed, 'cid'):
            cid = embed.cid
            print(f"   ✅ CID: {cid}")
        
        if hasattr(embed, 'thumbnail'):
            thumbnail = embed.thumbnail
            print(f"   ✅ Thumbnail: {thumbnail}")
        
        if not cid:
            print("   ❌ No CID found")
            return
        
        # ============================================================
        # DOWNLOAD VIDEO USING CDN URL
        # ============================================================
        
        download_video_from_cdn(client, did, cid, post)
        
    else:
        print("   ❌ No embed found")

def download_video_from_cdn(client, did, cid, post):
    """
    Download video using the CDN URL format
    """
    
    os.makedirs("downloads", exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print(f"\n📥 Attempting to download video from CDN...")
    print(f"   DID: {did}")
    print(f"   CID: {cid}")
    
    # ============================================================
    # METHOD 1: Try direct CDN segment download
    # ============================================================
    
    # Common qualities
    qualities = ['720p', '480p', '360p', '240p']
    max_segments = 100
    
    session_string = client.export_session_string()
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'video/mp2t, video/*, application/octet-stream',
        'Authorization': f'Bearer {session_string}'
    }
    
    for quality in qualities:
        print(f"\n   Trying quality: {quality}")
        
        # Try to find the segments for this quality
        segment_urls = []
        
        for i in range(1, max_segments + 1):
            segment_url = f"https://video.cdn.bsky.app/hls/{did}/{cid}/{quality}/video{i}.ts"
            print(f"      Checking: video{i}.ts...", end=' ')
            
            try:
                response = requests.head(segment_url, headers=headers, timeout=10)
                if response.status_code == 200:
                    print(f"✅ Found!")
                    segment_urls.append(segment_url)
                elif response.status_code == 404:
                    print(f"❌ 404")
                    # If we get a 404, this might be the end of the segments
                    if i > 3:  # If we've already found some segments, stop checking
                        break
                else:
                    print(f"⚠️ {response.status_code}")
                    # If we get a 401/403, we need to handle authentication differently
                    if response.status_code in [401, 403]:
                        print(f"      🔑 Authentication required")
                        break
            except Exception as e:
                print(f"❌ {str(e)[:30]}")
        
        if segment_urls:
            print(f"\n   ✅ Found {len(segment_urls)} segments for {quality}")
            
            # Download all segments
            filename = f"noelreports_video_{quality}_{timestamp}.mp4"
            filepath = os.path.join("downloads", filename)
            
            total_bytes = 0
            success_count = 0
            
            with open(filepath, 'wb') as outfile:
                for i, seg_url in enumerate(segment_urls, 1):
                    print(f"   Segment {i}/{len(segment_urls)}...", end=' ')
                    try:
                        seg_response = requests.get(seg_url, headers=headers, timeout=30)
                        if seg_response.status_code == 200:
                            outfile.write(seg_response.content)
                            total_bytes += len(seg_response.content)
                            success_count += 1
                            print(f"✅ ({len(seg_response.content)//1024}KB)")
                        else:
                            print(f"❌ {seg_response.status_code}")
                    except Exception as e:
                        print(f"❌ {str(e)[:30]}")
            
            file_size = total_bytes / (1024 * 1024)
            
            if file_size > 0.1:  # At least 0.1 MB
                print(f"\n✅ Video downloaded successfully!")
                print(f"   Quality: {quality}")
                print(f"   Segments: {success_count}/{len(segment_urls)}")
                print(f"   Saved to: {filepath}")
                print(f"   File size: {file_size:.2f} MB")
                
                save_caption(post, timestamp, cid, did, quality)
                return filepath
            else:
                os.remove(filepath)
                print(f"   ⚠️ File too small ({file_size:.2f} MB), trying next quality...")
        else:
            print(f"   ❌ No segments found for {quality}")
    
    # ============================================================
    # METHOD 2: Try the blob endpoint
    # ============================================================
    
    print(f"\n🔄 Trying blob endpoint...")
    
    try:
        endpoint = "https://bsky.social/xrpc/com.atproto.sync.getBlob"
        params = {"ref": cid}
        
        response = requests.get(endpoint, headers=headers, params=params, stream=True, timeout=30)
        
        if response.status_code == 200:
            filename = f"noelreports_video_blob_{timestamp}.mp4"
            filepath = os.path.join("downloads", filename)
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            file_size = os.path.getsize(filepath) / (1024 * 1024)
            
            if file_size > 0.1:
                print(f"\n✅ Video downloaded successfully via blob endpoint!")
                print(f"   Saved to: {filepath}")
                print(f"   File size: {file_size:.2f} MB")
                save_caption(post, timestamp, cid, did, "blob")
                return filepath
    except Exception as e:
        print(f"   ❌ Blob endpoint failed: {e}")
    
    # ============================================================
    # METHOD 3: Save info for manual download
    # ============================================================
    
    print(f"\n💾 Saving video info for manual download...")
    
    info_file = os.path.join("downloads", f"noelreports_video_info_{timestamp}.txt")
    
    text = post.record.text if hasattr(post.record, 'text') else '[No caption]'
    
    with open(info_file, 'w', encoding='utf-8') as f:
        f.write("=" * 60 + "\n")
        f.write("VIDEO INFO - MANUAL DOWNLOAD\n")
        f.write("=" * 60 + "\n\n")
        f.write(f"Post URI: {post.uri}\n")
        f.write(f"DID: {did}\n")
        f.write(f"CID: {cid}\n\n")
        f.write(f"Caption:\n{text}\n\n")
        f.write("To download the video, try one of these methods:\n\n")
        f.write("1. Use yt-dlp (recommended):\n")
        f.write(f"   yt-dlp https://video.bsky.app/watch/{did}/{cid}\n\n")
        f.write("2. Try the CDN URL pattern:\n")
        f.write(f"   https://video.cdn.bsky.app/hls/{did}/{cid}/720p/video1.ts\n")
        f.write(f"   (then increment video1, video2, etc.)\n\n")
        f.write("3. Use ffmpeg:\n")
        f.write(f"   ffmpeg -i https://video.bsky.app/playlist/{did}/{cid}/video.m3u8 -c copy output.mp4\n\n")
        f.write("4. Try the blob endpoint with curl:\n")
        f.write(f"   curl -H 'Authorization: Bearer YOUR_TOKEN' 'https://bsky.social/xrpc/com.atproto.sync.getBlob?ref={cid}' --output video.mp4\n")
    
    print(f"   💾 Info saved to: {info_file}")
    print(f"   🔗 Post URL: https://bsky.app/profile/noelreports.com/post/{post.uri.split('/')[-1]}")

def save_caption(post, timestamp, cid, did, quality):
    """Save the caption and metadata"""
    
    text = post.record.text if hasattr(post.record, 'text') else '[No caption]'
    caption_file = os.path.join("downloads", f"noelreports_caption_{timestamp}.txt")
    
    with open(caption_file, 'w', encoding='utf-8') as f:
        f.write(f"Source: @noelreports.com\n")
        f.write(f"Post URI: {post.uri}\n")
        f.write(f"DID: {did}\n")
        f.write(f"CID: {cid}\n")
        f.write(f"Quality: {quality}\n")
        f.write(f"\nCaption:\n{text}\n")
    
    print(f"   💾 Caption saved to: {caption_file}")

# ============================================================
# RUN THE TEST
# ============================================================

if __name__ == "__main__":
    test_noelreports_video()