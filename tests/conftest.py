import os
import tempfile


os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="insider-edge-tests-"))
os.environ.setdefault("LOG_DIR", os.environ["STATE_DIR"])
