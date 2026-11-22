#!/usr/bin/env python3
"""IK DOWNLOADER - multi-connection download manager with a browser video bar.

Run:  python ik_downloader.py      (Python 3.8+; `pip install -r requirements.txt` for video sites)
The code lives in the `ikd` package: common (constants/helpers), netsec (network safety), engine (downloads),
api (local browser link), ui (Tkinter).
"""
from ikd.ui import main

if __name__ == "__main__":
    main()
