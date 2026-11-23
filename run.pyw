#!/usr/bin/env python3
# Sağ tık menüsünün çağırdığı dosya. .pyw uzantısı Windows'ta konsol penceresi açılmasını engeller.
import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":          # paralel işlemde alt süreçler bu dosyayı yeniden yükler: koruma şart
    multiprocessing.freeze_support()  # paketlenmiş .exe'de alt süreçler için
    from riffle.main import main
    sys.exit(main())
