# app.py
# Portfolio website with Admin and Viewer login.
#
# Run locally:   python app.py          (then open http://127.0.0.1:5000)
# Deploy:        gunicorn app:app       (see Procfile / render.yaml)
#
# Database: the app uses Postgres when the DATABASE_URL environment variable
# is set (for example a free Neon project, set on the Render dashboard under
# Environment). Without DATABASE_URL it falls back to a local SQLite file
# (portfolio.db) for quick local testing. All SQL below is written once with
# "?" placeholders and translated automatically, so both backends behave the
# same.
#
# First visit: create the admin account. Afterwards:
#   - Admin tab  -> sign in -> manage profile, projects, skills, viewers
#   - Viewer tab -> create an account, sign in -> read-only portfolio

import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

# Secret key signs the login cookie. Set SECRET_KEY on the hosting
# dashboard. The fallback below is only for quick local testing.
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

# ------------------------------------------------------- database backend
# Set DATABASE_URL on the host to use a permanent Postgres database that
# survives restarts and redeploys. Without it, a local SQLite file is used
# (handy for testing on your own computer).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    from psycopg2.errors import UniqueViolation as DupError
else:
    DupError = sqlite3.IntegrityError


def q(sql):
    """Translate "?" placeholders to "%s" for Postgres; unchanged for SQLite."""
    return sql.replace("?", "%s") if USE_PG else sql


class _PGConn:
    """Thin wrapper so psycopg2 can be used exactly like sqlite3 below."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(q(sql), params)
        return cur

    def executemany(self, sql, seq):
        cur = self._conn.cursor()
        cur.executemany(q(sql), seq)
        return cur

    def executescript(self, sql):
        # Our schema below has no semicolons inside statements,
        # so a plain split is safe here.
        cur = self._conn.cursor()
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "portfolio.db")


# ---------------------------------------------------------------- helpers

def utcnow():
    """Current UTC time as text, e.g. '2026-10-18 04:40:12'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    """One database connection per request, kept on flask.g."""
    if "db" not in g:
        if USE_PG:
            g.db = _PGConn(psycopg2.connect(DATABASE_URL))
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row  # rows behave like dicts
            g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """Close the request's database connection, if one was opened."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,            -- 'admin' or 'viewer'
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS signins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        full_name TEXT,
        role_title TEXT,
        intro TEXT,
        location TEXT,
        email TEXT,
        availability TEXT
    );
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        tags TEXT,
        live_url TEXT,
        github_url TEXT,
        visible INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        level INTEGER DEFAULT 50
    );
"""


def init_db():
    """Create tables and seed starter content on first run."""
    db = get_db()
    schema = SCHEMA
    if USE_PG:
        # Postgres has no AUTOINCREMENT keyword; SERIAL is its equivalent.
        schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                                "SERIAL PRIMARY KEY")
    db.executescript(schema)

    # Seed the single profile row (id = 1) if missing.
    row = db.execute("SELECT COUNT(*) AS c FROM profile").fetchone()
    if row["c"] == 0:
        db.execute(
            """INSERT INTO profile
               (id, full_name, role_title, intro, location, email, availability)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            ("Harekrishna Chaudhary",
             "Data Analyst & Data Scientist",
             "I turn raw data into clear insights and working machine learning "
             "applications \u2014 from prediction models to live web dashboards.",
             "Nepal",
             "youremail@example.com",
             "Open to Work"))

    # Seed starter projects if the table is empty.
    row = db.execute("SELECT COUNT(*) AS c FROM projects").fetchone()
    if row["c"] == 0:
        db.executemany(
            """INSERT INTO projects
               (title, description, tags, live_url, github_url, visible)
               VALUES (?, ?, ?, ?, ?, 1)""",
            [("Air Passengers Forecasting",
              "Time series forecasting model that predicts future monthly air "
              "passenger numbers using Linear Regression on lag features.",
              "Python, scikit-learn, Streamlit",
              "https://air-passengers-forecasting10.streamlit.app/",
              "https://github.com/harekrishna10/air-passengers-forecasting"),
             ("Loan Approval Prediction",
              "Predicts whether a loan application will be approved, with "
              "85.37% test accuracy using tuned Logistic Regression.",
              "Python, Logistic Regression, Streamlit",
              "https://loan-approval-prediction-project10.streamlit.app/",
              "https://github.com/harekrishna10/loan-approval-prediction-project")])

    # Seed starter skills if the table is empty.
    row = db.execute("SELECT COUNT(*) AS c FROM skills").fetchone()
    if row["c"] == 0:
        db.executemany(
            "INSERT INTO skills (name, level) VALUES (?, ?)",
            [("Python", 90),
             ("Pandas & NumPy", 85),
             ("Scikit-learn", 85),
             ("Streamlit", 80)])
    db.commit()


def admin_exists():
    """True once the first admin account has been created."""
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()
    return row["c"] > 0


def insert_user(db, username, password_hash, role):
    """Create a user and return the new row's id (both backends)."""
    if USE_PG:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (username, password_hash, role, utcnow()))
        return cur.fetchone()["id"]
    cur = db.execute(
        "INSERT INTO users (username, password_hash, role, created_at) "
        "VALUES (?, ?, ?, ?)",
        (username, password_hash, role, utcnow()))
    return cur.lastrowid


def log_signin(user_id, username, role):
    """Record every successful sign-in (admin and viewer)."""
    db = get_db()
    db.execute(
        "INSERT INTO signins (user_id, username, role, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, username, role, utcnow()))
    db.commit()


def do_login(user):
    """Store the login in the session cookie."""
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]


# ------------------------------------------------------- access control

def login_required(view):
    """Allow only signed-in users (any role)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    """Allow only signed-in admins. Viewers get HTTP 403."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("index"))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.errorhandler(403)
def forbidden(error):
    return ("<h1>403 \u2014 Admins only.</h1>"
            "<p><a href='/'>Back to home</a></p>"), 403


@app.context_processor
def inject_site_profile():
    """Make the profile available in every template as site_profile."""
    try:
        db = get_db()
        profile = db.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        return {"site_profile": profile}
    except Exception:
        return {"site_profile": None}


# ---------------------------------------------------------------- routes

@app.route("/")
def index():
    """Landing page: photo, name, intro + Admin / Viewer tabs."""
    if "user_id" in session:  # already signed in -> go to the right home
        target = "admin" if session.get("role") == "admin" else "portfolio"
        return redirect(url_for(target))
    db = get_db()
    profile = db.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return render_template("index.html", profile=profile,
                           need_setup=not admin_exists())


@app.route("/setup-admin", methods=["POST"])
def setup_admin():
    """First-run form: create the one admin account."""
    if admin_exists():
        return redirect(url_for("index"))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("index"))
    if password != confirm:
        flash("Passwords do not match.")
        return redirect(url_for("index"))
    db = get_db()
    try:
        new_id = insert_user(db, username, generate_password_hash(password),
                             "admin")
        db.commit()
    except DupError:
        flash("That username is taken.")
        return redirect(url_for("index"))
    log_signin(new_id, username, "admin")
    do_login({"id": new_id, "username": username, "role": "admin"})
    flash("Admin account created. Welcome!")
    return redirect(url_for("admin"))


@app.route("/register", methods=["POST"])
def register():
    """Viewer self-registration: username + password, then sign in."""
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("index"))
    if password != confirm:
        flash("Passwords do not match.")
        return redirect(url_for("index"))
    db = get_db()
    try:
        new_id = insert_user(db, username, generate_password_hash(password),
                             "viewer")
        db.commit()
    except DupError:
        flash("That username is taken. Please choose another.")
        return redirect(url_for("index"))
    log_signin(new_id, username, "viewer")
    do_login({"id": new_id, "username": username, "role": "viewer"})
    flash("Account created. Welcome!")
    return redirect(url_for("portfolio"))


@app.route("/login", methods=["POST"])
def login():
    """Sign in. The hidden 'role' field says which tab the form came from."""
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "viewer")
    if role not in ("admin", "viewer"):
        role = "viewer"
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE username = ? AND role = ?",
        (username, role)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        log_signin(user["id"], user["username"], user["role"])
        do_login(user)
        target = "admin" if user["role"] == "admin" else "portfolio"
        return redirect(url_for(target))
    flash("Wrong username or password.")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/portfolio")
@login_required
def portfolio():
    """Read-only portfolio page for signed-in users (any role)."""
    db = get_db()
    profile = db.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    projects = db.execute(
        "SELECT * FROM projects WHERE visible = 1 ORDER BY id").fetchall()
    skills = db.execute("SELECT * FROM skills ORDER BY id").fetchall()
    return render_template("viewer_home.html", profile=profile,
                           projects=projects, skills=skills)


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin():
    """Overview: counts of projects, skills, viewers and total sign-ins."""
    db = get_db()
    counts = {
        "projects": db.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"],
        "skills": db.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"],
        "viewers": db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'viewer'").fetchone()["c"],
        "signins": db.execute("SELECT COUNT(*) AS c FROM signins").fetchone()["c"],
    }
    return render_template("admin_overview.html", counts=counts)


@app.route("/admin/profile", methods=["GET", "POST"])
@admin_required
def admin_profile():
    """Edit the public profile (name, title, intro, ...)."""
    db = get_db()
    if request.method == "POST":
        db.execute(
            """UPDATE profile SET full_name = ?, role_title = ?, intro = ?,
                                  location = ?, email = ?, availability = ?
               WHERE id = 1""",
            (request.form.get("full_name", "").strip(),
             request.form.get("role_title", "").strip(),
             request.form.get("intro", "").strip(),
             request.form.get("location", "").strip(),
             request.form.get("email", "").strip(),
             request.form.get("availability", "").strip()))
        db.commit()
        flash("Profile saved.")
        return redirect(url_for("admin_profile"))
    profile = db.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    return render_template("admin_profile.html", profile=profile)


@app.route("/admin/projects", methods=["GET", "POST"])
@admin_required
def admin_projects():
    """List projects, add a new one, or edit one (?edit=<id>)."""
    db = get_db()
    if request.method == "POST":
        # One form does both jobs: hidden 'project_id' means "edit".
        pid = request.form.get("project_id")
        data = (request.form.get("title", "").strip(),
                request.form.get("description", "").strip(),
                request.form.get("tags", "").strip(),
                request.form.get("live_url", "").strip(),
                request.form.get("github_url", "").strip(),
                1 if request.form.get("visible") else 0)
        if not data[0]:
            flash("Title is required.")
        elif pid:
            db.execute(
                """UPDATE projects SET title = ?, description = ?, tags = ?,
                                      live_url = ?, github_url = ?, visible = ?
                   WHERE id = ?""", data + (pid,))
            db.commit()
            flash("Project updated.")
        else:
            db.execute(
                """INSERT INTO projects
                   (title, description, tags, live_url, github_url, visible)
                   VALUES (?, ?, ?, ?, ?, ?)""", data)
            db.commit()
            flash("Project added.")
        return redirect(url_for("admin_projects"))
    projects = db.execute("SELECT * FROM projects ORDER BY id").fetchall()
    edit_project = None
    edit_id = request.args.get("edit")
    if edit_id:
        edit_project = db.execute(
            "SELECT * FROM projects WHERE id = ?", (edit_id,)).fetchone()
    return render_template("admin_projects.html", projects=projects,
                           edit_project=edit_project)


@app.route("/admin/projects/<int:pid>/delete", methods=["POST"])
@admin_required
def delete_project(pid):
    db = get_db()
    db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    db.commit()
    flash("Project deleted.")
    return redirect(url_for("admin_projects"))


@app.route("/admin/skills", methods=["GET", "POST"])
@admin_required
def admin_skills():
    """List skills, add a new one, or edit one (?edit=<id>)."""
    db = get_db()
    if request.method == "POST":
        sid = request.form.get("skill_id")
        name = request.form.get("name", "").strip()
        try:
            level = int(request.form.get("level", 50))
        except ValueError:
            level = 50
        level = max(0, min(100, level))  # keep the bar between 0 and 100
        if not name:
            flash("Skill name is required.")
        elif sid:
            db.execute("UPDATE skills SET name = ?, level = ? WHERE id = ?",
                       (name, level, sid))
            db.commit()
            flash("Skill updated.")
        else:
            db.execute("INSERT INTO skills (name, level) VALUES (?, ?)",
                       (name, level))
            db.commit()
            flash("Skill added.")
        return redirect(url_for("admin_skills"))
    skills = db.execute("SELECT * FROM skills ORDER BY id").fetchall()
    edit_skill = None
    edit_id = request.args.get("edit")
    if edit_id:
        edit_skill = db.execute(
            "SELECT * FROM skills WHERE id = ?", (edit_id,)).fetchone()
    return render_template("admin_skills.html", skills=skills,
                           edit_skill=edit_skill)


@app.route("/admin/skills/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_skill(sid):
    db = get_db()
    db.execute("DELETE FROM skills WHERE id = ?", (sid,))
    db.commit()
    flash("Skill deleted.")
    return redirect(url_for("admin_skills"))


@app.route("/admin/viewers")
@admin_required
def admin_viewers():
    """Viewer accounts: totals, per-viewer sign-in stats, activity log.

    Passwords (hashes) are never selected here, so they can never leak
    into this page.
    """
    db = get_db()
    viewers = db.execute(
        """SELECT u.username, u.created_at,
                  (SELECT MAX(created_at) FROM signins s
                    WHERE s.user_id = u.id) AS last_sign_in,
                  (SELECT COUNT(*) FROM signins s
                    WHERE s.user_id = u.id) AS sign_in_count
           FROM users u
           WHERE u.role = 'viewer'
           ORDER BY u.created_at DESC""").fetchall()
    activity = db.execute(
        """SELECT username, role, created_at FROM signins
           ORDER BY created_at DESC LIMIT 100""").fetchall()
    return render_template("admin_viewers.html", viewers=viewers,
                           activity=activity)


# ---------------------------------------------------------------- start

with app.app_context():
    init_db()  # create tables + seed data on first run

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
