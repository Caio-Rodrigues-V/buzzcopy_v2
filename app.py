# ── INSTAGRAM ────────────────────────────────────────────────────────────────

@app.route("/collect/instagram/<username>", methods=["POST"])
def collect_instagram(username):
    """
    Coleta posts do Instagram via Apify e armazena no Supabase.
    Query params: limit (default 10)
    """
    limit = int(request.args.get("limit", 10))

    try:
        import requests as req

        apify_token = os.environ["APIFY_TOKEN"]
        url = f"https://api.apify.com/v2/acts/apify~instagram-scraper/run-sync-get-dataset-items?token={apify_token}"

        payload = {
            "directUrls": [f"https://www.instagram.com/{username}/"],
            "resultsType": "posts",
            "resultsLimit": limit,
        }

        response = req.post(url, json=payload, timeout=120)
        posts = response.json()

        if not isinstance(posts, list):
            return jsonify({"error": "Resposta inesperada do Apify", "raw": posts}), 500

        db = get_db()

        # Busca profile_id correspondente
        profile = (
            db.table("profiles")
            .select("id")
            .eq("platform", "instagram")
            .eq("platform_id", username)
            .single()
            .execute()
        )
        profile_id = profile.data["id"] if profile.data else None

        rows = []
        for post in posts:
            rows.append({
                "id":               post.get("id"),
                "profile_id":       profile_id,
                "owner_username":   post.get("ownerUsername"),
                "caption":          post.get("caption"),
                "post_type":        post.get("type"),
                "likes_count":      post.get("likesCount"),
                "comments_count":   post.get("commentsCount"),
                "video_view_count": post.get("videoViewCount"),
                "url":              post.get("url"),
                "hashtags":         post.get("hashtags", []),
                "posted_at":        post.get("timestamp"),
            })

        if rows:
            db.table("instagram_posts").upsert(rows, on_conflict="id").execute()

        return jsonify({
            "username":      username,
            "posts_saved":   len(rows),
            "profile_id":    profile_id,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500