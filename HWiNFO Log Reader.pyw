# Double-click to open the HWiNFO Log Reader window (no console) with every .csv in this folder.
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
sys.path.insert(0, here)

import hwinfo_gui

hwinfo_gui.run(sys.argv[1:])
