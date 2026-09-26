FROM python:3.12-slim

RUN useradd --create-home --uid 10001 bot
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
RUN ln -s /app/jobtrack.py /usr/local/bin/jobtrack && chmod +x /app/jobtrack.py

ENV JOBTRACK_DB=/data/jobtrack.db \
    PYTHONUNBUFFERED=1

USER bot
CMD ["python", "-u", "matrixbot.py"]
