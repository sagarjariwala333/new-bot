import os
import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        # Defaults to localhost-only if HOST is missing/misconfigured - fail
        # SAFE (not reachable) rather than fail OPEN (publicly exposed).
        # Set HOST=0.0.0.0 explicitly in .env if you deliberately want this
        # reachable beyond localhost (only with a proper reverse proxy/VPN
        # in front of it - see README.md 'Setup').
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
