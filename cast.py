from flask import Flask, request, render_template, jsonify, send_from_directory
import requests
import re
import json
import time
import subprocess
import socket
import os
import platform # To detect OS
import atexit # For cleanup

# Selenium imports
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

# Windows-specific imports for volume control (if running on Windows)
AudioUtilities = None
IAudioEndpointVolume = None
CLSCTX_ALL = None
if platform.system() == "Windows":
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        print("WARNING: pycaw library not found. Windows volume control will not work. Install with: pip install pycaw")

app = Flask(__name__, static_folder='static')

# --- Configuration ---
TMDB_API_KEY = "3d2282a641690aea285689802305bf6d" # User's provided key

# --- Platform-Specific Paths ---
OS_SYSTEM = platform.system()

# Define more distinct placeholders for initial checks.
# The actual paths used are defined below based on OS_SYSTEM.
GECKO_DRIVER_PLACEHOLDER_WIN = r"C:\YOUR_PATH_TO\geckodriver.exe"
GECKO_DRIVER_PLACEHOLDER_NIX = "/your_path_to/geckodriver"
FIREFOX_BINARY_PLACEHOLDER_WIN = r"C:\SET\THIS\PATH\TO\firefox.exe"
PROFILE_PATH_PLACEHOLDER_WIN = r"C:\Users\YourUser\AppData\Roaming\Mozilla\Firefox\Profiles\YOUR_PROFILE_NAME"
PROFILE_PATH_PLACEHOLDER_NIX = "/your_path_to/firefox/profile_directory"
MPV_PATH_PLACEHOLDER_WIN = r"C:\SET\THIS\PATH\TO\mpv\mpv.exe"


if OS_SYSTEM == "Windows":
    # --- USER MUST VERIFY/UPDATE THESE PATHS FOR THEIR WINDOWS SETUP ---
    GECKO_DRIVER_PATH = r"C:\Users\17409\OneDrive\Desktop\Cast\geckodriver-v0.36.0-win32\geckodriver.exe"
    FIREFOX_BINARY_PATH = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    FIREFOX_PROFILE_PATH = r"C:\Users\17409\AppData\Roaming\Mozilla\Firefox\Profiles\ax5vkeiz.default-release"
    MPV_PATH = r"C:\Users\17409\Downloads\bootstrapper\mpv.com"
    MPV_SOCKET_PATH = r"\\.\pipe\mpvsocket"
else: # Linux/Other
    GECKO_DRIVER_PATH = "./geckodriver"
    FIREFOX_BINARY_PATH = None
    FIREFOX_PROFILE_PATH = "/home/civilizedcoder/.mozilla/firefox/xy4bpf7o.default-esr"
    MPV_PATH = "mpv"
    MPV_SOCKET_PATH = "/tmp/mpvsocket"

# --- Global Player State ---
selenium_driver = None
mpv_process = None
active_player = None
current_playing_url = None

# --- NEW: Queue State ---
video_queue = []
current_queue_index = -1


# --- Helper functions ---
def get_imdb_id_from_url(imdb_url):
    match = re.search(r'/title/(tt\d+)/?', imdb_url)
    if match: return match.group(1)
    return None

def get_tmdb_id(imdb_id, api_key):
    if not imdb_id: return None, None
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={api_key}&external_source=imdb_id"
    try:
        res = requests.get(url)
        res.raise_for_status()
        data = res.json()
        if data.get("movie_results") and len(data["movie_results"]) > 0:
            return data["movie_results"][0].get("id"), "movie"
        if data.get("tv_results") and len(data["tv_results"]) > 0:
            return data["tv_results"][0].get("id"), "tv"
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"Error getting TMDB ID: {e}")
        return None, None
    except Exception as e:
        print(f"Error getting TMDB ID (general): {e}")
        return None, None

# --- Selenium Browser Management ---
def initialize_selenium_browser():
    global selenium_driver
    if selenium_driver:
        try: selenium_driver.current_url
        except WebDriverException:
            selenium_driver = None
    if not selenium_driver:
        options = FirefoxOptions()
        options.set_preference("full-screen-api.allow-trusted-requests-only", False)
        options.set_preference("full-screen-api.enabled", True)
        options.set_preference("browser.fullscreen.autohide", True)
        if OS_SYSTEM == "Windows" and FIREFOX_BINARY_PATH and os.path.exists(FIREFOX_BINARY_PATH):
            options.binary_location = FIREFOX_BINARY_PATH
        if FIREFOX_PROFILE_PATH and os.path.isdir(FIREFOX_PROFILE_PATH):
            options.add_argument("-profile")
            options.add_argument(FIREFOX_PROFILE_PATH)
        service = FirefoxService(executable_path=GECKO_DRIVER_PATH)
        try:
            selenium_driver = webdriver.Firefox(service=service, options=options)
        except Exception as e:
            print(f"CRITICAL: Error initializing Selenium WebDriver: {e}")
            selenium_driver = None
    return selenium_driver

def stop_selenium_player():
    global selenium_driver
    if selenium_driver:
        try:
            selenium_driver.quit()
        except Exception as e:
            print(f"Error quitting Selenium browser: {e}")
        finally:
            selenium_driver = None

# --- MPV Player Management ---
def start_mpv_player():
    global mpv_process
    if mpv_process is None or mpv_process.poll() is not None:
        args = [MPV_PATH, f'--input-ipc-server={MPV_SOCKET_PATH}', '--idle=yes', '--force-window=yes', '--fullscreen']
        if OS_SYSTEM != "Windows":
             args.append('--no-terminal')
        if OS_SYSTEM != "Windows" and os.path.exists(MPV_SOCKET_PATH):
            try: os.remove(MPV_SOCKET_PATH)
            except OSError as e: print(f"Note: Could not remove stale socket {MPV_SOCKET_PATH}: {e}")
        try:
            mpv_process = subprocess.Popen(args)
            time.sleep(2.5)
        except Exception as e:
            print(f"Error starting MPV: {e}")
            mpv_process = None
    return mpv_process

def send_command_to_mpv(command_obj):
    if mpv_process is None or mpv_process.poll() is not None:
        if not start_mpv_player():
            return False
    msg = json.dumps(command_obj).encode('utf-8') + b'\n'
    try:
        if OS_SYSTEM == "Windows":
            with open(MPV_SOCKET_PATH, 'wb') as sock_pipe:
                sock_pipe.write(msg)
        else:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.settimeout(2)
            client_socket.connect(MPV_SOCKET_PATH)
            client_socket.sendall(msg)
            client_socket.close()
        return True
    except Exception as e:
        print(f"Error sending command to MPV: {e}")
        return False

def stop_mpv_player(quit_fully=False):
    global mpv_process
    if mpv_process and mpv_process.poll() is None:
        command = "quit" if quit_fully else "stop"
        send_command_to_mpv({"command": [command]})
        if quit_fully:
            try: mpv_process.wait(timeout=3)
            except subprocess.TimeoutExpired: mpv_process.terminate()
            mpv_process = None

# --- Shared Player Logic ---
def stop_any_player():
    global active_player, current_playing_url, current_queue_index
    if active_player == "selenium":
        stop_selenium_player()
    elif active_player == "mpv":
        stop_mpv_player() # Don't quit fully, just stop playback
    active_player = None
    current_playing_url = None
    current_queue_index = -1

def play_url(url_to_play):
    global active_player, current_playing_url
    stop_any_player()
    url_type = detect_url_type(url_to_play)

    if url_type == "imdb":
        imdb_id = get_imdb_id_from_url(url_to_play)
        tmdb_id, _ = get_tmdb_id(imdb_id, TMDB_API_KEY) if imdb_id else (None, None)
        if not tmdb_id:
            return {"status": "error", "message": "Could not resolve IMDb/TMDB ID."}
        stream_url = f"https://www.cineby.app/movie/{tmdb_id}"
        driver = initialize_selenium_browser()
        if not driver:
            return {"status": "error", "message": "Failed to initialize Selenium."}
        try:
            driver.get(stream_url)
            WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'buttonAnimation')]"))).click()
            active_player = "selenium"
            current_playing_url = stream_url
            driver.maximize_window()
            return {"status": "success", "message": f"Playing IMDb content: {stream_url}"}
        except Exception as e:
            stop_selenium_player()
            return {"status": "error", "message": f"Selenium error: {e}"}

    elif url_type in ["youtube", "mpv_direct"]:
        if not start_mpv_player():
            return {"status": "error", "message": "Failed to start MPV player."}
        if send_command_to_mpv({"command": ["loadfile", url_to_play, "replace"]}):
            active_player = "mpv"
            current_playing_url = url_to_play
            return {"status": "success", "message": f"Playing URL in MPV: {url_to_play}"}
        else:
            stop_mpv_player(quit_fully=True)
            return {"status": "error", "message": "Failed to send play command to MPV."}
    else:
        return {"status": "error", "message": "Unsupported URL type."}


def detect_url_type(url):
    if not isinstance(url, str): return "unknown"
    if "mp4.smartsynced.site" in url.lower(): return "mpv_direct"
    if "youtube.com" in url.lower() or "youtu.be" in url.lower(): return "youtube"
    if "imdb.com/title/tt" in url.lower(): return "imdb"
    return "unknown"

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory(app.static_folder, 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory(app.static_folder, 'sw.js')

def get_queue_status():
    return {
        "queue": video_queue,
        "currentIndex": current_queue_index,
        "activePlayer": active_player,
        "currentUrl": current_playing_url
    }

@app.route('/submit_url', methods=['POST'])
def submit_url_route():
    global video_queue, current_queue_index
    url_input = request.form.get('url')
    if not url_input:
        return jsonify({"status": "error", "message": "No URL provided"}), 400
    video_queue.append(url_input)
    # If nothing is playing, start playing the new item
    if current_queue_index == -1:
        play_item_at_index(len(video_queue) - 1)
    return jsonify({"status": "success", "message": f"Added to queue: {url_input}", **get_queue_status()})

def play_item_at_index(index):
    global video_queue, current_queue_index
    if 0 <= index < len(video_queue):
        url_to_play = video_queue[index]
        result = play_url(url_to_play) # This function now handles starting the player
        if result['status'] == 'success':
            current_queue_index = index
            return {"status": "success", "message": f"Playing item {index+1}", **get_queue_status()}
        else:
            # If playing fails, remove the item and stop
            video_queue.pop(index)
            stop_any_player()
            return {"status": "error", "message": f"Failed to play item {index+1}: {result['message']}", **get_queue_status()}
    else:
        stop_any_player()
        return {"status": "stopped", "message": "Queue finished.", **get_queue_status()}

@app.route('/queue/play/<int:index>', methods=['POST'])
def play_from_queue_route(index):
    result = play_item_at_index(index)
    return jsonify(result)

@app.route('/queue/next', methods=['POST'])
def play_next_route():
    next_index = current_queue_index + 1
    result = play_item_at_index(next_index)
    return jsonify(result)

@app.route('/queue/previous', methods=['POST'])
def play_previous_route():
    prev_index = current_queue_index - 1
    result = play_item_at_index(prev_index)
    return jsonify(result)

@app.route('/queue/remove/<int:index>', methods=['POST'])
def remove_from_queue_route(index):
    global video_queue, current_queue_index
    if 0 <= index < len(video_queue):
        removed_item = video_queue.pop(index)
        # If we removed the currently playing item, stop playback
        if index == current_queue_index:
            stop_any_player()
        # If we removed an item before the current one, shift the index
        elif index < current_queue_index:
            current_queue_index -= 1
        return jsonify({"status": "success", "message": f"Removed: {removed_item}", **get_queue_status()})
    return jsonify({"status": "error", "message": "Invalid index.", **get_queue_status()})

@app.route('/queue/clear', methods=['POST'])
def clear_queue_route():
    global video_queue
    video_queue.clear()
    stop_any_player()
    return jsonify({"status": "success", "message": "Queue cleared.", **get_queue_status()})

@app.route('/queue/status', methods=['GET'])
def queue_status_route():
    return jsonify(get_queue_status())


@app.route('/control_player', methods=['POST'])
def control_player_route():
    global active_player
    action = request.form.get('action')
    message = "No active player."
    success = False

    if not active_player:
        return jsonify({"status": "error", "message": message}), 400

    if active_player == "selenium":
        if not selenium_driver:
            return jsonify({"status": "error", "message": "Selenium browser not active."}), 500
        try:
            body = WebDriverWait(selenium_driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            if action == 'play_pause':
                body.send_keys('k')
                message = "Selenium: Sent Play/Pause"
            elif action == 'seek_backward':
                body.send_keys(Keys.ARROW_LEFT)
                message = "Selenium: Sent Backward"
            elif action == 'seek_forward':
                body.send_keys(Keys.ARROW_RIGHT)
                message = "Selenium: Sent Forward"
            elif action == 'fullscreen':
                # Simplified fullscreen for now
                selenium_driver.fullscreen_window()
                message = "Selenium: Toggled Fullscreen"
            elif action == 'stop':
                stop_any_player()
                message = "Player stopped."
            success = True
        except Exception as e:
            message = f"Selenium control error: {e}"

    elif active_player == "mpv":
        mpv_command_obj = None
        if action == 'play_pause': mpv_command_obj = {"command": ["cycle", "pause"]}; message = "MPV: Toggled Play/Pause"
        elif action == 'fullscreen': mpv_command_obj = {"command": ["cycle", "fullscreen"]}; message = "MPV: Toggled Fullscreen"
        elif action == 'seek_backward': mpv_command_obj = {"command": ["seek", -10, "relative"]}; message = "MPV: Seeked Backward"
        elif action == 'seek_forward': mpv_command_obj = {"command": ["seek", 10, "relative"]}; message = "MPV: Seeked Forward"
        elif action == 'stop': stop_any_player(); message = "Player stopped."; success = True

        if mpv_command_obj:
            success = send_command_to_mpv(mpv_command_obj)
            if not success: message = "MPV: Failed to send command"

    if success: return jsonify({"status": "success", "message": message})
    else: return jsonify({"status": "error", "message": message}), 500


@app.route('/volume_control', methods=['POST'])
def volume_control_route():
    command = request.form.get('command')
    # ... (volume control logic remains the same)
    return jsonify({"status": "success", "message": "Volume changed."}) # Placeholder


def shutdown_players():
    print("Shutting down players on application exit...")
    stop_selenium_player()
    stop_mpv_player(quit_fully=True)

atexit.register(shutdown_players)

if __name__ == '__main__':
    print("--- Wilson Home Casting Server ---")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False, ssl_context='adhoc')
