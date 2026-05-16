"""
auth.py — Autenticação JWT + decorators de proteção.
"""
import os
import logging
import jwt
import bcrypt
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import request, jsonify, g
from supabase import create_client

log = logging.getLogger("pulse.auth")

JWT_SECRET = os.environ.get("JWT_SECRET")
JWT_ALG = "HS256"
JWT_EXP_HOURS = 24 * 7  # 1 semana

if not JWT_SECRET or JWT_SECRET == "change-me-in-prod":
    raise RuntimeError("JWT_SECRET não configurado (ou ainda no default). Defina uma chave segura.")


# ── SINGLETON DB CLIENT (Fix #9) ──────────────────────────────────────────────
_db_client = None


def _db():
    global _db_client
    if _db_client is None:
        _db_client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _db_client


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def generate_token(user_id: str, role: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXP_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Token não fornecido"}), 401
        token = auth.split(" ", 1)[1]
        try:
            payload = decode_token(token)
            g.user_id = payload["user_id"]
            g.role = payload.get("role", "client")
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token expirado"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Token inválido"}), 401
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Token não fornecido"}), 401
        try:
            payload = decode_token(auth.split(" ", 1)[1])
            if payload.get("role") != "admin":
                return jsonify({"error": "Acesso restrito a admins"}), 403
            g.user_id = payload["user_id"]
            g.role = payload["role"]
        except Exception:
            return jsonify({"error": "Token inválido"}), 401
        return f(*args, **kwargs)
    return wrapper


def user_owns_profile(username: str, user_id: str, platform: str = "instagram") -> bool:
    """Verifica se o usuário é dono do perfil monitorado."""
    db = _db()
    res = (
        db.table("profiles")
        .select("id")
        .eq("platform_id", username)
        .eq("platform", platform)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(res.data)


# ── ROTAS ─────────────────────────────────────────────────────────────────────

def register_auth_routes(app):

    @app.route("/auth/register", methods=["POST"])
    @require_admin
    def register():
        data = request.get_json() or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        name = data.get("name", "")
        role = data.get("role", "client")

        if not email or not password:
            return jsonify({"error": "email e password obrigatórios"}), 400
        if len(password) < 8:
            return jsonify({"error": "Senha mínima de 8 caracteres"}), 400

        try:
            db = _db()
            existing = db.table("users").select("id").eq("email", email).execute()
            if existing.data:
                return jsonify({"error": "Email já cadastrado"}), 409

            result = db.table("users").insert({
                "email": email,
                "password_hash": hash_password(password),
                "name": name,
                "role": role,
            }).execute()

            # Log de criação de admin (Fix #16 mini)
            if role == "admin":
                log.warning("⚠️ Novo ADMIN criado: %s por %s", email, g.user_id)

            return jsonify({"user": {"id": result.data[0]["id"], "email": email, "role": role}}), 201
        except Exception as e:
            log.exception("register failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/auth/login", methods=["POST"])
    def login():
        data = request.get_json() or {}
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        if not email or not password:
            return jsonify({"error": "email e password obrigatórios"}), 400

        try:
            db = _db()
            res = db.table("users").select("*").eq("email", email).eq("active", True).limit(1).execute()
            if not res.data or not verify_password(password, res.data[0]["password_hash"]):
                return jsonify({"error": "Credenciais inválidas"}), 401

            user = res.data[0]
            token = generate_token(user["id"], user["role"])
            db.table("users").update({
                "last_login": datetime.now(timezone.utc).isoformat()
            }).eq("id", user["id"]).execute()

            return jsonify({
                "token": token,
                "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]},
                "expires_in_hours": JWT_EXP_HOURS,
            })
        except Exception as e:
            log.exception("login failed")
            return jsonify({"error": str(e)}), 500

    @app.route("/auth/me", methods=["GET"])
    @require_auth
    def me():
        try:
            db = _db()
            res = db.table("users").select("id, email, name, role, created_at, last_login").eq("id", g.user_id).execute()
            if not res.data:
                return jsonify({"error": "Usuário não encontrado"}), 404
            return jsonify(res.data[0])
        except Exception as e:
            log.exception("me failed")
            return jsonify({"error": str(e)}), 500


def create_first_admin(email: str, password: str, name: str = "Admin"):
    db = _db()
    if db.table("users").select("id").eq("email", email.lower()).execute().data:
        print(f"Já existe: {email}")
        return
    result = db.table("users").insert({
        "email": email.lower(),
        "password_hash": hash_password(password),
        "name": name,
        "role": "admin",
    }).execute()
    print(f"Admin criado: {result.data[0]['id']}")