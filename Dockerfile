FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (for Docker cache efficiency)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy rest of the app
COPY . .

# Expose port
EXPOSE 8080

# Start the app
CMD ["uvicorn", "main:app", "--interface", "wsgi", "--host", "0.0.0.0", "--port", "8080"]
