import base64
import mimetypes
import os
import re
import time

from flask import Flask, Response, jsonify, request
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


# ---------- Père Noël secret ----------
SANTA_FEATURES = ("secret_santa", "secret_santa_draw", "secret_santa_wishes", "secret_santa_swaps")


def santa_features():
    """Interrupteurs lus dans la table site_features (tout est désactivé par défaut)."""
    rows = (supabase_admin.table("site_features").select("key, enabled")
            .in_("key", list(SANTA_FEATURES)).execute().data or [])
    flags = {r["key"]: bool(r["enabled"]) for r in rows}
    master = flags.get("secret_santa", False)
    return {k: master and flags.get(k, False) for k in SANTA_FEATURES}


def santa_names(ids):
    ids = list(set(ids))
    if not ids:
        return {}
    rows = supabase_admin.table("profiles").select("id, full_name, email").in_("id", ids).execute().data or []
    return {p["id"]: p.get("full_name") or (p.get("email") or "Élève").split("@")[0] for p in rows}


def handle_santa(path, method, body, uid):
    db = supabase_admin
    f = santa_features()

    def disabled():
        return json_response(403, {"error": "Cette fonctionnalité est désactivée pour le moment."})

    if not f["secret_santa"]:
        return disabled()

    if path == "santa/my" and method == "GET":
        draw = db.table("secret_santa_draws").select("receiver_id").eq("giver_id", uid).limit(1).execute().data
        receiver = None
        if draw:
            rid = draw[0]["receiver_id"]
            likes = ""
            if f["secret_santa_wishes"]:
                w = db.table("secret_santa_wishes").select("likes").eq("student_id", rid).limit(1).execute().data
                likes = (w[0].get("likes") or "") if w else ""
            receiver = {"name": santa_names([rid]).get(rid, "Élève"), "likes": likes}
        mine = db.table("secret_santa_wishes").select("likes").eq("student_id", uid).limit(1).execute().data
        incoming, outgoing = [], []
        if f["secret_santa_swaps"]:
            swaps = (db.table("secret_santa_swaps").select("id, from_id, to_id").eq("status", "pending")
                     .or_(f"from_id.eq.{uid},to_id.eq.{uid}").execute().data or [])
            names = santa_names([s["from_id"] for s in swaps] + [s["to_id"] for s in swaps])
            incoming = [{"id": s["id"], "from_name": names.get(s["from_id"], "Élève")} for s in swaps if s["to_id"] == uid]
            outgoing = [{"id": s["id"], "to_name": names.get(s["to_id"], "Élève")} for s in swaps if s["from_id"] == uid]
        return json_response(200, {
            "features": f, "has_drawn": bool(draw), "receiver": receiver,
            "my_likes": (mine[0].get("likes") or "") if mine else "",
            "incoming": incoming, "outgoing": outgoing,
        })

    if path == "santa/draw" and method == "POST":
        if not f["secret_santa_draw"]:
            return disabled()
        try:
            rid = db.rpc("santa_draw", {"p_giver": uid}).execute().data
        except Exception as e:
            if "not_participant" in str(e):
                return json_response(403, {"error": "Ce compte ne participe pas au Père Noël secret."})
            return json_response(409, {"error": "Tirage impossible pour le moment, préviens un délégué."})
        ids = [r["id"] for r in (db.table("santa_participants").select("id").execute().data or [])]
        pool = [n for i, n in santa_names(ids).items() if i != uid]  # tous les participants, pour l'animation
        return json_response(200, {"receiver": santa_names([rid]).get(rid, "Élève"), "names": pool})

    if path == "santa/wishes" and method == "POST":
        if not f["secret_santa_wishes"]:
            return disabled()
        likes = (body.get("likes") or "").strip()[:500]
        db.table("secret_santa_wishes").upsert(
            {"student_id": uid, "likes": likes, "updated_at": "now()"}, on_conflict="student_id").execute()
        return json_response(200, {"message": "Enregistré."})

    if path.startswith("santa/swap") or path == "santa/candidates":
        if not f["secret_santa_swaps"]:
            return disabled()

        if path == "santa/candidates" and method == "GET":
            rows = db.table("secret_santa_draws").select("giver_id").neq("giver_id", uid).execute().data or []
            names = santa_names([r["giver_id"] for r in rows])
            return json_response(200, {"candidates": sorted(
                [{"id": i, "name": n} for i, n in names.items()], key=lambda c: c["name"].lower())})

        if path == "santa/swap/propose" and method == "POST":
            to_id = body.get("to_id")
            if not to_id or to_id == uid:
                return json_response(400, {"error": "Destinataire invalide."})
            have = db.table("secret_santa_draws").select("giver_id").in_("giver_id", [uid, to_id]).execute().data or []
            if len(have) != 2:
                return json_response(400, {"error": "Vous devez tous les deux avoir déjà tiré au sort."})
            dup = (db.table("secret_santa_swaps").select("id").eq("from_id", uid).eq("to_id", to_id)
                   .eq("status", "pending").execute().data)
            if dup:
                return json_response(409, {"error": "Tu as déjà proposé un échange à cette personne."})
            db.table("secret_santa_swaps").insert({"from_id": uid, "to_id": to_id}).execute()
            return json_response(200, {"message": "Proposition envoyée !"})

        if path == "santa/swap/respond" and method == "POST":
            if body.get("accept"):
                ok = db.rpc("santa_accept_swap", {"p_swap": body.get("swap_id"), "p_user": uid}).execute().data
                msg = "Échange accepté ! Ta personne tirée a changé." if ok else "Cet échange n'est pas possible."
                return json_response(200, {"message": msg})
            db.table("secret_santa_swaps").update({"status": "declined"}) \
                .eq("id", body.get("swap_id")).eq("to_id", uid).eq("status", "pending").execute()
            return json_response(200, {"message": "Proposition refusée."})

        if path == "santa/swap/cancel" and method == "POST":
            db.table("secret_santa_swaps").update({"status": "cancelled"}) \
                .eq("id", body.get("swap_id")).eq("from_id", uid).eq("status", "pending").execute()
            return json_response(200, {"message": "Proposition annulée."})

    return json_response(404, {"error": "Route non trouvée."})


@app.route("/", defaults={"_": ""}, methods=["GET", "POST", "OPTIONS"])
@app.route("/<path:_>", methods=["GET", "POST", "OPTIONS"])
def handler(_):
    path = normalize_path()
    method = request.method
    body = request.get_json(silent=True) or {}
    db = supabase_admin

    try:
        # ---------- Routes publiques ----------
        if path == "sw.js" and method == "GET":
            js_content = """
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Service worker minimal pour valider les critères PWA
});
"""
            return Response(js_content, mimetype="application/javascript")

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

        # --- Père Noël secret : interrupteurs (public, ne révèle rien de sensible) ---
        if path == "santa/features" and method == "GET":
            return json_response(200, santa_features())

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
        # --- Commission Restauration ---
        if path == "cantine/my" and method == "GET":
            res = db.table("cantine_responses").select("*").eq("student_id", user.id).execute()
            return json_response(200, res.data[0] if res.data else None)

        if path == "cantine/submit" and method == "POST":
            db.table("cantine_responses").upsert({
                "student_id": user.id,
                "q_quality": body.get("q_quality"),
                "q_quantity": body.get("q_quantity"),
                "q_variety": body.get("q_variety"),
                "q_temperature": body.get("q_temperature"),
                "q_ambiance": body.get("q_ambiance"),
                "q_waiting_time": body.get("q_waiting_time"),
                "q_hygiene": body.get("q_hygiene"),
                "q_waste": body.get("q_waste"),
                "q_price": body.get("q_price"),
                "q_suggestions": body.get("q_suggestions")
            }, on_conflict="student_id").execute()
            return json_response(200, {
                "message": "Réponses pour la commission restauration enregistrées."
            })

        

        # --- Admin : Récupération des retours cantine avec noms ---
        if is_admin and path == "cantine/responses" and method == "GET":
            res = db.table("cantine_responses").select("*").order("created_at", desc=True).execute()
            return json_response(200, combine_with_profiles(res.data))

        # --- Conseil de Classe ---
        if path == "conseil-classe/my" and method == "GET":
            res = db.table("conseil_classe_reponses").select("*").eq("student_id", user.id).execute()
            return json_response(200, res.data[0] if res.data else None)

        if path == "conseil-classe/submit" and method == "POST":
            db.table("conseil_classe_reponses").upsert({
                "student_id": user.id,
                "q_nom_prenom": body.get("q_nom_prenom"),
                "q_integre": body.get("q_integre"),
                "q_aise_oral": body.get("q_aise_oral"),
                "q_respecte": body.get("q_respecte"),
                "q_climat_cours": body.get("q_climat_cours"),
                "q_comportements_derangeants": body.get("q_comportements_derangeants"),
                "q_matieres_pref": body.get("q_matieres_pref"),
                "q_matieres_diff": body.get("q_matieres_diff"),
                "q_fait_ap": body.get("q_fait_ap"),
                "q_remarques_profs": body.get("q_remarques_profs"),
                "q_quel_ap": body.get("q_quel_ap"),
                "q_aide_ap": body.get("q_aide_ap"),
                "q_satisfait_ap": body.get("q_satisfait_ap"),
                "q_utile_ap": body.get("q_utile_ap"),
                "q_activites_extrasolaires": body.get("q_activites_extrasolaires"),
                "q_temps_devoirs": body.get("q_temps_devoirs"),
                "q_charge_travail": body.get("q_charge_travail"),
                "q_organisation": body.get("q_organisation"),
                "q_satisfaction_notes": body.get("q_satisfaction_notes"),
                "q_ressenti_classe": body.get("q_ressenti_classe"),
                "q_ressenti_lycee": body.get("q_ressenti_lycee"),
                "q_relations_eleves": body.get("q_relations_eleves"),
                "q_relations_profs": body.get("q_relations_profs"),
                "q_autre_remarque": body.get("q_autre_remarque")
            }, on_conflict="student_id").execute()
            return json_response(200, {
                "message": "Réponses pour le conseil de classe enregistrées avec succès."
            })

        # --- Admin : Récupération des retours du conseil de classe avec noms ---
        if is_admin and path == "conseil-classe/responses" and method == "GET":
            res = db.table("conseil_classe_reponses").select("*").order("created_at", desc=True).execute()
            return json_response(200, combine_with_profiles(res.data))

        # --- Père Noël secret ---
        if path.startswith("santa/"):
            return handle_santa(path, method, body, user.id)

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
                    "title": body.get("title")  or "Conseil de classe",
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