# ZTE-MC888-B09-TALKY-TALKY
"Set-Cmd Endpoints" or "GoForm APIs" and Hashing for the ZTE MC888 B09 Router

ZTE MC888 B09+ Telemetry & Authentication Utility

Project Status:

UNMAINTAINED / ARCHIVAL DROP 

Author: 
Chris Veteran Firmware Developer (40+ Years) 

This repo contains a standalone Python utility developed to interface with the ZTE MC888 series running B09 firmware revision (my router happens to be a B09). 
It was created to solve the specific problem where after a nightly N78 shutdown on a EE mast I needed a way to kick it back up to N78 without rebooting it 
(becuase that upsets my LAN which upsets my Windows 11 IOT server). 

Key Technical Implementations:

  Double-Hashed AD Token Generation: 
  Reverse engineered the new authentication handshake required for DISCONNECT_NETWORK, CONNECT_NETWORK, and SET_BEARER_PREFERENCE on secured units.

  Extended Telemetry Access: 

  Successfully pulls signal diagnostics (RSRQ, SINR, and per-band Carrier Aggregation info) that are now hidden from standard unauthenticated API calls. 
  Logs real-time data to a local file for easy consumption by IoT platforms (Home Assistant, etc.) without requiring a full integration rebuild. 

@stich86 and @Kajkac:

I am releasing this source code "as-is".
This is a final code drop.
I will not be providing technical support, maintenance, or responses to Issues/Pull Requests.
I hope it works somewhere other than at my place and hope you guys find it useful.

## Installation (Windows)

1. Install Python 3.12 from:
   https://www.python.org/downloads/windows/

   During installation:
   - Tick "Add Python to PATH"
   - Tick "Install for all users"
   - Finish the installer

2. Open Command Prompt:
   Press Win + R → type: cmd → press ENTER

3. Install the required Python packages:
   pip install flask requests

4. Go to the folder where the script is stored:
   cd C:\path\to\ZTE-MC888-B09-TALKY-TALKY

   (Replace the path above with the actual folder location)

5. Start the server by running:
   python mc888_router_server_v3_public.py

   You must run this command from the same folder as the file.

   When it starts successfully, you will see:
   * Running on http://127.0.0.1:5000
   * Press CTRL+C to quit

6. Open your browser and go to:
   http://127.0.0.1:5000

   Leave the Command Prompt window open while using the dashboard.

"Once the dashboard is visible in your Browser, go to the Configuration Panel near the top, enter your router's IP Its Username and Password, and hit Save."
 Good Luck.  

 
<img width="423" height="1302" alt="image" src="https://github.com/user-attachments/assets/1264fe3d-bab9-4a93-a6aa-40c649a9d732" />

 
