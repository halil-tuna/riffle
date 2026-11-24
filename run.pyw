#!/usr/bin/env python3
# The file the right-click menu calls. The .pyw extension prevents a console window on Windows.
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":          # in parallel runs the child processes reload this file: the guard is required
    multiprocessing.freeze_support()  # for child processes in the packaged .exe
    from riffle.main import main
    sys.exit(main())
