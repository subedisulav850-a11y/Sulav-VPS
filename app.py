import os
import json
import subprocess
import shutil
import zipfile
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file, abort
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sulav-vps-secret-2025")

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

ADMIN_PASSWORD = "676767"

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"servers": {}, "users": {}, "settings": {"maintenance": False, "maintenance_msg": "System under maintenance."}}

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance") and session.get("username") != "__admin__":
            return render_template("maintenance.html", message=settings.get("maintenance_msg", "Under maintenance"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            return render_template("login.html", error="Enter a username")
        session["username"] = username
        data = load_data()
        if username not in data["users"]:
            data["users"][username] = {"joined": datetime.now().isoformat()}
            save_data(data)
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=username)

@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {"name": name, "owner": session["username"], "runtime": runtime, "status": "stopped", "main_file": "", "port": 8080, "packages": [], "created": datetime.now().isoformat()}
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and (cfg.get("owner") == session["username"] or session.get("admin")):
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template("server.html", server_name=name, config=cfg, files=files)

def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file"})
    f = request.files["file"]
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    extracted_files = []
    if f.filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                z.extractall(extract_dir)
                extracted_files = [m.filename for m in z.infolist() if not m.is_dir()]
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        dest = extract_dir / f.filename
        shutil.copy(upload_path, dest)
        upload_path.unlink(missing_ok=True)
        extracted_files = [f.filename]
        if not cfg.get("main_file") and f.filename.endswith((".py", ".js")):
            cfg["main_file"] = f.filename
            data["servers"][name] = cfg
            save_data(data)
    return jsonify({"success": True, "files": extracted_files})

@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
def install_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    pkg_name = payload.get("name", "").strip()
    pkg_ver = payload.get("version", "").strip()
    if not pkg_name:
        return jsonify({"success": False, "error": "Package name required"})
    install_str = f"{pkg_name}=={pkg_ver}" if pkg_ver else pkg_name
    try:
        subprocess.check_output(["pip", "install", install_str], stderr=subprocess.STDOUT, timeout=120)
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "error": e.output.decode()[:300]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    pkgs = cfg.get("packages", [])
    pkgs = [p for p in pkgs if p["name"] != pkg_name]
    pkgs.append({"name": pkg_name, "version": pkg_ver or "", "installed_at": datetime.now().isoformat()})
    cfg["packages"] = pkgs
    data["servers"][name] = cfg
    save_data(data)
    req_path = SERVERS_DIR / name / "extracted" / "requirements.txt"
    try:
        lines = req_path.read_text().splitlines() if req_path.exists() else []
        lines = [l for l in lines if not l.startswith(pkg_name)]
        lines.append(install_str)
        req_path.write_text("\n".join(lines) + "\n")
    except Exception:
        pass
    return jsonify({"success": True, "package": pkg_name})

@app.route("/server/<name>/packages/remove", methods=["POST"])
@login_required
def remove_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    payload = request.get_json()
    pkg_name = payload.get("name", "")
    cfg["packages"] = [p for p in cfg.get("packages", []) if p["name"] != pkg_name]
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Not found"}), 404
    main_file = cfg.get("main_file") or "main.py"
    extract_dir = SERVERS_DIR / name / "extracted"
    main_path = extract_dir / main_file
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    cfg["status"] = "running"
    data["servers"][name] = cfg
    save_data(data)
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as lf:
        lf.write(f"[{datetime.now().isoformat()}] Server started\n")
    return jsonify({"success": True, "pid": 0})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    cfg["status"] = "stopped"
    data["servers"][name] = cfg
    save_data(data)
    log_path = SERVERS_DIR / name / "logs.txt"
    if log_path.exists():
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    return jsonify({"success": True})

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    log_path = SERVERS_DIR / name / "logs.txt"
    logs = log_path.read_text() if log_path.exists() else "No logs yet. Start the server to see output."
    return jsonify({"logs": logs})

# ─── Admin ───────────────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="Wrong admin password")
    return render_template("admin_login.html", error=None)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    data = load_data()
    servers = data["servers"]
    users_raw = data["users"]
    settings = data.get("settings", {})
    running = sum(1 for v in servers.values() if v.get("status") == "running")
    total_files = 0
    for name in servers:
        ed = SERVERS_DIR / name / "extracted"
        if ed.exists():
            total_files += sum(1 for f in ed.rglob("*") if f.is_file())
    user_stats = []
    for u in users_raw:
        u_servers = [v for v in servers.values() if v.get("owner") == u]
        u_files = 0
        for sv in u_servers:
            ed = SERVERS_DIR / sv["name"] / "extracted"
            if ed.exists():
                u_files += sum(1 for f in ed.rglob("*") if f.is_file())
        user_stats.append({"username": u, "projects": len(u_servers), "running": sum(1 for sv in u_servers if sv.get("status") == "running"), "files": u_files, "joined": users_raw[u].get("joined", "")})
    return render_template("admin.html", users=user_stats, servers=servers, settings=settings, total_users=len(users_raw), total_projects=len(servers), running=running, total_files=total_files)

@app.route("/admin/user/<username>/files")
@admin_required
def admin_user_files(username):
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    file_data = {}
    for name, cfg in user_servers.items():
        ed = SERVERS_DIR / name / "extracted"
        file_data[name] = {"config": cfg, "files": list_files(ed)}
    return render_template("admin_files.html", username=username, file_data=file_data)

@app.route("/admin/user/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    data = load_data()
    to_delete = [k for k, v in data["servers"].items() if v.get("owner") == username]
    for name in to_delete:
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
        del data["servers"][name]
    data["users"].pop(username, None)
    save_data(data)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/maintenance", methods=["POST"])
@admin_required
def toggle_maintenance():
    data = load_data()
    payload = request.get_json()
    data["settings"]["maintenance"] = payload.get("enabled", False)
    data["settings"]["maintenance_msg"] = payload.get("message", "Under maintenance")
    save_data(data)
    return jsonify({"success": True})

# ── Download Routes ────────────────────────────────────────────────────────────

@app.route("/admin/file/<project_name>/download")
@admin_required
def admin_download_file(project_name):
    file_path = request.args.get("path", "")
    if not file_path:
        abort(400)
    safe = Path(file_path).name  # only allow basename if path traversal
    safe_path = (SERVERS_DIR / project_name / "extracted" / file_path).resolve()
    base = (SERVERS_DIR / project_name / "extracted").resolve()
    if not str(safe_path).startswith(str(base)) or not safe_path.exists() or safe_path.is_dir():
        abort(404)
    return send_file(safe_path, as_attachment=True, download_name=safe_path.name)


@app.route("/admin/project/<project_name>/download")
@admin_required
def admin_download_project(project_name):
    type_filter = request.args.get("type", "all")
    extract_dir = SERVERS_DIR / project_name / "extracted"
    if not extract_dir.exists():
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in extract_dir.rglob("*"):
            if not f.is_file():
                continue
            if type_filter != "all" and not f.name.endswith(type_filter):
                continue
            zf.write(f, f.relative_to(extract_dir))
    buf.seek(0)
    ext_part = type_filter.replace(".", "") if type_filter != "all" else ""
    fname = f"{project_name}{'-' + ext_part if ext_part else ''}.zip"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/zip")


@app.route("/admin/user/<username>/download")
@admin_required
def admin_download_user(username):
    type_filter = request.args.get("type", "all")
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in user_servers:
            extract_dir = SERVERS_DIR / name / "extracted"
            if not extract_dir.exists():
                continue
            for f in extract_dir.rglob("*"):
                if not f.is_file():
                    continue
                if type_filter != "all" and not f.name.endswith(type_filter):
                    continue
                arcname = Path(name) / f.relative_to(extract_dir)
                zf.write(f, arcname)
    buf.seek(0)
    ext_part = type_filter.replace(".", "") if type_filter != "all" else ""
    fname = f"{username}-files{'-' + ext_part if ext_part else ''}.zip"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
