FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py config.example.yaml ./
ENV STATE_FILE=/data/state.json
VOLUME /data
CMD ["sh", "-c", "[ -f config.yaml ] || cp config.example.yaml config.yaml; python monitor.py"]
