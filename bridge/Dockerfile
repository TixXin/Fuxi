FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends xdotool xclip x11-apps \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY fuxi_bridge /app/fuxi_bridge
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DISPLAY=:1

CMD ["python", "-m", "fuxi_bridge.minimal"]
