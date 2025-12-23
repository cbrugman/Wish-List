import json
import sqlite3
import requests
import os
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory, g, session, redirect, url_for, render_template_string
from urllib.parse import urlparse
from pathlib import Path
from datetime import date

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
DB_FILE = Path("/data/wishlist.db") if Path("/data").exists() else Path("wishlist.db")
WISHLIST_JSON = Path("wishlist.json")
ARCHIVE_JSON = Path("archive.json")

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_FILE)
        db.row_factory = sqlite3.Row  # Return rows as dict-like objects
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        # Ensure the directory exists if using the absolute path
        if DB_FILE.is_absolute():
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            
        db = get_db()
        cursor = db.cursor()
        
        # Create users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL
            )
        ''')
        
        # Create items table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                url TEXT NOT NULL,
                title TEXT,
                description TEXT,
                image TEXT,
                price TEXT,
                source TEXT,
                added_date TEXT,
                purchased BOOLEAN DEFAULT 0,
                archived BOOLEAN DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        # Migration for 'external_link' column
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN external_link TEXT")
            db.commit()
            print("Added external_link column to users.")
        except sqlite3.OperationalError:
            pass # Column likely exists

        # Migration: Check if we have legacy JSON data to import
        # We will import it under a default user "chris" ONLY if DB is empty
        cursor.execute("SELECT count(*) FROM users")
        if cursor.fetchone()[0] == 0:
            print("Migrating legacy data to SQLite...")
            cursor.execute("INSERT INTO users (username) VALUES ('chris')")
            user_id = cursor.lastrowid
            
            # Migrate Active Wishlist
            if WISHLIST_JSON.exists():
                try:
                    data = json.loads(WISHLIST_JSON.read_text(encoding="utf-8"))
                    for item in data:
                        cursor.execute('''
                            INSERT INTO items (user_id, url, title, description, image, price, source, added_date, purchased, archived)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        ''', (
                            user_id, 
                            item.get('url'), 
                            item.get('title'), 
                            item.get('description'), 
                            item.get('image'), 
                            item.get('price'), 
                            item.get('source'), 
                            item.get('added'), 
                            1 if item.get('purchased') else 0
                        ))
                except Exception as e:
                    print(f"Error migrating wishlist.json: {e}")

            # Migrate Archive
            if ARCHIVE_JSON.exists():
                try:
                    data = json.loads(ARCHIVE_JSON.read_text(encoding="utf-8"))
                    for item in data:
                        cursor.execute('''
                            INSERT INTO items (user_id, url, title, description, image, price, source, added_date, purchased, archived)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ''', (
                            user_id, 
                            item.get('url'), 
                            item.get('title'), 
                            item.get('description'), 
                            item.get('image'), 
                            item.get('price'), 
                            item.get('source'), 
                            item.get('added'), 
                            1 # Archived items are typically purchased, can assume so
                        ))
                except Exception as e:
                    print(f"Error migrating archive.json: {e}")
            
            db.commit()
            print("Migration complete.")

def fetch_metadata(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"Error fetching metadata: {e}")
        return None, None, None, None

    soup = BeautifulSoup(r.text, "html.parser")

    def og(name):
        tag = soup.find("meta", property=f"og:{name}")
        return tag["content"] if tag else None

    title = og("title") or (soup.title.string.strip() if soup.title else None)
    description = og("description")
    image = og("image")

    # Price Scraping
    price = None
    
    # 1. Check meta tags
    price_tag = soup.find("meta", property="product:price:amount") or \
                soup.find("meta", property="og:price:amount") or \
                soup.find("meta", itemprop="price")
    
    if price_tag and price_tag.get("content"):
        price = price_tag["content"]

    # 2. Check JSON-LD if no price yet
    if not price:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    # Handle @graph (list of objects)
                    if "@graph" in data:
                        objects = data["@graph"]
                    else:
                        objects = [data]
                    
                    for obj in objects:
                        if "offers" in obj:
                            offers = obj["offers"]
                            if isinstance(offers, list):
                                price = offers[0].get("price")
                            elif isinstance(offers, dict):
                                price = offers.get("price")
                            if price: break
            except:
                continue
            if price: break

    return title, description, image, price

# Initialize DB on startup
init_db()

@app.route("/")
def index():
    db = get_db()
    users = db.execute("SELECT username FROM users ORDER BY username COLLATE NOCASE ASC").fetchall()
    users_list = [dict(row) for row in users]
    return render_template_string(open("landing.html").read(), users=users_list)

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    if request.method == "POST":
        password = request.form.get("password")
        admin_pass = os.environ.get("ADMIN_PASSWORD")
        
        if admin_pass and password == admin_pass:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_page'))
        else:
            return render_template_string(open("admin.html").read(), error="Invalid Password")

    if not session.get('admin_logged_in'):
        return render_template_string(open("admin.html").read())

    # Logged In: Show Dashboard
    db = get_db()
    users = db.execute('''
        SELECT u.username, COUNT(i.id) as item_count 
        FROM users u 
        LEFT JOIN items i ON u.id = i.user_id 
        GROUP BY u.id
        ORDER BY u.username COLLATE NOCASE ASC
    ''').fetchall()
    
    users_list = [dict(row) for row in users]
    return render_template_string(open("admin.html").read(), users=users_list, logged_in=True)

@app.route("/api/admin/delete_user", methods=["POST"])
def admin_delete_user():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
        
    data = request.get_json()
    username = data.get("username")
    
    db = get_db()
    
    # Get ID
    cur = db.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if not row:
        return jsonify({"status": "error", "message": "User not found"}), 404
        
    user_id = row['id']
    
    # Cascade delete
    db.execute("DELETE FROM items WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    
    return jsonify({"status": "deleted"})

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop('admin_logged_in', None)
    return jsonify({"status": "logged_out"})


@app.route("/<username>")
def user_wishlist(username):
    if username == "favicon.ico":
        return "", 404
        
    # Check if user exists (do NOT create automatically)
    user_id = get_user_id(username, create=False)
    if not user_id:
        return "User not found. Please ask the administrator to create your profile.", 404

    base_dir = Path(__file__).parent.resolve()
    return send_from_directory(base_dir, "index.html")

# Helper to get user_id (create if not exists by default)
def get_user_id(username, create=True):
    db = get_db()
    cur = db.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    if row:
        return row['id']
    
    if not create:
        return None

    # Create user
    cur = db.execute("INSERT INTO users (username) VALUES (?)", (username,))
    db.commit()
    return cur.lastrowid

@app.route("/api/admin/add_user", methods=["POST"])
def admin_add_user():
    if not session.get('admin_logged_in'):
        return jsonify({"status": "error", "message": "Unauthorized"}), 403
        
    data = request.get_json()
    username = data.get("username")
    
    if not username:
        return jsonify({"status": "error", "message": "Username required"}), 400

    # Create user if not exists
    get_user_id(username, create=True)
    
    return jsonify({"status": "added", "username": username})

@app.route("/api/<username>/wishlist")
def get_wishlist(username):
    user_id = get_user_id(username)
    db = get_db()
    cur = db.execute('''
        SELECT url, title, description, image, price, source, added_date as added, purchased 
        FROM items 
        WHERE user_id = ? AND archived = 0 
        ORDER BY id DESC
    ''', (user_id,))
    
    items = [dict(row) for row in cur.fetchall()]
    return jsonify(items)

@app.route("/api/<username>/archive")
def get_archive_items(username):
    user_id = get_user_id(username)
    db = get_db()
    cur = db.execute('''
        SELECT url, title, description, image, price, source, added_date as added, purchased 
        FROM items 
        WHERE user_id = ? AND archived = 1 
        ORDER BY id DESC
    ''', (user_id,))
    
    items = [dict(row) for row in cur.fetchall()]
    return jsonify(items)

@app.route("/api/<username>/add", methods=["POST"])
def add_item(username):
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    user_id = get_user_id(username)
    db = get_db()
    
    # Check duplicate or archived
    cur = db.execute("SELECT id, archived FROM items WHERE user_id = ? AND url = ?", (user_id, url))
    row = cur.fetchone()
    
    if row:
        if row['archived']:
            # Restore it
            db.execute("UPDATE items SET archived = 0, purchased = 0, added_date = ? WHERE id = ?", (date.today().isoformat(), row['id']))
            db.commit()
            # Fetch metadata again? No, assume it's fine. Or maybe update it? Let's just restore.
            # Actually, return title so frontend can say "Restored X"
            cur = db.execute("SELECT title FROM items WHERE id = ?", (row['id'],))
            title = cur.fetchone()['title']
            return jsonify({"status": "restored", "title": title})
        else:
            return jsonify({"status": "exists"}), 200

    title, description, image, price = fetch_metadata(url)
    
    db.execute('''
        INSERT INTO items (user_id, url, title, description, image, price, source, added_date, purchased, archived)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
    ''', (
        user_id,
        url,
        title,
        description,
        image,
        price,
        urlparse(url).netloc,
        date.today().isoformat()
    ))
    db.commit()

    return jsonify({"status": "added", "title": title})

@app.route("/api/<username>/delete", methods=["POST"])
def delete_item(username):
    data = request.get_json()
    url_to_delete = data.get("url")
    user_id = get_user_id(username)
    
    db = get_db()
    db.execute("DELETE FROM items WHERE user_id = ? AND url = ?", (user_id, url_to_delete))
    db.commit()
    
    return jsonify({"status": "deleted"})

@app.route("/api/<username>/mark_purchased", methods=["POST"])
def mark_purchased(username):
    data = request.get_json()
    url = data.get("url")
    user_id = get_user_id(username)
    
    db = get_db()
    db.execute("UPDATE items SET purchased = 1 WHERE user_id = ? AND url = ?", (user_id, url))
    db.commit()
    return jsonify({"status": "marked"})

@app.route("/api/<username>/unmark_purchased", methods=["POST"])
def unmark_purchased(username):
    data = request.get_json()
    url = data.get("url")
    user_id = get_user_id(username)
    
    db = get_db()
    db.execute("UPDATE items SET purchased = 0 WHERE user_id = ? AND url = ?", (user_id, url))
    db.commit()
    return jsonify({"status": "unmarked"})

@app.route("/api/<username>/archive_purchased", methods=["POST"])
def archive_purchased(username):
    user_id = get_user_id(username)
    db = get_db()
    
    result = db.execute("UPDATE items SET archived = 1 WHERE user_id = ? AND purchased = 1 AND archived = 0", (user_id,))
    db.commit()
    
    return jsonify({"status": "archived", "count": result.rowcount})

@app.route("/api/<username>/restore", methods=["POST"])
def restore_item(username):
    data = request.get_json()
    url = data.get("url")
    user_id = get_user_id(username)
    
    db = get_db()
    
    # Check if active list already has this URL (deduplication)
    cur = db.execute("SELECT id FROM items WHERE user_id = ? AND url = ? AND archived = 0", (user_id, url))
    if cur.fetchone():
        return jsonify({"status": "exists_active"}), 400

    db.execute("UPDATE items SET archived = 0, purchased = 0 WHERE user_id = ? AND url = ?", (user_id, url))
    db.commit()
    return jsonify({"status": "restored"})

@app.route("/api/<username>/info")
def get_user_info(username):
    user_id = get_user_id(username, create=False)
    if not user_id:
        return jsonify({}), 404
    
    db = get_db()
    row = db.execute("SELECT external_link FROM users WHERE id = ?", (user_id,)).fetchone()
    return jsonify({"external_link": row['external_link']})

@app.route("/api/<username>/set_link", methods=["POST"])
def set_external_link(username):
    data = request.get_json()
    link = data.get("link")
    user_id = get_user_id(username)
    
    db = get_db()
    db.execute("UPDATE users SET external_link = ? WHERE id = ?", (link, user_id))
    db.commit()
    return jsonify({"status": "updated", "link": link})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
