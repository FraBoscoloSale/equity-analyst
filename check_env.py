import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

print(os.environ["API_KEY"][:4] + "...")
print(os.environ["SEC_USER_AGENT"])
