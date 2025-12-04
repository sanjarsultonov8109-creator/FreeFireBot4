import os
from dotenv import load_dotenv

load_dotenv()

# Bot egasi (OWNER) va mukofot konfiguratsiyasi
OWNER_ID = int(os.getenv("OWNER_ID", "900592608"))


# Majburiy kanallar (vergul bilan ajratib yoziladi): @kanal1,@kanal2
_raw = os.getenv("REQUIRED_CHANNELS", "")
REQUIRED_CHANNELS_DEFAULT = [s.strip() for s in _raw.split(",") if s.strip()]
