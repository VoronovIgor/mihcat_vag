import os
import sqlite3
import secrets as _secrets
from functools import wraps

from flask import (Flask, render_template, request, redirect, flash, Response)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vag-admin-2025-secret")

DB_PATH    = os.environ.get("KEYS_DB",     "/data/vag_api_keys.sqlite")
ADMIN_USER = os.environ.get("ADMIN_USER",  "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS",  "VagAdmin2025!")
API_URL    = os.environ.get("API_URL",     "http://91.216.248.151:8572")


# ─── DB ──────────────────────────────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_keys():
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT id, key_value, description, active, created_at, last_used, requests "
            "FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ─── Auth ────────────────────────────────────────────────────────────────────
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != ADMIN_PASS:
            return Response(
                "Требуется авторизация", 401,
                {"WWW-Authenticate": 'Basic realm="VAG API Admin"'},
            )
        return f(*args, **kwargs)
    return decorated


# ─── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
@requires_auth
def index():
    return render_template("index.html", api_url=API_URL)


@app.route("/apikeys")
@requires_auth
def apikeys():
    return render_template("apikeys.html", keys=list_keys(), api_url=API_URL)


@app.route("/apikeys/create", methods=["POST"])
@requires_auth
def apikeys_create():
    description = request.form.get("description", "").strip()
    if not description:
        flash("Укажите название клиента", "danger")
        return redirect("/apikeys")

    key = "vag-" + _secrets.token_hex(16)
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO api_keys (key_value, description) VALUES (?, ?)",
            (key, description)
        )
        conn.commit()
        conn.close()
        flash(f"Ключ создан для «{description}»", "success")
        return render_template("apikeys.html", keys=list_keys(),
                               api_url=API_URL, new_key=key)
    except Exception as e:
        flash(f"Ошибка: {e}", "danger")
        return redirect("/apikeys")


@app.route("/apikeys/revoke", methods=["POST"])
@requires_auth
def apikeys_revoke():
    key_id = request.form.get("key_id", "")
    try:
        conn = get_db()
        conn.execute("UPDATE api_keys SET active=0 WHERE id=?", (key_id,))
        conn.commit()
        conn.close()
        flash("Ключ отозван", "success")
    except Exception as e:
        flash(f"Ошибка: {e}", "danger")
    return redirect("/apikeys")


@app.route("/apikeys/activate", methods=["POST"])
@requires_auth
def apikeys_activate():
    key_id = request.form.get("key_id", "")
    try:
        conn = get_db()
        conn.execute("UPDATE api_keys SET active=1 WHERE id=?", (key_id,))
        conn.commit()
        conn.close()
        flash("Ключ активирован", "success")
    except Exception as e:
        flash(f"Ошибка: {e}", "danger")
    return redirect("/apikeys")


@app.route("/apikeys/delete", methods=["POST"])
@requires_auth
def apikeys_delete():
    key_id = request.form.get("key_id", "")
    try:
        conn = get_db()
        conn.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
        conn.commit()
        conn.close()
        flash("Ключ удалён", "success")
    except Exception as e:
        flash(f"Ошибка: {e}", "danger")
    return redirect("/apikeys")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
