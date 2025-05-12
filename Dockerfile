FROM python:3.11-slim

WORKDIR /app

# Install required packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .

# Create output directory for analysis files
RUN mkdir -p /app/output

# Set environment variables
ENV OUTPUT_DIR=/app/output
ENV CHECK_INTERVAL=60

# Run as non-root user for security
RUN useradd -m appuser
RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "main.py"]

# deployment file:${scarlettk}/ai-agent:latest image and put the imagepullpolicy to Always, i havent pushed to the hub yet currently i have it in local
# Replace with your base64 encoded Mistral API key, convert the actual api key to base64 using:
  #[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("8LLev8jl5CUHAwlF3uEh1vfpF0X0IDdo")) run it any terminal it will gith base64 format,
  #kubernetes only accepts base64 format keys.