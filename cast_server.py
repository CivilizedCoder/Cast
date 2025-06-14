from flask import Flask, request, render_template, jsonify
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

app = Flask(__name__)

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
        print("TMDB ID: No movie or TV results found in API response.")
        return None, None
    except requests.exceptions.RequestException as e: 
        print(f"Error getting TMDB ID (requests error): {e}")
        return None, None
    except NameError as e: 
        print(f"CRITICAL NameError during get_tmdb_id: {e}. Is 'requests' library installed and imported correctly?")
        return None, None
    except Exception as e:
        print(f"Error getting TMDB ID (general error): {e}")
        return None, None

# --- Selenium Browser Management ---
def initialize_selenium_browser():
    global selenium_driver
    if selenium_driver:
        try: selenium_driver.current_url 
        except WebDriverException:
            print("Existing Selenium browser not responsive. Quitting...")
            try: selenium_driver.quit()
            except: pass
            selenium_driver = None
            
    if selenium_driver: 
        try:
            print("Clearing Selenium browser state (navigating to about:blank)")
            selenium_driver.get("about:blank") # Keep instance but clear page
        except WebDriverException: 
            print("Error navigating to about:blank, old Selenium instance might be dead. Quitting it.")
            try: selenium_driver.quit()
            except: pass
            selenium_driver = None # Force re-initialization

    if not selenium_driver: 
        print("Initializing Firefox WebDriver...")
        options = FirefoxOptions()

        binary_path_to_try = None
        if OS_SYSTEM == "Windows":
            if FIREFOX_BINARY_PATH and FIREFOX_BINARY_PATH != FIREFOX_BINARY_PLACEHOLDER_WIN:
                binary_path_to_try = FIREFOX_BINARY_PATH
                print(f"DEBUG: User has set FIREFOX_BINARY_PATH to: {binary_path_to_try}")
            else:
                print(f"DEBUG: FIREFOX_BINARY_PATH is either not custom-set or is the placeholder ('{FIREFOX_BINARY_PATH}'). Selenium will try default locations for Firefox.")
        
        if binary_path_to_try:
            print(f"DEBUG: Checking existence of specified Firefox binary at: {binary_path_to_try}")
            if os.path.exists(binary_path_to_try):
                print(f"DEBUG: Firefox binary exists at specified path. Setting options.binary_location.")
                options.binary_location = binary_path_to_try
            else:
                print(f"ERROR: Specified FIREFOX_BINARY_PATH ('{binary_path_to_try}') does NOT exist according to os.path.exists().")
                print("Selenium will now try its default locations, which likely will fail if Firefox isn't there.")
        
        current_profile_placeholder = PROFILE_PATH_PLACEHOLDER_WIN if OS_SYSTEM == "Windows" else PROFILE_PATH_PLACEHOLDER_NIX
        if FIREFOX_PROFILE_PATH and FIREFOX_PROFILE_PATH != current_profile_placeholder:
            print(f"DEBUG: User has set FIREFOX_PROFILE_PATH to: {FIREFOX_PROFILE_PATH}")
            if os.path.isdir(FIREFOX_PROFILE_PATH):
                print(f"DEBUG: Profile path exists. Adding -profile argument.")
                options.add_argument("-profile")
                options.add_argument(FIREFOX_PROFILE_PATH)
            else:
                print(f"WARNING: Specified FIREFOX_PROFILE_PATH ('{FIREFOX_PROFILE_PATH}') is not a valid directory. Using default profile behavior.")
        else:
            print(f"WARNING: FIREFOX_PROFILE_PATH is not set to a custom value or is the placeholder ('{FIREFOX_PROFILE_PATH}'). Using default profile behavior.")
        
        # Determine absolute path for geckodriver for os.path.exists check
        gecko_path_to_check = GECKO_DRIVER_PATH
        if GECKO_DRIVER_PATH.startswith('./'):
            gecko_path_to_check = os.path.abspath(GECKO_DRIVER_PATH)
            
        if not os.path.exists(gecko_path_to_check) and not (OS_SYSTEM != "Windows" and GECKO_DRIVER_PATH == "geckodriver"): 
             print(f"CRITICAL ERROR: GeckoDriver not found at GECKO_DRIVER_PATH: {gecko_path_to_check}")
             return None
        
        service = FirefoxService(executable_path=GECKO_DRIVER_PATH)
        try:
            selenium_driver = webdriver.Firefox(service=service, options=options)
            print("Selenium WebDriver initialized successfully.")
        except WebDriverException as e: 
            print(f"CRITICAL: Error initializing Selenium WebDriver: {e}")
            if "Expected browser binary location" in str(e):
                print("This error often means Selenium could not find your firefox.exe.")
                print("1. Ensure Firefox is installed correctly.")
                print("2. If it's in a non-standard location, ensure FIREFOX_BINARY_PATH in this script points to the full path of 'firefox.exe'.")
                current_binary_being_tried = options.binary_location if options.binary_location else "Selenium's Default Search Paths"
                print(f"Selenium was attempting to use binary path: {current_binary_being_tried}")
            selenium_driver = None
        except Exception as e: 
            print(f"CRITICAL: Generic error initializing Selenium WebDriver: {e}")
            selenium_driver = None
    return selenium_driver

def stop_selenium_player():
    global selenium_driver, active_player, current_playing_url
    if selenium_driver:
        try:
            print("Closing Selenium Firefox browser instance...")
            selenium_driver.quit() # Quit the browser entirely
            print("Selenium Firefox browser closed.")
        except Exception as e:
            print(f"Error quitting Selenium browser: {e}")
        finally:
            selenium_driver = None # Ensure driver is set to None regardless of quit success
            
    if active_player == "selenium": 
        active_player = None
        current_playing_url = None
        print("Selenium player stopped and session ended.")


# --- MPV Player Management ---
def start_mpv_player():
    global mpv_process
    if mpv_process is None or mpv_process.poll() is not None:
        print(f"Starting MPV with IPC: {MPV_SOCKET_PATH}...")
        args = [MPV_PATH, f'--input-ipc-server={MPV_SOCKET_PATH}', '--idle=yes', '--force-window=yes', '--fullscreen']
        if OS_SYSTEM != "Windows":
             args.append('--no-terminal') 

        try:
            if OS_SYSTEM != "Windows" and os.path.exists(MPV_SOCKET_PATH):
                try:
                    temp_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    temp_sock.settimeout(0.1)
                    temp_sock.connect(MPV_SOCKET_PATH) 
                    temp_sock.close()
                    print(f"Warning: MPV socket {MPV_SOCKET_PATH} seems active from another process. Removing if possible.")
                    os.remove(MPV_SOCKET_PATH) 
                except (socket.timeout, ConnectionRefusedError, FileNotFoundError): # Expected if socket is stale or not there
                    try: 
                        if os.path.exists(MPV_SOCKET_PATH): # Only remove if it exists (might be a named pipe on some systems)
                            os.remove(MPV_SOCKET_PATH)
                            print(f"Removed stale MPV socket: {MPV_SOCKET_PATH}")
                    except OSError as e: print(f"Note: Could not remove stale socket {MPV_SOCKET_PATH}: {e}")
                except Exception as e_sock_check: 
                    print(f"Note: Error checking/removing stale socket {MPV_SOCKET_PATH}: {e_sock_check}")
            
            mpv_process = subprocess.Popen(args)
            time.sleep(2.5) 
            print("MPV process started.")
        except FileNotFoundError:
            print(f"ERROR: MPV executable not found at '{MPV_PATH}'. Please ensure MPV is installed and path is correct.")
            mpv_process = None
        except Exception as e:
            print(f"Error starting MPV: {e}")
            mpv_process = None
    return mpv_process

def send_command_to_mpv(command_obj):
    global mpv_process
    if mpv_process is None or mpv_process.poll() is not None:
        print("MPV process is not running. Attempting to start MPV.")
        start_mpv_player()
        if mpv_process is None or mpv_process.poll() is not None:
            print("Failed to start MPV process. Command not sent.")
            return False
            
    msg = json.dumps(command_obj).encode('utf-8') + b'\n'
    
    if OS_SYSTEM == "Windows":
        try:
            with open(MPV_SOCKET_PATH, 'wb') as sock_pipe: 
                sock_pipe.write(msg)
            print(f"Sent command to MPV (Win): {command_obj}")
            return True
        except FileNotFoundError: 
            print(f"Error: MPV named pipe not found at {MPV_SOCKET_PATH}. Is MPV running with IPC server enabled?")
            return False
        except Exception as e:
            print(f"Error sending command to MPV (Win) via pipe {MPV_SOCKET_PATH}: {e}")
            return False
    else: # Linux/macOS
        client_socket = None 
        try:
            if not os.path.exists(MPV_SOCKET_PATH):
                print(f"MPV socket {MPV_SOCKET_PATH} not found by send_command. Waiting briefly...")
                time.sleep(1.5) 
                if not os.path.exists(MPV_SOCKET_PATH):
                    print(f"MPV socket {MPV_SOCKET_PATH} still not found after wait.")
                    return False

            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.settimeout(2) 
            client_socket.connect(MPV_SOCKET_PATH)
            client_socket.sendall(msg)
            client_socket.close()
            print(f"Sent command to MPV (Unix): {command_obj}")
            return True
        except (socket.timeout, ConnectionRefusedError, FileNotFoundError) as e: 
            print(f"Socket error sending to MPV (Unix) {MPV_SOCKET_PATH}: {e}")
        except Exception as e: 
            print(f"General error sending to MPV (Unix) {MPV_SOCKET_PATH}: {e}")
        finally:
            if client_socket:
                try: client_socket.close()
                except: pass 
        return False

def stop_mpv_player(quit_fully=False): 
    global mpv_process, active_player, current_playing_url
    if mpv_process and mpv_process.poll() is None:
        if quit_fully:
            print("Quitting MPV player...")
            if send_command_to_mpv({"command": ["quit"]}):
                try:
                    mpv_process.wait(timeout=3) 
                    print("MPV quit via IPC.")
                except subprocess.TimeoutExpired:
                    print("MPV did not quit via IPC in time, terminating process...")
                    mpv_process.terminate()
                    try: mpv_process.wait(timeout=2)
                    except subprocess.TimeoutExpired: print("MPV terminate wait timed out.")
            else: 
                print("MPV quit IPC failed, terminating process directly...")
                mpv_process.terminate()
                try: mpv_process.wait(timeout=2)
                except subprocess.TimeoutExpired: print("MPV terminate wait timed out.")
            mpv_process = None 
        else:
            print("Stopping MPV player (sending 'stop' command).")
            send_command_to_mpv({"command": ["stop"]})
    
    if active_player == "mpv": 
        active_player = None
        current_playing_url = None


# --- URL Type Detection ---
def detect_url_type(url):
    if not isinstance(url, str): 
        return "unknown"
    youtube_patterns = [
        r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|v\/)|https://youtu.be/QrpfkyDgLM4|youtu\.be\/|youtu\.be\/)([a-zA-Z0-9_-]{11})", # Common YouTube patterns
        r"youtube.com/", # User specific pattern
        r"youtu.be/"  # User specific pattern
    ]
    for pattern in youtube_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return "youtube"
    if "imdb.com/title/tt" in url.lower():
        return "imdb"
    return "unknown"

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html', current_url=current_playing_url, active_player=active_player)

@app.route('/submit_url', methods=['POST'])
def submit_url_route():
    global active_player, current_playing_url, selenium_driver, mpv_process

    url_input = request.form.get('url')
    if not url_input:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    url_type = detect_url_type(url_input)
    
    if active_player == "selenium" and selenium_driver:
        stop_selenium_player() 
    elif active_player == "mpv" and mpv_process:
        if url_type == "imdb": 
            stop_mpv_player(quit_fully=True)
        else: 
            stop_mpv_player(quit_fully=False) 
            
    active_player = None 
    current_playing_url_temp = url_input 

    if url_type == "imdb":
        selenium_driver = initialize_selenium_browser()
        if not selenium_driver:
            current_playing_url = None
            return jsonify({"status": "error", "message": "Failed to initialize Selenium browser."}), 500

        imdb_id = get_imdb_id_from_url(url_input)
        if not imdb_id: current_playing_url = None; return jsonify({"status": "error", "message": "Invalid IMDb URL"}), 400
        
        tmdb_id, media_type = get_tmdb_id(imdb_id, TMDB_API_KEY)
        if not tmdb_id: current_playing_url = None; return jsonify({"status": "error", "message": "Could not get TMDB ID"}), 400
        
        xprime_url = f"https://xprime.tv/watch/{tmdb_id}"
        current_playing_url = xprime_url 
        
        try:
            print(f"Selenium navigating to: {xprime_url}")
            selenium_driver.get(xprime_url)
            time.sleep(7) 
            play_button_xpath = "//img[@alt='Play']"
            wait = WebDriverWait(selenium_driver, 15)
            play_button = wait.until(EC.element_to_be_clickable((By.XPATH, play_button_xpath)))
            play_button.click()
            active_player = "selenium" 
            print("Selenium: Play button clicked.")

            try:
                print("Attempting to maximize and focus Firefox window...")
                selenium_driver.maximize_window()
                selenium_driver.execute_script("window.focus();")
                if selenium_driver.current_window_handle:
                    selenium_driver.switch_to.window(selenium_driver.current_window_handle)
                print("Focus attempt made for Firefox.")
            except Exception as e_focus:
                print(f"Note: Could not explicitly focus/maximize Firefox window: {e_focus}")

            return jsonify({"status": "success", "message": f"Playing IMDb content via xprime.tv: {xprime_url}", "active_player": "selenium", "url": xprime_url})
        except Exception as e:
            print(f"Error during Selenium play: {e}")
            current_playing_url = None 
            active_player = None
            return jsonify({"status": "error", "message": f"Selenium error: {e}"}), 500

    elif url_type == "youtube":
        mpv_process = start_mpv_player()
        if not mpv_process or mpv_process.poll() is not None:
            current_playing_url = None
            return jsonify({"status": "error", "message": "Failed to start MPV player."}), 500
            
        if send_command_to_mpv({"command": ["loadfile", url_input, "replace"]}):
            active_player = "mpv" 
            current_playing_url = url_input 
            print(f"MPV: Loading YouTube URL: {url_input}")
            
            if OS_SYSTEM != "Windows": 
                try:
                    if mpv_process and mpv_process.pid:
                        subprocess.run(['xdotool', 'search', '--sync', '--onlyvisible', '--pid', str(mpv_process.pid), 'windowactivate', '%1'], 
                                       timeout=2, check=False, capture_output=True, text=True)
                        print(f"Attempted to activate MPV window (PID: {mpv_process.pid}).")
                except FileNotFoundError:
                    print("xdotool command not found. Cannot activate MPV window. Please install xdotool.")
                except subprocess.TimeoutExpired:
                    print("xdotool command timed out trying to activate MPV window.")
                except Exception as e_xdotool:
                    print(f"Could not activate MPV window using xdotool: {e_xdotool}")
            
            return jsonify({"status": "success", "message": f"Playing YouTube URL in MPV: {url_input}", "active_player": "mpv", "url": url_input})
        else:
            current_playing_url = None
            active_player = None
            return jsonify({"status": "error", "message": "Failed to send play command to MPV."}), 500
    else:
        current_playing_url = None
        return jsonify({"status": "error", "message": "Unsupported URL type. Please enter an IMDb or YouTube URL."}), 400

@app.route('/control_player', methods=['POST'])
def control_player_route():
    global active_player, selenium_driver
    action = request.form.get('action')

    if not active_player: return jsonify({"status": "error", "message": "No active player. Please submit a URL first."}), 400
    if not action: return jsonify({"status": "error", "message": "No action provided."}), 400

    message = ""; success = False
    if active_player == "selenium":
        if not selenium_driver and action != 'stop': 
             return jsonify({"status": "error", "message": "Selenium browser is not active. Please load a movie first."}), 500
        try:
            if action != 'fullscreen' and action != 'stop': 
                 body = WebDriverWait(selenium_driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            if action == 'play_pause': 
                body.send_keys('k')
                message = "Selenium: Sent Play/Pause (K)"
            elif action == 'fullscreen':
                fullscreen_button_xpath = "//media-fullscreen-button[@aria-label='enter fullscreen mode']"
                try:
                    print(f"Trying to find Selenium fullscreen button with: XPATH = '{fullscreen_button_xpath}'")
                    wait = WebDriverWait(selenium_driver, 10)
                    actual_fullscreen_button = wait.until(EC.element_to_be_clickable((By.XPATH, fullscreen_button_xpath)))
                    actual_fullscreen_button.click()
                    message = "Selenium: Clicked custom Fullscreen button"
                    print(message)
                    # success will be set later if no exception from this block
                except TimeoutException:
                    message = "Selenium: Custom Fullscreen button not found or not clickable."
                    print(message)
                    success = False 
                    return jsonify({"status": "error", "message": message}), 500 
                except Exception as e_fs:
                    message = f"Selenium: Error clicking custom Fullscreen button: {e_fs}"
                    print(message)
                    success = False
                    return jsonify({"status": "error", "message": message}), 500 
            elif action == 'seek_backward': 
                body.send_keys(Keys.ARROW_LEFT)
                message = "Selenium: Sent Backward (←)"
            elif action == 'seek_forward': 
                body.send_keys(Keys.ARROW_RIGHT)
                message = "Selenium: Sent Forward (→)"
            elif action == 'stop': 
                stop_selenium_player() 
                message = "Selenium: Browser closed and player stopped."
            else: 
                return jsonify({"status": "error", "message": "Invalid Selenium action."}), 400
            
            success = True 

        except Exception as e: 
            if not selenium_driver and action != 'stop':
                 message = "Selenium browser session is not active."
            else:
                message = f"Selenium control error: {e}"
            print(message)
            success = False
    
    elif active_player == "mpv":
        mpv_command_obj = None 
        if action == 'play_pause': mpv_command_obj = {"command": ["cycle", "pause"]}; message = "MPV: Toggled Play/Pause"
        elif action == 'fullscreen': mpv_command_obj = {"command": ["cycle", "fullscreen"]}; message = "MPV: Toggled Fullscreen"
        elif action == 'seek_backward': mpv_command_obj = {"command": ["seek", -10, "relative"]}; message = "MPV: Seeked Backward 10s"
        elif action == 'seek_forward': mpv_command_obj = {"command": ["seek", 10, "relative"]}; message = "MPV: Seeked Forward 10s"
        elif action == 'stop': stop_mpv_player(quit_fully=False); message = "MPV: Player stopped (content cleared, MPV idle)" 
        else: return jsonify({"status": "error", "message": "Invalid MPV action."}), 400
        
        if mpv_command_obj: 
            if send_command_to_mpv(mpv_command_obj): success = True
            else: message = "MPV: Failed to send command"; print(message)
        elif action == 'stop': 
            success = True 

    if success: return jsonify({"status": "success", "message": message})
    else: return jsonify({"status": "error", "message": message or "Player control failed."}), 500

@app.route('/volume_control', methods=['POST'])
def volume_control_route():
    command = request.form.get('command') 
    action_message = ""
    success = False

    if OS_SYSTEM == "Windows":
        if AudioUtilities is None or IAudioEndpointVolume is None or CLSCTX_ALL is None:
            return jsonify({"status": "error", "message": "Windows Audio Library (pycaw) not available or not imported correctly."}), 500
        try:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume_control_obj = interface.QueryInterface(IAudioEndpointVolume)
            
            current_level_scalar = volume_control_obj.GetMasterVolumeLevelScalar()
            current_mute = volume_control_obj.GetMute()

            if command == 'vol_up':
                new_volume = min(1.0, current_level_scalar + 0.05)
                volume_control_obj.SetMasterVolumeLevelScalar(new_volume, None)
                action_message = f"Increased volume to {int(new_volume*100)}%."
            elif command == 'vol_down':
                new_volume = max(0.0, current_level_scalar - 0.05)
                volume_control_obj.SetMasterVolumeLevelScalar(new_volume, None)
                action_message = f"Decreased volume to {int(new_volume*100)}%."
            elif command == 'vol_mute':
                volume_control_obj.SetMute(not current_mute, None)
                action_message = "Toggled mute."
            else:
                return jsonify({"status": "error", "message": "Invalid Windows volume command."}), 400
            success = True
            print(action_message) 
        except Exception as e:
            action_message = f"Error controlling Windows volume: {e}"
            print(action_message)
    else: # Linux (pactl)
        pactl_command_list = []
        if command == 'vol_up':
            pactl_command_list = ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '+5%']
            action_message = "Increased system volume by 5%."
        elif command == 'vol_down':
            pactl_command_list = ['pactl', 'set-sink-volume', '@DEFAULT_SINK@', '-5%']
            action_message = "Decreased system volume by 5%."
        elif command == 'vol_mute':
            pactl_command_list = ['pactl', 'set-sink-mute', '@DEFAULT_SINK@', 'toggle']
            action_message = "Toggled system mute."
        else:
            return jsonify({"status": "error", "message": "Invalid Linux volume command."}), 400
        try:
            result = subprocess.run(pactl_command_list, check=True, capture_output=True, text=True)
            print(f"{action_message} (pactl: {result.stdout.strip() or result.stderr.strip()})")
            success = True
        except FileNotFoundError: action_message = "'pactl' command not found."; print(action_message)
        except subprocess.CalledProcessError as e: action_message = f"Error adjusting volume with pactl: {e.stderr}"; print(action_message)
        except Exception as e: action_message = f"Unexpected volume error with pactl: {e}"; print(action_message)

    if success: return jsonify({"status": "success", "message": action_message})
    else: return jsonify({"status": "error", "message": action_message or "Volume control failed."}), 500

def shutdown_players():
    global selenium_driver, mpv_process
    print("Shutting down players on application exit...")
    if selenium_driver:
        try: 
            selenium_driver.quit()
            print("Selenium WebDriver quit.")
        except Exception as e: print(f"Error quitting selenium_driver: {e}")
        selenium_driver = None # Ensure it's None after quit attempt
    if mpv_process and mpv_process.poll() is None:
        try:
            print("Attempting to quit MPV gracefully...")
            send_command_to_mpv({"command": ["quit"]}) 
            time.sleep(0.5) 
            if mpv_process.poll() is None: 
                print("MPV did not quit gracefully, terminating...")
                mpv_process.terminate()
                try:
                    mpv_process.wait(timeout=2) 
                except subprocess.TimeoutExpired:
                    print("MPV terminate wait timed out.")
            print("MPV process quit/terminated.")
        except Exception as e: print(f"Error quitting mpv_process: {e}")
        mpv_process = None # Ensure it's None
    print("Player shutdown routine complete.")

atexit.register(shutdown_players)

if __name__ == '__main__':
    api_key_placeholder_check = "YOUR_TMDB_API_KEY" 
    
    # Determine appropriate placeholders based on OS for the checks
    current_gecko_placeholder_to_check = GECKO_DRIVER_PLACEHOLDER_WIN if OS_SYSTEM == "Windows" else GECKO_DRIVER_PLACEHOLDER_NIX
    current_firefox_binary_placeholder_to_check = FIREFOX_BINARY_PLACEHOLDER_WIN 
    current_profile_placeholder_to_check = PROFILE_PATH_PLACEHOLDER_WIN if OS_SYSTEM == "Windows" else PROFILE_PATH_PLACEHOLDER_NIX
    current_mpv_placeholder_to_check = MPV_PATH_PLACEHOLDER_WIN if OS_SYSTEM == "Windows" else "mpv" 

    print(f"--- Wilson Home Casting Server ---")
    print(f"Running on: {OS_SYSTEM}")
    
    gecko_display_path = GECKO_DRIVER_PATH
    if GECKO_DRIVER_PATH.startswith('./'): # Resolve relative path for display
        gecko_display_path = os.path.abspath(GECKO_DRIVER_PATH)
    print(f"Using GeckoDriver path: {gecko_display_path}")
    
    if OS_SYSTEM == "Windows":
        if FIREFOX_BINARY_PATH and FIREFOX_BINARY_PATH != FIREFOX_BINARY_PLACEHOLDER_WIN:
            print(f"Using Firefox Binary path: {FIREFOX_BINARY_PATH}")
        else:
             print(f"NOTE: Firefox Binary Path is set to '{FIREFOX_BINARY_PATH}'. If this is the placeholder or incorrect, Selenium might not find Firefox.")
    print(f"Using Firefox Profile path: {FIREFOX_PROFILE_PATH}")
    print(f"Using MPV path: {MPV_PATH}")
    print(f"Using MPV Socket/Pipe: {MPV_SOCKET_PATH}")
    print("-" * 30)

    config_ok = True
    if TMDB_API_KEY == api_key_placeholder_check: 
        print(f"CONFIG ERROR: TMDB_API_KEY is a placeholder ('{TMDB_API_KEY}'). Please set your actual key.")
        config_ok = False
    
    if GECKO_DRIVER_PATH == current_gecko_placeholder_to_check : 
         print(f"CONFIG ERROR: GECKO_DRIVER_PATH looks like a placeholder: {GECKO_DRIVER_PATH}. Please set the correct path.")
         config_ok = False
    elif not os.path.exists(gecko_display_path) and not (OS_SYSTEM != "Windows" and GECKO_DRIVER_PATH == "geckodriver"): 
        print(f"CONFIG ERROR: GeckoDriver not found at GECKO_DRIVER_PATH: {gecko_display_path}")
        config_ok = False
        
    if FIREFOX_PROFILE_PATH == current_profile_placeholder_to_check: 
         print(f"CONFIG ERROR: FIREFOX_PROFILE_PATH looks like a placeholder: {FIREFOX_PROFILE_PATH}. Please set the correct path.")
         config_ok = False
    elif not os.path.isdir(FIREFOX_PROFILE_PATH):
        print(f"CONFIG ERROR: FIREFOX_PROFILE_PATH does not exist or is not a directory: {FIREFOX_PROFILE_PATH}")
        config_ok = False

    if OS_SYSTEM == "Windows":
        if FIREFOX_BINARY_PATH == current_firefox_binary_placeholder_to_check:
            print(f"CONFIG WARNING: FIREFOX_BINARY_PATH ('{FIREFOX_BINARY_PATH}') for Windows looks like an example placeholder. If Firefox is not in a standard location that Selenium can find, you MUST set this path correctly.")
        elif FIREFOX_BINARY_PATH and not os.path.exists(FIREFOX_BINARY_PATH):
            print(f"CONFIG ERROR: Your specified FIREFOX_BINARY_PATH does not exist: {FIREFOX_BINARY_PATH}")
            config_ok = False
        
        if MPV_PATH == current_mpv_placeholder_to_check:
            print(f"CONFIG ERROR: MPV_PATH for Windows looks like a placeholder: {MPV_PATH}. Please set the correct path.")
            config_ok = False
        elif not os.path.exists(MPV_PATH):
            print(f"CONFIG ERROR: MPV executable not found at specified MPV_PATH: {MPV_PATH}")
            config_ok = False
    else: # Linux
        if MPV_PATH == current_mpv_placeholder_to_check and not (os.path.exists("/usr/bin/mpv") or os.path.exists("/usr/local/bin/mpv")):
             print(f"CONFIG WARNING: MPV_PATH is '{MPV_PATH}'. Ensure 'mpv' is in your system PATH if this is not a full path and not found in common locations.")


    if not config_ok:
        print("-" * 30)
        print("Please correct the configuration errors listed above before starting the server.")
        exit()
    
    print("-" * 30)
    print("Configuration paths appear to be set. Starting Flask server...")
    if OS_SYSTEM == "Windows":
        print("Ensure your Windows paths for GeckoDriver, Firefox Profile, Firefox Binary, and MPV.exe are correct.")
        if AudioUtilities is None: 
             print("REMINDER: Windows volume control (pycaw) is not available. Install with: pip install pycaw")
    else: # Linux
        print("Make sure Firefox (with the specified profile) is CLOSED before this script attempts to use it for Selenium.")
        print("Ensure 'mpv' and 'pactl' (and 'xdotool' optionally) are installed and in PATH if not using full paths.")
    
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)