import os
import sys
import httpx

print(">>> Starting Model Download during Build Phase...")

# Public GGUF model (always available)
URL = "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"

cache_dir = "/opt/render/.cache/huggingface/hub"
os.makedirs(cache_dir, exist_ok=True)
model_path = os.path.join(cache_dir, "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf")

try:
    print(f"Downloading from {URL} ...")
    response = httpx.get(URL, timeout=120)
    response.raise_for_status()
    
    with open(model_path, "wb") as f:
        f.write(response.content)
    
    print(f">>> Build downloaded model to: {model_path}")
except Exception as e:
    print(f">>> FATAL: Build failed to download model: {e}")
    sys.exit(1)