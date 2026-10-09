"""Run Agentic D&D standalone:  python app.py

Needs a Redis server (REDIS_URL, default redis://localhost:6379/0, see
store.py) and OPENROUTER_API_KEY for the agents to actually think.
Without a key every turn falls back to a "pauses to think..." message.
"""

import logging
import os

from flask import Flask, redirect

from routes import agentic_dnd_bp

app = Flask(__name__)  # serves ./static at /static
# Signs the session cookie that remembers which campaigns you created.
# Without FLASK_SECRET_KEY a random key is used, so restarting the server
# forgets that (you can still watch, but not advance, old campaigns).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.register_blueprint(agentic_dnd_bp)


@app.route("/")
def index():
    return redirect("/tools/agentic-dnd")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8000"))
    print(f"Agentic D&D on http://localhost:{port}/tools/agentic-dnd")
    app.run(host="127.0.0.1", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
