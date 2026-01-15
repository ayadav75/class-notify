import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
import class_checker
import requests
import time
from collections import defaultdict
import os # Import the os module

# --- Setup Robust Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
# Use Flask's logger for consistency
gunicorn_logger = logging.getLogger('gunicorn.error')
app.logger.handlers = gunicorn_logger.handlers
app.logger.setLevel(gunicorn_logger.level)

CORS(app)

# --- In-memory Data Storage ---
tracked_classes = {}
app_settings = {'term': '2257', 'ntfyTopic': 'susumaanclassalerts'}
notify_tracker = {}

MAX_NOTIFICATIONS = 10
NOTIFICATION_INTERVAL_HOURS = 1

# --- Helper ---
def get_term_name(term_code):
    if not term_code or len(term_code) != 4: return 'Unknown Term'
    year = 2000 + int(term_code[1:3])
    term_char = term_code[3]
    if term_char == '1': return f"Spring {year}"
    if term_char == '4': return f"Summer {year}"
    if term_char == '7': return f"Fall {year}"
    return 'Unknown Term'

# --- API Endpoints (No changes in this section) ---
@app.route('/api/state', methods=['GET'])
def get_full_state():
    return jsonify({
        "settings": { "term": app_settings["term"], "termName": get_term_name(app_settings["term"]), "ntfyTopic": app_settings["ntfyTopic"] },
        "trackedClasses": list(tracked_classes.values())
    })

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.get_json()
    term_changed = False
    if 'term' in data and data['term'] != app_settings['term']:
        app.logger.info(f"Term changed from {app_settings['term']} to {data['term']}")
        app_settings['term'] = data['term']
        tracked_classes.clear()
        notify_tracker.clear()
        term_changed = True
    if 'ntfyTopic' in data:
        app_settings['ntfyTopic'] = data['ntfyTopic']
    return jsonify({ "message": "Settings updated", "termName": get_term_name(app_settings["term"]), "termChanged": term_changed })

@app.route('/api/search/<class_name>', methods=['GET'])
def search_class(class_name):
    term = app_settings.get('term')
    results = class_checker.fetch_class_details(class_name, term)
    return jsonify(results)

@app.route('/api/tracked', methods=['POST'])
def add_tracked_class():
    class_details = request.get_json()
    class_number = class_details.get('classNumber')
    if not class_number: return jsonify({"error": "classNumber is required"}), 400
    
    tracked_classes[class_number] = class_details
    app.logger.info(f"Added {class_number} to tracking list. Performing immediate check.")
    perform_immediate_check(class_details)
    return jsonify(list(tracked_classes.values())), 201

@app.route('/api/tracked/<class_number>', methods=['DELETE'])
def delete_tracked_class(class_number):
    tracked_classes.pop(class_number, None)
    notify_tracker.pop(class_number, None)
    app.logger.info(f"Removed {class_number} from tracking list.")
    return jsonify(list(tracked_classes.values()))

# --- Notification Logic ---
def perform_immediate_check(class_details):
    class_number = class_details['classNumber']
    fresh_details_list = class_checker.fetch_class_details(class_details['className'], app_settings['term'])
    for fresh_details in fresh_details_list:
        if fresh_details['classNumber'] == class_number:
            tracked_classes[class_number] = fresh_details
            if fresh_details['status'] == 'OPEN':
                app.logger.info(f"Newly added class {class_number} is already OPEN. Sending notification.")
                tracker = {'count': 0, 'lastSent': 0, 'lastStatus': 'FULL'}
                notify_tracker[class_number] = tracker
                send_notification(fresh_details, 'OPEN')
                tracker.update({'count': 1, 'lastSent': int(time.time() * 1000), 'lastStatus': 'OPEN'})
            else:
                notify_tracker[class_number] = {'count': 0, 'lastSent': 0, 'lastStatus': 'FULL'}
            break

def check_class_statuses():
    with app.app_context():
        app.logger.info("--- Running background status check ---")
        if not tracked_classes: return

        classes_to_fetch = defaultdict(list)
        for details in tracked_classes.values(): classes_to_fetch[details['className']].append(details['classNumber'])

        for class_name, numbers in classes_to_fetch.items():
            fresh_details_list = class_checker.fetch_class_details(class_name, app_settings['term'])
            fresh_details_map = {d['classNumber']: d for d in fresh_details_list}

            for num in numbers:
                if num not in fresh_details_map: continue
                new_details = fresh_details_map[num]
                tracked_classes[num] = new_details
                
                tracker = notify_tracker.get(num, {'count': 0, 'lastSent': 0, 'lastStatus': 'FULL'})
                old_status, new_status = tracker['lastStatus'], new_details['status']

                if new_status == old_status: continue
                app.logger.info(f"Status change for {num}: {old_status} -> {new_status}")
                
                if new_status == 'OPEN':
                    tracker.update({'count': 0, 'lastSent': 0})
                    send_notification(new_details, 'OPEN')
                    tracker.update({'count': 1, 'lastSent': int(time.time() * 1000)})
                elif new_status == 'FULL' and old_status == 'OPEN':
                    send_notification(new_details, 'FULL')
                    tracker.update({'count': 0, 'lastSent': 0})
                
                tracker['lastStatus'] = new_status

def hourly_reminder_check():
    with app.app_context():
        now = int(time.time() * 1000)
        for num, tracker in list(notify_tracker.items()):
            details = tracked_classes.get(num)
            if not details or details['status'] != 'OPEN': continue
            if 0 < tracker['count'] < MAX_NOTIFICATIONS and (now - tracker['lastSent']) >= (NOTIFICATION_INTERVAL_HOURS * 60 * 60 * 1000):
                app.logger.info(f"Sending hourly reminder for {num}.")
                send_notification(details, 'REMINDER')
                tracker['count'] += 1
                tracker['lastSent'] = now

def send_notification(class_details, reason):
    ntfy_topic = app_settings.get('ntfyTopic')
    if not ntfy_topic:
        app.logger.warning("ntfy.sh topic is not set. Skipping notification.")
        return

    class_name = class_details['className']
    
    # --- FIX 1: Remove emoji from title, put it in the message body ---
    if reason == 'OPEN':
        title = f"Seat Open: {class_name}"
        message = f"✅ A seat just opened for {class_name} ({class_details['classNumber']})!"
    elif reason == 'FULL':
        title = f"Class Full: {class_name}"
        message = f"❌ The open seat for {class_name} ({class_details['classNumber']}) is now full."
    elif reason == 'REMINDER':
        count = notify_tracker.get(class_details['classNumber'], {}).get('count', 0)
        title = f"Still Open: {class_name}"
        message = f"📢 Reminder ({count}/{MAX_NOTIFICATIONS}): A seat is still open for {class_name} ({class_details['classNumber']})."
    else: return

    full_message = f"{message}\nTitle: {class_details['title']}\nInstructor: {class_details['instructor']}\nSeats: {class_details['seats']}"
    
    try:
        # The message body is already being correctly encoded to UTF-8
        requests.post(
            f"https://ntfy.sh/{ntfy_topic}",
            data=full_message.encode('utf-8'),
            headers={"Title": title, "Priority": "high", "Tags": "tada"}
        )
        app.logger.info(f"Sent notification for {class_details['classNumber']} ({reason}) to topic '{ntfy_topic}'")
    except Exception as e:
        app.logger.error(f"Error sending ntfy.sh notification: {e}")


# --- Scheduler Setup ---
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_class_statuses, 'interval', minutes=8, id='status_check_job')
scheduler.add_job(hourly_reminder_check, 'interval', minutes=5, id='reminder_check_job')

# --- FIX 2: Prevent scheduler from running twice in debug mode ---
# The reloader runs the app in a subprocess. The main process which monitors for file changes
# should not start the scheduler. `os.environ.get('WERKZEUG_RUN_MAIN')` checks this.
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler.start()
    app.logger.info("Scheduler started.")
else:
    app.logger.info("Scheduler not started in Flask reloader parent process.")


if __name__ == '__main__':
    # Use the PORT environment variable provided by Render, default to 5000
    port = int(os.environ.get('PORT', 5000))
    # strictly disable debug mode in production
    app.run(host='0.0.0.0', port=port, debug=False)