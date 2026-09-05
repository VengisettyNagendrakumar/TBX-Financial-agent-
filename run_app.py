"""
Unified Launcher for TBX Finance Assistant
==========================================
Starts both:
1. FastAPI Backend Server (http://localhost:8000 / Swagger at http://localhost:8000/docs)
2. Streamlit Conversational UI (http://localhost:8501)

Usage:
    py run_app.py
"""

import sys
import time
import subprocess
import os

def main():
    print("=" * 65)
    print("  LAUNCHING GROUNDED FINANCIAL INTELLIGENCE ASSISTANT")
    print("=" * 65)
    
    # 1. Launch FastAPI Backend
    print("\n[1/2] Starting FastAPI Backend on http://localhost:8000 ...")
    api_cmd = [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8000"]
    api_proc = subprocess.Popen(api_cmd)
    
    # Wait 2 seconds for FastAPI to bind
    time.sleep(2)
    print("      -> FastAPI running! OpenAPI docs available at: http://localhost:8000/docs")
    
    # 2. Launch Streamlit UI
    print("\n[2/2] Starting Streamlit Chat Interface on http://localhost:8501 ...")
    st_cmd = [sys.executable, "-m", "streamlit", "run", "app.py"]
    st_proc = subprocess.Popen(st_cmd)
    
    print("\n" + "=" * 65)
    print("  SYSTEM READY!")
    print("  • Web UI:      http://localhost:8501")
    print("  • REST API:    http://localhost:8000")
    print("  • API Docs:    http://localhost:8000/docs")
    print("  Press Ctrl+C to stop both servers.")
    print("=" * 65 + "\n")
    
    try:
        st_proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down both servers cleanly...")
    finally:
        api_proc.terminate()
        st_proc.terminate()
        print("Shutdown complete.")

if __name__ == "__main__":
    main()
