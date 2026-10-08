FROM python:3.12-slim

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 暴露服务端口（云端健康检查）
EXPOSE 8080

# 启动机器人
CMD ["python", "bot.py"]

