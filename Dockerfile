FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY . /app

RUN pip install --upgrade pip
RUN pip install -r requirements.txt

EXPOSE 8000

CMD ["python", "server.py"]

