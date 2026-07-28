# -*- coding: utf-8 -*-
"""Hedef CMDB API'si (simulasyon). Calistirma: pip install flask && python hedef_api.py
Kurallar:
 - Auth: once POST /api/token  body: {"client_id": "staj", "client_secret": "konsalt2026"}
   -> {"token": "..."}  (token 60 sn gecerli!)
 - POST /api/ci  header: Authorization: Bearer <token>
   body: {"ci_name": str, "ci_type": "server|application|network_device",
          "ip_address": str|null, "os": str|null, "owner_email": str|null,
          "ram_gb": int|null, "location": str}
 - Ayni ci_name ikinci kez POST edilirse 409 doner -> PUT /api/ci/<ci_name> ile guncelleyin.
 - API kasitli olarak %10 ihtimalle 503 doner (retry mantiginizi test etmek icin).
"""
import random, string, time
from flask import Flask, jsonify, request

app = Flask(__name__)
TOKENS = {}
DB = {}
VALID_TYPES = {"server", "application", "network_device"}

@app.post("/api/token")
def token():
    body = request.get_json(silent=True) or {}
    if body.get("client_id") == "staj" and body.get("client_secret") == "konsalt2026":
        t = "".join(random.choices(string.ascii_letters + string.digits, k=24))
        TOKENS[t] = time.time() + 60
        return jsonify({"token": t, "expires_in": 60})
    return jsonify({"error": "invalid credentials"}), 401

def _check_auth():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return "missing bearer token", 401
    t = h[7:]
    exp = TOKENS.get(t)
    if not exp:
        return "unknown token", 401
    if time.time() > exp:
        return "token expired", 401
    return None, None

def _validate(body):
    if not body.get("ci_name"):
        return "ci_name is required"
    if body.get("ci_type") not in VALID_TYPES:
        return "ci_type must be one of %s" % sorted(VALID_TYPES)
    ram = body.get("ram_gb")
    if ram is not None and not isinstance(ram, int):
        return "ram_gb must be an integer or null"
    return None

@app.post("/api/ci")
def create_ci():
    err, code = _check_auth()
    if err:
        return jsonify({"error": err}), code
    if random.random() < 0.10:
        return jsonify({"error": "service temporarily unavailable"}), 503
    body = request.get_json(silent=True) or {}
    v = _validate(body)
    if v:
        return jsonify({"error": v}), 400
    name = body["ci_name"]
    if name in DB:
        return jsonify({"error": "CI already exists", "hint": "use PUT /api/ci/<name>"}), 409
    DB[name] = body
    return jsonify({"created": name}), 201

@app.put("/api/ci/<name>")
def update_ci(name):
    err, code = _check_auth()
    if err:
        return jsonify({"error": err}), code
    if random.random() < 0.10:
        return jsonify({"error": "service temporarily unavailable"}), 503
    body = request.get_json(silent=True) or {}
    v = _validate(body)
    if v:
        return jsonify({"error": v}), 400
    if name not in DB:
        return jsonify({"error": "not found"}), 404
    DB[name] = body
    return jsonify({"updated": name})

@app.get("/api/ci")
def list_ci():
    err, code = _check_auth()
    if err:
        return jsonify({"error": err}), code
    return jsonify({"count": len(DB), "items": sorted(DB)})

if __name__ == "__main__":
    app.run(port=5050, debug=False)
