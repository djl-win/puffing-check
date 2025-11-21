# 1. 使用 Playwright 官方 Python 镜像（已经内置 Chromium/Firefox/Webkit）
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 2. 设置工作目录
WORKDIR /app

# 3. 把当前目录所有文件复制进容器
COPY . /app

# 4. 安装依赖
#   注意：镜像里已经带 playwright 了，requirements.txt 里可以有，也可以去掉 playwright，
#   重新装一遍也没关系。
RUN pip install --no-cache-dir -r requirements.txt

# 5. 暴露端口 8000（和你 server.py 里用的端口一致）
EXPOSE 8000

# 6. 启动命令
#   你本地 python server.py 已经能跑，就用同样的命令
CMD ["python", "server.py"]
