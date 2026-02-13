from flask import Flask, render_template, request, redirect, url_for, session, flash
import json
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(24)
DB_FILE = "filters.json"
LOG_FILE = "logs.json"
ADMIN_PASSWORD = "admin"  # Change this to a secure password

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def load_db():
    # Define default categories structure
    default_structure = {
        "global": [], 
        "whitelisted_ids": [], 
        "blocked_ids": [], 
        "video_photo": [], 
        "animation": [], 
        "sticker": [],
        "logs": []
    }
    
    if not os.path.exists(DB_FILE):
        return default_structure
        
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Ensure all default keys exist (in case of old json file)
            for key in default_structure:
                if key not in data:
                    data[key] = []
            return data
    except:
        return default_structure

def save_db(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def load_logs():
    if not os.path.exists(LOG_FILE):
        return {"last_reset": "", "entries": []}
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"last_reset": "", "entries": []}

def save_logs(data):
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash('Invalid password')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    db = load_db()
    logs = load_logs()
    return render_template('index.html', db=db, logs=logs)

@app.route('/add_item/<category>', methods=['POST'])
@login_required
def add_item(category):
    item = request.form.get('item', '').strip()
    # Basic validation to ensure we don't add empty items
    if item:
        db = load_db()
        # Only add if category exists (security check)
        if category in db:
            # Avoid duplicates
            if item not in db[category]:
                db[category].append(item)
                save_db(db)
    return redirect(url_for('index'))

@app.route('/remove_item/<category>/<int:index_id>')
@login_required
def remove_item(category, index_id):
    db = load_db()
    if category in db and 0 <= index_id < len(db[category]):
        db[category].pop(index_id)
        save_db(db)
    return redirect(url_for('index'))

@app.route('/edit_item/<category>/<int:index_id>', methods=['POST'])
@login_required
def edit_item(category, index_id):
    new_value = request.form.get('new_value', '').strip()
    if new_value:
        db = load_db()
        if category in db and 0 <= index_id < len(db[category]):
            db[category][index_id] = new_value
            save_db(db)
    return redirect(url_for('index'))

@app.route('/clear_logs')
@login_required
def clear_logs():
    logs = load_logs()
    logs['entries'] = []
    save_logs(logs)
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)