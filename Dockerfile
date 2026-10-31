FROM python:3.12-alpine

WORKDIR /app
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .
COPY checkwarranty_netbox_sync ./checkwarranty_netbox_sync

RUN mkdir -p /state && chown appuser:appgroup /state
ENV CHECKWARRANTY_STATE_FILE=/state/progress.json
VOLUME ["/state"]
USER appuser

ENTRYPOINT ["python", "sync.py"]
CMD ["--limit", "50"]
