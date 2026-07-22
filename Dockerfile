FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# O worker Dramatiq usa esta mesma imagem com outro comando:
#   dramatiq worker --processes 1 --threads 4
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
