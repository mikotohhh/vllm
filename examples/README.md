# Run Chatbot examples

Note: need to add huggingface token to download meta-llama/Llama-2-7b-chat-hf

## Normal workflow 
start server
```
python -m vllm.entrypoints.openai.api_server --model meta-llama/Llama-2-7b-chat-hf --max-model-len 2064 # default port 8000
```

start client
```
python examples/gradio_openai_chatbot_webserver.py --model meta-llama/Llama-2-7b-chat-hf --port 8001 # default server port 8000
```
Open http://127.0.0.1:8001 and sent some prompts

## Export and share KV cache

The kv cache will be saved under /tmp/kv_cache

start server
```
USE_EXTER_CACHE=True CUDA_VISIBLE_DEVICES=1 python -m vllm.entrypoints.openai.api_server --model meta-llama/Llama-2-7b-chat-hf \
 --max-model-len 2064 --port 8000
```

start client
```
python examples/gradio_openai_chatbot_webserver.py --model meta-llama/Llama-2-7b-chat-hf --port 8001 # default server port 8000
```