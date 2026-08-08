from huggingface_hub import hf_hub_download
import sys

print(">>> Starting Model Download during Build Phase...")
try:
    model_path = hf_hub_download(
        repo_id="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGML",  # GGML repository
        filename="tinyllama-1.1b-chat-v1.0.Q4_0.bin"      # .bin (GGML) file
    )
    print(f">>> Build downloaded model to: {model_path}")
except Exception as e:
    print(f">>> FATAL: Build failed to download model: {e}")
    sys.exit(1)