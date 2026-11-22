import os

DATA_DIR = os.environ.get("DATA_DIR", "./data")
BOOK_PATH = os.environ.get("BOOK_PATH", "/opt/book/8moves_v3.pgn")
FASTCHESS = os.environ.get("FASTCHESS", "fastchess")
DOCKER = os.environ.get("DOCKER", "docker")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "") or ADMIN_PASSWORD + "-scacc-session"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
BUILD_TIMEOUT_S = int(os.environ.get("BUILD_TIMEOUT_S", "900"))
BUILD_WORKERS = int(os.environ.get("BUILD_WORKERS", "2"))
# Public URL used in the instructions shown to players (e.g. https://chess.example.com).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
