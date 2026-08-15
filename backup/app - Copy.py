import requests
import os
import json
from datetime import datetime

# ============================================================
# CONFIG
# ============================================================

USERNAME = "dailymotivator.bsky.social"
PASSWORD = "jnyu-ax57-veye-po6b"

POST_LIMIT = 100

OUTPUT_DIR = "bluesky_images"
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ============================================================
# LOGIN
# ============================================================

print("🔐 Logging in...")

login_url = "https://bsky.social/xrpc/com.atproto.server.createSession"

login_response = requests.post(
    login_url,
    json={
        "identifier": USERNAME,
        "password": PASSWORD
    },
    timeout=30
)

login_response.raise_for_status()

session = login_response.json()

ACCESS_JWT = session["accessJwt"]
DID = session["did"]

print(f"✅ Logged in: {USERNAME}")
print(f"🆔 DID: {DID}")


# ============================================================
# GET POSTS
# ============================================================

print()
print("📥 Fetching posts...")

feed_url = "https://bsky.social/xrpc/app.bsky.feed.getAuthorFeed"

response = requests.get(
    feed_url,
    headers={
        "Authorization": f"Bearer {ACCESS_JWT}"
    },
    params={
        "actor": DID,
        "limit": POST_LIMIT,
        "filter": "posts_no_replies"
    },
    timeout=30
)

response.raise_for_status()

data = response.json()

feed = data.get("feed", [])

print(f"📊 Found {len(feed)} posts")
print("=" * 70)


# ============================================================
# PROCESS POSTS
# ============================================================

all_posts = []

image_number = 0

for post_number, item in enumerate(feed, 1):

    post = item["post"]

    record = post["record"]

    text = record.get("text", "")

    print()
    print(f"📝 POST #{post_number}")
    print(f"   Text: {text or '[No text]'}")

    embed = post.get("embed")

    if not embed:

        print("   📷 No images")
        continue

    images = embed.get("images", [])

    if not images:

        print("   📷 No images")
        continue

    print(f"   📸 Found {len(images)} image(s)")

    post_images = []

    # ========================================================
    # DOWNLOAD EACH IMAGE
    # ========================================================

    for image_index, image in enumerate(images, 1):

        image_number += 1

        alt = image.get("alt", "")

        print()
        print(f"   🖼️ Image #{image_index}")

        # ----------------------------------------------------
        # RAW IMAGE OBJECT
        # ----------------------------------------------------

        image_blob = image.get("image", {})

        ref = image_blob.get("ref", {})

        # Bluesky normally gives the CID as $link
        cid = ref.get("$link")

        # Fallback
        if not cid:
            cid = ref.get("link")

        if not cid:

            print("   ❌ Could not find image CID")

            print("   Raw image object:")
            print(
                json.dumps(
                    image,
                    indent=2,
                    default=str
                )
            )

            continue

        print(f"   🆔 CID: {cid}")

        # ----------------------------------------------------
        # BUILD THE EXACT BLUESKY CDN URL
        # ----------------------------------------------------

        image_url = (
            "https://cdn.bsky.app/img/"
            "feed_fullsize/plain/"
            f"{DID}/{cid}"
        )

        print(f"   🔗 {image_url}")

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        filename = (
            f"post_{post_number}_"
            f"image_{image_index}.jpg"
        )

        filepath = os.path.join(
            OUTPUT_DIR,
            filename
        )

        try:

            image_response = requests.get(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0"
                },
                timeout=60
            )

            print(
                f"   🌐 HTTP: "
                f"{image_response.status_code}"
            )

            if image_response.status_code == 200:

                with open(
                    filepath,
                    "wb"
                ) as f:

                    f.write(
                        image_response.content
                    )

                size = len(
                    image_response.content
                )

                print(
                    f"   ✅ Downloaded: "
                    f"{filepath}"
                )

                print(
                    f"   📦 Size: "
                    f"{size:,} bytes"
                )

                post_images.append({

                    "cid": cid,

                    "url": image_url,

                    "alt": alt,

                    "file": filepath

                })

            else:

                print(
                    f"   ❌ Download failed: "
                    f"{image_response.status_code}"
                )

        except Exception as e:

            print(
                f"   ❌ Error: {e}"
            )

    # --------------------------------------------------------
    # SAVE POST
    # --------------------------------------------------------

    all_posts.append({

        "uri": post.get("uri", ""),

        "cid": post.get("cid", ""),

        "text": text,

        "created_at": record.get(
            "createdAt",
            ""
        ),

        "likes": post.get(
            "likeCount",
            0
        ),

        "reposts": post.get(
            "repostCount",
            0
        ),

        "replies": post.get(
            "replyCount",
            0
        ),

        "images": post_images

    })


# ============================================================
# SAVE JSON
# ============================================================

timestamp = datetime.now().strftime(
    "%Y%m%d_%H%M%S"
)

json_file = os.path.join(
    OUTPUT_DIR,
    f"posts_{timestamp}.json"
)

with open(
    json_file,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        all_posts,
        f,
        indent=2,
        ensure_ascii=False
    )


# ============================================================
# SUMMARY
# ============================================================

downloaded = sum(
    len(post["images"])
    for post in all_posts
)

print()
print("=" * 70)
print("📊 COMPLETE")
print("=" * 70)

print(f"Posts found:       {len(feed)}")
print(f"Images downloaded: {downloaded}")

print()
print(f"📁 Images: {OUTPUT_DIR}")
print(f"📄 JSON:   {json_file}")

print()
print("✅ DONE!")