# Render 部署说明

## 文件结构

```
app.py
requirements.txt
render.yaml
```

## Render 配置

- Service Type: Web Service
- Environment: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180`

## 注意

1. 不要使用 `python app.py` 作为生产启动命令。
2. 不要固定端口 5000。Render 会通过 `$PORT` 注入端口。
3. 如果 AI 分析经常超时，可以把模型输出 token 降低，或者先用 DeepSeek。
4. API Key 目前由前端输入，不需要在 Render 环境变量里配置。
