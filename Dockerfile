FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py r2.py ./

EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--timeout", "600", "--workers", "2", "app:app"]
