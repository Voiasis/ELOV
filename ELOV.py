#!/usr/bin/env python3
import time
from datetime import datetime
import psutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk
from pythonosc import udp_client
import re
import os
import glob
import tzlocal
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
import json
import pyperclip
import base64
from io import BytesIO
from PIL import Image, ImageTk
import urllib.request

# Config file location
CONFIG_DIR = os.path.expanduser("~/.config/VoisLOVE")
CONFIG_FILE = os.path.join(CONFIG_DIR, "vois_love_config.json")
ICON_PATH = os.path.join(CONFIG_DIR, "heart.png")
ICON_URL = "https://raw.githubusercontent.com/Voiasis/VoisLOVE/refs/heads/main/heart.png"

# Cache for primary GPU
primary_gpu_cache = None

def get_gpu_info():
    """Detect all GPUs and map to DRM cards."""
    try:
        lspci = subprocess.run(
            ["lspci", "-nn"], capture_output=True, text=True, check=True
        )
        gpus = []
        for line in lspci.stdout.splitlines():
            if "VGA" in line or "3D" in line:
                bus_id = line.split()[0]
                name = line.lower()
                gpu_type = None
                if "amd" in name or "ati" in name:
                    gpu_type = "amd"
                elif "nvidia" in name:
                    gpu_type = "nvidia"
                elif "intel" in name:
                    gpu_type = "intel"
                if gpu_type:
                    card = None
                    for drm_card in glob.glob("/sys/class/drm/card[0-9]*"):
                        try:
                            with open(os.path.join(drm_card, "device", "uevent"), "r") as f:
                                uevent = f.read()
                                if bus_id in uevent:
                                    card = os.path.basename(drm_card)
                                    break
                        except (IOError, FileNotFoundError):
                            continue
                    gpus.append({
                        "bus_id": bus_id,
                        "type": gpu_type,
                        "name": line,
                        "card": card
                    })
        return gpus
    except subprocess.SubprocessError as e:
        print(f"Error listing GPUs: {e}")
        return []

def get_primary_gpu_xrandr():
    """Identify the GPU connected to the primary monitor."""
    try:
        xrandr = subprocess.run(
            ["xrandr", "--current"], capture_output=True, text=True, check=True
        )
        primary_output = None
        for line in xrandr.stdout.splitlines():
            if " primary " in line and " connected " in line:
                primary_output = line.split()[0]
                break
        if not primary_output:
            print("No primary output detected")
            return None

        drm_cards = glob.glob("/sys/class/drm/card[0-9]*")
        primary_bus_id = None
        for card in drm_cards:
            try:
                with open(os.path.join(card, "device", "uevent"), "r") as f:
                    uevent = f.read()
                    bus_id_match = re.search(r"PCI_SLOT_NAME=0000:([0-9a-f:.]+)", uevent)
                    if bus_id_match:
                        bus_id = bus_id_match.group(1)
                        output_path = os.path.join(os.path.dirname(card), primary_output)
                        if os.path.exists(output_path):
                            primary_bus_id = bus_id
                            break
            except (IOError, FileNotFoundError):
                continue

        if not primary_bus_id:
            print("Could not map primary output to GPU")
            return None

        gpus = get_gpu_info()
        for gpu in gpus:
            if gpu["bus_id"] == primary_bus_id:
                print(f"Primary GPU (xrandr): {gpu['type']} at {gpu['bus_id']} ({gpu['card']})")
                return gpu
        print(f"No GPU found for bus ID {primary_bus_id}")
        return None
    except subprocess.SubprocessError as e:
        print(f"xrandr error: {e}")
        return None

def get_rendering_gpu():
    """Identify the GPU used for rendering (via glxinfo)."""
    try:
        for dri_prime in ["1", "0"]:
            env = os.environ.copy()
            env["DRI_PRIME"] = dri_prime
            glxinfo = subprocess.run(
                ["glxinfo"], capture_output=True, text=True, check=True, env=env
            )
            renderer = None
            for line in glxinfo.stdout.splitlines():
                if "OpenGL renderer" in line:
                    renderer = line.lower()
                    break
            if renderer:
                gpus = get_gpu_info()
                if "amd" in renderer or "radeon" in renderer:
                    for gpu in gpus:
                        if gpu["type"] == "amd":
                            print(f"Rendering GPU (DRI_PRIME={dri_prime}): AMD at {gpu['bus_id']} ({gpu['card']})")
                            return gpu
                elif "nvidia" in renderer:
                    for gpu in gpus:
                        if gpu["type"] == "nvidia":
                            print(f"Rendering GPU (DRI_PRIME={dri_prime}): NVIDIA at {gpu['bus_id']} ({gpu['card']})")
                            return gpu
                elif "intel" in renderer:
                    for gpu in gpus:
                        if gpu["type"] == "intel":
                            print(f"Rendering GPU (DRI_PRIME={dri_prime}): Intel at {gpu['bus_id']} ({gpu['card']})")
                            return gpu
        print(f"No GPU matched for renderer: {renderer}")
        return None
    except subprocess.SubprocessError as e:
        print(f"glxinfo error: {e}")
        return None

def select_primary_gpu():
    """Select the primary GPU and cache it."""
    global primary_gpu_cache
    if primary_gpu_cache:
        return primary_gpu_cache

    gpus = get_gpu_info()
    if not gpus:
        print("No GPUs detected")
        return None

    primary_gpu = get_primary_gpu_xrandr()
    if primary_gpu and primary_gpu["card"]:
        usage = get_gpu_usage_by_type(primary_gpu)
        if usage > 0:
            primary_gpu_cache = primary_gpu
            return primary_gpu

    rendering_gpu = get_rendering_gpu()
    if rendering_gpu and rendering_gpu["card"]:
        usage = get_gpu_usage_by_type(rendering_gpu)
        if usage > 0:
            primary_gpu_cache = rendering_gpu
            return rendering_gpu

    for gpu in gpus:
        if gpu["card"]:
            usage = get_gpu_usage_by_type(gpu)
            if usage > 0:
                primary_gpu_cache = gpu
                return gpu

    for gpu in gpus:
        if gpu["bus_id"] == "28:00.0":
            print(f"Defaulting to RX 6950 XT: {gpu['type']} at {gpu['bus_id']} ({gpu['card']})")
            primary_gpu_cache = gpu
            return gpu

    print(f"Defaulting to first GPU: {gpus[0]['type']} at {gpus[0]['bus_id']} ({gpus[0]['card']})")
    primary_gpu_cache = gpus[0]
    return gpus[0]

def get_system_stats(gpu, config):
    """Get system stats based on config."""
    stats = {}
    if config["system_stats"]["cpu_usage"].get():
        stats["cpu_usage"] = psutil.cpu_percent(interval=0.5)
    if config["system_stats"]["cpu_temp"].get():
        try:
            temps = psutil.sensors_temperatures()
            cpu_temp = None
            for key, entries in temps.items():
                if "coretemp" in key or "k10temp" in key:
                    for entry in entries:
                        if "Package" in entry.label or "Tctl" in entry.label:
                            cpu_temp = entry.current
                            break
                    if cpu_temp:
                        break
            stats["cpu_temp"] = cpu_temp or 0.0
            if config["system_stats"]["temp_unit"].get() == "F":
                stats["cpu_temp"] = stats["cpu_temp"] * 9/5 + 32
        except Exception as e:
            print(f"CPU temp error: {e}")
            stats["cpu_temp"] = 0.0

    if config["system_stats"]["gpu_usage"].get():
        stats["gpu_usage"] = get_gpu_usage_by_type(gpu)
    if config["system_stats"]["gpu_temp"].get():
        try:
            if gpu["card"]:
                temp_file = glob.glob(f"/sys/class/drm/{gpu['card']}/device/hwmon/*/temp1_input")
                if temp_file:
                    with open(temp_file[0], "r") as f:
                        temp = float(f.read().strip()) / 1000.0
                    stats["gpu_temp"] = temp
                    if config["system_stats"]["temp_unit"].get() == "F":
                        stats["gpu_temp"] = temp * 9/5 + 32
                else:
                    stats["gpu_temp"] = 0.0
            else:
                stats["gpu_temp"] = 0.0
        except (IOError, ValueError) as e:
            print(f"GPU temp error: {e}")
            stats["gpu_temp"] = 0.0

    if config["system_stats"]["ram_usage"].get():
        mem = psutil.virtual_memory()
        stats["ram_used"] = round(mem.used / 1024**3, 1)
        stats["ram_total"] = round(mem.total / 1024**3, 1)
    if config["system_stats"]["vram_usage"].get():
        try:
            if gpu["card"]:
                with open(f"/sys/class/drm/{gpu['card']}/device/mem_info_vram_used", "r") as f:
                    used = int(f.read().strip()) / 1024**3
                with open(f"/sys/class/drm/{gpu['card']}/device/mem_info_vram_total", "r") as f:
                    total = int(f.read().strip()) / 1024**3
                stats["vram_used"] = round(used, 1)
                stats["vram_total"] = round(total, 1)
            else:
                stats["vram_used"] = 0.0
                stats["vram_total"] = 0.0
        except (IOError, ValueError) as e:
            print(f"VRAM error: {e}")
            stats["vram_used"] = 0.0
            stats["vram_total"] = 0.0
    return stats

def get_gpu_usage_by_type(gpu):
    """Get usage for a specific GPU."""
    gpu_type = gpu["type"]
    bus_id = gpu["bus_id"]
    card = gpu.get("card")

    if gpu_type == "amd":
        if card:
            try:
                with open(f"/sys/class/drm/{card}/device/gpu_busy_percent", "r") as f:
                    usage = float(f.read().strip())
                print(f"AMD GPU usage (sysfs, {card}, bus {bus_id}): {usage}%")
                return usage
            except (IOError, ValueError, FileNotFoundError) as e:
                print(f"sysfs error for {card} (bus {bus_id}): {e}")

        try:
            cmd = ["radeontop", "-d", "-", "-l", "1"]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate(timeout=0.5)
            print(f"radeontop cmd: {cmd}")
            print(f"radeontop stdout: {stdout}")
            if stderr:
                print(f"radeontop stderr: {stderr}")
                if "Permission denied" in stderr or "root" in stderr:
                    print("Permission issue. Run with 'sudo' or add user to 'video' group")
                    print("E.g., 'sudo usermod -aG video $USER' and log out/in")

            match = re.search(r"gpu\s+(\d+\.\d+)%", stdout)
            if match:
                usage = float(match.group(1))
                print(f"AMD GPU usage (radeontop, bus {bus_id}): {usage}%")
                return usage
            print("No AMD GPU usage data from radeontop")
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            print(f"radeontop error: {e}")

    elif gpu_type == "nvidia":
        print("NVIDIA not supported in this script")
    elif gpu_type == "intel":
        print("Intel GPU usage not implemented")
    return 0.0

def get_music_info(config):
    """Get music info using playerctl."""
    if not config["music"]["enable"].get():
        return ""
    try:
        result = subprocess.run(
            ["playerctl", "status"],
            capture_output=True,
            text=True,
            timeout=0.5
        )
        status = result.stdout.strip()
        if status != "Playing":
            print(f"Player status: {status or 'No player'}")
            return "⏸️"

        result = subprocess.run(
            ["playerctl", "metadata", "--format", "{{title}} - {{artist}}"],
            capture_output=True,
            text=True,
            timeout=0.5
        )
        music = result.stdout.strip()
        if music and music != " - ":
            print(f"Music detected: {music}")
            prefix = ""
            if config["music"]["prefix"].get() == "emoji":
                prefix = "🎶 "
            elif config["music"]["prefix"].get() == "text":
                prefix = "Listening to: "
            output = f"{prefix}{music}"
            if config["music"]["progress"].get() and status == "Playing":
                try:
                    pos = subprocess.run(
                        ["playerctl", "position", "--format", "{{duration(position)}}"],
                        capture_output=True,
                        text=True,
                        timeout=0.5
                    ).stdout.strip()
                    dur = subprocess.run(
                        ["playerctl", "metadata", "--format", "{{duration(mpris:length)}}"],
                        capture_output=True,
                        text=True,
                        timeout=0.5
                    ).stdout.strip()
                    if pos and dur:
                        output += f" {pos}/{dur}"
                except subprocess.SubprocessError as e:
                    print(f"Music progress error: {e}")
            return output
        print("Empty or invalid metadata")
        return "⏸️"
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"Music detection error: {e}")
        return "⏸️"

def get_current_time(config):
    """Get current time based on config."""
    if not config["time"]["enable"].get():
        return ""
    try:
        tz = tzlocal.get_localzone()
        fmt = "%H:%M" if config["time"]["24hour"].get() else "%I:%M %p"
        time_str = datetime.now(tz).strftime(fmt)
        if config["time"]["timezone"].get():
            import time as time_mod
            tz_name = time_mod.tzname[0]  # e.g., PDT
            time_str += f" {tz_name}"
        if config["time"]["prefix"].get():
            return f"My time: {time_str}"
        print(f"Time config: 24hour={config['time']['24hour'].get()}, timezone={config['time']['timezone'].get()}, prefix={config['time']['prefix'].get()}")
        return time_str
    except Exception as e:
        print(f"Time error: {e}")
        return ""

def build_message(stats, time_str, music_str, chat_text, config):
    if chat_text.strip():
        return chat_text[:140] + ("\u0003\u001f" if config["skinny_mode"].get() else "")
    lines = []
    system_enabled = config["system_stats"]["enable"].get()
    if system_enabled:
        cpu_usage = config["system_stats"]["cpu_usage"].get()
        cpu_temp = config["system_stats"]["cpu_temp"].get()
        gpu_usage = config["system_stats"]["gpu_usage"].get()
        gpu_temp = config["system_stats"]["gpu_temp"].get()
        ram_usage = config["system_stats"]["ram_usage"].get()
        vram_usage = config["system_stats"]["vram_usage"].get()
        extra_stats = cpu_temp or gpu_temp or ram_usage or vram_usage
        temp_unit = config["system_stats"]["temp_unit"].get()
        if not extra_stats and cpu_usage and gpu_usage:
            lines.append(f"CPU: {stats.get('cpu_usage', 0.0):.1f}% | GPU: {stats.get('gpu_usage', 0.0):.1f}%")
        else:
            if cpu_usage or cpu_temp:
                cpu_line = []
                if cpu_usage:
                    cpu_line.append(f"CPU: {stats.get('cpu_usage', 0.0):.1f}%")
                if cpu_temp:
                    cpu_line.append(f"Temp: {stats.get('cpu_temp', 0.0):.0f}{temp_unit.lower()}")
                lines.append(" | ".join(cpu_line))
            if gpu_usage or gpu_temp:
                gpu_line = []
                if gpu_usage:
                    gpu_line.append(f"GPU: {stats.get('gpu_usage', 0.0):.1f}%")
                if gpu_temp:
                    gpu_line.append(f"Temp: {stats.get('gpu_temp', 0.0):.0f}{temp_unit.lower()}")
                lines.append(" | ".join(gpu_line))
            if ram_usage or vram_usage:
                ram_line = []
                if ram_usage:
                    ram_line.append(f"RAM: {stats.get('ram_used', 0.0)}/{stats.get('ram_total', 0.0)}gb")
                if vram_usage:
                    ram_line.append(f"VRAM: {stats.get('vram_used', 0.0)}/{stats.get('vram_total', 0.0)}gb")
                lines.append(" | ".join(ram_line))
    if time_str:
        lines.append(time_str)
    if music_str:
        lines.append(music_str)
    return "\n".join(lines) + ("\u0003\u001f" if config["skinny_mode"].get() else "")

class VRChatOSCApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Voi's Linux OSC for VRChat (Experimental)")
        self.root.configure(bg="#2D2D2D")  # Dark gray background

        # Ensure config directory exists
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            os.chmod(CONFIG_DIR, 0o755)
            print(f"Config directory ensured: {CONFIG_DIR}")
        except OSError as e:
            print(f"Error creating config directory {CONFIG_DIR}: {e}")

        # Download and set heart icon
        try:
            if not os.path.exists(ICON_PATH):
                req = urllib.request.Request(
                    ICON_URL,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req) as response:
                    with open(ICON_PATH, "wb") as f:
                        f.write(response.read())
                print(f"Downloaded heart icon to {ICON_PATH}")
            icon = tk.PhotoImage(file=ICON_PATH)
            self.root.iconphoto(True, icon)
            print(f"Loaded heart icon from {ICON_PATH}")
        except Exception as e:
            print(f"Error loading icon from {ICON_PATH}: {e}. Using default icon.")

        self.config = {
            "system_stats": {
                "enable": tk.BooleanVar(value=True),
                "cpu_usage": tk.BooleanVar(value=True),
                "cpu_temp": tk.BooleanVar(value=False),
                "gpu_usage": tk.BooleanVar(value=True),
                "gpu_temp": tk.BooleanVar(value=False),
                "ram_usage": tk.BooleanVar(value=False),
                "vram_usage": tk.BooleanVar(value=False),
                "temp_unit": tk.StringVar(value="C"),
            },
            "time": {
                "enable": tk.BooleanVar(value=True),
                "prefix": tk.BooleanVar(value=True),
                "timezone": tk.BooleanVar(value=False),
                "short_tz": tk.BooleanVar(value=False),
                "24hour": tk.BooleanVar(value=False),
            },
            "music": {
                "enable": tk.BooleanVar(value=True),
                "progress": tk.BooleanVar(value=False),
                "prefix": tk.StringVar(value="emoji"),
            },
            "skinny_mode": tk.BooleanVar(value=True),
            "app": {
                "ip": tk.StringVar(value="127.0.0.1"),
                "port": tk.StringVar(value="9000"),
            },
            "chat_timeout": tk.StringVar(value="5")
        }
        self.chat_text = tk.StringVar(value="")
        self.live_edit = tk.BooleanVar(value=False)
        self.chat_history = []
        self.program_running = tk.BooleanVar(value=True)
        self.osc_client = None
        self.last_chat_time = None
        self.update_osc_client()
        self.load_config()
        self.setup_gui()
        self.gpu = select_primary_gpu()
        self.running = True
        self.osc_thread = threading.Thread(target=self.send_osc_messages, daemon=True)
        self.osc_thread.start()

    def update_osc_client(self):
        """Update OSC client with current IP/port."""
        try:
            ip = self.config["app"]["ip"].get()
            port = int(self.config["app"]["port"].get())
            self.osc_client = udp_client.SimpleUDPClient(ip, port)
            print(f"OSC client updated: {ip}:{port}")
        except ValueError as e:
            print(f"Invalid IP/port: {e}")

    def load_config(self):
        """Load config from JSON file."""
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print(f"Config load error: Expected dictionary, got {type(data)}")
                return
            for section, settings in data.items():
                if section in ["skinny_mode", "chat_timeout"]:
                    if section == "skinny_mode" and isinstance(settings, bool):
                        self.config["skinny_mode"].set(settings)
                    elif section == "chat_timeout" and isinstance(settings, str):
                        self.config["chat_timeout"].set(settings)
                elif section in self.config and isinstance(settings, dict):
                    for key, value in settings.items():
                        if key in self.config[section]:
                            self.config[section][key].set(value)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as e:
            print(f"Config load error: {e}")

    def save_config(self):
        """Save config to JSON file."""
        data = {
            "system_stats": {
                "enable": self.config["system_stats"]["enable"].get(),
                "cpu_usage": self.config["system_stats"]["cpu_usage"].get(),
                "cpu_temp": self.config["system_stats"]["cpu_temp"].get(),
                "gpu_usage": self.config["system_stats"]["gpu_usage"].get(),
                "gpu_temp": self.config["system_stats"]["gpu_temp"].get(),
                "ram_usage": self.config["system_stats"]["ram_usage"].get(),
                "vram_usage": self.config["system_stats"]["vram_usage"].get(),
                "temp_unit": self.config["system_stats"]["temp_unit"].get(),
            },
            "time": {
                "enable": self.config["time"]["enable"].get(),
                "prefix": self.config["time"]["prefix"].get(),
                "timezone": self.config["time"]["timezone"].get(),
                "short_tz": self.config["time"]["short_tz"].get(),
                "24hour": self.config["time"]["24hour"].get(),
            },
            "music": {
                "enable": self.config["music"]["enable"].get(),
                "progress": self.config["music"]["progress"].get(),
                "prefix": self.config["music"]["prefix"].get(),
            },
            "skinny_mode": self.config["skinny_mode"].get(),
            "app": {
                "ip": self.config["app"]["ip"].get(),
                "port": self.config["app"]["port"].get(),
            },
            "chat_timeout": self.config["chat_timeout"].get()
        }
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=4)
            os.chmod(CONFIG_FILE, 0o644)
        except IOError as e:
            print(f"Config save error: {e}")

    def setup_gui(self):
        """Setup GUI with tabs and preview."""
        # Darkly-inspired theme with accent and rounded widgets
        style = ttk.Style()
        style.configure("TNotebook", background="#3C3C3C")
        style.configure("TNotebook.Tab", background="#3C3C3C", foreground="#E0E0E0", padding=[10, 5])
        style.map("TNotebook.Tab",
                  background=[("selected", "#3C3C3C"), ("active", "#5A5A5A")],
                  foreground=[("selected", "#4682B4"), ("active", "#E0E0E0")])
        style.configure("TFrame", background="#2D2D2D")
        style.configure("TLabel", background="#2D2D2D", foreground="#E0E0E0")
        style.configure("TCheckbutton", background="#2D2D2D", foreground="#E0E0E0")
        style.configure("TRadiobutton", background="#2D2D2D", foreground="#E0E0E0")
        style.configure("TButton", background="#4A4A4A", foreground="#4682B4", padding=[12, 6], relief="flat")
        style.configure("TEntry", fieldbackground="#3C3C3C", foreground="#E0E0E0", padding=[8, 4], relief="flat")
        style.configure("TLabelFrame", background="#2D2D2D", foreground="#E0E0E0", relief="flat")

        main_container = ttk.Frame(self.root)
        main_container.pack(fill="both", expand=True)

        # Preview and Program Toggle
        right_panel = ttk.Frame(main_container)
        right_panel.pack(side="right", fill="y", padx=10, pady=10)
        ttk.Label(right_panel, text="Preview", anchor="center").pack(fill="x")
        self.preview_text = tk.Text(
            right_panel, height=8, width=30, state="disabled",
            bg="#3C3C3C", fg="#E0E0E0", insertbackground="#E0E0E0"
        )
        self.preview_text.pack(pady=5)
        ttk.Checkbutton(
            right_panel, text="Program On/Off", variable=self.program_running
        ).pack(pady=5)

        # Tabs
        notebook = ttk.Notebook(main_container)
        notebook.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        # Main Tab
        main_frame = ttk.Frame(notebook)
        notebook.add(main_frame, text="Main")
        ttk.Label(main_frame, text="Modules").pack(anchor="w", padx=10, pady=5)
        ttk.Checkbutton(
            main_frame, text="System Stats", variable=self.config["system_stats"]["enable"],
            command=self.save_config
        ).pack(anchor="w", padx=20, pady=2)
        ttk.Checkbutton(
            main_frame, text="Time", variable=self.config["time"]["enable"],
            command=self.save_config
        ).pack(anchor="w", padx=20, pady=2)
        ttk.Checkbutton(
            main_frame, text="Music", variable=self.config["music"]["enable"],
            command=self.save_config
        ).pack(anchor="w", padx=20, pady=2)

        # Chat Tab
        chat_frame = ttk.Frame(notebook)
        notebook.add(chat_frame, text="Chat")
        self.history_frame = ttk.Frame(chat_frame)
        self.history_frame.pack(fill="both", expand=True, padx=10, pady=5)
        input_frame = ttk.Frame(chat_frame)
        input_frame.pack(fill="x", padx=10, pady=5)
        ttk.Checkbutton(
            input_frame, text="Live Edit", variable=self.live_edit
        ).pack(anchor="w", pady=2)
        ttk.Label(input_frame, text="Chat Input (140 chars max):").pack(anchor="w")
        chat_entry = ttk.Entry(input_frame, textvariable=self.chat_text)
        chat_entry.pack(side="left", fill="x", expand=True, padx=5)
        chat_entry.bind("<Return>", self.send_chat)
        ttk.Button(
            input_frame, text="Clear", command=self.clear_chat
        ).pack(side="left", padx=2)
        ttk.Button(
            input_frame, text="Send", command=self.send_chat
        ).pack(side="left", padx=2)
        ttk.Button(
            input_frame, text="Paste", command=self.paste_chat
        ).pack(side="left", padx=2)
        chat_entry.config(validate="key", validatecommand=(self.root.register(self.limit_chat_input), "%P"))

        # Settings Tab
        settings_frame = ttk.Frame(notebook)
        notebook.add(settings_frame, text="Settings")

        # App Options
        app_frame = ttk.LabelFrame(settings_frame, text="App Options")
        app_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(app_frame, text="IP:").pack(side="left", padx=5)
        ip_entry = ttk.Entry(app_frame, textvariable=self.config["app"]["ip"], width=15)
        ip_entry.pack(side="left", padx=5)
        ip_entry.bind("<Return>", lambda e: [self.update_osc_client(), self.save_config()])
        ttk.Label(app_frame, text="Port:").pack(side="left", padx=5)
        port_entry = ttk.Entry(app_frame, textvariable=self.config["app"]["port"], width=8)
        port_entry.pack(side="left", padx=5)
        port_entry.bind("<Return>", lambda e: [self.update_osc_client(), self.save_config()])

        # System Stats Section
        system_frame = ttk.LabelFrame(settings_frame, text="System Stats")
        system_frame.pack(fill="x", padx=10, pady=5)
        ttk.Checkbutton(
            system_frame, text="CPU Usage", variable=self.config["system_stats"]["cpu_usage"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            system_frame, text="CPU Temp", variable=self.config["system_stats"]["cpu_temp"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            system_frame, text="GPU Usage", variable=self.config["system_stats"]["gpu_usage"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            system_frame, text="GPU Temp", variable=self.config["system_stats"]["gpu_temp"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            system_frame, text="RAM Usage", variable=self.config["system_stats"]["ram_usage"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            system_frame, text="VRAM Usage", variable=self.config["system_stats"]["vram_usage"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        temp_frame = ttk.Frame(system_frame)
        temp_frame.pack(anchor="w", padx=5, pady=2)
        ttk.Radiobutton(
            temp_frame, text="°C", variable=self.config["system_stats"]["temp_unit"], value="C",
            command=self.save_config
        ).pack(side="left")
        ttk.Radiobutton(
            temp_frame, text="°F", variable=self.config["system_stats"]["temp_unit"], value="F",
            command=self.save_config
        ).pack(side="left", padx=10)

        # Time Section
        time_frame = ttk.LabelFrame(settings_frame, text="Time")
        time_frame.pack(fill="x", padx=10, pady=5)
        ttk.Checkbutton(
            time_frame, text="Show 'My time:' Prefix", variable=self.config["time"]["prefix"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            time_frame, text="Show Timezone (e.g., PDT)", variable=self.config["time"]["timezone"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Checkbutton(
            time_frame, text="24-Hour Format", variable=self.config["time"]["24hour"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)

        # Music Section
        music_frame = ttk.LabelFrame(settings_frame, text="Music")
        music_frame.pack(fill="x", padx=10, pady=5)
        ttk.Checkbutton(
            music_frame, text="Show Track Progress", variable=self.config["music"]["progress"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        prefix_frame = ttk.Frame(music_frame)
        prefix_frame.pack(anchor="w", padx=5, pady=2)
        ttk.Radiobutton(
            prefix_frame, text="🎶", variable=self.config["music"]["prefix"], value="emoji",
            command=self.save_config
        ).pack(side="left")
        ttk.Radiobutton(
            prefix_frame, text="Listening to:", variable=self.config["music"]["prefix"], value="text",
            command=self.save_config
        ).pack(side="left", padx=10)
        ttk.Radiobutton(
            prefix_frame, text="None", variable=self.config["music"]["prefix"], value="none",
            command=self.save_config
        ).pack(side="left", padx=10)

        # Extras Section
        extras_frame = ttk.LabelFrame(settings_frame, text="Extras")
        extras_frame.pack(fill="x", padx=10, pady=5)
        ttk.Checkbutton(
            extras_frame, text="Skinny Mode (Add OSC Formatting)", variable=self.config["skinny_mode"],
            command=self.save_config
        ).pack(anchor="w", padx=5, pady=2)
        ttk.Label(extras_frame, text="Chat Timeout (seconds):").pack(anchor="w", padx=5, pady=2)
        timeout_entry = ttk.Entry(extras_frame, textvariable=self.config["chat_timeout"], width=5)
        timeout_entry.pack(anchor="w", padx=5, pady=2)
        timeout_entry.bind("<Return>", lambda e: self.save_config())
        timeout_entry.config(validate="key", validatecommand=(self.root.register(self.validate_timeout), "%P"))

    def validate_timeout(self, text):
        """Validate chat timeout input."""
        if not text:
            return True
        try:
            value = float(text)
            return value >= 0
        except ValueError:
            return False

    def limit_chat_input(self, text):
        """Limit chat input to 140 characters."""
        return len(text) <= 140

    def paste_chat(self):
        """Paste clipboard into chat."""
        try:
            text = pyperclip.paste()[:140]
            self.chat_text.set(text)
        except Exception as e:
            print(f"Paste error: {e}")

    def clear_chat(self):
        """Clear the VRChat chatbox."""
        try:
            self.osc_client.send_message("/chatbox/input", ["", True, False])
            self.last_chat_time = None
            print("Cleared chatbox")
            self.update_preview("Chatbox cleared")
        except Exception as e:
            print(f"Clear chat error: {e}")

    def send_chat(self, event=None):
        """Send chat message and add to history."""
        text = self.chat_text.get().strip()[:140]
        if text:
            self.chat_history.append(text)
            if len(self.chat_history) > 5:
                self.chat_history.pop(0)
            self.update_history()
            self.last_chat_time = time.time()
            self.osc_client.send_message("/chatbox/input", [text + ("\u0003\u001f" if self.config["skinny_mode"].get() else ""), True, False])
            print(f"Sent chat: {text}")
            self.chat_text.set("")
        self.live_edit.set(False)

    def resend_chat(self, text):
        """Resend chat message."""
        self.chat_text.set(text)
        self.send_chat()

    def copy_chat(self, text):
        """Copy chat message to clipboard."""
        pyperclip.copy(text)

    def update_history(self):
        """Update chat history display."""
        for widget in self.history_frame.winfo_children():
            widget.destroy()
        for idx, text in enumerate(reversed(self.chat_history)):
            frame = ttk.Frame(self.history_frame)
            frame.pack(fill="x", pady=2)
            ttk.Label(frame, text=text, wraplength=300).pack(side="left", padx=5)
            ttk.Button(
                frame, text="Resend", command=lambda t=text: self.resend_chat(t)
            ).pack(side="right", padx=2)
            ttk.Button(
                frame, text="Copy", command=lambda t=text: self.copy_chat(t)
            ).pack(side="right", padx=2)

    def update_preview(self, message):
        """Update preview text."""
        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, message)
        self.preview_text.config(state="disabled")

    def send_osc_messages(self):
        """Send OSC messages based on config."""
        while self.running:
            try:
                start_time = time.time()
                if not self.program_running.get():
                    self.update_preview("Program Off")
                    time.sleep(2.0)
                    continue

                stats = get_system_stats(self.gpu, self.config) if self.config["system_stats"]["enable"].get() else {}
                time_str = get_current_time(self.config)
                music_str = get_music_info(self.config)
                chat_text = self.chat_text.get().strip() if self.live_edit.get() else ""

                if not chat_text and self.last_chat_time:
                    try:
                        timeout = float(self.config["chat_timeout"].get())
                    except ValueError:
                        timeout = 5.0
                    if time.time() - self.last_chat_time < timeout:
                        chat_text = self.chat_history[-1] if self.chat_history else ""
                    else:
                        self.last_chat_time = None

                message = build_message(stats, time_str, music_str, chat_text, self.config)

                self.osc_client.send_message("/chatbox/input", [message, True, False])
                print(f"Sent: {message}")

                self.update_preview(message)
                elapsed = time.time() - start_time
                print(f"Update took {elapsed:.2f}s")
                time.sleep(2.0)
            except Exception as e:
                print(f"OSC thread error: {e}. Restarting in 2s...")
                time.sleep(2.0)

    def shutdown(self):
        """Cleanup on exit."""
        self.save_config()
        self.running = False
        self.root.destroy()

if __name__ == "__main__":
    print("Starting VRChat OSC script...")
    try:
        lspci = subprocess.run(
            ["lspci", "-nn"], capture_output=True, text=True
        )
        print("Detected GPUs:")
        for line in lspci.stdout.splitlines():
            if "VGA" in line or "3D" in line:
                print(line)
        primary_gpu = select_primary_gpu()
        if primary_gpu:
            print(f"Using GPU: {primary_gpu['type']} at {primary_gpu['bus_id']} ({primary_gpu['card']})")
        else:
            print("No GPU selected; will try fallback")
    except subprocess.SubprocessError as e:
        print(f"Error listing GPUs: {e}")

    root = tk.Tk()
    print("Using darkly-inspired theme with blue accents and rounded widgets")
    app = VRChatOSCApp(root)
    root.protocol("WM_DELETE_WINDOW", app.shutdown)
    root.mainloop()
