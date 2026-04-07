import os
import zipfile
import subprocess
import signal
import shutil
import json
import sys
import time
import threading
import atexit
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "Sulav_hosting_secret_key_2024")

UPLOAD_FOLDER = "servers"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

processes = {}

def force_delete_directory(path, max_retries=5, delay=1):
    for i in range(max_retries):
        try:
            if os.path.exists(path):
                shutil.rmtree(path, ignore_errors=True)
                return True
        except Exception as e:
            print(f"Attempt {i+1} failed: {str(e)}")
            time.sleep(delay)
    return False

@atexit.register
def cleanup_on_exit():
    for (username, server_name), process in list(processes.items()):
        try:
            if process.poll() is None:
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
        except:
            pass

def get_user_server_path():
    if 'username' not in session:
        return None
    user_dir = os.path.join(UPLOAD_FOLDER, session['username'])
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(extract_to)

def install_requirements(path, log_path=None):
    req = os.path.join(path, "requirements.txt")
    if os.path.exists(req):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req],
                capture_output=True, text=True, timeout=300
            )
            if log_path:
                with open(log_path, 'a') as f:
                    f.write(f"[pip] {result.stdout}\n")
                    if result.stderr:
                        f.write(f"[pip stderr] {result.stderr}\n")
        except Exception as e:
            if log_path:
                with open(log_path, 'a') as f:
                    f.write(f"[pip error] {str(e)}\n")

def find_main_file(path, preferred=None):
    if preferred and os.path.exists(os.path.join(path, preferred)):
        return preferred
    common_files = ["main.py", "app.py", "bot.py", "server.py", "index.py", "start.py", "run.py"]
    for filename in common_files:
        if os.path.exists(os.path.join(path, filename)):
            return filename
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith('.py') and not file.startswith('_'):
                filepath = os.path.join(root, file)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if '__main__' in content or 'if __name__' in content:
                            return os.path.relpath(filepath, path)
                except:
                    continue
    return None

def save_server_config(username, server_name, config):
    config_path = os.path.join(UPLOAD_FOLDER, username, server_name, "config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)

def load_server_config(username, server_name):
    config_path = os.path.join(UPLOAD_FOLDER, username, server_name, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except:
            pass
    return {
        "status": "stopped",
        "type": "web",
        "port": 8080,
        "main_file": None,
        "packages": [],
        "created_at": str(datetime.now())
    }

def get_all_servers(username):
    user_dir = os.path.join(UPLOAD_FOLDER, username)
    if not os.path.exists(user_dir):
        return []
    servers = []
    for name in os.listdir(user_dir):
        server_dir = os.path.join(user_dir, name)
        if os.path.isdir(server_dir):
            config = load_server_config(username, name)
            key = (username, name)
            proc = processes.get(key)
            if proc and proc.poll() is None:
                config['status'] = 'running'
            else:
                config['status'] = 'stopped'
            servers.append({"name": name, "config": config})
    return servers

def list_files(path, base=""):
    if not os.path.exists(path):
        return []
    result = []
    for f in os.listdir(path):
        if f == "config.json" or f == "logs.txt" or f == "server.zip":
            continue
        full = os.path.join(path, f)
        rel = f"{base}/{f}" if base else f
        if os.path.isdir(full):
            result.append({"name": f, "path": rel, "type": "dir", "size": 0})
            result.extend(list_files(full, rel))
        else:
            result.append({"name": f, "path": rel, "type": "file", "size": os.path.getsize(full)})
    return result

# -------- Routes --------

@app.route("/")
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if username:
            session['username'] = username
            return redirect(url_for('dashboard'))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route("/dashboard")
def dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))
    servers = get_all_servers(session['username'])
    return render_template("dashboard.html", username=session['username'], servers=servers)

@app.route("/server/create", methods=["POST"])
def create_server():
    if 'username' not in session:
        return redirect(url_for('login'))
    name = request.form.get("name", "").strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        return jsonify({"error": "Invalid server name"}), 400
    server_dir = os.path.join(UPLOAD_FOLDER, session['username'], name)
    if os.path.exists(server_dir):
        return jsonify({"error": "Server already exists"}), 400
    os.makedirs(server_dir, exist_ok=True)
    config = {
        "status": "stopped",
        "type": "web",
        "port": 8080,
        "main_file": None,
        "packages": [],
        "created_at": str(datetime.now())
    }
    save_server_config(session['username'], name, config)
    return jsonify({"success": True, "name": name})

@app.route("/server/<server_name>/delete", methods=["POST"])
def delete_server(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    key = (session['username'], server_name)
    proc = processes.get(key)
    if proc and proc.poll() is None:
        proc.terminate()
        time.sleep(1)
    processes.pop(key, None)
    server_dir = os.path.join(UPLOAD_FOLDER, session['username'], server_name)
    force_delete_directory(server_dir)
    return jsonify({"success": True})

@app.route("/server/<server_name>")
def server_detail(server_name):
    if 'username' not in session:
        return redirect(url_for('login'))
    server_dir = os.path.join(UPLOAD_FOLDER, session['username'], server_name)
    if not os.path.exists(server_dir):
        return redirect(url_for('dashboard'))
    config = load_server_config(session['username'], server_name)
    key = (session['username'], server_name)
    proc = processes.get(key)
    config['status'] = 'running' if (proc and proc.poll() is None) else 'stopped'
    extract_dir = os.path.join(server_dir, "extracted")
    files = list_files(extract_dir)
    return render_template("server.html",
        username=session['username'],
        server_name=server_name,
        config=config,
        files=files
    )

@app.route("/server/<server_name>/upload", methods=["POST"])
def upload_file(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    if 'file' not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "No filename"}), 400
    server_dir = os.path.join(UPLOAD_FOLDER, session['username'], server_name)
    os.makedirs(server_dir, exist_ok=True)
    extract_dir = os.path.join(server_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    filename = file.filename
    if filename.endswith(".zip"):
        zip_path = os.path.join(server_dir, "server.zip")
        file.save(zip_path)
        try:
            shutil.rmtree(extract_dir, ignore_errors=True)
            os.makedirs(extract_dir, exist_ok=True)
            extract_zip(zip_path, extract_dir)
        except Exception as e:
            return jsonify({"error": f"Failed to extract: {str(e)}"}), 500
    else:
        dest = os.path.join(extract_dir, filename)
        file.save(dest)
    # Auto-detect main file
    config = load_server_config(session['username'], server_name)
    if not config.get("main_file"):
        main = find_main_file(extract_dir)
        if main:
            config["main_file"] = main
            save_server_config(session['username'], server_name, config)
    files = list_files(extract_dir)
    return jsonify({"success": True, "files": [f["path"] for f in files], "main_file": config.get("main_file")})

@app.route("/server/<server_name>/settings", methods=["POST"])
def update_settings(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    config = load_server_config(session['username'], server_name)
    data = request.get_json()
    if "main_file" in data:
        config["main_file"] = data["main_file"]
    if "port" in data:
        config["port"] = int(data["port"])
    save_server_config(session['username'], server_name, config)
    return jsonify({"success": True, "config": config})

@app.route("/server/<server_name>/packages/install", methods=["POST"])
def install_package(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json()
    package_name = data.get("name", "").strip()
    version = data.get("version", "").strip()
    if not package_name:
        return jsonify({"error": "Package name required"}), 400
    pkg_str = f"{package_name}=={version}" if version else package_name
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg_str, "--quiet"],
            capture_output=True, text=True, timeout=120
        )
        success = result.returncode == 0
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    config = load_server_config(session['username'], server_name)
    packages = config.get("packages", [])
    if not any(p["name"] == package_name for p in packages):
        packages.append({"name": package_name, "version": version, "installed_at": str(datetime.now())})
        config["packages"] = packages
        save_server_config(session['username'], server_name, config)
    # Update requirements.txt
    extract_dir = os.path.join(UPLOAD_FOLDER, session['username'], server_name, "extracted")
    if os.path.exists(extract_dir):
        req_path = os.path.join(extract_dir, "requirements.txt")
        existing = open(req_path).read().splitlines() if os.path.exists(req_path) else []
        existing = [l for l in existing if l.strip() and not l.startswith(package_name)]
        existing.append(pkg_str)
        with open(req_path, 'w') as f:
            f.write("\n".join(existing) + "\n")
    return jsonify({"success": success, "package": pkg_str, "output": result.stdout if success else result.stderr})

@app.route("/server/<server_name>/packages/remove", methods=["POST"])
def remove_package(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json()
    package_name = data.get("name", "").strip()
    config = load_server_config(session['username'], server_name)
    config["packages"] = [p for p in config.get("packages", []) if p["name"] != package_name]
    save_server_config(session['username'], server_name, config)
    return jsonify({"success": True})

@app.route("/server/<server_name>/start", methods=["POST"])
def start_server(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    key = (session['username'], server_name)
    existing = processes.get(key)
    if existing and existing.poll() is None:
        return jsonify({"error": "Already running"}), 400
    server_dir = os.path.join(UPLOAD_FOLDER, session['username'], server_name)
    extract_dir = os.path.join(server_dir, "extracted")
    config = load_server_config(session['username'], server_name)
    log_path = os.path.join(server_dir, "logs.txt")
    with open(log_path, 'a') as f:
        f.write(f"\n{'='*50}\n[{datetime.now()}] Starting {server_name}\n")
    if not os.path.exists(extract_dir) or not os.listdir(extract_dir):
        return jsonify({"error": "No files uploaded. Upload a ZIP file first."}), 400
    install_requirements(extract_dir, log_path)
    main_file = config.get("main_file") or find_main_file(extract_dir)
    if not main_file:
        return jsonify({"error": "Could not find main file. Set it in Settings."}), 400
    python_cmd = sys.executable
    try:
        log_file = open(log_path, 'a')
        p = subprocess.Popen(
            [python_cmd, main_file],
            cwd=extract_dir,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True
        )
        processes[key] = p
        config['status'] = 'running'
        config['pid'] = p.pid
        config['started_at'] = str(datetime.now())
        save_server_config(session['username'], server_name, config)
        def monitor(proc, k, cfg_username, cfg_name):
            proc.wait()
            processes.pop(k, None)
            c = load_server_config(cfg_username, cfg_name)
            c['status'] = 'stopped'
            c.pop('pid', None)
            save_server_config(cfg_username, cfg_name, c)
        threading.Thread(target=monitor, args=(p, key, session['username'], server_name), daemon=True).start()
        return jsonify({"success": True, "pid": p.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/server/<server_name>/stop", methods=["POST"])
def stop_server(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    key = (session['username'], server_name)
    proc = processes.get(key)
    if proc and proc.poll() is None:
        proc.terminate()
        time.sleep(2)
        if proc.poll() is None:
            proc.kill()
        processes.pop(key, None)
    config = load_server_config(session['username'], server_name)
    config['status'] = 'stopped'
    config.pop('pid', None)
    save_server_config(session['username'], server_name, config)
    return jsonify({"success": True})

@app.route("/server/<server_name>/logs")
def get_logs(server_name):
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    log_path = os.path.join(UPLOAD_FOLDER, session['username'], server_name, "logs.txt")
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        # Return last 8KB
        return jsonify({"logs": content[-8192:], "size": len(content)})
    return jsonify({"logs": "", "size": 0})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
