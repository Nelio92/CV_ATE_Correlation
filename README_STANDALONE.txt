CorreLaTE - Standalone Windows Application
===========================================

Version: {{VERSION}}
Author: Wandji Lionel Wilfried (ES RF D RAD PTE TE4)

QUICK START
-----------
1. Extract the complete release ZIP to a local folder.
2. Double-click CorreLaTE.exe.
3. Use the six numbered tabs from left to right.
4. For Section 2, populate the included CorreLaTE_Chips_Manifest.xlsx or
  create a fresh copy with the Section 2 "Save template..." button.

Python, administrator permissions, and an internet connection are not required.
Do not run the EXE directly from inside the ZIP archive.

INPUT AND OUTPUT DATA
---------------------
CorreLaTE processes selected CSV and Excel files locally. It does not upload
measurement data. Reports and workbooks are written only to destinations that
the user selects.

Custom profiles are stored per Windows user at:
  %APPDATA%\CorreLaTE\profiles.json

Profiles remain available when a newer CorreLaTE release replaces the EXE.
Custom profiles may contain absolute paths and can require updates when data is
moved to another computer or drive.

SECTION 2 CHIP MANIFEST
-----------------------
The release includes CorreLaTE_Chips_Manifest.xlsx. It contains one worksheet,
an Excel input table, validation prompts, and a formatted instruction panel.
Every chip row must provide all five fields:
  DUT Nr     Required unique chip number used for TE-CV alignment.
  Wafer      Required wafer identifier matching the raw TE data.
  X / Y      Required integer die coordinates matching the raw TE data.
  DoE split  Required process-corner label used for plots and variation review.

Keep every DUT Nr and every Wafer/X/Y combination unique. Common DoE split
values are TT, SS, and FF; project-specific process-corner labels are allowed.

INTEGRITY CHECK
---------------
The release includes SHA-256 files. To verify the ZIP or EXE in PowerShell:
  Get-FileHash .\CorreLaTE.exe -Algorithm SHA256

Compare the result with CorreLaTE.exe.sha256.txt.

WINDOWS SECURITY
----------------
An unsigned internal build can trigger Microsoft Defender SmartScreen. Verify
the SHA-256 checksum and release source before choosing More info > Run anyway.
A signed organizational release should show its trusted publisher instead.

TROUBLESHOOTING
---------------
If CorreLaTE cannot start, it writes a diagnostic log to:
  %LOCALAPPDATA%\CorreLaTE\logs\startup-error.log

For workflow errors shown inside the application, retain the complete error
message, selected profile name, input file names, and application version when
contacting the tool maintainer.
