FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY main.py .

RUN useradd --create-home agent
USER agent

EXPOSE 8080
# Secrets come from the environment at run time (docker run --env-file .env), never from the image.
CMD ["python", "main.py"]
