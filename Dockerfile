FROM python:3.12-slim

WORKDIR /app

# Dependencies install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot copy
COPY bot.py .

# /tmp already exists in Docker — WORK_DIR ok
CMD ["python", "-u", "bot.py"]