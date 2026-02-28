import os
import subprocess
import sys

service_type = os.environ.get("SERVICE_TYPE", "web")

if service_type == "worker":
    subprocess.run([
        sys.executable, "-m", "celery",
        "-A", "workers.celery_app",
        "worker",
        "--loglevel=info",
        "--concurrency=4",
    ], check=True)
else:
    subprocess.run([
        sys.executable, "-m", "uvicorn",
        "api.main:app",
        "--host", "0.0.0.0",
        "--port", os.environ.get("PORT", "8080"),
    ], check=True)
