FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# -u = unbuffered stdout. Without it the tournament runner never sees your moves.
ENTRYPOINT ["python3", "-u", "engine.py"]
