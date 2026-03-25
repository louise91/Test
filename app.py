import os, json
import re, time
import requests
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, Response, g, session, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
import godseed
godseed.init_godseed(app)

# ── API Key 從 config.py 讀取（不要寫死在程式裡）──
try:
    from config import SECRET_KEY
    app.secret_key = SECRET_KEY
except ImportError:
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")

def load_json(relpath, default):
    try:
        with open(os.path.join(BASE, relpath), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

@app.route("/")
def index():
    return render_template("index.html")

@app.template_filter("twd")
def twd_filter(cents):
    try:
        cents = int(cents)
        return f"NT$ {int(cents)/100:,.0f}"
    except Exception:
        return "NT$ -"

@app.route("/shop")
def shop():
    catalog  = load_json("data/catalog.json", [])
    products = []
    for item in catalog:
        if not item.get("active", True):
            continue
        products.append({
            "slug":        item.get("slug", ""),
            "name":        item.get("name", "未命名商品"),
            "price_cents": item.get("price_cents", 0),
            "stock":       item.get("stock", 0),
            "img":         item.get("img") or url_for("static", filename="img/placeholder.png"),
            "desc":        item.get("desc", "")
        })
    return render_template("shop.html", products=products)

@app.route("/sim")
def show_sim():
    return render_template("sim.html")

@app.route("/portfolio")
def portfolio():
    return render_template("portfolio.html")

@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "GET":
        product = request.args.get("product", "")
        return render_template("contact.html", product=product)

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    if rate_limited(ip, seconds=60):
        flash("送出太頻繁，請稍後再試。")
        return redirect(url_for("contact"))

    name          = (request.form.get("name") or "").strip()
    email         = (request.form.get("email") or "").strip()
    business_type = (request.form.get("business_type") or "").strip()
    message       = (request.form.get("message") or "").strip()
    product       = (request.form.get("product") or "").strip()
    trap          = (request.form.get("website") or "").strip()

    if trap:
        flash("已送出。")
        return redirect(url_for("contact"))

    if not name or not EMAIL_RE.match(email) or not message:
        flash("請填入姓名、有效 Email 與訊息。")
        return redirect(url_for("contact"))

    allow = {"網站/自動化", "編織包/維修", "其他"}
    if business_type not in allow:
        business_type = "其他"

    entry = {
        "ts":            datetime.utcnow().isoformat() + "Z",
        "ip":            ip,
        "name":          name,
        "email":         email,
        "business_type": business_type,
        "product":       product,
        "message":       message[:2000]
    }
    with open(INQ_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print("[CONTACT]", name, email, business_type, product)
    flash("已送出，我們會盡快回覆。")
    return redirect(url_for("contact"))

@app.route("/services")
def services():
    return render_template("services.html")

@app.route("/policies")
def policies():
    return render_template("policies.html")

# ===== 站點設定 =====
DEFAULT_SITE = {
    "brand":   "織站工房 KnitSite Studio",
    "tagline": "讓手作的溫度，配上程式的精準",
    "email":   "@example.com",
    "phone":   "",
    "colors": {
        "primary": "#222222",
        "accent":  "#3b5b52",
        "bg":      "#ffffff"
    },
    "nav": [
        {"label": "商店",   "endpoint": "shop"},
        {"label": "作品集", "endpoint": "portfolio"},
        {"label": "服務",   "endpoint": "services"},
        {"label": "聯絡",   "endpoint": "contact"},
    ],
    "trustline": "不會要求 ATM/簡訊驗證碼｜第三方金流｜七日鑑賞｜政策與條款"
}

SITE     = load_json("site.json", DEFAULT_SITE)
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
INQ_LOG  = os.path.join(DATA_DIR, "inquiries.log")

EMAIL_RE   = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_last_post = {}

def rate_limited(ip: str, seconds: int = 60) -> bool:
    now = time.time()
    ts  = _last_post.get(ip, 0.0)
    if (now - ts) < seconds:
        return True
    _last_post[ip] = now
    return False

@app.context_processor
def inject_site_vars():
    return {
        "BRAND":         SITE.get("brand"),
        "TAGLINE":       SITE.get("tagline"),
        "NAV":           SITE.get("nav", []),
        "COLORS":        SITE.get("colors", {}),
        "CONTACT_EMAIL": SITE.get("email", ""),
        "CONTACT_PHONE": SITE.get("phone", ""),
        "TRUSTLINE":     SITE.get("trustline", ""),
    }

# ===== 整合 AI 聊天室 =====
from ai_routes import register_ai_routes
register_ai_routes(app)

if __name__ == "__main__":
    app.run(debug=False)