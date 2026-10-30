FROM python:3.11-alpine

WORKDIR /app
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .
COPY checkwarranty_netbox_sync ./checkwarranty_netbox_sync

USER appuser

ENTRYPOINT ["python", "sync.py"]
CMD ["--limit", "50"]
