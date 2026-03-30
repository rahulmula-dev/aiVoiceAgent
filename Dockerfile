# Use an official Python runtime as a parent image, slim version for smaller size
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install FastText language detection (Linux wheel available in slim image)
# Pin numpy<2.0 — fasttext-wheel uses np.array(copy=False) which breaks in NumPy 2.x
RUN pip install --no-cache-dir "numpy<2.0" fasttext-wheel

# Download FastText language identification model (lid.176.ftz ~126MB)
RUN mkdir -p /app/models && \
    curl -fSL https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz \
         -o /app/models/lid.176.ftz

# Point the language guard at the model file
ENV FASTTEXT_MODEL_PATH=/app/models/lid.176.ftz

# Copy the current directory contents into the container at /app
COPY . .

# Expose port 8000 for the FastAPI application
EXPOSE 8000

# Healthcheck for K8s/Docker monitoring
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Run the application
# 24x7 Warm Operation: Uvicorn workers ensure no cold starts
CMD ["python", "run_server.py"]
