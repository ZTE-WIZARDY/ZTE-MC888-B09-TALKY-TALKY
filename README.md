# ZTE-MC888-B09-TALKY-TALKY
"Set-Cmd Endpoints" or "GoForm APIs" and Hashing for the ZTE MC888 B09 Router

ZTE MC888 B09+ Telemetry & Authentication UtilityProject Status: UNMAINTAINED / ARCHIVAL DROP 
Author: Chris Veteran Firmware Developer (40+ Years) 

This repo contains a standalone Python utility developed to interface with the ZTE MC888 series running B09 firmware revision (my router happens to be a B09). It was created to solve the specific problem where after a nightly N78 shutdown on a EE mast I needed a way to kick it back up to N78 without rebooting it (becuase that upsets my LAN which upsets my Windows 11 IOT server). 

Key Technical Implementations:

Double-Hashed AD Token Generation: Reverse engineered the new authentication handshake required for DISCONNECT_NETWORK, CONNECT_NETWORK, and SET_BEARER_PREFERENCE on secured units.

Extended Telemetry Access: 
Successfully pulls high-fidelity signal diagnostics (RSRQ, SINR, and per-band Carrier Aggregation info) that are now hidden from standard unauthenticated API calls. 
Logs real-time data to a local file for easy consumption by IoT platforms (Home Assistant, etc.) without requiring a full integration rebuild. 

@stich86 and @Kajkac:

I am releasing this source code "as-is".
This is a final code drop.
I will not be providing technical support, maintenance, or responses to Issues/Pull Requests.
I hope it works somewhere other than at my place and hope you guys find it useful.
