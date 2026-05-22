# ZTE-MC888-B09-TALKY-TALKY
### "Set-Cmd Endpoints" or "GoForm APIs" and Hashing for the ZTE MC888 B09 Router


| **Project Overview** | **Dashboard Preview** |
| :--- | :--- |
| **ZTE MC888 B09+ Telemetry & Authentication Utility**<br><br>**Project Status:** UNMAINTAINED / ARCHIVAL DROP<br>**Author:** Chris, Veteran Firmware Developer (40+ Years)<br><br>This repo contains a standalone Python utility developed to interface with the ZTE MC888 series running B09 firmware revision. It was created to solve the specific problem where after a nightly N78 shutdown on an EE mast, I needed a way to kick it back up to N78 without rebooting it (because that upsets my LAN and my Windows 11 IOT server).<br><br>**Key Technical Implementations:**<br>1. **Double-Hashed AD Token Generation:** Reversed the new authentication handshake required for DISCONNECT_NETWORK, CONNECT_NETWORK, and SET_BEARER_PREFERENCE.<br>2. **Extended Telemetry Access:** Pulls signal diagnostics (RSRQ, SINR, and per-band CA info) hidden from standard unauthenticated API calls. | <img src="https://github.com" width="350"> |

### @stich86 and @Kajkac:
I am releasing this source code "as-is". This is a final code drop. I will not be providing technical support, maintenance, or responses to Issues/Pull Requests. I hope you guys find it useful.

---

## Installation (Windows)

1. **Install Python 3.12** from: https://python.org
   - Tick **"Add Python to PATH"**
   - Tick **"Install for all users"**
   - Finish the installer

2. **Open Command Prompt:** Press `Win + R` → type: `cmd` → press `ENTER`

3. **Install the required Python packages:**
   `pip install flask requests`

4. **Go to the folder** where the script is stored:
   `cd C:\path\to\ZTE-MC888-B09-TALKY-TALKY`
   *(Replace the path above with the actual folder location)*

5. **Start the server:**
   `python mc888_router_server_v3_public.py`
   *(You must run this command from the same folder as the file)*

6. **Open your browser and go to:** http://127.0.0.1:5000

> **"Once the dashboard is visible in your Browser, go to the Configuration Panel near the top, enter your router's IP, Username, and Password, and hit Save."**

---

**Liability Disclaimer:**
**USE AT YOUR OWN RISK.** This utility interacts with router firmware at a low level. While it works for my setup, I am not responsible for bricked hardware, voided warranties, or ISP service interruptions. By running this script, you accept all responsibility for your own equipment. If you aren't comfortable with that, don't run the code.
