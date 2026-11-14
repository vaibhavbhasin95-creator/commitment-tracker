"""The page and API routes for asking questions about your meetings (Step 1.6).

    GET  /ask                 the Ask page (signed-in users only)
    GET  /api/rag/status      {"enabled": true}; the sidebar uses it to decide whether to show the link
    POST /api/rag/ask         {"question": "..."} -> the answer with its citations (answer.answer_question)
    POST /api/rag/consent     records the one-time OK to send meetings to a cloud AI engine
    POST /api/rag/reindex     {"meeting_id": N} -> rebuilds that meeting's search index now

Registered by app.py inside its [rag] block, so RAG_ENABLED=0 removes all of it. Like ai_extraction.py,
it receives get_db and require_email from the app instead of importing the app.
"""

from flask import Blueprint, jsonify, redirect, request, send_from_directory

from .answer import answer_question
from .errors import MeetingNotFound, RagConsentRequired, RagError
from .indexer import index_meeting
from .settings import grant_rag_consent, load_engine

MAX_QUESTION_CHARS = 1000
PROVIDER_LABELS = {"anthropic": "Anthropic (Claude)", "openai_compatible": "your OpenAI-compatible AI service"}


def init_app(app, get_db, require_email):
    """Registers this feature's page and API routes on the Flask app."""
    bp = Blueprint("rag", __name__)

    def unauthorized():
        return jsonify({"error": "not identified"}), 401

    def bad_request(message):
        return jsonify({"error": message}), 400

    @bp.route("/ask")
    def page():
        if not require_email():
            return redirect("/")  # same as the app's other pages: signed-out visitors go to sign-in
        return send_from_directory(app.static_folder, "ask.html")

    @bp.route("/api/rag/status")
    def status():
        return jsonify({"enabled": True})

    @bp.route("/api/rag/ask", methods=["POST"])
    def ask():
        email = require_email()
        if not email:
            return unauthorized()
        question = (request.get_json(silent=True) or {}).get("question")
        if not isinstance(question, str) or not question.strip():
            return bad_request("Type a question first.")
        if len(question.strip()) > MAX_QUESTION_CHARS:
            return bad_request(f"Keep the question under {MAX_QUESTION_CHARS} characters.")
        db = get_db()
        try:
            result = answer_question(db, email, question)
        except RagConsentRequired as e:
            return jsonify({"error": str(e), "consent_required": True, "provider": e.provider,
                            "provider_label": PROVIDER_LABELS.get(e.provider, e.provider)}), 409
        except RagError as e:
            return jsonify({"error": str(e)}), 502
        finally:
            db.close()
        return jsonify(result)

    @bp.route("/api/rag/consent", methods=["POST"])
    def consent():
        """Only records consent when a cloud engine is saved and waiting for it, so it can't be
        granted ahead of time for an engine the user hasn't chosen."""
        email = require_email()
        if not email:
            return unauthorized()
        db = get_db()
        try:
            try:
                load_engine(db, email)
                return jsonify({"ok": True, "needed": False})
            except RagConsentRequired as e:
                grant_rag_consent(db, email)
                return jsonify({"ok": True, "needed": True, "provider": e.provider})
            except RagError as e:
                return jsonify({"error": str(e)}), 409
        finally:
            db.close()

    @bp.route("/api/rag/reindex", methods=["POST"])
    def reindex():
        email = require_email()
        if not email:
            return unauthorized()
        meeting_id = (request.get_json(silent=True) or {}).get("meeting_id")
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int):
            return bad_request("meeting_id must be a number.")
        db = get_db()
        try:
            owned = db.execute("SELECT 1 FROM meetings WHERE id = ? AND owner_email = ?", (meeting_id, email)).fetchone()
            if not owned:
                return jsonify({"error": "meeting not found"}), 404
            try:
                chunks = index_meeting(db, meeting_id)
            except MeetingNotFound:
                return jsonify({"error": "meeting not found"}), 404
            except RagError as e:
                return jsonify({"error": str(e)}), 502
        finally:
            db.close()
        return jsonify({"ok": True, "chunks_indexed": chunks})

    app.register_blueprint(bp)
