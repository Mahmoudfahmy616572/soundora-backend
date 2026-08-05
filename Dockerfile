# Soundora Audio Resolver — deployment image (Koyeb / any Docker host).
# Build & run the FastAPI + yt-dlp backend so full-song playback does not
# depend on a laptop.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
