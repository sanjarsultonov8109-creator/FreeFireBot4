import sys
import os

# Loyihaning katalogi
path = '/home/sanjar01/www'
if path not in sys.path:
    sys.path.append(path)

os.chdir(path)

from main import app as application  # yoki "bot" emas, "app" bo‘lishi kerak
