import base64
import mimetypes
import os
import re
import time

from flask import Flask, jsonify, request
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = Flask(__name__)


def json_response(status, data):
    return jsonify(data), status


def get_profile(user_id):
    res = supabase_admin.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_auth_context():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        res = supabase_admin.auth.get_user(token)
    except Exception:
        return None
    user = getattr(res, "user", None)
    if not user:
        return None
    return {"user": user, "profile": get_profile(user.id), "token": token}


def normalize_path():
    """Retourne le chemin sans le préfixe /api (ex: 'auth/login')."""
    path = request.path
    path = re.sub(r"^/\.netlify/functions/api/?", "", path)
    path = re.sub(r"^/api/?", "", path)
    path = path.strip("/")
    # Si Vercel a réécrit vers /api/index?path=..., on récupère le vrai chemin
    if path in ("", "index") and request.args.get("path"):
        path = request.args["path"].strip("/")
    return path


def user_to_dict(user):
    return user.model_dump(mode="json") if hasattr(user, "model_dump") else dict(user)


def combine_with_profiles(responses):
    profiles = supabase_admin.table("profiles").select("id, full_name, email").execute().data or []
    profile_map = {p["id"]: p for p in profiles}
    return [
        {**r, "profiles": profile_map.get(r["student_id"], {"full_name": "Élève inconnu", "email": ""})}
        for r in (responses or [])
    ]


@app.route("/", defaults={"_": ""}, methods=["GET", "POST", "OPTIONS"])
@app.route("/<path:_>", methods=["GET", "POST", "OPTIONS"])
def handler(_):
    path = normalize_path()
    method = request.method
    body = request.get_json(silent=True) or {}
    db = supabase_admin

    try:
        # ---------- Routes publiques ----------
        if path == "auth/login" and method == "POST":
            email, password = body.get("email"), body.get("password")
            # Client dédié pour ne pas partager de session entre requêtes
            client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
            try:
                res = client.auth.sign_in_with_password({"email": email, "password": password})
            except Exception as e:
                return json_response(400, {"error": str(e)})
            profile = get_profile(res.user.id)
            return json_response(200, {
                "token": res.session.access_token,
                "user": user_to_dict(res.user),
                "profile": profile,
            })

        if path == "posts" and method == "GET":
            res = db.table("posts").select("*").order("created_at", desc=True).execute()
            return json_response(200, res.data)

        if path == "sheets/approved" and method == "GET":
            res = (db.table("revision_sheets").select("*").eq("status", "approved")
                   .order("created_at", desc=True).execute())
            return json_response(200, res.data)

        # ---------- Routes authentifiées ----------
        auth = get_auth_context()
        if not auth:
            return json_response(401, {"error": "Non autorisé. Veuillez vous connecter."})

        user, profile = auth["user"], auth["profile"]
        role = (profile or {}).get("role")
        is_admin = role in ("admin", "webmaster")
        is_webmaster = role == "webmaster"

        if path == "auth/me" and method == "GET":
            return json_response(200, {"user": user_to_dict(user), "profile": profile})

        if path == "auth/change-password" and method == "POST":
            try:
                db.auth.admin.update_user_by_id(user.id, {"password": body.get("new_password")})
            except Exception as e:
                return json_response(400, {"error": str(e)})
            return json_response(200, {"message": "Mot de passe mis à jour."})

        if path == "results/my" and method == "GET":
            res = (db.table("student_results").select("*").eq("student_id", user.id)
                   .order("updated_at", desc=True).execute())
            return json_response(200, res.data)

        if path == "sheets/my" and method == "GET":
            res = (db.table("revision_sheets").select("*").eq("created_by", user.id)
                   .order("created_at", desc=True).execute())
            return json_response(200, res.data)

        if path == "sheets/upload" and method == "POST":
            title, subject = body.get("title"), body.get("subject")
            file_name, file_base64 = body.get("fileName", ""), body.get("fileBase64", "")

            match = re.match(r"^data:(.*?);base64,", file_base64)
            content_type = match.group(1) if match else (
                mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            )
            raw = re.sub(r"^data:.*;base64,", "", file_base64)
            buffer = base64.b64decode(raw)

            file_ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "bin"
            storage_path = f"{user.id}/{int(time.time() * 1000)}.{file_ext}"

            try:
                db.storage.from_("revision_files").upload(
                    storage_path, buffer, {"content-type": content_type}
                )
            except Exception as e:
                return json_response(500, {"error": str(e)})

            public_url = db.storage.from_("revision_files").get_public_url(storage_path)

            db.table("revision_sheets").insert({
                "title": title,
                "subject": subject,
                "file_url": public_url,
                "file_name": file_name,
                "created_by": user.id,
                "status": "pending",
                "rejection_reason": None,
            }).execute()
            return json_response(200, {"message": "Fiche envoyée en modération."})

        if path == "intake/my" and method == "GET":
            res = db.table("delegate_intake_responses").select("*").eq("student_id", user.id).execute()
            return json_response(200, res.data[0] if res.data else None)

        if path == "intake/submit" and method == "POST":
            keys = ["info", "has_whatsapp", "whatsapp_handle", "has_snapchat",
                    "snapchat_handle", "has_instagram", "instagram_handle"]
            row = {"student_id": user.id, **{k: body.get(k) for k in keys}}
            db.table("delegate_intake_responses").upsert(row, on_conflict="student_id").execute()
            return json_response(200, {"message": "Réponses enregistrées avec succès."})

        # --- Mouvement Lycéen ---
        if path == "mouvement/my" and method == "GET":
            res = db.table("mouvement_lyceen_responses").select("*").eq("student_id", user.id).execute()
            return json_response(200, res.data[0] if res.data else None)

        if path == "mouvement/submit" and method == "POST":
            db.table("mouvement_lyceen_responses").upsert({
                "student_id": user.id,
                "difficulties": body.get("difficulties"),
                "improvements": body.get("improvements"),
            }, on_conflict="student_id").execute()
            return json_response(200, {
                "message": "Réponses sur le mouvement lycéen enregistrées avec succès."
            })

        # ---------- Routes admin ----------
        if is_admin:
            if path == "posts/create" and method == "POST":
                db.table("posts").insert({"title": body.get("title"), "content": body.get("content")}).execute()
                return json_response(200, {"message": "Annonce publiée."})

            if path == "posts/update" and method == "POST":
                db.table("posts").update({"title": body.get("title"), "content": body.get("content")}) \
                    .eq("id", body.get("id")).execute()
                return json_response(200, {"message": "Annonce mise à jour."})

            if path == "posts/delete" and method == "POST":
                db.table("posts").delete().eq("id", body.get("id")).execute()
                return json_response(200, {"message": "Annonce supprimée."})

            if path == "sheets/pending" and method == "GET":
                res = (db.table("revision_sheets").select("*").eq("status", "pending")
                       .order("created_at", desc=True).execute())
                return json_response(200, res.data)

            if path == "sheets/approve" and method == "POST":
                db.table("revision_sheets").update({"status": "approved", "rejection_reason": None}) \
                    .eq("id", body.get("sheetId")).execute()
                return json_response(200, {"message": "Fiche approuvée."})

            if path == "sheets/reject" and method == "POST":
                db.table("revision_sheets").update({
                    "status": "rejected",
                    "rejection_reason": body.get("reason") or "Aucune raison spécifiée.",
                }).eq("id", body.get("sheetId")).execute()
                return json_response(200, {"message": "Fiche refusée avec motif."})

            if path == "sheets/delete" and method == "POST":
                db.table("revision_sheets").delete().eq("id", body.get("sheetId")).execute()
                return json_response(200, {"message": "Fiche supprimée."})

            if path == "admin/students" and method == "GET":
                res = db.table("profiles").select("id, email, full_name, role").neq("role", "webmaster").execute()
                return json_response(200, res.data)

            if path == "admin/student-results" and method == "POST":
                res = (db.table("student_results").select("*").eq("student_id", body.get("studentId"))
                       .order("updated_at", desc=True).execute())
                return json_response(200, res.data)

            if path == "admin/add-result" and method == "POST":
                db.table("student_results").insert({
                    "student_id": body.get("studentId"),
                    "title": body.get("title") or "Conseil de classe",
                    "appreciation": body.get("appreciation"),
                }).execute()
                return json_response(200, {"message": "Résultat ajouté."})

            if path == "intake/responses" and method == "GET":
                res = db.table("delegate_intake_responses").select("*").order("created_at", desc=True).execute()
                return json_response(200, combine_with_profiles(res.data))

            if path == "mouvement/responses" and method == "GET":
                res = db.table("mouvement_lyceen_responses").select("*").order("created_at", desc=True).execute()
                return json_response(200, combine_with_profiles(res.data))

        # ---------- Routes webmaster ----------
        if is_webmaster:
            if path == "webmaster/create-user" and method == "POST":
                try:
                    new_user = db.auth.admin.create_user({
                        "email": body.get("email"),
                        "password": body.get("password"),
                        "email_confirm": True,
                        "user_metadata": {"full_name": body.get("name")},
                    })
                except Exception as e:
                    return json_response(400, {"error": str(e)})
                db.table("profiles").update({"role": body.get("role")}).eq("id", new_user.user.id).execute()
                return json_response(200, {"message": "Utilisateur créé avec succès."})

            if path == "webmaster/profiles" and method == "GET":
                res = db.table("profiles").select("*").order("email").execute()
                return json_response(200, res.data)

            if path == "webmaster/update-profile" and method == "POST":
                db.table("profiles").update({"full_name": body.get("name"), "role": body.get("role")}) \
                    .eq("id", body.get("userId")).execute()
                return json_response(200, {"message": "Profil mis à jour."})

            if path == "webmaster/reset-password" and method == "POST":
                db.auth.admin.update_user_by_id(body.get("userId"), {"password": body.get("newPassword")})
                return json_response(200, {"message": "Mot de passe réinitialisé."})

        return json_response(404, {"error": "Route non trouvée ou privilèges insuffisants."})

    except Exception as e:
        return json_response(500, {"error": str(e)})
