FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The database lives on a shared volume so web, worker and beat open the
# same file.
RUN mkdir -p /data

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
