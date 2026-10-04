FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 6010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6010", "--workers", "1"]
