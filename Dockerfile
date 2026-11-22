# keyvault —— 零依赖，所以镜像也做得很小
FROM python:3.12-alpine

WORKDIR /app

# 只有一个源文件要拷，连 COPY . 都不需要
COPY keyvault.py ./

# 非 root 跑。日志里没有 key，但内存里有，降权没坏处
RUN adduser -D -H -u 10001 kv
USER kv

EXPOSE 3997

# keys.json 必须挂进来（只读）
ENV KEYVAULT_HOST=0.0.0.0 \
    KEYVAULT_PORT=3997 \
    KEYVAULT_KEYS=/app/keys.json \
    KEYVAULT_LOG=info

HEALTHCHECK --interval=30s --timeout=3s --start-period=3s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('KEYVAULT_PORT','3997'),timeout=2)" || exit 1

CMD ["python", "keyvault.py"]
