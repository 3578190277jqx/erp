FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home app && mkdir /data && chown app:app /data
COPY --chown=app:app . .
USER app
ENV SQLITE_PATH=/data/db.sqlite3
EXPOSE 8000
CMD ["python", "scripts/container_start.py"]
