FROM mcr.microsoft.com/playwright/python:v1.32.1-focal

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "monitor.py"]
